"""Minimal local end-to-end demo runner for Google All Things Agentic.

The public CLI defaults to a deterministic offline backend.  The real Gemini
backend is reachable only through ``--mode real`` and requires an explicit
model plus ``GEMINI_API_KEY``.  Both modes use the existing one-process-per-
request isolated transport and stop after proposal validation and staging.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Callable, Mapping, Optional

import antigravity_isolated_child as child
from antigravity_gemini_provider import AntigravityGeminiConfig, AntigravityGeminiProvider
from antigravity_isolated_transport import IsolatedGeminiTransport
from antigravity_parent_supervision import supervise_parent_death
from mission_generator_llm_producer import MissionGeneratorCandidateProducer
from mission_proposal_staging import stage_proposal_batch
from provider_capability_registry import default_provider_capability_registry


REAL_CREDENTIAL_ENV = "GEMINI_API_KEY"
OFFLINE_MODEL_ID = "offline-google-agentic-demo"
REAL_CHILD_PYTHON = Path(__file__).resolve().parent / ".antigravity_isolated_venv" / "bin" / "python"
DEMO_MAX_CANDIDATES = 1


class DemoConfigurationError(Exception):
    """The requested demo mode is not safely runnable."""


def _offline_candidate_json() -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "candidates": [
                {
                    "mission_name": "Google agentic provider health check",
                    "objective": "Verify the isolated Google provider proposal path",
                    "capability_id": "external.providers.health.v1",
                    "parameters": [],
                    "depends_on_batch_index": [],
                    "acceptance_criteria": ["provider health proposal reaches staging"],
                    "rationale": "Deterministic offline end-to-end demo fixture",
                }
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _adapt_backend(real_backend) -> child.Backend:
    """Translate the SDK backend's structural contract into the child contract."""

    def _backend(request: child.ChildRequest) -> child.BackendResult:
        try:
            result = real_backend(request)
        except Exception as exc:  # only categorized backend errors cross the boundary
            category = getattr(exc, "category", None)
            message = getattr(exc, "message", None)
            if category in child._KNOWN_ERROR_CATEGORIES and isinstance(message, str):
                raise child.BackendError(category, message) from None
            raise child.BackendError("SDK_EXCEPTION", "real backend failed without a recognized category") from None
        return child.BackendResult(
            text=result.text,
            response_model_id=result.response_model_id,
            response_id=result.response_id,
            prompt_token_count=result.prompt_token_count,
            candidates_token_count=result.candidates_token_count,
            total_token_count=result.total_token_count,
        )

    return _backend


def select_child_backend(
    mode: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
    real_backend_factory: Optional[Callable[..., object]] = None,
) -> child.Backend:
    """Select a backend inside the isolated child; never discovers a live mode."""

    if mode == "offline":
        return child.make_offline_mock_backend(
            response_model_id=OFFLINE_MODEL_ID,
            text=_offline_candidate_json(),
        )
    if mode != "real":
        raise DemoConfigurationError("demo child mode must be offline or real")

    environment = os.environ if environ is None else environ
    api_key = environment.get(REAL_CREDENTIAL_ENV)
    if not isinstance(api_key, str) or not api_key:
        raise DemoConfigurationError(f"real mode requires {REAL_CREDENTIAL_ENV}")
    if real_backend_factory is None:
        from antigravity_google_genai_backend import GoogleGenAIBackend

        real_backend_factory = GoogleGenAIBackend
    return _adapt_backend(real_backend_factory(api_key=api_key))


def build_transport(
    mode: str,
    *,
    model_id: Optional[str],
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[IsolatedGeminiTransport, str]:
    environment = os.environ if environ is None else environ
    if mode == "offline":
        selected_model = model_id or OFFLINE_MODEL_ID
        credential_env = None
    elif mode == "real":
        if not isinstance(model_id, str) or not model_id:
            raise DemoConfigurationError("real mode requires an explicit --model")
        api_key = environment.get(REAL_CREDENTIAL_ENV)
        if not isinstance(api_key, str) or not api_key:
            raise DemoConfigurationError(f"real mode requires {REAL_CREDENTIAL_ENV}")
        if not REAL_CHILD_PYTHON.is_file():
            raise DemoConfigurationError("real mode requires the isolated Google SDK interpreter")
        selected_model = model_id
        credential_env = {REAL_CREDENTIAL_ENV: api_key}
    else:
        raise DemoConfigurationError("mode must be offline or real")

    child_python = str(REAL_CHILD_PYTHON) if mode == "real" else sys.executable
    argv = [child_python, str(Path(__file__).resolve()), "--demo-child", mode]
    return IsolatedGeminiTransport(argv, credential_env=credential_env), selected_model


def run_demo(
    *,
    mode: str = "offline",
    model_id: Optional[str] = None,
    goal: str = "Verify the governed Google agentic proposal pipeline",
    output_dir: Optional[Path] = None,
    environ: Optional[Mapping[str, str]] = None,
):
    """Run proposal -> validation -> staging. Never approves or promotes."""

    transport, selected_model = build_transport(
        mode, model_id=model_id, environ=environ
    )
    provider = AntigravityGeminiProvider(
        AntigravityGeminiConfig(
            model_id=selected_model,
            timeout_seconds=30.0,
            max_input_chars=100_000,
            max_response_chars=100_000,
        ),
        transport=transport,
    )
    registry = default_provider_capability_registry()
    producer = MissionGeneratorCandidateProducer(
        provider,
        registry=registry,
        max_candidates=DEMO_MAX_CANDIDATES,
    )
    candidates = producer.produce_batch(
        goal=goal,
        available_mission_ids=("M-901",),
    )

    destination = Path(output_dir) if output_dir is not None else Path(
        tempfile.mkdtemp(prefix="google-agentic-demo-")
    )
    destination.mkdir(parents=True, exist_ok=True)
    batch = stage_proposal_batch(
        candidates,
        registry=registry,
        proposals_path=destination / "MISSION_PROPOSALS.md",
        contracts_path=destination / "MISSION_PROPOSAL_CONTRACTS.json",
        candidates_path=destination / "MISSION_PROPOSAL_CANDIDATES.json",
    )
    return batch


def _run_child_mode(mode: str, argv: list[str]) -> int:
    expected_parent_pid = child._extract_expected_parent_pid(argv)
    supervise_parent_death(expected_parent_pid)
    try:
        backend = select_child_backend(mode)
    except DemoConfigurationError as exc:
        # No request has been read and no request_id exists.  Exit without
        # protocol output; the parent converts this to a fail-closed transport error.
        sys.stderr.write(str(exc) + "\n")
        return 1
    return child.run_child(backend)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local governed Google agentic demo")
    parser.add_argument("--mode", choices=("offline", "real"), default="offline")
    parser.add_argument(
        "--model",
        default="gemini-3.5-flash",
        help="model ID (default: gemini-3.5-flash)",
    )
    parser.add_argument("--goal", default="Verify the governed Google agentic proposal pipeline")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--demo-child", choices=("offline", "real"), help=argparse.SUPPRESS)
    parser.add_argument("--expected-parent-pid", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    raw_argv = sys.argv[1:] if argv is None else argv
    if args.demo_child is not None:
        return _run_child_mode(args.demo_child, raw_argv)
    try:
        batch = run_demo(
            mode=args.mode,
            model_id=args.model,
            goal=args.goal,
            output_dir=args.output_dir,
        )
    except DemoConfigurationError as exc:
        sys.stderr.write(f"demo configuration error: {exc}\n")
        return 2
    print(f"mode={args.mode}")
    print(f"status=STAGED")
    print(f"candidates={len(batch.candidates)}")
    print(f"output_dir={batch.proposals_path.parent}")
    print("authority_effects=NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
