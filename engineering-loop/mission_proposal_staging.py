"""Staging of a validated Mission Generator batch to MISSION_PROPOSALS.md
plus a parallel candidate-contract document, and the batch human-approval
gate (piece 3 of evidence/MISSION_GENERATOR_DESIGN_V1.md sections 2.5/3/4.1
-- sign-offs #5, #6, #7).

Never writes MISSION_QUEUE.md or MISSION_EXECUTION_CONTRACTS.json (the
CSI-ceiling boundary of section 3, sign-off #6). The copy from an approved
batch to those canonical files is a separate, double-gated module
(mission_proposal_promotion.py, Session 4) -- this module stops at
"approved candidates returned", nothing more.

mission_execution_contracts.MissionExecutionContract and
CONTRACT_SCHEMA_VERSION are reused unmodified: candidate contracts are the
exact same certified type a human-authored contract already is, round-
tripped through the real, unmodified load_contracts() to prove it, per
sign-off #14 (resolve_contract and friends stay untouched).

Session 4 adds a third staging document, MISSION_PROPOSAL_CANDIDATES.json
(stage_proposal_batch, PROPOSAL_BATCH_SCHEMA_VERSION), carrying the full
9-field GeneratedMissionCandidateV1 shape -- MISSION_PROPOSAL_CONTRACTS.json
alone never round-tripped depends_on/acceptance_criteria/rationale/
generation_id. load_proposal_batch reloads it for a separate-process
approval (approve now, propose ran earlier): it never trusts the document,
re-validating the whole batch through validate_candidate_batch (unmodified)
and re-hashing both older staging files before handing check_batch_human_gate
(unmodified) the exact same ProposalBatchV1 shape it always received. See
evidence/MISSION_GENERATOR_SESSION_4_DESIGN_TWO_PHASE_APPROVAL_AND_
PROMOTION_V1.md."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile

import mission_execution_contracts as execution_contracts
import mission_generator_candidates as candidates_module


PROPOSAL_BATCH_SCHEMA_VERSION = 1
_CANDIDATE_DOCUMENT_FIELDS = {
    "mission_id", "mission_name", "objective", "capability_id", "parameters",
    "depends_on", "acceptance_criteria", "rationale", "generation_id",
}


class ProposalStagingError(Exception):
    """Base class for every error this module raises."""


class BatchHumanConfirmationRequiredError(ProposalStagingError):
    """No path in this module may return approved candidates without an
    explicit confirm=True, same discipline as
    governed_scheduler.check_human_gate."""


class InvalidBatchApprovalError(ProposalStagingError):
    """The approved mission id selection is empty, duplicated, or names a
    mission_id outside this batch."""


class StaleProposalBatchError(ProposalStagingError):
    """MISSION_PROPOSALS.md changed on disk after staging and before
    approval -- same TOCTOU guard as mission_queue_planner's
    _assert_queue_unchanged, applied to the staging file instead of the
    canonical queue. Also raised by load_proposal_batch (Session 4) for a
    MISSION_PROPOSAL_CONTRACTS.json mismatch -- same staleness concept,
    same exception type, one more file covered."""


class InvalidProposalCandidateDocumentError(ProposalStagingError):
    """MISSION_PROPOSAL_CANDIDATES.json is unreadable, malformed, or does
    not match PROPOSAL_BATCH_SCHEMA_VERSION -- fails closed the same way
    load_contracts() does for a corrupt contract document."""


class InconsistentProposalDocumentsError(ProposalStagingError):
    """The candidates reconstructed from MISSION_PROPOSAL_CANDIDATES.json
    do not produce the exact same contracts already on disk in
    MISSION_PROPOSAL_CONTRACTS.json -- the two staging documents were
    edited independently and now disagree."""


@dataclass(frozen=True, slots=True)
class ProposalBatchV1:
    candidates: tuple[candidates_module.GeneratedMissionCandidateV1, ...]
    proposals_path: Path
    proposals_sha256: str
    contracts_path: Path
    contracts_sha256: str
    contracts: tuple[execution_contracts.MissionExecutionContract, ...]


def _cell(text: str) -> str:
    return text.replace("\n", " ").replace("|", "\\|").strip()


def render_proposal_document(candidates: tuple) -> str:
    """Same 7-column shape as MISSION_QUEUE.md's active table, plus the
    mandatory Origen column already decided in
    PLANNER_SCHEDULER_MISSION_GENERATOR_ARCHITECTURE_PROPOSAL_V1.md (2026-
    08-09). Prose-only acceptance/evidence cells, same convention as
    mission_execution_contracts.py's "the queue is deliberately
    prose-only". Deterministic: same candidates always render identical
    bytes."""
    lines = [
        "# DIXKEEPER — MISSION PROPOSALS (STAGING)",
        "",
        "**Estado:** `STAGING` — sin efecto hasta aprobación humana de lote completo",
        "",
        "Ninguna fila de este documento tiene autoridad de ejecución. Copiar una fila",
        "a `MISSION_QUEUE.md`/`MISSION_EXECUTION_CONTRACTS.json` requiere aprobación",
        "de lote explícita (`check_batch_human_gate`, `confirm=True`) y, en esta",
        "sesión, un paso de copia manual — ver",
        "evidence/MISSION_GENERATOR_DESIGN_V1.md sección 3.",
        "",
        "## Lote propuesto",
        "",
        "| ID | Prioridad | Misión | Estado | Dependencias | Criterio de salida | Evidencia | Origen |",
        "|---|---:|---|---|---|---|---|---|",
    ]
    for index, candidate in enumerate(candidates, start=1):
        dependencies = ", ".join(candidate.depends_on) or "Ninguna"
        acceptance = "; ".join(candidate.acceptance_criteria)
        evidence = f"{candidate.rationale} (generation_id: {candidate.generation_id})"
        lines.append(
            "| {mission_id} | {priority} | `{name}` | `PROPOSED` | {deps} | {accept} | {evidence} | `GENERATED` |".format(
                mission_id=candidate.mission_id, priority=index,
                name=_cell(candidate.mission_name), deps=_cell(dependencies),
                accept=_cell(acceptance), evidence=_cell(evidence),
            )
        )
    lines.append("")
    return "\n".join(lines)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    directory = path.parent
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            try:
                os.fsync(stream.fileno())
            except OSError:
                pass
            os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _build_contract_candidates(candidates, queue_sha256):
    return tuple(
        execution_contracts.MissionExecutionContract(
            candidate.mission_id, candidate.mission_name, queue_sha256,
            candidate.objective, candidate.capability_id,
            tuple(sorted(candidate.parameters)),
        )
        for candidate in candidates
    )


def _contract_document_bytes(contracts) -> bytes:
    return json.dumps(
        {
            "schema_version": execution_contracts.CONTRACT_SCHEMA_VERSION,
            "contracts": [
                {
                    "mission_id": contract.mission_id,
                    "mission_name": contract.mission_name,
                    "queue_sha256": contract.queue_sha256,
                    "objective": contract.objective,
                    "capability_id": contract.capability_id,
                    "parameters": [
                        {"key": key, "value": value} for key, value in contract.parameters
                    ],
                }
                for contract in contracts
            ],
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def _candidate_document_bytes(
    candidates, *, proposals_path, proposals_sha256, contracts_path, contracts_sha256,
) -> bytes:
    return json.dumps(
        {
            "schema_version": PROPOSAL_BATCH_SCHEMA_VERSION,
            "proposals_path": str(proposals_path),
            "proposals_sha256": proposals_sha256,
            "contracts_path": str(contracts_path),
            "contracts_sha256": contracts_sha256,
            "candidates": [
                {
                    "mission_id": candidate.mission_id,
                    "mission_name": candidate.mission_name,
                    "objective": candidate.objective,
                    "capability_id": candidate.capability_id,
                    "parameters": [
                        {"key": key, "value": value} for key, value in candidate.parameters
                    ],
                    "depends_on": list(candidate.depends_on),
                    "acceptance_criteria": list(candidate.acceptance_criteria),
                    "rationale": candidate.rationale,
                    "generation_id": candidate.generation_id,
                }
                for candidate in candidates
            ],
        },
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def stage_proposal_batch(
    candidates, *, registry, proposals_path, contracts_path, candidates_path,
    existing_mission_ids=frozenset(), resolvable_dependency_ids=frozenset(),
):
    """Validate, then materialize the batch to disk in the order fixed by
    design doc section 2.5: generate (already done by the caller) ->
    validate -> write MISSION_PROPOSALS.md -> hash it -> derive candidate
    contracts bound to that hash -> write the contract document -> round-
    trip it through the real load_contracts() -> write
    MISSION_PROPOSAL_CANDIDATES.json (Session 4, full 9-field fidelity, for
    a later load_proposal_batch call). Never touches
    MISSION_QUEUE.md/MISSION_EXECUTION_CONTRACTS.json."""
    validated = candidates_module.validate_candidate_batch(
        candidates, registry=registry, existing_mission_ids=existing_mission_ids,
        resolvable_dependency_ids=resolvable_dependency_ids,
    )
    proposals_path = Path(proposals_path)
    contracts_path = Path(contracts_path)
    candidates_path = Path(candidates_path)
    document = render_proposal_document(validated).encode("utf-8")
    _atomic_write_bytes(proposals_path, document)
    proposals_sha256 = hashlib.sha256(document).hexdigest()
    contracts = _build_contract_candidates(validated, proposals_sha256)
    contract_document = _contract_document_bytes(contracts)
    _atomic_write_bytes(contracts_path, contract_document)
    contracts_sha256 = hashlib.sha256(contract_document).hexdigest()
    reloaded = execution_contracts.load_contracts(contracts_path)
    if reloaded != contracts:
        raise ProposalStagingError("staged contract document did not round-trip exactly")
    candidate_document = _candidate_document_bytes(
        validated, proposals_path=proposals_path, proposals_sha256=proposals_sha256,
        contracts_path=contracts_path, contracts_sha256=contracts_sha256,
    )
    _atomic_write_bytes(candidates_path, candidate_document)
    return ProposalBatchV1(
        validated, proposals_path, proposals_sha256,
        contracts_path, contracts_sha256, contracts,
    )


def _parse_candidate(value):
    if type(value) is not dict or set(value) != _CANDIDATE_DOCUMENT_FIELDS:
        raise InvalidProposalCandidateDocumentError("invalid candidate fields")
    if type(value["parameters"]) is not list:
        raise InvalidProposalCandidateDocumentError("invalid parameters")
    parameters = []
    for item in value["parameters"]:
        if type(item) is not dict or set(item) != {"key", "value"}:
            raise InvalidProposalCandidateDocumentError("invalid parameter")
        parameters.append((item["key"], item["value"]))
    if type(value["depends_on"]) is not list:
        raise InvalidProposalCandidateDocumentError("invalid depends_on")
    if type(value["acceptance_criteria"]) is not list:
        raise InvalidProposalCandidateDocumentError("invalid acceptance_criteria")
    return candidates_module.GeneratedMissionCandidateV1(
        value["mission_id"], value["mission_name"], value["objective"],
        value["capability_id"], tuple(parameters), tuple(value["depends_on"]),
        tuple(value["acceptance_criteria"]), value["rationale"], value["generation_id"],
    )


def load_proposal_batch(
    *, registry, proposals_path, contracts_path, candidates_path,
    existing_mission_ids=frozenset(), resolvable_dependency_ids=frozenset(),
):
    """The inverse of stage_proposal_batch, for a separate-process approval
    (Session 4): disk -> ProposalBatchV1. Never trusts
    MISSION_PROPOSAL_CANDIDATES.json -- (1) parses it with the same
    fail-closed rigor as load_contracts, (2) re-validates the whole
    reconstructed batch through validate_candidate_batch unmodified (a
    tampered or stale document must fail exactly like a bad LLM candidate
    would), (3) re-hashes BOTH MISSION_PROPOSALS.md and
    MISSION_PROPOSAL_CONTRACTS.json against the hashes recorded at staging
    time (check_batch_human_gate only ever re-hashes the former; this is
    the additive staleness layer for the latter), (4) reloads the real
    contract document and confirms it matches what the candidates document
    implies, byte for byte. Only then does it return a ProposalBatchV1 --
    the exact same shape stage_proposal_batch already returns, ready to
    pass, unmodified, to check_batch_human_gate.

    existing_mission_ids/resolvable_dependency_ids are requested fresh by
    the caller every time (never persisted from the propose-time call) --
    real time may have passed between propose and approve, and the queue
    may have changed since; reusing stale values would make step (2) a
    no-op instead of a real re-validation."""
    proposals_path = Path(proposals_path)
    contracts_path = Path(contracts_path)
    candidates_path = Path(candidates_path)
    try:
        raw = candidates_path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidProposalCandidateDocumentError("candidate document unreadable") from exc
    required = {
        "schema_version", "proposals_path", "proposals_sha256",
        "contracts_path", "contracts_sha256", "candidates",
    }
    if type(document) is not dict or set(document) != required:
        raise InvalidProposalCandidateDocumentError("invalid candidate document fields")
    if (
        document["schema_version"] != PROPOSAL_BATCH_SCHEMA_VERSION
        or type(document["candidates"]) is not list
    ):
        raise InvalidProposalCandidateDocumentError("unsupported candidate document")
    reconstructed = tuple(_parse_candidate(item) for item in document["candidates"])

    validated = candidates_module.validate_candidate_batch(
        reconstructed, registry=registry, existing_mission_ids=existing_mission_ids,
        resolvable_dependency_ids=resolvable_dependency_ids,
    )

    current_proposals_sha256 = hashlib.sha256(proposals_path.read_bytes()).hexdigest()
    if current_proposals_sha256 != document["proposals_sha256"]:
        raise StaleProposalBatchError(
            "MISSION_PROPOSALS.md changed since staging; re-stage before approving"
        )
    current_contracts_sha256 = hashlib.sha256(contracts_path.read_bytes()).hexdigest()
    if current_contracts_sha256 != document["contracts_sha256"]:
        raise StaleProposalBatchError(
            "MISSION_PROPOSAL_CONTRACTS.json changed since staging; re-stage before approving"
        )

    reloaded_contracts = execution_contracts.load_contracts(contracts_path)
    expected_contracts = _build_contract_candidates(validated, current_proposals_sha256)
    if reloaded_contracts != expected_contracts:
        raise InconsistentProposalDocumentsError(
            "MISSION_PROPOSAL_CANDIDATES.json and MISSION_PROPOSAL_CONTRACTS.json disagree"
        )

    return ProposalBatchV1(
        validated, proposals_path, current_proposals_sha256,
        contracts_path, current_contracts_sha256, reloaded_contracts,
    )


def check_batch_human_gate(
    batch: ProposalBatchV1, approved_mission_ids: tuple, *, confirm: bool,
) -> tuple:
    """The single human-confirmation checkpoint for a proposal batch --
    same discipline as governed_scheduler.check_human_gate (literal
    confirm=True, no truthy shortcut), extended to N candidates approved
    (or partially approved) together, per design doc section 3 point 2.
    Also re-hashes MISSION_PROPOSALS.md on disk to reject a stale approval
    against a batch that changed since staging. Returns only the approved
    candidates -- never copies anything to the canonical queue/contracts."""
    if not isinstance(confirm, bool) or confirm is not True:
        raise BatchHumanConfirmationRequiredError(
            "explicit confirm=True is required before approving any proposal batch"
        )
    if type(approved_mission_ids) is not tuple or not approved_mission_ids:
        raise InvalidBatchApprovalError("a non-empty approved mission id tuple is required")
    if len(set(approved_mission_ids)) != len(approved_mission_ids):
        raise InvalidBatchApprovalError("duplicate mission_id in approval")
    batch_ids = {candidate.mission_id for candidate in batch.candidates}
    unknown = tuple(
        mission_id for mission_id in approved_mission_ids if mission_id not in batch_ids
    )
    if unknown:
        raise InvalidBatchApprovalError(
            f"approval references mission_id(s) outside the batch: {unknown}"
        )
    current = hashlib.sha256(Path(batch.proposals_path).read_bytes()).hexdigest()
    if current != batch.proposals_sha256:
        raise StaleProposalBatchError(
            "MISSION_PROPOSALS.md changed after staging; re-stage before approving"
        )
    return tuple(
        candidate for candidate in batch.candidates
        if candidate.mission_id in approved_mission_ids
    )


__all__ = (
    "ProposalBatchV1", "render_proposal_document", "stage_proposal_batch",
    "load_proposal_batch", "check_batch_human_gate", "PROPOSAL_BATCH_SCHEMA_VERSION",
    "ProposalStagingError", "BatchHumanConfirmationRequiredError",
    "InvalidBatchApprovalError", "StaleProposalBatchError",
    "InvalidProposalCandidateDocumentError", "InconsistentProposalDocumentsError",
)
