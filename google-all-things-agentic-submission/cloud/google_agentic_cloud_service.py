"""Minimal authenticated Cloud Run surface for the governed Gemini demo."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
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

# TODO(sesión supervisada): almacenamiento in-memory/proceso -- se pierde en
# cada reinicio del proceso y no se comparte entre réplicas de Cloud Run. En
# producción esto debería ser un almacén persistente (p.ej. el mismo Cloud
# Storage que ya usa mission_executor.py, o Firestore) keyed por incident_id.
_QUARANTINE_STORE: dict = {}


class CloudDemoConfigurationError(Exception):
    pass


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


def run_cloud_redteam_session(*, rounds: int = DEFAULT_REDTEAM_ROUNDS, environ=None) -> dict:
    """Ejecuta una sesión red-team completa (M-9: red_team_session.py) en
    modo offline/determinista -- ver NIGHT_QUESTIONS.md, entrada M-7a, sobre
    por qué esta fase de código-únicamente-sin-desplegar no cablea un modo
    "real" contra Gemini vivo (necesitaría un segundo transport aislado
    para Gemma, además del del atacante). use_gemma_fallback=True evita esa
    necesidad por completo -- ninguna llamada de red real ocurre aquí."""

    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1 or rounds > MAX_REDTEAM_ROUNDS:
        raise CloudDemoConfigurationError(f"rounds must be an integer between 1 and {MAX_REDTEAM_ROUNDS}")

    environment = os.environ if environ is None else environ
    transport, selected_model = build_transport("offline", model_id=None, environ=environment)
    registry = default_provider_capability_registry()
    result = run_red_team_session(
        _REDTEAM_GOAL, registry, transport, rounds=rounds,
        model_id=selected_model, use_gemma_fallback=True,
    )

    for incident in result.incidents:
        _QUARANTINE_STORE[incident.incident_id] = result.quarantine_report

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "COMPLETED",
        "session_id": result.session_id,
        "rounds": rounds,
        "incident_count": len(result.incidents),
        "validation_bypass_count": len(result.validation_bypasses),
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
        report = _QUARANTINE_STORE.get(incident_id)
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

    def _read_redteam_rounds(self):
        length_header = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(length_header)
        except ValueError as exc:
            raise CloudDemoConfigurationError("invalid Content-Length") from exc
        body = self.rfile.read(length) if length > 0 else b""
        if not body:
            return DEFAULT_REDTEAM_ROUNDS
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CloudDemoConfigurationError("request body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise CloudDemoConfigurationError("request body must be a JSON object")
        rounds = payload.get("rounds", DEFAULT_REDTEAM_ROUNDS)
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1 or rounds > MAX_REDTEAM_ROUNDS:
            raise CloudDemoConfigurationError(f"rounds must be an integer between 1 and {MAX_REDTEAM_ROUNDS}")
        return rounds

    def _handle_redteam(self):
        try:
            rounds = self._read_redteam_rounds()
        except CloudDemoConfigurationError:
            # fail closed without leaking SDK diagnostics -- same discipline as _handle_demo
            self._json(400, {"status": "FAILED", "category": "CONFIGURATION", "authority_effects": "NONE"})
            return
        try:
            result = run_cloud_redteam_session(rounds=rounds)
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

