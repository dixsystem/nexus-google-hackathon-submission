"""Minimal authenticated Cloud Run surface for the governed Gemini demo."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile

from antigravity_gemini_provider import AntigravityGeminiConfig, AntigravityGeminiProvider
from google_agentic_demo import OFFLINE_MODEL_ID, build_transport
from mission_generator_llm_producer import MissionGeneratorCandidateProducer
from mission_proposal_staging import stage_proposal_batch
from provider_capability_registry import default_provider_capability_registry


_GOAL = "Verify the governed Google agentic proposal pipeline"


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
        goal=_GOAL, available_mission_ids=("M-901",)
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
        if self.path != "/health":
            self._json(404, {"status": "NOT_FOUND"})
            return
        self._json(200, {"status": "LIVE", "authority_effects": "NONE"})

    def do_POST(self):
        if self.path == "/demo":
            mode = "real"
        elif self.path == "/demo/offline":
            mode = "offline"
        else:
            self._json(404, {"status": "NOT_FOUND"})
            return
        try:
            result = run_cloud_demo(mode=mode)
        except Exception as exc:  # fail closed without leaking SDK diagnostics
            category = getattr(exc, "category", "DEMO_FAILED")
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

