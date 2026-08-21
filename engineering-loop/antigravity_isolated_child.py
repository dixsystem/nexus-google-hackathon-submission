"""Entrypoint real del hijo aislado (M-AG010 Fase 2). Este es el futuro
proceso de produccion que en una mision posterior, con autorizacion humana
explicita, importaria google.genai y viviria en un venv aislado separado
del entorno de Keeper (Seccion C del diseno M-AG009). En ESTA mision
permanece deliberadamente OFFLINE: no importa ni llama a google.genai en
ningun punto de este archivo, verificable por ausencia de ese import
(test estatico dedicado).

Contrato de proceso (Seccion G del diseno, sin inventar un schema nuevo --
Fase 9 del brief M-AG010): el hijo lee EXACTAMENTE una linea JSON de
stdin, la valida contra el mismo schema_version que
antigravity_isolated_transport_schema.py ya usa del lado del padre,
invoca un backend inyectable para realizar UNA operacion de generacion,
escribe EXACTAMENTE una linea JSON de respuesta a stdout, y termina. Sin
daemon de larga duracion, sin reuso, sin estado persistente entre
invocaciones -- cada ejecucion de este script es un proceso nuevo del
sistema operativo que el padre (IsolatedGeminiTransport) crea, usa una
vez, y mata/espera.

stdout = solo datos de protocolo (Seccion M). stderr = solo diagnostico,
nunca contenido de propuesta -- el padre (antigravity_isolated_transport.py)
ya descarta stderr deliberadamente sin incluirlo en excepciones/evidencia;
este modulo escribe ahi solo para depuracion humana directa del proceso,
nunca datos que el padre vaya a interpretar.

Identidad de modelo (invariante N.4 del diseno): este modulo NUNCA decide
VERIFIED_MATCH/UNVERIFIED/MODEL_ID_MISMATCH -- esa comparacion sigue
siendo responsabilidad exclusiva de AntigravityGeminiProvider.evaluate()
(M-AG008), del lado del padre. El backend offline de este modulo puede
devolver un response_model_id de prueba SOLO cuando se lo indican
explicitamente (nunca lo fabrica por su cuenta a partir del model_id de
la peticion, salvo que el caller de pruebas lo pida explicitamente para
simular el camino feliz)."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

from antigravity_isolated_transport_schema import SCHEMA_VERSION
from antigravity_parent_supervision import supervise_parent_death

_MAX_REQUEST_BYTES = 1_048_576  # 1 MiB -- mismo limite duro que build_request_envelope del lado del padre.

# Mismas categorias conocidas que antigravity_isolated_transport_schema.py
# ya acepta en un envelope ok:false (_KNOWN_CHILD_ERROR_CATEGORIES) --
# reusadas aqui tal cual, sin inventar una lista paralela. Ampliado en
# M-AG017 (Fase 10 del brief) con las categorias que
# antigravity_google_genai_backend.BackendError puede reportar --
# puramente aditivo, ningun test existente de este modulo (M-AG010)
# construye ni depende de estas categorias nuevas.
_KNOWN_ERROR_CATEGORIES = frozenset(
    {
        "SDK_EXCEPTION",
        "SDK_EMPTY_RESPONSE",
        "MALFORMED_REQUEST",
        "AUTH_MISSING",
        "AUTH_INVALID",
        "MODEL_NOT_FOUND",
        "RATE_LIMIT",
        "NETWORK_FAILURE",
        "SDK_ERROR",
        "MALFORMED_RESPONSE",
    }
)

_REQUEST_ALLOWED_FIELDS = frozenset(
    {"schema_version", "request_id", "model_id", "prompt", "format", "timeout_seconds", "max_response_chars"}
)


class ChildProtocolError(Exception):
    """Peticion recibida invalida -- nunca se construye una ChildRequest
    parcial a partir de esto."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


class BackendError(Exception):
    """Un backend (offline o, en el futuro, uno real de google.genai) lanza
    esto para reportar un fallo estructurado -- nunca una excepcion cruda
    sin categoria, que este modulo tendria que adivinar como clasificar."""

    def __init__(self, category: str, message: str):
        if category not in _KNOWN_ERROR_CATEGORIES:
            raise ValueError(f"unknown backend error category: {category!r}")
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True)
class ChildRequest:
    request_id: str
    model_id: str
    prompt: str
    format: Optional[dict]
    timeout_seconds: float
    max_response_chars: int


@dataclass(frozen=True)
class BackendResult:
    """Lo que un backend debe devolver en el camino de exito. Los mismos
    campos que RawGeminiResult espera del lado del padre (sin importar esa
    clase aqui, para que este modulo pueda ejecutarse en un venv aislado
    que no tenga por que tener el resto de engineering-loop/ en su
    sys.path en el futuro)."""

    text: str
    response_model_id: Optional[str]
    response_id: Optional[str]
    prompt_token_count: Optional[int]
    candidates_token_count: Optional[int]
    total_token_count: Optional[int]


# backend(ChildRequest) -> BackendResult, o lanza BackendError.
Backend = Callable[[ChildRequest], BackendResult]


def parse_request_line(raw_line: bytes) -> ChildRequest:
    """Fail-closed: cualquier desviacion del schema exacto -> ChildProtocolError.
    Nunca recupera silenciosamente un campo faltante ni adivina un valor."""

    if not isinstance(raw_line, (bytes, bytearray)):
        raise ChildProtocolError("MALFORMED_REQUEST", "request must be raw bytes")
    if len(raw_line) > _MAX_REQUEST_BYTES:
        raise ChildProtocolError("MALFORMED_REQUEST", "request exceeds transport size limit")
    if not raw_line.strip():
        raise ChildProtocolError("MALFORMED_REQUEST", "empty request")

    try:
        text = raw_line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ChildProtocolError("MALFORMED_REQUEST", "request is not valid UTF-8") from exc

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChildProtocolError("MALFORMED_REQUEST", "request is not valid JSON") from exc

    if not isinstance(obj, dict):
        raise ChildProtocolError("MALFORMED_REQUEST", "request must be a JSON object")

    unknown = set(obj) - _REQUEST_ALLOWED_FIELDS
    if unknown:
        raise ChildProtocolError("MALFORMED_REQUEST", f"unknown fields in request: {sorted(unknown)}")

    if obj.get("schema_version") != SCHEMA_VERSION:
        raise ChildProtocolError("MALFORMED_REQUEST", "unknown or missing schema_version")

    request_id = obj.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise ChildProtocolError("MALFORMED_REQUEST", "request_id must be a non-empty string")

    model_id = obj.get("model_id")
    if not isinstance(model_id, str) or not model_id:
        raise ChildProtocolError("MALFORMED_REQUEST", "model_id must be a non-empty string")

    prompt = obj.get("prompt")
    if not isinstance(prompt, str):
        raise ChildProtocolError("MALFORMED_REQUEST", "prompt must be a string")

    fmt = obj.get("format")
    if fmt is not None and not isinstance(fmt, dict):
        raise ChildProtocolError("MALFORMED_REQUEST", "format must be an object or null")

    timeout_seconds = obj.get("timeout_seconds")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise ChildProtocolError("MALFORMED_REQUEST", "timeout_seconds must be a number")

    max_response_chars = obj.get("max_response_chars")
    if isinstance(max_response_chars, bool) or not isinstance(max_response_chars, int):
        raise ChildProtocolError("MALFORMED_REQUEST", "max_response_chars must be an integer")

    return ChildRequest(
        request_id=request_id,
        model_id=model_id,
        prompt=prompt,
        format=fmt,
        timeout_seconds=float(timeout_seconds),
        max_response_chars=max_response_chars,
    )


def _serialize_success(request_id: str, result: BackendResult) -> str:
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "ok": True,
        "text": result.text,
        "response_model_id": result.response_model_id,
        "response_id": result.response_id,
        "prompt_token_count": result.prompt_token_count,
        "candidates_token_count": result.candidates_token_count,
        "total_token_count": result.total_token_count,
    }
    return json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))


def _serialize_failure(request_id: str, category: str, message: str) -> str:
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "ok": False,
        "error_category": category,
        # Truncado al mismo limite que antigravity_isolated_transport_schema.py
        # exige del lado del padre (4096 caracteres) -- nunca se filtra
        # diagnostico interno sin acotar (Seccion O del diseno).
        "error_message": message[:4096],
    }
    return json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))


def run_child(backend: Backend, *, stdin=None, stdout=None) -> int:
    """Punto de entrada real, inyectable para tests offline. Lee EXACTAMENTE
    una linea de stdin, procesa a traves de `backend`, escribe EXACTAMENTE
    una linea a stdout, y devuelve el exit code con el que el proceso debe
    terminar. Nunca escribe mas de una linea de protocolo -- este flujo
    solo puede alcanzar un unico punto de escritura por ejecucion."""

    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout

    raw_line = stdin.readline()

    try:
        request = parse_request_line(raw_line)
    except ChildProtocolError:
        # Sin un request_id valido no hay a quien dirigir una respuesta de
        # protocolo -- salir sin escribir nada es la unica opcion
        # fail-closed correcta. El padre ya trata "stdout cerrado sin linea
        # completa" como CHILD_PROCESS_CRASHED, categoria ya cubierta y
        # probada del lado de antigravity_isolated_transport.py.
        return 1

    try:
        result = backend(request)
    except BackendError as exc:
        stdout.write(_serialize_failure(request.request_id, exc.category, exc.message) + "\n")
        stdout.flush()
        return 0
    except Exception as exc:  # noqa: BLE001 -- cualquier excepcion no prevista del backend se reporta como fallo estructurado, nunca crashea silenciosamente ni deja el proceso colgado
        stdout.write(_serialize_failure(request.request_id, "SDK_EXCEPTION", str(exc)) + "\n")
        stdout.flush()
        return 0

    stdout.write(_serialize_success(request.request_id, result) + "\n")
    stdout.flush()
    return 0


# ---------------------------------------------------------------------
# Backend offline/mock -- el UNICO backend que esta mision autoriza.
# NUNCA importa google.genai. El backend real que importaria
# google.genai.Client queda diferido a una mision futura con
# autorizacion humana explicita y credenciales reales aprovisionadas
# fuera de este repo (mismo patron ya usado por
# antigravity_gemini_provider.py: "no default live transport shipped").
# ---------------------------------------------------------------------


def make_offline_mock_backend(
    *,
    response_model_id: Optional[str] = None,
    text: str = "offline mock response",
    hang_seconds: Optional[float] = None,
) -> Backend:
    """Backend deterministico para pruebas y para el uso directo de este
    entrypoint mientras no exista autorizacion para un backend real.
    response_model_id se pasa explicitamente por el caller -- este backend
    NUNCA lo deriva de request.model_id por su cuenta, para no fabricar
    una identidad de modelo verificada que no ocurrio de verdad (ese
    seria exactamente el tipo de atajo que M-AG007/M-AG008 ya prohibieron
    del lado del provider; este backend offline lo respeta tambien).

    hang_seconds (M-AG014, solo para pruebas de la Fase 6/7 del brief):
    si se indica, el backend duerme ese tiempo DESPUES de haber leido la
    peticion, antes de responder -- deja al proceso hijo genuinamente
    ocupado, ya no bloqueado en stdin.readline(). Esto es indispensable
    para reproducir de forma fiel la ventana de carrera de muerte del
    padre: un hijo bloqueado en stdin muere por EOF del pipe cerrado en
    cuanto el padre desaparece, sin que la supervision de muerte del
    padre entre en juego -- exactamente el confundidor que la auditoria
    independiente M-AG013 identifico y corrigio en su propia sonda."""

    def _backend(request: ChildRequest) -> BackendResult:
        if hang_seconds is not None:
            time.sleep(hang_seconds)
        return BackendResult(
            text=text,
            response_model_id=response_model_id,
            response_id="offline-mock-response-id",
            prompt_token_count=0,
            candidates_token_count=0,
            total_token_count=0,
        )

    return _backend


def _no_backend_configured(request: ChildRequest) -> BackendResult:  # noqa: ARG001
    """Backend por defecto del entrypoint de produccion (M-AG012,
    Objetivo C). Cierra el hallazgo #2 de la auditoria independiente
    M-AG011: el bloque __main__ anterior hacia eco de request.model_id
    como response_model_id, fabricando un VERIFIED_MATCH trivial para
    cualquier string sin que ninguna verificacion real hubiera
    ocurrido. Este backend SIEMPRE falla cerrado -- nunca construye un
    BackendResult, nunca emite response_model_id, nunca puede producir
    una identidad de modelo verificada. Un caller de produccion real
    debe inyectar explicitamente un backend real (o, en pruebas, pasar
    --test-backend) en vez de depender de este default."""

    raise BackendError(
        "SDK_EXCEPTION",
        "no backend configured: the default isolated-child entrypoint "
        "requires an explicit backend to be injected by the caller; "
        "offline/test runs must pass --test-backend explicitly",
    )


def _extract_expected_parent_pid(argv: list) -> int:
    """M-AG014: extrae --expected-parent-pid <PID> de argv (la lista SIN
    argv[0]). Fail-closed ante ausencia/formato invalido/valor invalido --
    termina el proceso de inmediato via os._exit(1), sin escribir nada a
    stdout (todavia no existe ningun request_id al que responder con un
    envelope de protocolo). Deliberadamente NO lanza una excepcion Python
    normal: en este punto no hay ningun handler de protocolo instalado
    todavia, asi que una salida dura es la unica opcion correcta.

    El valor lo calcula el PADRE (IsolatedGeminiTransport.run(), via
    os.getpid() ANTES de spawnear) y se lo pasa a este proceso como
    informacion de arranque inmutable -- nunca lo descubre el propio
    hijo de forma perezosa despues de arrancar. Esto es lo que cierra la
    ventana de carrera que la auditoria independiente M-AG013 reprodujo
    25/25 veces contra el mecanismo anterior (que capturaba su "linea
    base" perezosamente, pudiendo capturarla ya contaminada por un
    reparenting que ya habia ocurrido)."""

    try:
        idx = argv.index("--expected-parent-pid")
    except ValueError:
        sys.stderr.write("fail closed: --expected-parent-pid ausente\n")
        os._exit(1)
    if idx + 1 >= len(argv):
        sys.stderr.write("fail closed: --expected-parent-pid sin valor\n")
        os._exit(1)
    raw = argv[idx + 1]
    try:
        pid = int(raw)
    except (TypeError, ValueError):
        sys.stderr.write("fail closed: --expected-parent-pid malformado\n")
        os._exit(1)
    if pid <= 0:
        sys.stderr.write("fail closed: --expected-parent-pid invalido\n")
        os._exit(1)
    return pid


def _extract_optional_float_flag(argv: list, flag: str) -> Optional[float]:
    """Solo para pruebas (M-AG014): --test-backend-hang-seconds <N>. Ausente
    o malformado -> None (nunca hace fail-closed al proceso completo por
    esto, a diferencia de --expected-parent-pid, porque esta bandera nunca
    por si sola activa un backend -- solo modula uno ya gateado)."""

    try:
        idx = argv.index(flag)
    except ValueError:
        return None
    if idx + 1 >= len(argv):
        return None
    try:
        return float(argv[idx + 1])
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    _argv = sys.argv[1:]

    # PRIMERA accion posible del proceso, antes de leer stdin, antes de
    # invocar cualquier backend, antes de cualquier inicializacion costosa
    # -- cierra el hallazgo #1 de M-AG011/M-AG013 (procesos huerfanos que
    # sobreviven a la muerte del proceso padre). Invariante futuro (Fase 0
    # del diseno M-AG014): NO VERIFIED LIVE PARENT = NO GEMINI CALL.
    _expected_parent_pid = _extract_expected_parent_pid(_argv)
    supervise_parent_death(_expected_parent_pid)

    # Objetivo C (M-AG012): el backend offline/mock SOLO se activa con un
    # flag explicito e inequivoco -- nunca se auto-selecciona en silencio.
    # Sin el flag, el entrypoint falla cerrado por defecto (arriba).
    # response_model_id queda deliberadamente en None (el valor por
    # defecto de make_offline_mock_backend): incluso en modo de prueba
    # explicito, este backend nunca fabrica una identidad de modelo a
    # partir de request.model_id.
    if "--test-backend" in _argv:
        _backend = make_offline_mock_backend(
            hang_seconds=_extract_optional_float_flag(_argv, "--test-backend-hang-seconds")
        )
    else:
        _backend = _no_backend_configured

    sys.exit(run_child(_backend))


__all__ = (
    "ChildProtocolError",
    "BackendError",
    "ChildRequest",
    "BackendResult",
    "Backend",
    "parse_request_line",
    "run_child",
    "make_offline_mock_backend",
    "_extract_expected_parent_pid",
    "_extract_optional_float_flag",
)
