"""Closed, human-maintained mappings from a queue row to a MissionInput.

The queue is deliberately prose-only.  This module is the separate,
versioned authority which may associate one exact queue preview with one
already-registered capability.  It never selects, authorizes, or executes a
mission.  Absence of a matching contract is a rejection, not a default.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re


CONTRACT_SCHEMA_VERSION = 1
_MISSION_ID = re.compile(r"M-\d{3}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_CAPABILITY = re.compile(r"[a-z][a-z0-9.-]{2,127}\Z")
_PARAMETER_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class MissionExecutionContractError(Exception):
    """Base class for non-executable contract failures."""


class InvalidMissionExecutionContractError(MissionExecutionContractError):
    """The human-maintained contract is malformed or ambiguous."""


class NoMissionExecutionContractError(MissionExecutionContractError):
    """No explicit contract authorizes mapping this queue preview."""


class StaleMissionExecutionContractError(MissionExecutionContractError):
    """A contract does not bind the current exact queue preview."""


@dataclass(frozen=True, slots=True)
class MissionExecutionContract:
    mission_id: str
    mission_name: str
    queue_sha256: str
    objective: str
    capability_id: str
    parameters: tuple[tuple[str, str], ...]


def _require_text(value, field, *, pattern=None, maximum=1000):
    if type(value) is not str or not value or len(value) > maximum:
        raise InvalidMissionExecutionContractError(f"invalid {field}")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise InvalidMissionExecutionContractError(f"invalid {field}")
    return value


def _parse_parameters(value):
    if type(value) is not list or len(value) > 32:
        raise InvalidMissionExecutionContractError("invalid parameters")
    parsed = []
    for item in value:
        if type(item) is not dict or set(item) != {"key", "value"}:
            raise InvalidMissionExecutionContractError("invalid parameter")
        key = _require_text(item["key"], "parameter key", pattern=_PARAMETER_KEY, maximum=64)
        parameter_value = _require_text(item["value"], "parameter value", maximum=512)
        parsed.append((key, parameter_value))
    normalized = tuple(sorted(parsed))
    if len({key for key, _value in normalized}) != len(normalized):
        raise InvalidMissionExecutionContractError("duplicate parameter key")
    return normalized


def _parse_contract(value):
    required = {
        "mission_id", "mission_name", "queue_sha256", "objective",
        "capability_id", "parameters",
    }
    if type(value) is not dict or set(value) != required:
        raise InvalidMissionExecutionContractError("invalid contract fields")
    return MissionExecutionContract(
        _require_text(value["mission_id"], "mission id", pattern=_MISSION_ID, maximum=5),
        _require_text(value["mission_name"], "mission name"),
        _require_text(value["queue_sha256"], "queue sha256", pattern=_SHA256, maximum=64),
        _require_text(value["objective"], "objective"),
        _require_text(value["capability_id"], "capability id", pattern=_CAPABILITY, maximum=128),
        _parse_parameters(value["parameters"]),
    )


def load_contracts(path: Path) -> tuple[MissionExecutionContract, ...]:
    """Load a strict JSON document; duplicate or unrecognized data fails closed."""
    try:
        raw = Path(path).read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidMissionExecutionContractError("contract document unreadable") from exc
    if type(value) is not dict or set(value) != {"schema_version", "contracts"}:
        raise InvalidMissionExecutionContractError("invalid contract document fields")
    if value["schema_version"] != CONTRACT_SCHEMA_VERSION or type(value["contracts"]) is not list:
        raise InvalidMissionExecutionContractError("unsupported contract document")
    contracts = tuple(_parse_contract(item) for item in value["contracts"])
    if len({item.mission_id for item in contracts}) != len(contracts):
        raise InvalidMissionExecutionContractError("duplicate mission contract")
    return contracts


def resolve_contract(
    contracts: tuple[MissionExecutionContract, ...], *, mission_id: str,
    mission_name: str, queue_sha256: str,
) -> MissionExecutionContract:
    """Return only an exact queue-bound contract, never a best-effort match."""
    matches = tuple(item for item in contracts if item.mission_id == mission_id)
    if not matches:
        raise NoMissionExecutionContractError("mission has no execution contract")
    contract = matches[0]
    if contract.mission_name != mission_name or contract.queue_sha256 != queue_sha256:
        raise StaleMissionExecutionContractError("contract does not match current queue preview")
    return contract


def document_sha256(path: Path) -> str:
    """Expose the exact human-maintained contract identity for audit output."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


__all__ = (
    "CONTRACT_SCHEMA_VERSION", "MissionExecutionContract",
    "load_contracts", "resolve_contract", "document_sha256",
    "MissionExecutionContractError", "InvalidMissionExecutionContractError",
    "NoMissionExecutionContractError", "StaleMissionExecutionContractError",
)
