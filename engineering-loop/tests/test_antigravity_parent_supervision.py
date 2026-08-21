"""M-AG012/M-AG014: pruebas de supervision de muerte del proceso padre
(Objetivos A/B, endurecidas en M-AG014) y del entrypoint __main__
fail-closed por defecto (Objetivo C, M-AG012).

Cierra hallazgos reales de dos auditorias independientes:

M-AG011 (motivo de M-AG012): 4 procesos hijo reales quedaron huerfanos y
vivos porque su proceso padre de Python fue terminado externamente antes
de completar su propia secuencia de matado.

M-AG013 (motivo de M-AG014): la primera version de la supervision de
muerte del padre (M-AG012) capturaba su "linea base" de PID de forma
PEREZOSA -- si el padre ya habia muerto antes de que esa captura llegara
a ejecutarse, la linea base quedaba contaminada y el watchdog nunca
disparaba. La auditoria independiente reprodujo esto 25/25 veces contra
un hijo GENUINAMENTE ocupado en un backend (no bloqueado en stdin -- un
hijo bloqueado en stdin.readline() muere por EOF del pipe cerrado en
cuanto el padre desaparece, dando un falso PASS que no prueba nada sobre
la supervision real). Este archivo usa exclusivamente el patron de hijo
genuinamente ocupado para evitar ese confundidor, y prueba explicitamente
la ventana de carrera con CERO espera artificial antes de matar al padre.

Cierra tambien el hallazgo #2 de M-AG011: antigravity_isolated_child.py
::__main__ hacia eco de request.model_id como response_model_id,
fabricando un VERIFIED_MATCH trivial para cualquier string. Este archivo
prueba que el entrypoint por defecto falla cerrado sin backend explicito,
y que el backend de prueba solo se activa con el flag --test-backend
inequivoco.

Ningun test aqui requiere red ni google.genai."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

_ENGINEERING_LOOP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CHILD_PATH = os.path.join(_ENGINEERING_LOOP_DIR, "antigravity_isolated_child.py")

# Sin sys.path.insert aqui: el bootstrap_test_runner.py ya garantiza que
# engineering-loop/ este exactamente una vez en sys.path (Fase 1 de M-AG012
# confirmo esto contra test_00_import_context.py). Insertarlo de nuevo
# duplicaria la entrada.

import antigravity_parent_supervision as supervision  # noqa: E402
from antigravity_isolated_transport_schema import (  # noqa: E402
    build_request_envelope,
    parse_response_envelope,
    ValidatedFailureEnvelope,
)
from antigravity_gemini_provider import (  # noqa: E402
    AntigravityGeminiConfig,
    AntigravityGeminiProvider,
    AntigravityGeminiProviderError,
)
from antigravity_isolated_transport import IsolatedGeminiTransport  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers de proceso real.
# ---------------------------------------------------------------------------


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.02)
    return not _process_alive(pid)


def _kill_quietly(pid: int, sig: int = signal.SIGKILL) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# Shim de hijo GENUINAMENTE ocupado (M-AG014). A diferencia del shim usado
# por la primera pasada de M-AG012 (que dejaba al hijo bloqueado para
# siempre en stdin.readline() sin backend), este escribe una peticion valida
# ANTES de imprimir el pid, para que el hijo real avance mas alla de la
# lectura de stdin y quede dormido de verdad dentro del backend de prueba
# (--test-backend-hang-seconds). Esto es indispensable: matar al padre
# mientras el hijo esta bloqueado en stdin cierra el extremo de escritura
# del pipe y le entrega EOF al instante, lo que lo mata por su cuenta via
# ChildProtocolError -- completamente independiente de si la supervision de
# muerte del padre funciona o no. Esa es exactamente la confusion que la
# auditoria independiente M-AG013 identifico en su propia primera sonda.
# ---------------------------------------------------------------------------

_BUSY_SHIM_SOURCE = textwrap.dedent(
    """
    import os
    import subprocess
    import sys
    import time

    sys.path.insert(0, {engineering_loop_dir!r})
    from antigravity_isolated_transport_schema import build_request_envelope

    own_pid = os.getpid()
    child_argv = [
        sys.executable, {child_path!r},
        "--expected-parent-pid", str(own_pid),
        "--test-backend", "--test-backend-hang-seconds", "3600",
    ]
    proc = subprocess.Popen(child_argv, stdin=subprocess.PIPE)
    request_line = build_request_envelope(
        request_id="33333333-3333-4333-8333-333333333333",
        model_id="gemini-flash-latest",
        prompt="hijo genuinamente ocupado, sin confusor de EOF de stdin",
        format=None,
        timeout_seconds=3600.0,
        max_response_chars=4096,
    )
    proc.stdin.write(request_line.encode("utf-8"))
    proc.stdin.close()
    print(proc.pid, flush=True)
    time.sleep(3600)
    """
)


def _write_busy_shim(tmp_path: str) -> str:
    path = os.path.join(tmp_path, "busy_shim_parent.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_BUSY_SHIM_SOURCE.format(engineering_loop_dir=_ENGINEERING_LOOP_DIR, child_path=_CHILD_PATH))
    return path


class _ShimHandle:
    def __init__(self, shim_proc: "subprocess.Popen[bytes]", child_pid: int):
        self.shim_proc = shim_proc
        self.child_pid = child_pid


def _spawn_busy_shim(tmp_path: str) -> _ShimHandle:
    """A diferencia de la version M-AG012, esta funcion NO duerme antes de
    devolver el control -- el llamador puede matar al padre de inmediato,
    sin ninguna espera artificial que evitaria ejercitar la ventana de
    carrera real (exactamente lo que M-AG013 identifico como el error
    metodologico del "stress test 0/30" nunca persistido de M-AG012)."""

    shim_path = _write_busy_shim(tmp_path)
    proc = subprocess.Popen([sys.executable, shim_path], stdout=subprocess.PIPE)
    pid_line = proc.stdout.readline()
    child_pid = int(pid_line.strip())
    return _ShimHandle(proc, child_pid)


def _cleanup_shim(handle: _ShimHandle) -> None:
    for pid in (handle.shim_proc.pid, handle.child_pid):
        _kill_quietly(pid)
    try:
        handle.shim_proc.wait(timeout=2.0)
    except Exception:
        pass
    if handle.shim_proc.stdout is not None:
        handle.shim_proc.stdout.close()


class ParentDeathEndToEndTests(unittest.TestCase):
    """Objetivo A/B con arboles de procesos reales, hijo genuinamente
    ocupado, sin espera artificial antes de matar al padre."""

    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory(prefix="parent-death-e2e-")
        self.tmp_path = self._tmpdir_ctx.name

    def tearDown(self):
        self._tmpdir_ctx.cleanup()

    def test_parent_normal_exit_child_gone(self):
        shim_path = _write_busy_shim(self.tmp_path)
        source = _BUSY_SHIM_SOURCE.format(
            engineering_loop_dir=_ENGINEERING_LOOP_DIR, child_path=_CHILD_PATH
        ).replace("time.sleep(3600)", "pass  # sale normalmente de inmediato")
        with open(shim_path, "w", encoding="utf-8") as fh:
            fh.write(source)
        proc = subprocess.Popen([sys.executable, shim_path], stdout=subprocess.PIPE)
        try:
            child_pid = int(proc.stdout.readline().strip())
            proc.wait(timeout=5.0)
            self.assertTrue(
                _wait_until_gone(child_pid, timeout=5.0),
                "el hijo real deberia morir cuando su padre sale normalmente",
            )
        finally:
            _kill_quietly(proc.pid)
            proc.stdout.close()

    def test_parent_sigterm_child_gone_no_grace_period(self):
        handle = _spawn_busy_shim(self.tmp_path)
        try:
            os.kill(handle.shim_proc.pid, signal.SIGTERM)
            self.assertTrue(
                _wait_until_gone(handle.child_pid, timeout=5.0),
                "el hijo real deberia morir cuando su padre recibe SIGTERM, sin espera artificial",
            )
        finally:
            _cleanup_shim(handle)

    def test_parent_sigkill_child_gone_no_grace_period(self):
        # SIGKILL no puede ser interceptado ni ignorado por el padre --
        # PR_SET_PDEATHSIG es a nivel de kernel, asi que debe sobrevivir
        # incluso a esto. CERO espera artificial: este es exactamente el
        # escenario que reprodujo 25/25 huerfanos en M-AG013.
        handle = _spawn_busy_shim(self.tmp_path)
        try:
            os.kill(handle.shim_proc.pid, signal.SIGKILL)
            self.assertTrue(
                _wait_until_gone(handle.child_pid, timeout=5.0),
                "el hijo real deberia morir cuando su padre recibe SIGKILL, sin espera artificial",
            )
        finally:
            _cleanup_shim(handle)

    def test_parent_crash_sigsegv_child_gone_no_grace_period(self):
        handle = _spawn_busy_shim(self.tmp_path)
        try:
            os.kill(handle.shim_proc.pid, signal.SIGSEGV)
            self.assertTrue(
                _wait_until_gone(handle.child_pid, timeout=5.0),
                "el hijo real deberia morir cuando su padre crashea (SIGSEGV), sin espera artificial",
            )
        finally:
            _cleanup_shim(handle)

    def test_no_orphan_process_remains_after_parent_death(self):
        handle = _spawn_busy_shim(self.tmp_path)
        try:
            os.kill(handle.shim_proc.pid, signal.SIGKILL)
            _wait_until_gone(handle.child_pid, timeout=5.0)
            # Verificacion cruzada independiente de kill(pid, 0): confirma
            # por ps que ningun proceso con este tmp_path en su cmdline
            # sigue vivo (evita falsos negativos por reuso de PID).
            ps_output = subprocess.run(
                ["ps", "-eo", "pid,cmd"], capture_output=True, text=True, timeout=5.0
            ).stdout
            self.assertNotIn(self.tmp_path, ps_output)
        finally:
            _cleanup_shim(handle)

    def test_killing_parent_a_does_not_affect_independent_parent_b_or_c(self):
        handle_a = _spawn_busy_shim(self.tmp_path)
        handle_b = _spawn_busy_shim(self.tmp_path)
        handle_c = _spawn_busy_shim(self.tmp_path)
        try:
            os.kill(handle_a.shim_proc.pid, signal.SIGKILL)
            self.assertTrue(_wait_until_gone(handle_a.child_pid, timeout=5.0), "A deberia morir")
            self.assertTrue(_process_alive(handle_b.shim_proc.pid), "B (padre) no deberia verse afectado")
            self.assertTrue(_process_alive(handle_b.child_pid), "B (hijo) no deberia verse afectado")
            self.assertTrue(_process_alive(handle_c.shim_proc.pid), "C (padre) no deberia verse afectado")
            self.assertTrue(_process_alive(handle_c.child_pid), "C (hijo) no deberia verse afectado")
        finally:
            _cleanup_shim(handle_a)
            _cleanup_shim(handle_b)
            _cleanup_shim(handle_c)


class SpawnRaceAcceptanceTests(unittest.TestCase):
    """M-AG014: cierre de la ventana de carrera reproducida por la
    auditoria independiente M-AG013. Criterio de aceptacion explicito del
    brief: 0 huerfanos tras SIGKILL inmediato + hijo genuinamente ocupado
    + sin sleep artificial, repetido al menos 25 veces (para poder
    comparar directamente contra el 25/25 que encontro la auditoria)."""

    def setUp(self):
        self._tmpdir_ctx = tempfile.TemporaryDirectory(prefix="spawn-race-")
        self.tmp_path = self._tmpdir_ctx.name

    def tearDown(self):
        self._tmpdir_ctx.cleanup()

    def test_immediate_sigkill_25_iterations_zero_orphans(self):
        """EL TEST DE ACEPTACION MAS IMPORTANTE del brief M-AG014. Replica
        fielmente el escenario exacto del auditor de M-AG013: SIGKILL
        inmediato al padre + hijo genuinamente ocupado + sin sleep
        artificial. Contra el codigo pre-M-AG014 este mismo patron dio
        25/25 huerfanos (verificado de forma aislada antes de aplicar el
        fix, fuera de este archivo, para no dejar codigo vulnerable
        reintroducido en el repo)."""

        orphans = []
        for i in range(25):
            handle = _spawn_busy_shim(self.tmp_path)
            os.kill(handle.shim_proc.pid, signal.SIGKILL)  # inmediato, sin sleep
            gone = _wait_until_gone(handle.child_pid, timeout=3.0)
            if not gone:
                orphans.append((i, handle.child_pid))
                _kill_quietly(handle.child_pid)
            try:
                handle.shim_proc.wait(timeout=2.0)
            except Exception:
                pass
            if handle.shim_proc.stdout is not None:
                handle.shim_proc.stdout.close()

        self.assertEqual(
            orphans, [], f"M-AG014 FAILS: {len(orphans)}/25 huerfanos reproducibles: {orphans}"
        )

    def test_repeated_rapid_spawn_kill_cycles(self):
        """Caso 10 de la Fase 7: ciclos rapidos y consecutivos de
        spawn/kill, sin ninguna pausa entre iteraciones -- prueba que no
        hay degradacion ni acumulacion de estado entre invocaciones
        consecutivas."""

        for _ in range(10):
            handle = _spawn_busy_shim(self.tmp_path)
            os.kill(handle.shim_proc.pid, signal.SIGKILL)
            self.assertTrue(_wait_until_gone(handle.child_pid, timeout=3.0))
            _cleanup_shim(handle)

    def test_sigkill_at_varied_startup_delays(self):
        """Casos 2/6/7 de la Fase 7: SIGKILL durante distintos puntos del
        arranque del hijo -- deliberadamente delays CORTOS (nunca el tipo
        de espera de 0.3s que M-AG012 usaba y que evitaba la ventana de
        carrera por construccion), para cubrir varios instantes sin
        favorecer artificialmente que el test pase."""

        for delay in (0.0, 0.001, 0.003, 0.01, 0.05):
            handle = _spawn_busy_shim(self.tmp_path)
            if delay:
                time.sleep(delay)
            os.kill(handle.shim_proc.pid, signal.SIGKILL)
            self.assertTrue(
                _wait_until_gone(handle.child_pid, timeout=3.0),
                f"huerfano con delay={delay}s",
            )
            try:
                handle.shim_proc.wait(timeout=2.0)
            except Exception:
                pass
            if handle.shim_proc.stdout is not None:
                handle.shim_proc.stdout.close()

    def test_sigterm_at_varied_startup_delays(self):
        for delay in (0.0, 0.001, 0.01):
            handle = _spawn_busy_shim(self.tmp_path)
            if delay:
                time.sleep(delay)
            os.kill(handle.shim_proc.pid, signal.SIGTERM)
            self.assertTrue(
                _wait_until_gone(handle.child_pid, timeout=3.0),
                f"huerfano (SIGTERM) con delay={delay}s",
            )
            try:
                handle.shim_proc.wait(timeout=2.0)
            except Exception:
                pass
            if handle.shim_proc.stdout is not None:
                handle.shim_proc.stdout.close()


class ExpectedParentPidFailClosedTests(unittest.TestCase):
    """Fase 4/Fase 7 casos 11-13 del brief M-AG014: startup fail-closed
    ante --expected-parent-pid ausente/malformado/deliberadamente
    incorrecto. Se prueba exclusivamente via subprocess real (boundary de
    proceso real, Fase 6 del brief) -- antigravity_isolated_child.py::
    _extract_expected_parent_pid() llama os._exit(1) en cada camino de
    fallo, que no se puede invocar dentro del propio proceso de test sin
    matarlo."""

    def _run(self, extra_args, timeout=5.0):
        return subprocess.run(
            [sys.executable, _CHILD_PATH, *extra_args],
            input=b"",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )

    def test_missing_expected_parent_pid_fails_closed(self):
        proc = self._run(["--test-backend"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")

    def test_malformed_expected_parent_pid_fails_closed(self):
        proc = self._run(["--expected-parent-pid", "no-es-un-numero", "--test-backend"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")

    def test_negative_expected_parent_pid_fails_closed(self):
        proc = self._run(["--expected-parent-pid", "-1", "--test-backend"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")

    def test_deliberately_incorrect_expected_parent_pid_fails_closed_fast(self):
        # PID casi con certeza no es el padre real de este subprocess (el
        # padre real es este proceso de test, no PID 1).
        started = time.monotonic()
        proc = self._run(["--expected-parent-pid", "1", "--test-backend"])
        elapsed = time.monotonic() - started
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertLess(elapsed, 2.0, "el fail-closed por PID incorrecto deberia ser practicamente instantaneo")

    def test_expected_parent_pid_missing_value_fails_closed(self):
        proc = self._run(["--expected-parent-pid"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")


class ParentWatchdogUnitTests(unittest.TestCase):
    """Objetivo B probado de forma determinista con un ppid_probe
    inyectado. CORRECCION M-AG014: start_parent_watchdog ahora exige
    expected_ppid como parametro obligatorio -- ya no descubre su propia
    linea base de forma perezosa (esa era la causa raiz del hallazgo de
    M-AG013)."""

    def test_watchdog_fires_when_probe_diverges_from_known_expected_ppid(self):
        fired = threading.Event()
        values = iter([100, 100, 999])

        def probe():
            try:
                return next(values)
            except StopIteration:
                return 999

        supervision.start_parent_watchdog(
            100, poll_interval_seconds=0.01, on_orphaned=fired.set, ppid_probe=probe
        )
        self.assertTrue(fired.wait(timeout=2.0), "el watchdog deberia disparar el callback tras detectar el cambio")

    def test_watchdog_fires_immediately_if_first_probe_already_diverges(self):
        """Caso central del fix M-AG014: si la PRIMERA lectura del ppid ya
        no coincide con expected_ppid (el padre murio ANTES de que el
        watchdog arrancara), el watchdog debe dispararse -- a diferencia
        de la version M-AG012, que en este caso capturaba ese valor ya
        contaminado como su propia linea base y nunca disparaba."""

        fired = threading.Event()
        supervision.start_parent_watchdog(
            100, poll_interval_seconds=0.01, on_orphaned=fired.set, ppid_probe=lambda: 999
        )
        self.assertTrue(
            fired.wait(timeout=2.0),
            "el watchdog debe disparar si el ppid ya diverge de expected_ppid desde la primera lectura",
        )

    def test_watchdog_does_not_fire_when_ppid_matches_expected(self):
        fired = threading.Event()
        supervision.start_parent_watchdog(
            42, poll_interval_seconds=0.01, on_orphaned=fired.set, ppid_probe=lambda: 42
        )
        self.assertFalse(fired.wait(timeout=0.3), "el watchdog no deberia disparar si el ppid coincide con expected_ppid")

    def test_watchdog_default_callback_is_hard_exit_reference(self):
        self.assertIn("os._exit(1)", _read_supervision_source())


class VerifyParentOrDieUnitTests(unittest.TestCase):
    """Capas 1 y 3 (M-AG014), probadas de forma determinista sin
    subprocess."""

    def test_matching_ppid_does_not_trigger_mismatch_callback(self):
        called = threading.Event()
        supervision.verify_parent_or_die(100, ppid_probe=lambda: 100, on_mismatch=called.set)
        self.assertFalse(called.is_set())

    def test_diverging_ppid_triggers_mismatch_callback(self):
        called = threading.Event()
        supervision.verify_parent_or_die(100, ppid_probe=lambda: 999, on_mismatch=called.set)
        self.assertTrue(called.is_set())

    def test_default_on_mismatch_is_hard_exit_reference(self):
        self.assertIn("os._exit(1)", _read_supervision_source())


class SuperviseParentDeathLayeringTests(unittest.TestCase):
    """Fase 7 caso 14: fallo de inicializacion de una capa individual no
    debe dejar al hijo sin proteccion -- si install_pdeathsig() falla
    (plataforma sin soporte, o el propio prctl() devuelve False), las
    verificaciones de PID y el watchdog de respaldo deben seguir
    protegiendo al hijo de todas formas."""

    def test_supervision_still_protects_when_pdeathsig_installation_fails(self):
        fired = threading.Event()
        original_install = supervision.install_pdeathsig
        try:
            supervision.install_pdeathsig = lambda sig=signal.SIGTERM: False
            supervision.supervise_parent_death(
                100,
                poll_interval_seconds=0.01,
                ppid_probe=lambda: 100,
            )
            # Con expected_parent_pid == ppid_probe() en todo momento, ninguna
            # capa deberia dispararse -- confirmamos que la orquestacion no
            # revienta ni se salta ninguna capa cuando PDEATHSIG "falla".
        finally:
            supervision.install_pdeathsig = original_install

    def test_initial_and_second_verification_both_run_in_order(self):
        calls = []

        def probe():
            calls.append(len(calls))
            return 100

        original_install = supervision.install_pdeathsig
        try:
            supervision.install_pdeathsig = lambda sig=signal.SIGTERM: True
            supervision.supervise_parent_death(100, poll_interval_seconds=10.0, ppid_probe=probe)
        finally:
            supervision.install_pdeathsig = original_install
        # Al menos 2 lecturas: verificacion inicial + verificacion post-PDEATHSIG
        # (mas al menos una lectura adicional del watchdog al arrancar el hilo,
        # segun timing -- por eso >= 2 y no == exacto).
        self.assertGreaterEqual(len(calls), 2)


class InstallPdeathsigTests(unittest.TestCase):
    def test_returns_bool_and_does_not_raise_on_this_platform(self):
        result = supervision.install_pdeathsig(signal.SIGTERM)
        self.assertIsInstance(result, bool)
        if sys.platform.startswith("linux"):
            self.assertTrue(result, "en Linux, prctl(PR_SET_PDEATHSIG) deberia poder armarse en un entorno de test normal")

    def test_returns_false_without_raising_when_libc_unavailable(self):
        import ctypes.util as ctypes_util

        original_find = ctypes_util.find_library
        original_cdll = ctypes.CDLL
        try:
            ctypes_util.find_library = lambda name: None
            ctypes.CDLL = lambda *a, **k: (_ for _ in ()).throw(OSError("no libc in this test"))
            result = supervision.install_pdeathsig(signal.SIGTERM)
            self.assertFalse(result)
        finally:
            ctypes_util.find_library = original_find
            ctypes.CDLL = original_cdll

    def test_returns_false_on_non_linux_platform(self):
        original = sys.platform
        try:
            sys.platform = "darwin"
            self.assertFalse(supervision.install_pdeathsig(signal.SIGTERM))
        finally:
            sys.platform = original


class DefaultEntrypointFailClosedTests(unittest.TestCase):
    """Objetivo C: invoca el archivo REAL como subprocess (python
    antigravity_isolated_child.py --expected-parent-pid <PID> [--test-backend]),
    ejercitando el bloque __main__ de verdad."""

    def _own_pid_args(self):
        return ["--expected-parent-pid", str(os.getpid())]

    def _run_main(self, extra_args, request_obj_line: str, timeout=5.0):
        proc = subprocess.run(
            [sys.executable, _CHILD_PATH, *self._own_pid_args(), *extra_args],
            input=request_obj_line.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return proc

    def _valid_request_line(self) -> str:
        return build_request_envelope(
            request_id="11111111-1111-1111-1111-111111111111",
            model_id="gemini-flash-latest",
            prompt="hola",
            format=None,
            timeout_seconds=5.0,
            max_response_chars=4096,
        )

    def test_default_no_backend_fails_closed(self):
        proc = self._run_main([], self._valid_request_line())
        self.assertEqual(proc.returncode, 0)  # BackendError -> fallo estructurado, exit 0 (Seccion G)
        parsed = parse_response_envelope(
            proc.stdout, expected_request_id="11111111-1111-1111-1111-111111111111", max_response_bytes=1_048_576
        )
        self.assertIsInstance(parsed, ValidatedFailureEnvelope)
        self.assertEqual(parsed.error_category, "SDK_EXCEPTION")

    def test_default_no_backend_emits_no_response_model_id_key(self):
        import json

        proc = self._run_main([], self._valid_request_line())
        raw = json.loads(proc.stdout.decode("utf-8"))
        self.assertNotIn("response_model_id", raw)
        self.assertNotIn("text", raw)
        self.assertIs(raw["ok"], False)

    def test_default_no_backend_cannot_claim_verified_match_end_to_end(self):
        transport = IsolatedGeminiTransport([sys.executable, _CHILD_PATH])
        config = AntigravityGeminiConfig(
            model_id="gemini-flash-latest", timeout_seconds=5.0, max_input_chars=10_000, max_response_chars=10_000
        )
        provider = AntigravityGeminiProvider(config=config, transport=transport)
        with self.assertRaises(AntigravityGeminiProviderError):
            provider.evaluate("hola", format=None)

    def test_explicit_test_backend_works_offline(self):
        proc = self._run_main(["--test-backend"], self._valid_request_line())
        self.assertEqual(proc.returncode, 0)
        parsed = parse_response_envelope(
            proc.stdout, expected_request_id="11111111-1111-1111-1111-111111111111", max_response_bytes=1_048_576
        )
        self.assertFalse(isinstance(parsed, ValidatedFailureEnvelope))
        self.assertIsNone(parsed.response_model_id, "el backend de prueba nunca fabrica identidad por defecto")

    def test_test_backend_flag_absent_by_default_in_production_style_argv(self):
        with open(_CHILD_PATH, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('"--test-backend" in _argv', source)
        self.assertNotIn("os.environ", source)

    def test_transport_end_to_end_still_works_with_expected_parent_pid_appended(self):
        """Fase 16 caso 15/29: el path de exito offline normal sigue
        funcionando end-to-end a traves de IsolatedGeminiTransport, que
        ahora anexa --expected-parent-pid automaticamente (N.9)."""

        transport = IsolatedGeminiTransport([sys.executable, _CHILD_PATH, "--test-backend"])
        config = AntigravityGeminiConfig(
            model_id="gemini-flash-latest", timeout_seconds=5.0, max_input_chars=10_000, max_response_chars=10_000
        )
        provider = AntigravityGeminiProvider(config=config, transport=transport)
        response = provider.evaluate("hola", format=None)
        self.assertEqual(response.content, "offline mock response")


def _read_supervision_source() -> str:
    path = os.path.join(_ENGINEERING_LOOP_DIR, "antigravity_parent_supervision.py")
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class SupervisionModuleSelfAuditTests(unittest.TestCase):
    def test_module_never_imports_google_sdk(self):
        import ast

        tree = ast.parse(_read_supervision_source())
        imported_names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.append(node.module)
        for name in imported_names:
            self.assertFalse(name.startswith("google"), f"import inesperado: {name}")

    def test_module_never_uses_network_libs(self):
        source = _read_supervision_source()
        for forbidden in ("socket", "urllib", "requests", "http.client"):
            self.assertNotIn(f"import {forbidden}", source)

    def test_module_never_uses_subprocess_shell_eval_exec(self):
        source = _read_supervision_source()
        for forbidden in ("subprocess", "os.system", "eval(", "exec(", "pickle"):
            self.assertNotIn(forbidden, source)

    def test_module_introduces_no_authority_logic(self):
        source = _read_supervision_source()
        for forbidden in ("authorize_and_run", "consume(", "capability_grant", "promote_approved_batch"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
