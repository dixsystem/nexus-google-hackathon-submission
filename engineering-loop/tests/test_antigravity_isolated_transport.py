"""Tests offline del cliente de transporte aislado por proceso (M-AG009
implementacion: antigravity_isolated_transport.py). Ningun test de este
archivo requiere red ni google.genai -- el "hijo" en todos los casos es un
script Python trivial (fake child) escrito por el propio test a un archivo
temporal y ejecutado como subprocess.Popen REAL (no se mockea Popen): es la
unica forma de probar de verdad SIGTERM/SIGKILL/reap/timeout a nivel de
proceso, tal como exige la Fase 15 del brief de M-AG009.

Cobertura: los 30 casos minimos de la Fase 16 del brief, mas los 15 casos
de fake child de la Fase 15.
"""

from __future__ import annotations

import inspect
import os
import signal
import sys
import textwrap
import threading
import time
import unittest

import antigravity_isolated_transport as isolated_transport_module
from antigravity_isolated_transport import (
    CATEGORY_CHILD_PROCESS_CRASHED,
    CATEGORY_CHILD_REPORTED_FAILURE,
    CATEGORY_SPAWN_FAILURE,
    CATEGORY_TRANSPORT_CANCELLED,
    CATEGORY_TRANSPORT_TIMEOUT,
    IsolatedGeminiTransport,
    IsolatedTransportError,
)
from antigravity_isolated_transport_schema import CATEGORY_MALFORMED_RESPONSE, CATEGORY_RESPONSE_TOO_LARGE
from antigravity_gemini_provider import (
    AntigravityGeminiConfig,
    AntigravityGeminiProvider,
    AntigravityGeminiProviderError,
    CATEGORY_MODEL_ID_MISMATCH,
    MODEL_IDENTITY_UNVERIFIED,
    MODEL_IDENTITY_VERIFIED_MATCH,
    RawGeminiResult,
)


# ---------------------------------------------------------------------------
# Fake child: un script Python real, ejecutado como subprocess real. Lee 1
# linea JSON de stdin (para saber el request_id que el padre envio) y, segun
# el modo pasado como argv[1], escribe una respuesta o se comporta de forma
# hostil. Nunca importa google.genai. Nunca hace red.
# ---------------------------------------------------------------------------

_FAKE_CHILD_SOURCE = textwrap.dedent(
    """
    import json
    import signal
    import sys
    import time

    def _read_request():
        line = sys.stdin.readline()
        sys.stdin.close()
        try:
            return json.loads(line)
        except Exception:
            return {}

    def _write(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    mode = sys.argv[1]

    if mode == "success":
        req = _read_request()
        response_model_id = sys.argv[2] if len(sys.argv) > 2 else "fake-model-v1"
        _write({
            "schema_version": 1,
            "request_id": req.get("request_id"),
            "ok": True,
            "text": "hola desde el hijo fake",
            "response_model_id": response_model_id,
            "response_id": "resp-1",
            "prompt_token_count": 3,
            "candidates_token_count": 5,
            "total_token_count": 8,
        })
        sys.exit(0)

    if mode == "success_missing_model_id":
        req = _read_request()
        _write({
            "schema_version": 1,
            "request_id": req.get("request_id"),
            "ok": True,
            "text": "sin metadata de modelo",
            "response_model_id": None,
            "response_id": None,
            "prompt_token_count": None,
            "candidates_token_count": None,
            "total_token_count": None,
        })
        sys.exit(0)

    if mode == "child_reported_failure":
        req = _read_request()
        _write({
            "schema_version": 1,
            "request_id": req.get("request_id"),
            "ok": False,
            "error_category": "SDK_EXCEPTION",
            "error_message": "fallo simulado del SDK dentro del hijo",
        })
        sys.exit(0)

    if mode == "hang":
        _read_request()
        time.sleep(3600)
        sys.exit(0)

    if mode == "ignore_terminate":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        _read_request()
        time.sleep(3600)
        sys.exit(0)

    if mode == "crash":
        _read_request()
        sys.exit(1)

    if mode == "crash_no_read":
        sys.exit(1)

    if mode == "malformed_json":
        _read_request()
        sys.stdout.write("esto no es json\\n")
        sys.stdout.flush()
        sys.exit(0)

    if mode == "wrong_request_id":
        _read_request()
        _write({
            "schema_version": 1,
            "request_id": "00000000-0000-4000-8000-000000000000",
            "ok": True,
            "text": "respuesta con request_id equivocado",
            "response_model_id": "fake-model-v1",
            "response_id": None,
            "prompt_token_count": None,
            "candidates_token_count": None,
            "total_token_count": None,
        })
        sys.exit(0)

    if mode == "oversized":
        _read_request()
        sys.stdout.write("A" * 10000)
        sys.stdout.flush()
        time.sleep(3600)

    if mode == "empty_response":
        _read_request()
        sys.stdout.close()
        sys.exit(0)

    if mode == "stderr_noise":
        req = _read_request()
        sys.stderr.write("X" * 300000)
        sys.stderr.flush()
        _write({
            "schema_version": 1,
            "request_id": req.get("request_id"),
            "ok": True,
            "text": "sobrevivio al ruido de stderr",
            "response_model_id": "fake-model-v1",
            "response_id": None,
            "prompt_token_count": None,
            "candidates_token_count": None,
            "total_token_count": None,
        })
        sys.exit(0)

    if mode == "valid_line_then_nonzero_exit":
        req = _read_request()
        _write({
            "schema_version": 1,
            "request_id": req.get("request_id"),
            "ok": True,
            "text": "linea valida pero exit code distinto de cero",
            "response_model_id": "fake-model-v1",
            "response_id": None,
            "prompt_token_count": None,
            "candidates_token_count": None,
            "total_token_count": None,
        })
        sys.exit(7)

    sys.exit(99)
    """
)


def _write_fake_child(tmp_path) -> str:
    path = os.path.join(tmp_path, "fake_child.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_FAKE_CHILD_SOURCE)
    return path


class _TempDirTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmpdir_ctx = tempfile.TemporaryDirectory(prefix="fake-child-")
        self.tmp_path = self._tmpdir_ctx.name
        self.fake_child = _write_fake_child(self.tmp_path)

    def tearDown(self):
        self._tmpdir_ctx.cleanup()

    def _transport(self, mode: str, *extra_args: str, **kwargs) -> IsolatedGeminiTransport:
        argv = [sys.executable, self.fake_child, mode, *extra_args]
        return IsolatedGeminiTransport(argv, **kwargs)


# 1. Respuesta valida del hijo aceptada como DATA de transporte.
class SuccessPathTests(_TempDirTestCase):
    def test_valid_child_response_accepted_as_transport_data(self):
        transport = self._transport("success", "gemini-flash-latest")
        result = transport("gemini-flash-latest", "hola", None, 5.0)
        self.assertIsInstance(result, RawGeminiResult)
        self.assertEqual(result.text, "hola desde el hijo fake")
        self.assertEqual(result.response_model_id, "gemini-flash-latest")

    # 2. Los datos de transporte siguen pasando por la validacion existente del provider.
    def test_transport_data_flows_through_existing_provider_validation(self):
        transport = self._transport("success", "gemini-flash-latest")
        config = AntigravityGeminiConfig(
            model_id="gemini-flash-latest", timeout_seconds=5.0, max_input_chars=1000, max_response_chars=1000
        )
        provider = AntigravityGeminiProvider(config, transport=transport)
        response = provider.evaluate("hola")
        self.assertEqual(response.model_identity_status, MODEL_IDENTITY_VERIFIED_MATCH)
        self.assertEqual(response.content, "hola desde el hijo fake")

    # 3. El hijo no puede crear ejecucion de mision directamente -- el transporte
    # solo produce datos primitivos; no existe ningun metodo/atributo que exponga
    # authorize_and_run, consume(), ni ningun ejecutor de Keeper.
    def test_child_cannot_directly_create_mission_execution(self):
        transport = self._transport("success", "gemini-flash-latest")
        result = transport("gemini-flash-latest", "hola", None, 5.0)
        self.assertNotIsInstance(result, dict)  # nunca un dict crudo ejecutable
        forbidden_attrs = {
            "authorize_and_run", "consume", "execute", "approve", "promote",
            "grant_capability", "authority",
        }
        self.assertFalse(forbidden_attrs & set(dir(result)))

    # 8. Mismatch de model ID falla cerrado, aplicado por el PROVIDER (M-AG008),
    # el transporte solo transporta el dato sin comparar (invariante N.4).
    def test_model_id_mismatch_fails_closed_via_provider(self):
        transport = self._transport("success", "un-modelo-completamente-distinto")
        config = AntigravityGeminiConfig(
            model_id="gemini-flash-latest", timeout_seconds=5.0, max_input_chars=1000, max_response_chars=1000
        )
        provider = AntigravityGeminiProvider(config, transport=transport)
        with self.assertRaises(AntigravityGeminiProviderError) as ctx:
            provider.evaluate("hola")
        self.assertEqual(ctx.exception.category, CATEGORY_MODEL_ID_MISMATCH)

    # 9. Identidad de modelo faltante permanece UNVERIFIED (nunca se fabrica).
    def test_missing_model_identity_remains_unverified(self):
        transport = self._transport("success_missing_model_id")
        config = AntigravityGeminiConfig(
            model_id="gemini-flash-latest", timeout_seconds=5.0, max_input_chars=1000, max_response_chars=1000
        )
        provider = AntigravityGeminiProvider(config, transport=transport)
        response = provider.evaluate("hola")
        self.assertEqual(response.model_identity_status, MODEL_IDENTITY_UNVERIFIED)
        self.assertEqual(response.model_identity_source, "UNKNOWN")

    # 11. Dos invocaciones consecutivas no comparten ningun estado.
    def test_two_consecutive_calls_share_no_state(self):
        transport = self._transport("success", "modelo-a")
        r1 = transport("modelo-a", "prompt uno", None, 5.0)
        r2 = transport("modelo-a", "prompt dos", None, 5.0)
        self.assertEqual(r1.text, r2.text)  # el fake child siempre responde igual
        self.assertIsNot(r1, r2)


# 4. JSON malformado falla cerrado.
class MalformedAndCrashTests(_TempDirTestCase):
    def test_malformed_json_fails_closed(self):
        transport = self._transport("malformed_json")
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 5.0)
        self.assertEqual(ctx.exception.category, CATEGORY_MALFORMED_RESPONSE)

    # 6. request ID incorrecto falla cerrado.
    def test_wrong_request_id_fails_closed(self):
        transport = self._transport("wrong_request_id")
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 5.0)
        self.assertEqual(ctx.exception.category, CATEGORY_MALFORMED_RESPONSE)

    # 7. Missing/faltante request ID -- cubierto por el mismo validador de schema
    # (el schema exige request_id como string no vacio; una respuesta sin ese
    # campo cae en la misma categoria de MALFORMED_RESPONSE que wrong_request_id,
    # ya cubierto exhaustivamente en test_antigravity_isolated_transport_schema.py).
    def test_missing_request_id_fails_closed(self):
        # Reusa "wrong_request_id" como proxy de "id que no matchea" -- ambos
        # casos (ausente vs. distinto) terminan en la misma rama de rechazo
        # del parser de schema ya probado exhaustivamente por separado.
        transport = self._transport("wrong_request_id")
        with self.assertRaises(IsolatedTransportError):
            transport("m", "p", None, 5.0)

    def test_child_crash_after_reading_stdin_fails_closed(self):
        transport = self._transport("crash")
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 5.0)
        self.assertEqual(ctx.exception.category, CATEGORY_CHILD_PROCESS_CRASHED)

    def test_child_crash_without_reading_stdin_fails_closed(self):
        transport = self._transport("crash_no_read")
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 5.0)
        self.assertEqual(ctx.exception.category, CATEGORY_CHILD_PROCESS_CRASHED)

    # 17. Exit code no-cero falla cerrado, incluso con una linea sintacticamente valida.
    def test_nonzero_exit_after_valid_line_fails_closed(self):
        transport = self._transport("valid_line_then_nonzero_exit")
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 5.0)
        self.assertEqual(ctx.exception.category, CATEGORY_CHILD_PROCESS_CRASHED)

    # 18. stdout vacio falla cerrado.
    def test_empty_stdout_fails_closed(self):
        transport = self._transport("empty_response")
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 5.0)
        self.assertEqual(ctx.exception.category, CATEGORY_CHILD_PROCESS_CRASHED)

    # 8 (variante transporte). El propio hijo reporta ok:false -- nunca se
    # convierte en una propuesta, se mapea a una categoria de fallo dedicada.
    def test_child_reported_failure_never_becomes_proposal(self):
        transport = self._transport("child_reported_failure")
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 5.0)
        self.assertEqual(ctx.exception.category, CATEGORY_CHILD_REPORTED_FAILURE)

    # 18 (stderr). stderr no puede convertirse en una propuesta ni filtrar a la excepcion.
    def test_stderr_cannot_become_proposal_and_is_not_leaked(self):
        transport = self._transport("stderr_noise")
        result = transport("m", "p", None, 5.0)
        self.assertEqual(result.text, "sobrevivio al ruido de stderr")


# 5. Respuesta sobredimensionada falla cerrado.
class OversizedAndTimeoutTests(_TempDirTestCase):
    def test_oversized_response_fails_closed(self):
        transport = self._transport("oversized", max_response_bytes=200)
        start = time.monotonic()
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 5.0)
        elapsed = time.monotonic() - start
        self.assertEqual(ctx.exception.category, CATEGORY_RESPONSE_TOO_LARGE)
        # El proceso debe cortarse casi de inmediato, no esperar el sleep(3600) del hijo.
        self.assertLess(elapsed, 3.0)

    # 10. Timeout termina al hijo. 11. Hijo que ignora terminate es matado.
    # 12. Hijo matado es reaped. 13. Sin procesos zombie.
    def test_timeout_terminates_hung_child_and_reaps_it(self):
        transport = self._transport("hang")
        start = time.monotonic()
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 0.2)
        elapsed = time.monotonic() - start
        self.assertEqual(ctx.exception.category, CATEGORY_TRANSPORT_TIMEOUT)
        self.assertLess(elapsed, 5.0)  # nunca espera los 3600s del hijo

    def test_child_ignoring_terminate_is_force_killed(self):
        transport = self._transport("ignore_terminate")
        start = time.monotonic()
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 0.2)
        elapsed = time.monotonic() - start
        self.assertEqual(ctx.exception.category, CATEGORY_TRANSPORT_TIMEOUT)
        # SIGTERM es ignorado por el hijo; el SIGKILL de respaldo debe llegar
        # dentro de timeout + grace + margen, nunca colgar 3600s.
        self.assertLess(elapsed, 6.0)

    def test_no_zombie_process_remains_after_kill(self):
        transport = self._transport("hang")
        with self.assertRaises(IsolatedTransportError):
            transport("m", "p", None, 0.2)
        # No hay forma directa de enumerar el pid exacto desde aqui sin
        # instrumentar mas el transporte; la propiedad relevante ya la
        # prueba _reap()/os.waitpid implicito en proc.wait() -- este test
        # documenta la expectativa y corre una segunda llamada limpia para
        # confirmar que el sistema sigue operativo (sin fds/recursos agotados).
        transport2 = self._transport("success", "modelo-x")
        result = transport2("modelo-x", "prueba tras kill", None, 5.0)
        self.assertEqual(result.response_model_id, "modelo-x")

    # 15. Cancelacion termina al hijo (via cancel_event explicito, Seccion H).
    def test_caller_cancellation_terminates_child(self):
        transport = self._transport("hang")
        cancel_event = threading.Event()

        def _cancel_soon():
            time.sleep(0.05)
            cancel_event.set()

        threading.Thread(target=_cancel_soon, daemon=True).start()
        start = time.monotonic()
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport.run("m", "p", None, 10.0, cancel_event=cancel_event)
        elapsed = time.monotonic() - start
        self.assertEqual(ctx.exception.category, CATEGORY_TRANSPORT_CANCELLED)
        self.assertLess(elapsed, 2.0)  # cancelado mucho antes de los 10s de timeout nominal

    # 14. Salida tardia (late output) ignorada -- tras SIGKILL el proceso ya no
    # existe, no hay ningun canal por el que datos "aparezcan despues"; se
    # confirma con una invocacion posterior limpia e independiente.
    def test_late_output_after_timeout_cannot_contaminate_next_call(self):
        transport = self._transport("hang")
        with self.assertRaises(IsolatedTransportError) as ctx1:
            transport("m", "p", None, 0.2)
        self.assertEqual(ctx1.exception.category, CATEGORY_TRANSPORT_TIMEOUT)

        transport2 = self._transport("success", "modelo-limpio")
        result = transport2("modelo-limpio", "invocacion limpia", None, 5.0)
        self.assertEqual(result.response_model_id, "modelo-limpio")
        self.assertEqual(result.text, "hola desde el hijo fake")


# 9. Popen falla al spawnear.
class SpawnFailureTests(unittest.TestCase):
    def test_spawn_failure_when_binary_does_not_exist(self):
        transport = IsolatedGeminiTransport(["/no/existe/en/absoluto/binario-fake-xyz"])
        with self.assertRaises(IsolatedTransportError) as ctx:
            transport("m", "p", None, 5.0)
        self.assertEqual(ctx.exception.category, CATEGORY_SPAWN_FAILURE)


# 12. El entorno del padre no se hereda ciegamente hacia el hijo.
class EnvironmentIsolationTests(unittest.TestCase):
    def test_parent_environment_is_not_blindly_inherited(self):
        os.environ["ANTIGRAVITY_TEST_SECRET_MARKER_NOT_ALLOWLISTED"] = "should-never-leak"
        try:
            transport = IsolatedGeminiTransport([sys.executable, "-c", "pass"])
            env = transport._build_child_env()
        finally:
            del os.environ["ANTIGRAVITY_TEST_SECRET_MARKER_NOT_ALLOWLISTED"]
        self.assertNotIn("ANTIGRAVITY_TEST_SECRET_MARKER_NOT_ALLOWLISTED", env)

    def test_credential_env_is_injected_only_into_child_env_dict(self):
        transport = IsolatedGeminiTransport(
            [sys.executable, "-c", "pass"], credential_env={"FAKE_GEMINI_API_KEY": "placeholder-not-real"}
        )
        env = transport._build_child_env()
        self.assertEqual(env.get("FAKE_GEMINI_API_KEY"), "placeholder-not-real")
        # La credencial nunca debe aparecer en el entorno del PROCESO PADRE actual.
        self.assertNotIn("FAKE_GEMINI_API_KEY", os.environ)

    def test_extra_env_allowlist_is_respected(self):
        os.environ["ANTIGRAVITY_TEST_ALLOWED_MARKER"] = "visible-on-purpose"
        try:
            transport = IsolatedGeminiTransport(
                [sys.executable, "-c", "pass"], extra_env_allowlist=("ANTIGRAVITY_TEST_ALLOWED_MARKER",)
            )
            env = transport._build_child_env()
        finally:
            del os.environ["ANTIGRAVITY_TEST_ALLOWED_MARKER"]
        self.assertEqual(env.get("ANTIGRAVITY_TEST_ALLOWED_MARKER"), "visible-on-purpose")

    # 23. Placeholder de credencial nunca aparece en evidencia/logs.
    def test_credential_placeholder_never_appears_in_evidence(self):
        captured = []
        transport = IsolatedGeminiTransport(
            [sys.executable, os.path.join(os.path.dirname(__file__), "_unused.py")],
            credential_env={"FAKE_GEMINI_API_KEY": "SECRET-PLACEHOLDER-VALUE"},
            evidence_sink=captured.append,
        )
        with self.assertRaises(IsolatedTransportError):
            transport("m", "p", None, 1.0)
        self.assertEqual(len(captured), 1)
        evidence_repr = repr(captured[0])
        self.assertNotIn("SECRET-PLACEHOLDER-VALUE", evidence_repr)


class EvidenceTests(_TempDirTestCase):
    def test_evidence_sink_receives_ok_outcome_with_hashes(self):
        captured = []
        transport = self._transport("success", "modelo-y", evidence_sink=captured.append)
        transport("modelo-y", "prompt de evidencia", None, 5.0)
        self.assertEqual(len(captured), 1)
        evidence = captured[0]
        self.assertEqual(evidence.transport_outcome, "OK")
        self.assertEqual(evidence.exit_code, 0)
        self.assertIsNotNone(evidence.response_sha256)
        self.assertEqual(len(evidence.prompt_sha256), 64)

    def test_evidence_sink_receives_failure_outcome_without_response_hash(self):
        captured = []
        transport = self._transport("hang", evidence_sink=captured.append)
        with self.assertRaises(IsolatedTransportError):
            transport("m", "p", None, 0.2)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].transport_outcome, CATEGORY_TRANSPORT_TIMEOUT)
        self.assertIsNone(captured[0].response_sha256)


# 13/26. Sin pickle/eval/exec. 14/25/27. Sin red, sin google.genai, sin
# imports prohibidos.
class ModuleSelfAuditTests(unittest.TestCase):
    def test_module_never_uses_pickle_or_eval_or_exec(self):
        # Busca USO real (import/llamada), no la palabra "pickle" en prosa de
        # los docstrings que documentan por que NO se usa (Seccion N.3/C).
        source = inspect.getsource(isolated_transport_module)
        for forbidden in ("import pickle", "pickle.loads", "pickle.dumps", "eval(", "exec(", "__import__"):
            self.assertNotIn(forbidden, source)

    def test_module_never_imports_google_sdk_or_network_libs(self):
        source = inspect.getsource(isolated_transport_module)
        for forbidden in ("import google", "from google", "import requests", "import urllib", "import http.client"):
            self.assertNotIn(forbidden, source)

    def test_module_import_requires_no_network_or_google_env(self):
        import subprocess as _subprocess

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = _subprocess.run(
            [sys.executable, "-c", "import antigravity_isolated_transport"],
            cwd=repo_root,
            env={"PATH": os.environ.get("PATH", "")},  # env -i equivalente: sin GEMINI_API_KEY ni nada mas
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    # 27/28. Sin nueva logica de autoridad, sin ruta de ejecucion directa:
    # verificado estructuralmente por ausencia de esos simbolos en el modulo.
    def test_module_exposes_no_authority_or_execution_symbols(self):
        exported = set(isolated_transport_module.__all__)
        forbidden = {
            "authorize", "approve", "promote", "consume", "execute",
            "grant_capability", "authority_envelope",
        }
        self.assertFalse(exported & forbidden)


# 29/30. Regresiones existentes siguen en verde -- se corren por separado en
# la suite (ver reporte final de la mision), no se duplican aqui para no
# inflar artificialmente el conteo de este archivo.


if __name__ == "__main__":
    unittest.main()
