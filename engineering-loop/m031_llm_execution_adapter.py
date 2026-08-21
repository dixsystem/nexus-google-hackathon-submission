"""M-031.3 -- real, directly-callable LLM dispatch adapter for the two
Ollama tiers registered in provider_capability_registry.py.

Scope note: this delivers the capability/provider registries (see
provider_capability_registry.py, governed_provider_manager.py) plus a
working adapter proven end-to-end against real Ollama inference (see
evidence/M031_3_LLM_DISPATCH_REPORT_V1.md). Wiring this into the
Scheduler/authorize-next CLI dispatch path (DispatchAuthorizationIssuer
one-shot consumption, broker routing equivalent to
governed_external_execution_adapter.py's ALLOWLISTED_OPERATIONS) is
deferred to a later increment -- that touches the live CLI/broker path
used by every existing mission and is a larger, separate change from
proving the LLM call + telemetry mechanism works with real data.

NVIDIA NIM is registered in the closed capability/provider registries
(nexus.llm.code.proposal.escalated.v1 -> nvidia-nim) but stays disabled
by its own provider default (NIMConfig.from_env with no override has
enabled=False, per nvidia_nim_provider.py) -- the pricing placeholder in
M031_TOKEN_REDUCTION_PIPELINE_DESIGN_V2.md Part 4 blocks turning it on.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib.util
from pathlib import Path


def _load(filename, name):
    try:
        return __import__(name)
    except ModuleNotFoundError:
        path = Path(__file__).with_name(filename)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


ollama = _load("ollama_qwen_provider.py", "ollama_qwen_provider")


class LLMExecutionAdapterError(Exception):
    pass


class UnknownTierError(LLMExecutionAdapterError):
    pass


# Real Ollama model tags per tier, exactly as sealed in
# M031_TOKEN_REDUCTION_PIPELINE_DESIGN_V2.md Part 2 / Part 14 decision.
# provider_id here matches the closed registrations in
# provider_capability_registry.py -- distinct per tier, not the generic
# "ollama-qwen" PROVIDER_ID constant the provider module itself exposes,
# so per-tier circuit breakers (design Part 11) can key off it.
TIER_MODELS = {
    "ollama-qwen-7b": "qwen2.5-coder:7b-instruct-q4_K_M",
    "ollama-qwen-14b": "qwen2.5-coder:14b-instruct-q4_K_M",
    # NIGHT_SENIOR tier (Night Factory) -- architecture/planning/audit/
    # complex-debug reasoning, distinct from the code-proposal 7b/14b
    # tiers above. Real Ollama tag confirmed via `ollama list`.
    "ollama-qwen-night-senior": "qwen3.8:latest",
}


@dataclass(frozen=True, slots=True)
class TierConfig:
    provider_id: str
    model_id: str
    timeout_seconds: float
    max_input_chars: int
    max_output_tokens: int
    max_response_bytes: int


def tier_config(
    provider_id: str,
    *,
    timeout_seconds: float = 30.0,
    max_input_chars: int = 100_000,
    max_output_tokens: int = 256,
    max_response_bytes: int = 2_097_152,
) -> TierConfig:
    if provider_id not in TIER_MODELS:
        raise UnknownTierError(
            f"no Ollama model configured for provider_id={provider_id!r}"
        )
    return TierConfig(
        provider_id=provider_id,
        model_id=TIER_MODELS[provider_id],
        timeout_seconds=timeout_seconds,
        max_input_chars=max_input_chars,
        max_output_tokens=max_output_tokens,
        max_response_bytes=max_response_bytes,
    )


def build_provider(
    tier: TierConfig,
    *,
    transport=None,
) -> "ollama.OllamaQwenProvider":
    """Real, enabled provider for a tier. transport is injectable for
    tests; omitted, it defaults to the real urllib transport (a live
    network call to the local Ollama daemon)."""
    config = ollama.OllamaQwenConfig(
        enabled=True,
        endpoint=ollama.DEFAULT_ENDPOINT,
        model_id=tier.model_id,
        timeout_seconds=tier.timeout_seconds,
        max_input_chars=tier.max_input_chars,
        max_output_tokens=tier.max_output_tokens,
        max_response_bytes=tier.max_response_bytes,
    ).validated()
    kwargs = {}
    if transport is not None:
        kwargs["transport"] = transport
    return ollama.OllamaQwenProvider(config, **kwargs)


def usage_payload_from_response(
    response: "ollama.OllamaQwenResponse",
    *,
    dispatch_id: str,
    provider_id: str,
    pipeline_stage: str = "ROUTED_DIRECT",
    cache_tier: str = "MISS",
) -> dict:
    """Build a RESOURCE_USAGE_RECORDED-ready payload from a real (or
    faithfully faked, in tests) OllamaQwenResponse. Never invents token
    counts: usage_available reflects exactly what the provider reported,
    per journal_contract.py's schema (M-031.0)."""
    usage_available = response.total_tokens is not None
    return {
        "dispatch_id": dispatch_id,
        "provider_id": provider_id,
        "model_id": response.model_id,
        "pipeline_stage": pipeline_stage,
        "cache_tier": cache_tier,
        "usage_available": usage_available,
        "prompt_tokens": response.prompt_tokens or 0,
        "completion_tokens": response.completion_tokens or 0,
        "total_tokens": response.total_tokens or 0,
        "estimated_cost_usd_micros": 0,
        "cost_model": "LOCAL_COMPUTE",
        "latency_ms": response.latency_ms,
    }


def dispatch(
    provider_id: str,
    prompt: str,
    *,
    dispatch_id: str,
    transport=None,
    pipeline_stage: str = "ROUTED_DIRECT",
    cache_tier: str = "MISS",
):
    """End-to-end: resolve tier -> call the real provider -> build the
    telemetry payload from its real response. Returns (response, payload)
    so callers can inspect the raw provider output as well as the
    journal-ready record."""
    tier = tier_config(provider_id)
    provider = build_provider(tier, transport=transport)
    response = provider.evaluate(prompt)
    payload = usage_payload_from_response(
        response,
        dispatch_id=dispatch_id,
        provider_id=provider_id,
        pipeline_stage=pipeline_stage,
        cache_tier=cache_tier,
    )
    return response, payload


__all__ = (
    "LLMExecutionAdapterError", "UnknownTierError", "TIER_MODELS",
    "TierConfig", "tier_config", "build_provider",
    "usage_payload_from_response", "dispatch",
)
