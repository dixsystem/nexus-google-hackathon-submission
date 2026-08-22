"""Minimal authenticated Cloud Run surface for the governed Gemini demo."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import unquote

from antigravity_gemini_provider import AntigravityGeminiConfig, AntigravityGeminiProvider
from google_agentic_demo import OFFLINE_MODEL_ID, build_transport
from mission_generator_llm_producer import MissionGeneratorCandidateProducer
from mission_proposal_staging import stage_proposal_batch
from provider_capability_registry import default_provider_capability_registry
from red_team_session import run_red_team_session


_GOAL = "Verify the governed Google agentic proposal pipeline"
_REDTEAM_GOAL = "Verify the governed Google agentic proposal pipeline (red team)"

DEFAULT_REDTEAM_ROUNDS = 5
# Límite máximo duro (M-7a) para evitar coste descontrolado -- un intento
# de pedir más rondas se rechaza con 400, nunca se recorta en silencio.
MAX_REDTEAM_ROUNDS = 15

DEFAULT_REDTEAM_MODE = "offline"
# M-7a paso 3 (sesión supervisada, modo real): límite más bajo que
# MAX_REDTEAM_ROUNDS porque cada ronda en modo "real" gasta al menos 2
# llamadas reales de cuota (ataque de red_team_attacker.py +
# gemini_assess_attack() si el intento es bloqueado) -- ver
# DEPLOYMENT_CHECKLIST.md.
MAX_REDTEAM_ROUNDS_REAL = 5

# M-7a paso 2 (sesión supervisada, persistencia real): un único bucket
# compartido, no un bucket por sesión como mission_executor.py (M-6) usa
# para ejecuciones ALLOW. Los informes de cuarentena son un log de
# auditoría de solo lectura, potencialmente de alto volumen (una sesión
# genera un incidente por ronda); multiplicar buckets por sesión aquí solo
# proliferaría infraestructura sin el beneficio de aislamiento que sí
# justifica un bucket por misión ejecutada (evento raro y deliberado). Cada
# incidente es un objeto independiente, direccionable directamente por su
# incident_id -- evita necesitar un índice separado session_id->incident_id.
DEFAULT_QUARANTINE_BUCKET_NAME = "nexus-redteam-quarantine"

_SAFE_QUARANTINE_INCIDENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")


def _quarantine_object_path(incident_id: str) -> str:
    return f"incidents/{incident_id}.json"


class QuarantineStore:
    """Persistencia de informes de cuarentena. Mismo contrato duck-typed
    que MissionExecutor (M-6, engineering-loop/mission_executor.py): nunca
    importa google.cloud.storage directamente, storage_client es inyectado
    -- pero aquí es .bucket(name) (obtiene una referencia local, sin
    crear/verificar nada en el servidor), no .create_bucket(name) como en
    M-6, porque este bucket es COMPARTIDO y reutilizado entre sesiones --
    llamar create_bucket() en cada escritura fallaría a partir de la
    segunda vez (el bucket ya existiría). El objeto devuelto por .bucket()
    debe exponer .blob(path) -> objeto con .upload_from_string(data,
    content_type=...) y .download_as_bytes().

    Si storage_client es None (el caso por defecto hoy -- construir un
    Client real de producción queda para la sesión de despliegue, paso 3),
    usa un fallback in-memory explícito -- mismo comportamiento exacto que
    el _QUARANTINE_STORE anterior, para no romper el modo offline ni los
    tests existentes."""

    def __init__(self, storage_client=None, *, bucket_name: str = DEFAULT_QUARANTINE_BUCKET_NAME):
        self._storage_client = storage_client
        self._bucket_name = bucket_name
        self._memory_fallback = {} if storage_client is None else None

    def put(self, incident_id: str, report: str) -> None:
        if _SAFE_QUARANTINE_INCIDENT_ID.fullmatch(incident_id) is None:
            return
        if self._storage_client is None:
            self._memory_fallback[incident_id] = report
            return
        document = json.dumps(
            {"incident_id": incident_id, "quarantine_report": report},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        bucket = self._storage_client.bucket(self._bucket_name)
        blob = bucket.blob(_quarantine_object_path(incident_id))
        blob.upload_from_string(document, content_type="application/json")

    def get(self, incident_id: str) -> str | None:
        if self._storage_client is None:
            return self._memory_fallback.get(incident_id)
        # Superficie de ataque real: incident_id llega aquí desde la URL de
        # GET /quarantine/<incident_id> (entrada del usuario), a diferencia
        # de put() que solo recibe incident_id ya generados internamente --
        # se valida antes de construir cualquier ruta de objeto (mismo
        # patrón que lyria_alert_sound._require_safe_incident_id).
        if _SAFE_QUARANTINE_INCIDENT_ID.fullmatch(incident_id) is None:
            return None
        bucket = self._storage_client.bucket(self._bucket_name)
        blob = bucket.blob(_quarantine_object_path(incident_id))
        try:
            raw = blob.download_as_bytes()
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:  # fail closed to "not found" -- never leak backend diagnostics
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed.get("quarantine_report")


def _build_storage_client(environ=None, *, storage_module=None):
    """Construye un storage.Client() real SOLO si ENABLE_REAL_STORAGE=true
    está presente en el entorno -- en cualquier otro caso (variable
    ausente, vacía, o con cualquier otro valor) devuelve None sin importar
    nada, exactamente el comportamiento de hoy (QuarantineStore cae al
    fallback in-memory). Esto preserva la disciplina "nunca importa
    google.cloud.storage directamente" salvo que se pida explícitamente:
    el import de `google.cloud.storage` ocurre perezosamente, dentro de
    esta función, solo en la rama donde el flag está activo -- así el
    módulo entero (y sus tests) siguen funcionando sin el paquete
    google-cloud-storage instalado mientras el flag no se active.

    `storage_module` es un punto de inyección para tests (mismo patrón de
    seam explícito que storage_client en MissionExecutor/QuarantineStore,
    en vez de parchear sys.modules) -- si se pasa, se usa tal cual en
    lugar de importar el SDK real; nunca se usa por defecto en
    producción."""

    environment = os.environ if environ is None else environ
    if str(environment.get("ENABLE_REAL_STORAGE", "")).strip().lower() != "true":
        return None
    module = storage_module
    if module is None:
        from google.cloud import storage as module  # import perezoso -- ver docstring
    return module.Client()


_QUARANTINE_STORE = QuarantineStore(_build_storage_client())


class CloudDemoConfigurationError(Exception):
    # Todo raise de esta clase en el módulo es, sin excepción, un fallo de
    # validación/configuración (rounds fuera de rango, body malformado,
    # modo no soportado, GEMINI_MODEL/GEMINI_API_KEY ausentes en modo
    # real) -- nunca un fallo del pipeline en sí. category expuesto como
    # atributo de clase para que _handle_redteam/_handle_demo (que hacen
    # getattr(exc, "category", ...)) reporten "CONFIGURATION" sin tener
    # que repetir el valor en cada punto de raise.
    category = "CONFIGURATION"


class _RecordingProvider:
    def __init__(self, provider):
        self._provider = provider
        self.response = None

    def evaluate(self, prompt, *, format=None, cancel_event=None):
        self.response = self._provider.evaluate(
            prompt, format=format, cancel_event=cancel_event
        )
        return self.response


def run_cloud_demo(*, mode: str, environ=None):
    environment = os.environ if environ is None else environ
    if mode == "real":
        model_id = environment.get("GEMINI_MODEL")
        if not isinstance(model_id, str) or not model_id:
            raise CloudDemoConfigurationError("real mode requires GEMINI_MODEL")
    elif mode == "offline":
        model_id = OFFLINE_MODEL_ID
    else:
        raise CloudDemoConfigurationError("unsupported demo mode")

    transport, selected_model = build_transport(
        mode, model_id=model_id, environ=environment
    )
    provider = _RecordingProvider(
        AntigravityGeminiProvider(
            AntigravityGeminiConfig(
                model_id=selected_model,
                timeout_seconds=30.0,
                max_input_chars=100_000,
                max_response_chars=100_000,
            ),
            transport=transport,
        )
    )
    registry = default_provider_capability_registry()
    producer = MissionGeneratorCandidateProducer(provider, registry=registry)
    candidates = producer.produce_batch(
        goal=_GOAL, available_mission_ids=("M-901", "M-902", "M-903", "M-904", "M-905")
    )
    destination = Path(tempfile.mkdtemp(prefix="google-agentic-cloud-"))
    batch = stage_proposal_batch(
        candidates,
        registry=registry,
        proposals_path=destination / "MISSION_PROPOSALS.md",
        contracts_path=destination / "MISSION_PROPOSAL_CONTRACTS.json",
        candidates_path=destination / "MISSION_PROPOSAL_CANDIDATES.json",
    )
    response = provider.response
    artifacts = {}
    for path in sorted(destination.iterdir()):
        artifacts[path.name] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "status": "STAGED",
        "candidate_count": len(batch.candidates),
        "requested_model": selected_model,
        "reported_model": response.response_model_id,
        "model_identity_status": response.model_identity_status,
        "authority_effects": "NONE",
        "artifacts": artifacts,
    }


def run_cloud_redteam_session(
    *, rounds: int = DEFAULT_REDTEAM_ROUNDS, mode: str = DEFAULT_REDTEAM_MODE, environ=None
) -> dict:
    """Ejecuta una sesión red-team completa (M-9: red_team_session.py).

    mode="offline" (default): backend determinista local, ninguna llamada
    de red real ocurre -- ver NIGHT_QUESTIONS.md, entrada M-7a original.

    mode="real" (M-7a paso 3, sesión supervisada): el ATACANTE
    (red_team_attacker.py) usa build_transport("real", ...) igual patrón
    que run_cloud_demo(mode="real") -- requiere GEMINI_MODEL y
    GEMINI_API_KEY en el entorno, falla cerrado (CloudDemoConfigurationError,
    category=CONFIGURATION) si falta cualquiera de los dos, antes de tocar
    build_transport. gemini_assessor NO se pasa explícitamente aquí -- por
    diseño de run_red_team_session(), cuando gemini_assessor es None
    reutiliza automáticamente el mismo `transport` que el atacante (ver
    docstring de run_red_team_session, gemini_assessor_transport), así que
    el modo real ya cablea el transport correcto para ambos sin construir
    un segundo transport. use_gemma_fallback sigue en True incluso en modo
    real -- Gemma se queda en clasificación por reglas (sin llamada real)
    para no requerir un tercer transport aislado; el límite de
    MAX_REDTEAM_ROUNDS_REAL asume exactamente 2 llamadas reales por ronda
    bloqueada (ataque + gemini_assessor), no 3."""

    if mode not in ("offline", "real"):
        raise CloudDemoConfigurationError("mode must be offline or real")

    max_rounds = MAX_REDTEAM_ROUNDS_REAL if mode == "real" else MAX_REDTEAM_ROUNDS
    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1 or rounds > max_rounds:
        raise CloudDemoConfigurationError(f"rounds must be an integer between 1 and {max_rounds}")

    environment = os.environ if environ is None else environ

    if mode == "real":
        model_id = environment.get("GEMINI_MODEL")
        if not isinstance(model_id, str) or not model_id:
            raise CloudDemoConfigurationError("real mode requires GEMINI_MODEL")
        api_key = environment.get("GEMINI_API_KEY")
        if not isinstance(api_key, str) or not api_key:
            raise CloudDemoConfigurationError("real mode requires GEMINI_API_KEY")
    else:
        model_id = None

    transport, selected_model = build_transport(mode, model_id=model_id, environ=environment)
    registry = default_provider_capability_registry()
    result = run_red_team_session(
        _REDTEAM_GOAL, registry, transport, rounds=rounds,
        model_id=selected_model, use_gemma_fallback=True,
    )

    # Solo se persiste cuando corresponde (consensus=ESCALATE) -- un
    # incidente archivado, sin consenso, o VALIDATION_BYPASS no genera
    # entrada de cuarentena (el bypass, en particular, nunca pasa por el
    # triple filtro en absoluto -- ver red_team_session.py).
    for incident_id in result.escalated_incident_ids:
        _QUARANTINE_STORE.put(incident_id, result.quarantine_report)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "COMPLETED",
        "session_id": result.session_id,
        "mode": mode,
        "rounds": rounds,
        "incident_count": len(result.incidents),
        "validation_bypass_count": len(result.validation_bypasses),
        "escalated_incident_ids": list(result.escalated_incident_ids),
        "authority_effects": "NONE",
        "quarantine_report": result.quarantine_report,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "NexusGoogleDemo/1"

    def log_message(self, fmt, *args):
        # Access metadata only. Request bodies, credentials, prompts, model
        # output, and exception details are never logged.
        super().log_message(fmt, *args)

    def _json(self, status, payload):
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "LIVE", "authority_effects": "NONE"})
            return
        if self.path.startswith("/quarantine/") and len(self.path) > len("/quarantine/"):
            self._handle_quarantine_lookup(self.path[len("/quarantine/"):])
            return
        self._json(404, {"status": "NOT_FOUND"})

    def _handle_quarantine_lookup(self, raw_incident_id):
        try:
            incident_id = unquote(raw_incident_id)
        except Exception:  # fail closed on a malformed path segment
            self._json(404, {"status": "NOT_FOUND"})
            return
        report = _QUARANTINE_STORE.get(incident_id)  # storage real si hay storage_client, in-memory si no
        if report is None:
            self._json(404, {"status": "NOT_FOUND", "incident_id": incident_id})
            return
        self._json(200, {"status": "FOUND", "incident_id": incident_id, "quarantine_report": report})

    def do_POST(self):
        if self.path == "/demo":
            self._handle_demo(mode="real")
            return
        if self.path == "/demo/offline":
            self._handle_demo(mode="offline")
            return
        if self.path == "/redteam":
            self._handle_redteam()
            return
        self._json(404, {"status": "NOT_FOUND"})

    def _handle_demo(self, *, mode):
        try:
            result = run_cloud_demo(mode=mode)
        except Exception as exc:  # fail closed without leaking SDK diagnostics
            category = getattr(exc, "category", "DEMO_FAILED")
            self._json(503, {"status": "FAILED", "category": category, "authority_effects": "NONE"})
            return
        self._json(200, result)

    def _read_redteam_request(self):
        length_header = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(length_header)
        except ValueError as exc:
            raise CloudDemoConfigurationError("invalid Content-Length") from exc
        body = self.rfile.read(length) if length > 0 else b""
        if not body:
            return DEFAULT_REDTEAM_ROUNDS, DEFAULT_REDTEAM_MODE
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CloudDemoConfigurationError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise CloudDemoConfigurationError("request body must be a JSON object")
        mode = payload.get("mode", DEFAULT_REDTEAM_MODE)
        if mode not in ("offline", "real"):
            raise CloudDemoConfigurationError("mode must be offline or real")
        max_rounds = MAX_REDTEAM_ROUNDS_REAL if mode == "real" else MAX_REDTEAM_ROUNDS
        rounds = payload.get("rounds", DEFAULT_REDTEAM_ROUNDS)
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1 or rounds > max_rounds:
            raise CloudDemoConfigurationError(f"rounds must be an integer between 1 and {max_rounds}")
        return rounds, mode

    def _handle_redteam(self):
        try:
            rounds, mode = self._read_redteam_request()
        except CloudDemoConfigurationError:
            # fail closed without leaking SDK diagnostics -- same discipline as _handle_demo
            self._json(400, {"status": "FAILED", "category": "CONFIGURATION", "authority_effects": "NONE"})
            return
        try:
            result = run_cloud_redteam_session(rounds=rounds, mode=mode)
        except Exception as exc:  # fail closed without leaking SDK diagnostics
            category = getattr(exc, "category", "REDTEAM_FAILED")
            self._json(503, {"status": "FAILED", "category": category, "authority_effects": "NONE"})
            return
        self._json(200, result)


def main():
    port_text = os.environ.get("PORT", "8080")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise CloudDemoConfigurationError("PORT must be an integer") from exc
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()

