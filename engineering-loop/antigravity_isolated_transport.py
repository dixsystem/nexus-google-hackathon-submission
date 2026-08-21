"""Cliente del lado del padre para el boundary de transporte aislado por
proceso (M-AG009 diseno: M_AG009_ISOLATED_TRANSPORT_BOUNDARY_DESIGN_V1.md,
Secciones B/D/E/G/H/N). Implementa el mismo protocolo Transport que
antigravity_gemini_provider.py ya espera de cualquier proveedor -- cero
cambios en ese archivo, cero cambios en la cadena de gobernanza NEXUS.

Resuelve la limitacion conocida y documentada en
antigravity_gemini_provider.py (M-AG008): un hilo Python colgado no se
puede matar de verdad. Aqui la unidad de aislamiento es un PROCESO del
sistema operativo, matable con SIGTERM->SIGKILL de verdad.

Invariantes obligatorios (Seccion N del diseno, no negociables):
- N.1 Un proceso hijo por llamada, nunca reusado -- cada run()/__call__
  crea su propio subprocess.Popen, directorio temporal y request_id.
- N.2 Este modulo NUNCA importa google.genai ni google.antigravity --
  verificable por ausencia de esos imports aqui (test estatico dedicado).
- N.3 El canal IPC transporta solo JSON de datos primitivos, deserializado
  exclusivamente via antigravity_isolated_transport_schema.parse_response_
  envelope (que a su vez usa json.loads exclusivamente) -- nunca pickle,
  nunca eval/exec de contenido recibido.
- N.4 Este modulo NO compara response_model_id contra el model_id
  configurado -- esa comparacion sigue siendo responsabilidad exclusiva
  de AntigravityGeminiProvider.evaluate() (M-AG008). Duplicarla aqui
  crearia una segunda fuente de verdad.
- N.5 El schema de request/response no tiene ningun campo de autoridad
  (ya lo garantiza antigravity_isolated_transport_schema.py con su
  politica de "unknown fields rejected").
- N.6 Este modulo nunca invoca authorize_and_run, consume(), ni ningun
  ejecutor de Keeper. Su responsabilidad termina al devolver un
  RawGeminiResult (datos) al provider.
- N.7 La credencial (si se suministra via credential_env) solo existe en
  el entorno especifico y efimero de cada proceso hijo -- nunca se
  establece en el entorno del proceso padre, nunca se incluye en
  evidencia/logs/mensajes de excepcion.
- N.8 Todo timeout/cancelacion termina con el proceso REALMENTE muerto
  (SIGTERM -> espera de gracia -> SIGKILL de respaldo sobre el grupo de
  proceso), nunca con un proceso o hilo abandonado.
- N.9 (M-AG014) Este modulo SIEMPRE anexa `--expected-parent-pid <PID>`
  (su propio os.getpid(), calculado ANTES de spawnear) al argv real con
  el que lanza al hijo -- nunca depende de que el llamador de
  IsolatedGeminiTransport lo agregue manualmente. Esto es lo que le
  permite al hijo real (antigravity_isolated_child.py) verificar su
  padre contra un valor conocido de antemano en vez de descubrirlo
  perezosamente, cerrando la ventana de carrera que la auditoria
  independiente M-AG013 reprodujo 25/25 veces.

Regla de fail-closed adicional de este modulo: una respuesta solo se
acepta si el proceso hijo ademas termino con exit_code == 0. Una linea
de respuesta sintacticamente valida seguida de un exit code distinto de
cero se descarta por completo (CHILD_PROCESS_CRASHED) -- una respuesta
"valida" no da derecho por si sola a dejar un proceso en estado anomalo.
"""

from __future__ import annotations

import hashlib
import os
import select
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, Union

from antigravity_gemini_provider import RawGeminiResult
from antigravity_transport_protocol import CancellableTransport
from antigravity_isolated_transport_schema import (
    CATEGORY_RESPONSE_TOO_LARGE,
    TransportEnvelopeError,
    ValidatedFailureEnvelope,
    build_request_envelope,
    parse_response_envelope,
)

_GRACE_SECONDS = 2.0
_POLL_SECONDS = 0.01
_STDERR_DRAIN_CAP_BYTES = 1_048_576  # 1 MiB -- solo para evitar deadlock del pipe, nunca se expone
_DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024

CATEGORY_TRANSPORT_TIMEOUT = "TRANSPORT_TIMEOUT"
CATEGORY_TRANSPORT_CANCELLED = "TRANSPORT_CANCELLED"
CATEGORY_CHILD_PROCESS_CRASHED = "CHILD_PROCESS_CRASHED"
CATEGORY_SPAWN_FAILURE = "SPAWN_FAILURE"
CATEGORY_CHILD_REPORTED_FAILURE = "CHILD_REPORTED_FAILURE"

# Variables de entorno minimas necesarias para que un interprete Python
# hijo arranque de forma razonable. Nunca incluye nada relacionado con
# credenciales -- esas llegan exclusivamente via credential_env (N.7).
_DEFAULT_ENV_ALLOWLIST = ("PATH",)


class IsolatedTransportError(Exception):
    """Fail-closed: cualquier desviacion del protocolo de transporte lanza
    esto. Nunca se construye un RawGeminiResult parcial."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class TransportEvidence:
    """Registro de transporte forense (Seccion J del diseno) -- NUNCA
    autoritativo, nunca participa en la decision ALLOW/DENY de NEXUS. El
    llamador decide donde persistirlo (evidence_sink); este modulo nunca
    escribe a disco por si mismo."""

    request_id: str
    spawn_timestamp: float
    term_timestamp: float
    process_pid: Optional[int]
    prompt_sha256: str
    response_sha256: Optional[str]
    configured_model_id: str
    transport_outcome: str
    latency_ms: int
    exit_code: Optional[int]


EvidenceSink = Callable[[TransportEvidence], None]

# Mismo protocolo Transport que antigravity_gemini_provider.py ya define.
Transport = Callable[[str, str, Union[str, dict, None], float], RawGeminiResult]


class _ChildDriveResult:
    __slots__ = ("status", "line", "exit_code")

    def __init__(self, status: str, line: Optional[bytes], exit_code: Optional[int]):
        self.status = status
        self.line = line
        self.exit_code = exit_code


class IsolatedGeminiTransport(CancellableTransport):
    """Transport que habla con un proceso hijo desechable por stdin/stdout
    en vez de invocar directamente un SDK. Cada llamada a __call__/run crea
    exactamente un subprocess.Popen nuevo (N.1).

    Hereda de CancellableTransport (M-AG010) para que
    AntigravityGeminiProvider.evaluate() pueda detectar via isinstance()
    real -- nunca duck-typing -- que este transport soporta cancel_event
    de punta a punta y prefiera invocar .run(..., cancel_event=...) en vez
    de __call__(...) (que siempre pasa cancel_event=None). Cierra el
    hallazgo dejado abierto por la primera pasada de M-AG009: sin esto,
    una cancelacion iniciada en evaluate() hacia que el hilo daemon del
    provider abandonara la espera, pero el proceso hijo real seguia vivo
    hasta su propio deadline interno."""

    def __init__(
        self,
        child_argv: Sequence[str],
        *,
        credential_env: Optional[Mapping[str, str]] = None,
        extra_env_allowlist: Sequence[str] = (),
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        evidence_sink: Optional[EvidenceSink] = None,
        uuid_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
    ):
        if not child_argv:
            raise IsolatedTransportError("CONFIGURATION", "child_argv must be non-empty")
        self._child_argv = list(child_argv)
        self._credential_env = dict(credential_env or {})
        self._env_allowlist = tuple(_DEFAULT_ENV_ALLOWLIST) + tuple(extra_env_allowlist)
        self._max_response_bytes = max_response_bytes
        self._evidence_sink = evidence_sink
        self._uuid_factory = uuid_factory

    def __call__(
        self, model_id: str, prompt: str, format: Union[str, dict, None], timeout_seconds: float
    ) -> RawGeminiResult:
        return self.run(model_id, prompt, format, timeout_seconds, cancel_event=None)

    def run(
        self,
        model_id: str,
        prompt: str,
        format: Union[str, dict, None],
        timeout_seconds: float,
        *,
        cancel_event: Optional[threading.Event] = None,
    ) -> RawGeminiResult:
        request_id = self._uuid_factory()
        request_line = build_request_envelope(
            request_id=request_id,
            model_id=model_id,
            prompt=prompt,
            format=format if isinstance(format, dict) else None,
            timeout_seconds=timeout_seconds,
            max_response_chars=self._max_response_bytes,
        )
        prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        spawn_ts = time.monotonic()
        env = self._build_child_env()
        # N.9: el padre conoce su propio PID ANTES de spawnear -- se lo pasa al
        # hijo como informacion de arranque inmutable, nunca dejando que el hijo
        # la descubra por su cuenta despues de arrancar (M-AG014).
        own_pid = os.getpid()
        argv = list(self._child_argv) + ["--expected-parent-pid", str(own_pid)]

        with tempfile.TemporaryDirectory(prefix="antigravity-isolated-") as cwd:
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    start_new_session=True,
                )
            except OSError as exc:
                term_ts = time.monotonic()
                self._emit_evidence(
                    request_id, spawn_ts, term_ts, None, prompt_sha256, None,
                    model_id, CATEGORY_SPAWN_FAILURE, 0, None,
                )
                raise IsolatedTransportError(
                    CATEGORY_SPAWN_FAILURE, f"failed to spawn isolated child process: {exc}"
                ) from exc

            try:
                with proc:  # garantiza cierre de stdin/stdout/stderr (Seccion G.7)
                    drive = self._drive_child(proc, request_line, timeout_seconds, cancel_event)
            finally:
                term_ts = time.monotonic()

            latency_ms = round((term_ts - spawn_ts) * 1000)

            if drive.status in (
                CATEGORY_TRANSPORT_TIMEOUT,
                CATEGORY_TRANSPORT_CANCELLED,
                CATEGORY_CHILD_PROCESS_CRASHED,
                CATEGORY_RESPONSE_TOO_LARGE,
            ):
                self._emit_evidence(
                    request_id, spawn_ts, term_ts, proc.pid, prompt_sha256, None,
                    model_id, drive.status, latency_ms, drive.exit_code,
                )
                raise IsolatedTransportError(
                    drive.status, f"isolated child transport failed: {drive.status}"
                )

            # drive.status == "LINE_OK": linea completa recibida y exit_code == 0 confirmado.
            try:
                parsed = parse_response_envelope(
                    drive.line, expected_request_id=request_id, max_response_bytes=self._max_response_bytes
                )
            except TransportEnvelopeError as exc:
                self._emit_evidence(
                    request_id, spawn_ts, term_ts, proc.pid, prompt_sha256, None,
                    model_id, exc.category, latency_ms, drive.exit_code,
                )
                raise IsolatedTransportError(exc.category, str(exc)) from exc

            response_sha256 = hashlib.sha256(drive.line).hexdigest()

            if isinstance(parsed, ValidatedFailureEnvelope):
                self._emit_evidence(
                    request_id, spawn_ts, term_ts, proc.pid, prompt_sha256, response_sha256,
                    model_id, CATEGORY_CHILD_REPORTED_FAILURE, latency_ms, drive.exit_code,
                )
                # Mensaje ya saneado/acotado por el propio hijo y validado por el
                # schema (longitud maxima); nunca se agrega texto libre adicional
                # que pudiera filtrar diagnostico sensible (Seccion O del diseno).
                raise IsolatedTransportError(CATEGORY_CHILD_REPORTED_FAILURE, parsed.error_message)

            self._emit_evidence(
                request_id, spawn_ts, term_ts, proc.pid, prompt_sha256, response_sha256,
                model_id, "OK", latency_ms, drive.exit_code,
            )
            return RawGeminiResult(
                text=parsed.text,
                response_model_id=parsed.response_model_id,
                response_id=parsed.response_id,
                prompt_token_count=parsed.prompt_token_count,
                candidates_token_count=parsed.candidates_token_count,
                total_token_count=parsed.total_token_count,
            )

    # -- internals -----------------------------------------------------

    def _build_child_env(self) -> dict:
        """Construye un entorno NUEVO y minimo para el proceso hijo -- nunca
        env=None (que heredaria todo os.environ del padre, incluyendo
        cualquier secreto de Keeper que no tenga nada que ver con Gemini).
        Solo copia variables explicitamente permitidas por la allowlist, mas
        las credenciales inyectadas explicitamente para este transporte."""

        env = {}
        for name in self._env_allowlist:
            if name in os.environ:
                env[name] = os.environ[name]
        env.update(self._credential_env)
        return env

    def _drive_child(
        self,
        proc: "subprocess.Popen[bytes]",
        request_line: str,
        timeout_seconds: float,
        cancel_event: Optional[threading.Event],
    ) -> _ChildDriveResult:
        deadline = time.monotonic() + timeout_seconds

        try:
            proc.stdin.write(request_line.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            self._kill_process_group(proc)
            exit_code = self._reap(proc)
            return _ChildDriveResult(CATEGORY_CHILD_PROCESS_CRASHED, None, exit_code)

        stdout_buf = bytearray()
        stderr_open = True
        stdout_open = True
        line: Optional[bytes] = None
        timed_out = False
        cancelled = False

        stdout_fd = proc.stdout.fileno()
        stderr_fd = proc.stderr.fileno()

        while stdout_open and line is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break

            watch = []
            if stdout_open:
                watch.append(stdout_fd)
            if stderr_open:
                watch.append(stderr_fd)
            ready, _, _ = select.select(watch, [], [], min(remaining, _POLL_SECONDS))

            if stdout_fd in ready:
                chunk = os.read(stdout_fd, 65536)
                if not chunk:
                    stdout_open = False
                else:
                    stdout_buf.extend(chunk)
                    if len(stdout_buf) > self._max_response_bytes:
                        self._kill_process_group(proc)
                        exit_code = self._reap(proc)
                        return _ChildDriveResult(CATEGORY_RESPONSE_TOO_LARGE, None, exit_code)
                    newline_at = stdout_buf.find(b"\n")
                    if newline_at != -1:
                        line = bytes(stdout_buf[:newline_at])
                        break

            if stderr_fd in ready:
                chunk = os.read(stderr_fd, 65536)
                if not chunk:
                    stderr_open = False
                # Contenido de stderr deliberadamente descartado: nunca se
                # incluye en excepciones/evidencia (Seccion O -- redaccion de
                # logs; un hijo comprometido no debe poder filtrar diagnostico
                # a traves de este canal).

        if line is None:
            if cancelled:
                self._kill_process_group(proc)
                exit_code = self._reap(proc)
                return _ChildDriveResult(CATEGORY_TRANSPORT_CANCELLED, None, exit_code)
            if timed_out:
                self._kill_process_group(proc)
                exit_code = self._reap(proc)
                return _ChildDriveResult(CATEGORY_TRANSPORT_TIMEOUT, None, exit_code)
            # stdout se cerro (EOF) sin entregar una linea completa.
            self._kill_process_group(proc)
            exit_code = self._reap(proc)
            return _ChildDriveResult(CATEGORY_CHILD_PROCESS_CRASHED, None, exit_code)

        # Linea completa recibida. Una respuesta "valida" no da por si sola
        # derecho a dejar el proceso en un estado anomalo: se exige que
        # termine limpio con exit_code == 0 dentro de un plazo de gracia
        # corto, o se trata como crash y se descarta la linea (Seccion G.5).
        try:
            exit_code = proc.wait(timeout=_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            self._kill_process_group(proc)
            exit_code = self._reap(proc)

        if exit_code != 0:
            return _ChildDriveResult(CATEGORY_CHILD_PROCESS_CRASHED, None, exit_code)

        return _ChildDriveResult("LINE_OK", line, exit_code)

    @staticmethod
    def _kill_process_group(proc: "subprocess.Popen[bytes]") -> None:
        """SIGTERM -> espera de gracia corta -> SIGKILL de respaldo, sobre el
        GRUPO de proceso completo (start_new_session=True hace pgid==pid) --
        esto es lo que resuelve la limitacion de M-AG008: un proceso del SO
        si puede matarse de verdad, a diferencia de un hilo Python."""

        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return  # ya no existe

        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            proc.wait(timeout=_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass

        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _reap(proc: "subprocess.Popen[bytes]") -> Optional[int]:
        """Espera a que el proceso sea reaped por el sistema operativo tras
        haber sido senalizado -- nunca deja un zombie."""

        try:
            return proc.wait(timeout=_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                return proc.wait(timeout=_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                return None

    def _emit_evidence(
        self,
        request_id: str,
        spawn_ts: float,
        term_ts: float,
        process_pid: Optional[int],
        prompt_sha256: str,
        response_sha256: Optional[str],
        configured_model_id: str,
        transport_outcome: str,
        latency_ms: int,
        exit_code: Optional[int],
    ) -> None:
        if self._evidence_sink is None:
            return
        evidence = TransportEvidence(
            request_id=request_id,
            spawn_timestamp=spawn_ts,
            term_timestamp=term_ts,
            process_pid=process_pid,
            prompt_sha256=prompt_sha256,
            response_sha256=response_sha256,
            configured_model_id=configured_model_id,
            transport_outcome=transport_outcome,
            latency_ms=latency_ms,
            exit_code=exit_code,
        )
        self._evidence_sink(evidence)


__all__ = (
    "IsolatedTransportError",
    "IsolatedGeminiTransport",
    "TransportEvidence",
    "Transport",
    "CATEGORY_TRANSPORT_TIMEOUT",
    "CATEGORY_TRANSPORT_CANCELLED",
    "CATEGORY_CHILD_PROCESS_CRASHED",
    "CATEGORY_SPAWN_FAILURE",
    "CATEGORY_CHILD_REPORTED_FAILURE",
)
