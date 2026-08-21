"""Structural validation for Mission Generator candidates (piece 1 of
evidence/MISSION_GENERATOR_DESIGN_V1.md section 2 -- sign-off #2).

No LLM call happens here. This module only proves a candidate is
well-formed and names an already-registered capability, exactly like
mission_execution_contracts.py proves a human-authored contract is
well-formed before it reaches the certified pipeline. A candidate that
fails here never reaches MISSION_PROPOSALS.md staging
(mission_proposal_staging.py)."""

from dataclasses import dataclass
import re


_MISSION_ID = re.compile(r"M-\d{3}\Z")
_PARAMETER_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_GENERATION_ID = re.compile(r"[0-9a-f]{64}\Z")
_MAX_BATCH_SIZE = 32


class GeneratedMissionCandidateError(Exception):
    """Base class for every error this module raises."""


class InvalidGeneratedMissionCandidateError(GeneratedMissionCandidateError):
    """A single candidate is malformed, independent of the rest of the batch."""


class UnregisteredCandidateCapabilityError(GeneratedMissionCandidateError):
    """A candidate names a capability outside the closed registry (design
    doc section 2.2: selection only, never invention -- sign-off #3)."""


class DuplicateCandidateMissionIdError(GeneratedMissionCandidateError):
    """Two candidates in one batch share a mission_id, or a candidate reuses
    a mission_id already present in the real queue."""


class UnresolvedCandidateDependencyError(GeneratedMissionCandidateError):
    """A candidate depends on a mission_id that is neither another candidate
    in this same batch nor already confirmed CLOSED/COMPLETED by the caller."""


class CyclicCandidateDependencyError(GeneratedMissionCandidateError):
    """Two or more candidates in the same batch depend on each other in a
    cycle -- no valid staging order exists."""


@dataclass(frozen=True, slots=True)
class GeneratedMissionCandidateV1:
    """evidence/MISSION_GENERATOR_DESIGN_V1.md section 2.1. Not a
    MissionInput/StagedExecutableMissionV1 -- a proposal for one queue row
    plus one execution contract, in the exact shape a human would fill in
    by hand, so batch review (section 3) is "read a queue-shaped row", not
    "audit a new DSL"."""
    mission_id: str
    mission_name: str
    objective: str
    capability_id: str
    parameters: tuple[tuple[str, str], ...]
    depends_on: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    rationale: str
    generation_id: str


def _require_text(value, field, *, pattern=None, maximum=2000):
    if type(value) is not str or not value or len(value) > maximum:
        raise InvalidGeneratedMissionCandidateError(f"invalid {field}")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise InvalidGeneratedMissionCandidateError(f"invalid {field}")
    return value


def _validate_parameters(value):
    if type(value) is not tuple or len(value) > 32:
        raise InvalidGeneratedMissionCandidateError("invalid parameters")
    parsed = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise InvalidGeneratedMissionCandidateError("invalid parameter")
        key, parameter_value = item
        _require_text(key, "parameter key", pattern=_PARAMETER_KEY, maximum=64)
        _require_text(parameter_value, "parameter value", maximum=512)
        parsed.append((key, parameter_value))
    if len({key for key, _value in parsed}) != len(parsed):
        raise InvalidGeneratedMissionCandidateError("duplicate parameter key")


def _validate_depends_on(candidate):
    value = candidate.depends_on
    if type(value) is not tuple or len(value) > 16:
        raise InvalidGeneratedMissionCandidateError("invalid depends_on")
    for item in value:
        _require_text(item, "dependency mission id", pattern=_MISSION_ID, maximum=5)
    if len(set(value)) != len(value):
        raise InvalidGeneratedMissionCandidateError("duplicate dependency")
    if candidate.mission_id in value:
        raise InvalidGeneratedMissionCandidateError("candidate cannot depend on itself")


def _validate_acceptance_criteria(value):
    if type(value) is not tuple or not value or len(value) > 16:
        raise InvalidGeneratedMissionCandidateError("invalid acceptance_criteria")
    for item in value:
        _require_text(item, "acceptance criterion", maximum=1000)


def validate_candidate(candidate, *, registry):
    """Prove one candidate is well-formed and names only an already
    registered capability. Raises otherwise; never mutates or defaults."""
    if type(candidate) is not GeneratedMissionCandidateV1:
        raise InvalidGeneratedMissionCandidateError("exact GeneratedMissionCandidateV1 required")
    _require_text(candidate.mission_id, "mission id", pattern=_MISSION_ID, maximum=5)
    _require_text(candidate.mission_name, "mission name")
    _require_text(candidate.objective, "objective")
    _require_text(candidate.capability_id, "capability id", maximum=128)
    if candidate.capability_id not in registry.capability_ids:
        raise UnregisteredCandidateCapabilityError(
            f"capability {candidate.capability_id!r} is not in the closed registry"
        )
    _validate_parameters(candidate.parameters)
    _validate_depends_on(candidate)
    _validate_acceptance_criteria(candidate.acceptance_criteria)
    _require_text(candidate.rationale, "rationale", maximum=2000)
    _require_text(candidate.generation_id, "generation id", pattern=_GENERATION_ID, maximum=64)
    return candidate


def _detect_cycle(batch_ids, edges):
    """edges: dict[mission_id, tuple[mission_id, ...]] restricted to
    batch-internal dependencies. Plain DFS with a recursion-stack marker,
    bounded by _MAX_BATCH_SIZE."""
    state = {mission_id: "unvisited" for mission_id in batch_ids}

    def visit(mission_id, stack):
        state[mission_id] = "visiting"
        for dependency in edges.get(mission_id, ()):
            if state[dependency] == "visiting":
                raise CyclicCandidateDependencyError(
                    "cyclic dependency among generated candidates: "
                    + " -> ".join((*stack, dependency))
                )
            if state[dependency] == "unvisited":
                visit(dependency, (*stack, dependency))
        state[mission_id] = "visited"

    for mission_id in batch_ids:
        if state[mission_id] == "unvisited":
            visit(mission_id, (mission_id,))


def validate_candidate_batch(
    candidates, *, registry, existing_mission_ids=frozenset(),
    resolvable_dependency_ids=frozenset(),
):
    """Prove a whole batch is safe to stage: every candidate individually
    valid, no mission_id collisions (within the batch or against the real
    queue), every dependency resolves to either another candidate in this
    batch or an id the caller already confirmed is CLOSED/COMPLETED in the
    real queue, and no dependency cycle exists among the batch itself. Pure
    -- no file I/O, no defaults on failure, same discipline as
    mission_queue_planner.select(). existing_mission_ids and
    resolvable_dependency_ids are supplied by the caller (e.g. derived from
    mission_queue_planner.parse_queue() on the real MISSION_QUEUE.md); this
    module never reads that file itself."""
    if type(candidates) is not tuple or not candidates or len(candidates) > _MAX_BATCH_SIZE:
        raise InvalidGeneratedMissionCandidateError(
            f"a candidate batch of 1..{_MAX_BATCH_SIZE} is required"
        )
    for candidate in candidates:
        validate_candidate(candidate, registry=registry)
    batch_ids = tuple(candidate.mission_id for candidate in candidates)
    if len(set(batch_ids)) != len(batch_ids):
        raise DuplicateCandidateMissionIdError("duplicate mission_id within the same batch")
    collisions = sorted(set(batch_ids) & set(existing_mission_ids))
    if collisions:
        raise DuplicateCandidateMissionIdError(
            f"candidate mission_id already exists in the real queue: {collisions}"
        )
    batch_id_set = set(batch_ids)
    resolvable = set(resolvable_dependency_ids)
    edges = {}
    for candidate in candidates:
        unresolved = tuple(
            dependency for dependency in candidate.depends_on
            if dependency not in batch_id_set and dependency not in resolvable
        )
        if unresolved:
            raise UnresolvedCandidateDependencyError(
                f"{candidate.mission_id} depends on unresolved mission_id(s): {unresolved}"
            )
        edges[candidate.mission_id] = tuple(
            dependency for dependency in candidate.depends_on if dependency in batch_id_set
        )
    _detect_cycle(batch_ids, edges)
    return candidates


__all__ = (
    "GeneratedMissionCandidateV1", "validate_candidate", "validate_candidate_batch",
    "GeneratedMissionCandidateError", "InvalidGeneratedMissionCandidateError",
    "UnregisteredCandidateCapabilityError", "DuplicateCandidateMissionIdError",
    "UnresolvedCandidateDependencyError", "CyclicCandidateDependencyError",
)
