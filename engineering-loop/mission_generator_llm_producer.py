"""LLM integration for goal -> candidates decomposition (Mission Generator
Session 2, evidence/MISSION_GENERATOR_SESSION_2_IMPLEMENTATION_REPORT_V1.md).

Wires the only real, already-callable LLM adapter this repo has today
(m031_llm_execution_adapter.py / ollama_qwen_provider.py, M-031.3) to
mission_generator_candidates.py's Session 1 structural validator,
UNMODIFIED. This module's only job is turning a free-text human goal into
GeneratedMissionCandidateV1 instances that satisfy that validator -- the
exact same contract Session 1's synthetic test fixtures already proved,
now fed by a real model response instead of a hand-built dict.

Two identity fields the model is never trusted to invent, by design:
- mission_id: assigned by this module from a caller-supplied pool of
  already-verified-free ids (a real queue-numbering concern, not a text
  generation task -- same "selection, not invention" principle sign-off
  #3 already applies to capability_id, extended here to mission_id).
- generation_id: a domain-separated sha256 over goal+provider_id+model_id+
  prompt_version+candidate content, computed by this module after the
  response returns -- never LLM-supplied, same convention as every other
  domain-separated hash in this codebase (queue_to_governed_execution.py's
  _queue_goal_id, etc.).

Dependencies the LLM may express are restricted to batch-internal
zero-based indices (depends_on_batch_index) only. Session 1's
GeneratedMissionCandidateV1/validate_candidate_batch still support
depends_on against already-CLOSED real mission ids (resolvable_
dependency_ids); this module simply never asks the LLM to guess a real
historical mission_id, which is a hallucination-prone question with no
benefit -- kept out of the prompt, not out of the type.

Model tier: no router logic is built here (explicitly out of scope this
session). DEFAULT_PROVIDER_ID is "ollama-qwen-14b" -- the strongest tier
that is actually enabled and callable in this codebase today.
nexus.llm.code.proposal.escalated.v1 / nvidia-nim is registered in
provider_capability_registry.py but stays disabled by its own pricing
placeholder (m031_llm_execution_adapter.py's module docstring); there is
no live "PREMIUM_DIRECT" adapter to call even if this module wanted to --
m031_router.py's ROUTE_PREMIUM_DIRECT is a routing label with no
execution binding wired to any callable provider yet. Design doc section
2.3's D1 ("always premium") is therefore NOT implemented by this default;
it stays undecidable until a premium adapter actually exists to call."""

import hashlib
import json

import mission_generator_candidates as candidates_module
import m031_llm_execution_adapter as llm_adapter


DEFAULT_PROVIDER_ID = "ollama-qwen-14b"
MAX_GOAL_CHARS = 4000
MAX_CANDIDATES = 8
PROMPT_VERSION = 1

_GENERATION_ID_DOMAIN = b"DIXKEEPER-MISSION-GENERATOR-CANDIDATE-V1\x00"
_RESPONSE_FIELDS = frozenset({"schema_version", "candidates"})
_CANDIDATE_FIELDS = frozenset({
    "mission_name", "objective", "capability_id", "parameters",
    "depends_on_batch_index", "acceptance_criteria", "rationale",
})
_PARAMETER_FIELDS = frozenset({"key", "value"})


class MissionGeneratorLLMError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def _candidate_json_schema(capability_ids, max_candidates):
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "integer"},
            "candidates": {
                "type": "array",
                "minItems": 1,
                "maxItems": max_candidates,
                "items": {
                    "type": "object",
                    "properties": {
                        "mission_name": {"type": "string"},
                        "objective": {"type": "string"},
                        "capability_id": {"type": "string", "enum": list(capability_ids)},
                        "parameters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string"},
                                    "value": {"type": "string"},
                                },
                                "required": ["key", "value"],
                                "additionalProperties": False,
                            },
                        },
                        "depends_on_batch_index": {
                            "type": "array",
                            "items": {"type": "integer"},
                        },
                        "acceptance_criteria": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                        "rationale": {"type": "string"},
                    },
                    "required": list(_CANDIDATE_FIELDS),
                    "additionalProperties": False,
                },
            },
        },
        "required": ["schema_version", "candidates"],
        "additionalProperties": False,
    }


def _build_prompt(goal, registry, max_candidates):
    capability_menu = [
        {"capability_id": item.capability.capability_id, "operation": item.capability.operation}
        for item in registry.registrations
    ]
    payload = {"goal": goal, "closed_capability_menu": capability_menu}
    return (
        "You are a governed mission-decomposition producer.\n"
        "Return exactly one JSON object and nothing else.\n"
        "Do not use markdown fences.\n"
        "Do not claim to execute, authorize, or dispatch anything.\n"
        "You have no authority beyond proposing mission candidates.\n"
        "Every candidate's capability_id MUST be exactly one value from "
        "closed_capability_menu -- never invent a new capability. If the "
        "goal needs a capability outside this menu, omit that part of the "
        "goal rather than inventing a capability_id.\n"
        f"Propose at most {max_candidates} candidates.\n"
        "depends_on_batch_index lists zero-based indices into this same "
        "candidates array (empty if none); a candidate must never depend "
        "on its own index.\n"
        'Required schema: {"schema_version":1,"candidates":[{"mission_name":'
        '"...","objective":"...","capability_id":"...","parameters":'
        '[{"key":"...","value":"..."}],"depends_on_batch_index":[0],'
        '"acceptance_criteria":["..."],"rationale":"..."}]}\n'
        "GOAL:\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _parse_exact_json_object(content):
    decoder = json.JSONDecoder()
    try:
        parsed, end = decoder.raw_decode(content)
    except json.JSONDecodeError as exc:
        raise MissionGeneratorLLMError("PROTOCOL", "provider returned invalid JSON") from exc
    if end != len(content):
        raise MissionGeneratorLLMError("PROTOCOL", "provider returned data outside the JSON object")
    if not isinstance(parsed, dict):
        raise MissionGeneratorLLMError("PROTOCOL", "response must be a JSON object")
    return parsed


def _generation_id(*, goal, provider_id, model_id, mission_name, objective,
                    capability_id, parameters, acceptance_criteria, rationale):
    values = {
        "goal": goal, "provider_id": provider_id, "model_id": model_id,
        "prompt_version": PROMPT_VERSION, "mission_name": mission_name,
        "objective": objective, "capability_id": capability_id,
        "parameters": [list(item) for item in parameters],
        "acceptance_criteria": list(acceptance_criteria), "rationale": rationale,
    }
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(_GENERATION_ID_DOMAIN + canonical).hexdigest()


def _build_parameters(raw_parameters):
    if not isinstance(raw_parameters, list):
        raise MissionGeneratorLLMError("PROTOCOL", "parameters must be a list")
    parameters = []
    for item in raw_parameters:
        if not isinstance(item, dict) or set(item) != _PARAMETER_FIELDS:
            raise MissionGeneratorLLMError("PROTOCOL", "each parameter must have key/value")
        key, value = item["key"], item["value"]
        if not isinstance(key, str) or not isinstance(value, str):
            raise MissionGeneratorLLMError("PROTOCOL", "parameter key/value must be strings")
        parameters.append((key, value))
    return tuple(parameters)


def _build_depends_on(raw_depends, index, assigned_ids):
    if not isinstance(raw_depends, list):
        raise MissionGeneratorLLMError("PROTOCOL", "depends_on_batch_index must be a list")
    depends_on = []
    for position in raw_depends:
        if isinstance(position, bool) or not isinstance(position, int):
            raise MissionGeneratorLLMError("PROTOCOL", "depends_on_batch_index entries must be integers")
        if position < 0 or position >= len(assigned_ids):
            raise MissionGeneratorLLMError("PROTOCOL", "depends_on_batch_index is out of range")
        if position == index:
            raise MissionGeneratorLLMError("PROTOCOL", "candidate cannot depend on its own batch index")
        depends_on.append(assigned_ids[position])
    if len(set(depends_on)) != len(depends_on):
        raise MissionGeneratorLLMError("PROTOCOL", "duplicate depends_on_batch_index entries")
    return tuple(depends_on)


def _build_candidate(index, mission_id, raw_candidate, assigned_ids, *, goal, provider_id, model_id):
    if not isinstance(raw_candidate, dict) or set(raw_candidate) != _CANDIDATE_FIELDS:
        raise MissionGeneratorLLMError("PROTOCOL", "candidate fields do not match schema")
    mission_name = raw_candidate["mission_name"]
    objective = raw_candidate["objective"]
    capability_id = raw_candidate["capability_id"]
    rationale = raw_candidate["rationale"]
    if not all(isinstance(value, str) for value in (mission_name, objective, capability_id, rationale)):
        raise MissionGeneratorLLMError("PROTOCOL", "candidate text fields must be strings")
    parameters = _build_parameters(raw_candidate["parameters"])
    raw_acceptance = raw_candidate["acceptance_criteria"]
    if not isinstance(raw_acceptance, list) or not all(isinstance(item, str) for item in raw_acceptance):
        raise MissionGeneratorLLMError("PROTOCOL", "acceptance_criteria must be a list of strings")
    acceptance_criteria = tuple(raw_acceptance)
    depends_on = _build_depends_on(raw_candidate["depends_on_batch_index"], index, assigned_ids)
    generation_id = _generation_id(
        goal=goal, provider_id=provider_id, model_id=model_id,
        mission_name=mission_name, objective=objective, capability_id=capability_id,
        parameters=parameters, acceptance_criteria=acceptance_criteria, rationale=rationale,
    )
    return candidates_module.GeneratedMissionCandidateV1(
        mission_id, mission_name, objective, capability_id, parameters,
        depends_on, acceptance_criteria, rationale, generation_id,
    )


class MissionGeneratorCandidateProducer:
    """Convert a free-text human goal into a batch of
    GeneratedMissionCandidateV1 already validated by Session 1's
    validate_candidate_batch (called unmodified, as the final and only
    authority on whether a candidate is safe to stage)."""

    def __init__(self, provider, *, registry, max_candidates=MAX_CANDIDATES):
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 1:
            raise MissionGeneratorLLMError("CONFIGURATION", "max_candidates must be a positive integer")
        self._provider = provider
        self._registry = registry
        self._max_candidates = max_candidates

    def produce_batch(self, *, goal, available_mission_ids,
                       existing_mission_ids=frozenset(), resolvable_dependency_ids=frozenset()):
        if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL_CHARS:
            raise MissionGeneratorLLMError("CONFIGURATION", "goal must be non-empty bounded text")
        if type(available_mission_ids) is not tuple or not available_mission_ids:
            raise MissionGeneratorLLMError("CONFIGURATION", "available_mission_ids must be a non-empty tuple")
        if len(set(available_mission_ids)) != len(available_mission_ids):
            raise MissionGeneratorLLMError("CONFIGURATION", "available_mission_ids must be unique")

        prompt = _build_prompt(goal, self._registry, self._max_candidates)
        schema = _candidate_json_schema(self._registry.capability_ids, self._max_candidates)
        response = self._provider.evaluate(prompt, format=schema)

        provider_id = getattr(response, "provider_id", None)
        model_id = getattr(response, "model_id", None)
        content = getattr(response, "content", None)
        if not isinstance(provider_id, str) or not provider_id:
            raise MissionGeneratorLLMError("PROTOCOL", "provider identity is invalid")
        if not isinstance(model_id, str) or not model_id:
            raise MissionGeneratorLLMError("PROTOCOL", "model identity is invalid")
        if not isinstance(content, str):
            raise MissionGeneratorLLMError("PROTOCOL", "provider content must be text")

        raw = _parse_exact_json_object(content)
        if set(raw) != _RESPONSE_FIELDS:
            raise MissionGeneratorLLMError("PROTOCOL", "response fields do not match schema")
        if raw["schema_version"] != 1:
            raise MissionGeneratorLLMError("PROTOCOL", "schema_version must equal 1")
        raw_candidates = raw["candidates"]
        if not isinstance(raw_candidates, list) or not raw_candidates:
            raise MissionGeneratorLLMError("PROTOCOL", "candidates must be a non-empty list")
        if len(raw_candidates) > self._max_candidates:
            raise MissionGeneratorLLMError("LIMIT", "response exceeds maximum candidate count")
        if len(available_mission_ids) < len(raw_candidates):
            raise MissionGeneratorLLMError(
                "CONFIGURATION", "not enough available_mission_ids for the returned candidates"
            )

        assigned_ids = available_mission_ids[:len(raw_candidates)]
        built = tuple(
            _build_candidate(
                index, mission_id, raw_candidate, assigned_ids,
                goal=goal, provider_id=provider_id, model_id=model_id,
            )
            for index, (mission_id, raw_candidate) in enumerate(zip(assigned_ids, raw_candidates))
        )
        return candidates_module.validate_candidate_batch(
            built, registry=self._registry,
            existing_mission_ids=existing_mission_ids,
            resolvable_dependency_ids=resolvable_dependency_ids,
        )


def default_producer(*, registry, transport=None, max_candidates=MAX_CANDIDATES):
    """Real, network-calling producer for DEFAULT_PROVIDER_ID. transport is
    injectable for tests, same as m031_llm_execution_adapter.build_provider;
    omitted, it defaults to a live call to the local Ollama daemon. No
    production caller invokes this yet -- CLI wiring is deferred (Session
    2 scope is library + tests only, per instruction)."""
    tier = llm_adapter.tier_config(DEFAULT_PROVIDER_ID)
    provider = llm_adapter.build_provider(tier, transport=transport)
    return MissionGeneratorCandidateProducer(provider, registry=registry, max_candidates=max_candidates)


__all__ = (
    "DEFAULT_PROVIDER_ID", "MAX_GOAL_CHARS", "MAX_CANDIDATES", "PROMPT_VERSION",
    "MissionGeneratorLLMError", "MissionGeneratorCandidateProducer", "default_producer",
)
