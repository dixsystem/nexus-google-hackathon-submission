"""Modelo de datos para cada intento de ataque red-team (M-2), encadenado
por hash SHA-256 de forma análoga al único patrón de hash con dominio
separado que ya existe en este repo
(mission_generator_llm_producer._generation_id).

Nota de diseño (ver NIGHT_QUESTIONS.md, sección M-2): mission_proposal_
staging.py no encadena registros entre sí -- usa sha256 de documentos
completos solo para detectar staleness contra disco (TOCTOU). No existe
en este repo un precedente literal de "hash-chaining" incidente-a-
incidente, así que este módulo extiende el patrón de hash con dominio
separado (dominio fijo + JSON canónico + sha256) que sí existe, añadiendo
el hash del incidente anterior como un campo más de la entrada canónica
-- la forma más simple y ya usada en este repo de producir una cadena de
integridad verificable.

Rol: PURO modelo de datos + un constructor validador (build_incident). No
hay I/O, no hay llamadas a red, no hay reloj interno -- timestamp es
siempre suministrado por el llamador (misma disciplina que
AntigravityGeminiProvider recibe su clock inyectado). Este módulo no
importa ni llama a stage_proposal_batch, a ningún executor, ni a
red_team_attacker.py -- solo modela lo que YA ocurrió (un intento de
ataque, ya recibido como texto crudo), nunca decide ni ejecuta nada."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Optional


_INCIDENT_HASH_DOMAIN = b"NEXUS-REDTEAM-INCIDENT-V1\x00"
_GENESIS_DOMAIN = b"NEXUS-REDTEAM-SESSION-GENESIS-V1\x00"

# Hash génesis fijo: encadenado del primer incidente de cualquier sesión.
# Calculado una sola vez sobre un dominio literal -- no depende de ningún
# dato de sesión, exactamente como una constante de configuración
# SYSTEM_CONSTANT (ver antigravity_gemini_provider.py sobre esa
# distinción).
GENESIS_INCIDENT_HASH = hashlib.sha256(_GENESIS_DOMAIN).hexdigest()

_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PAYLOAD_CHARS = 200_000
_MAX_REJECTION_REASON_CHARS = 2000


class RedTeamIncidentError(Exception):
    """Base class for every error this module raises."""


class InvalidRedTeamIncidentError(RedTeamIncidentError):
    """One or more fields supplied to build_incident are malformed."""


def _require_text(value, field_name, *, maximum, pattern=None):
    if type(value) is not str or not value or len(value) > maximum:
        raise InvalidRedTeamIncidentError(f"invalid {field_name}")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise InvalidRedTeamIncidentError(f"invalid {field_name}")
    return value


def _canonical_incident_fields(
    *, incident_id, session_id, attack_round, raw_attack_payload,
    rejection_reason, blocked, timestamp, previous_incident_hash,
):
    return {
        "incident_id": incident_id,
        "session_id": session_id,
        "attack_round": attack_round,
        "raw_attack_payload": raw_attack_payload,
        "rejection_reason": rejection_reason,
        "blocked": blocked,
        "timestamp": timestamp,
        "previous_incident_hash": previous_incident_hash,
    }


def _compute_incident_hash(fields: dict) -> str:
    canonical = json.dumps(
        fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(_INCIDENT_HASH_DOMAIN + canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class RedTeamIncident:
    """Un intento de ataque ya recibido (raw_attack_payload) más el
    veredicto que Nexus ya emitió sobre él (rejection_reason/blocked),
    encadenado criptográficamente al incidente anterior de la misma
    sesión vía previous_incident_hash/incident_hash. Nunca construido
    directamente -- usar build_incident(), que es la única función que
    calcula incident_hash correctamente."""

    incident_id: str
    session_id: str
    attack_round: int
    raw_attack_payload: str
    rejection_reason: Optional[str]
    blocked: bool
    timestamp: str
    previous_incident_hash: str
    incident_hash: str


def build_incident(
    *,
    session_id: str,
    attack_round: int,
    raw_attack_payload: str,
    rejection_reason: Optional[str],
    blocked: bool,
    timestamp: str,
    previous_incident_hash: Optional[str] = None,
    incident_id: Optional[str] = None,
) -> RedTeamIncident:
    """Construye un RedTeamIncident con su incident_hash correctamente
    encadenado. previous_incident_hash=None (el default) significa "este
    es el primer incidente de la sesión" -- se encadena a
    GENESIS_INCIDENT_HASH, nunca a una cadena vacía o inventada.
    incident_id=None deriva un id determinista session_id:round-N -- el
    llamador puede suministrar uno propio si necesita otra convención."""

    session_id = _require_text(session_id, "session_id", maximum=128, pattern=_SESSION_ID)
    if isinstance(attack_round, bool) or not isinstance(attack_round, int) or attack_round < 1:
        raise InvalidRedTeamIncidentError("attack_round must be a positive integer")
    raw_attack_payload = _require_text(
        raw_attack_payload, "raw_attack_payload", maximum=_MAX_PAYLOAD_CHARS
    )
    if rejection_reason is not None:
        rejection_reason = _require_text(
            rejection_reason, "rejection_reason", maximum=_MAX_REJECTION_REASON_CHARS
        )
    if not isinstance(blocked, bool):
        raise InvalidRedTeamIncidentError("blocked must be a bool")
    timestamp = _require_text(timestamp, "timestamp", maximum=64)

    if previous_incident_hash is None:
        previous_incident_hash = GENESIS_INCIDENT_HASH
    else:
        previous_incident_hash = _require_text(
            previous_incident_hash, "previous_incident_hash", maximum=64, pattern=_SHA256_HEX
        )

    if incident_id is None:
        incident_id = f"{session_id}:round-{attack_round}"
    else:
        incident_id = _require_text(incident_id, "incident_id", maximum=256)

    fields = _canonical_incident_fields(
        incident_id=incident_id, session_id=session_id, attack_round=attack_round,
        raw_attack_payload=raw_attack_payload, rejection_reason=rejection_reason,
        blocked=blocked, timestamp=timestamp, previous_incident_hash=previous_incident_hash,
    )
    incident_hash = _compute_incident_hash(fields)

    return RedTeamIncident(
        incident_id=incident_id, session_id=session_id, attack_round=attack_round,
        raw_attack_payload=raw_attack_payload, rejection_reason=rejection_reason,
        blocked=blocked, timestamp=timestamp,
        previous_incident_hash=previous_incident_hash, incident_hash=incident_hash,
    )


def incident_to_dict(incident: RedTeamIncident) -> dict:
    """Serialización determinista campo a campo (mismo orden siempre) --
    usar json.dumps(..., sort_keys=True) sobre el resultado para bytes
    deterministas, igual que mission_proposal_staging._candidate_document_bytes."""
    return {
        "incident_id": incident.incident_id,
        "session_id": incident.session_id,
        "attack_round": incident.attack_round,
        "raw_attack_payload": incident.raw_attack_payload,
        "rejection_reason": incident.rejection_reason,
        "blocked": incident.blocked,
        "timestamp": incident.timestamp,
        "previous_incident_hash": incident.previous_incident_hash,
        "incident_hash": incident.incident_hash,
    }


def incident_to_json(incident: RedTeamIncident) -> str:
    return json.dumps(
        incident_to_dict(incident), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


@dataclass(frozen=True, slots=True)
class RedTeamSession:
    """Agrupa una lista ordenada de RedTeamIncident bajo una misma sesión
    de pentest. Puramente un contenedor -- no valida el encadenado entre
    sus incidentes (eso es responsabilidad de verify_session_chain);
    tampoco escribe a disco ni ejecuta nada."""

    session_id: str
    started_at: str
    goal: str
    incidents: tuple = field(default_factory=tuple)


def build_session(*, session_id: str, started_at: str, goal: str, incidents=()) -> RedTeamSession:
    session_id = _require_text(session_id, "session_id", maximum=128, pattern=_SESSION_ID)
    started_at = _require_text(started_at, "started_at", maximum=64)
    goal = _require_text(goal, "goal", maximum=4000)
    incidents = tuple(incidents)
    for incident in incidents:
        if type(incident) is not RedTeamIncident:
            raise InvalidRedTeamIncidentError("exact RedTeamIncident required in incidents")
    return RedTeamSession(session_id, started_at, goal, incidents)


def verify_session_chain(session: RedTeamSession) -> bool:
    """True si session.incidents forma una cadena de hash intacta: el
    primer incidente encadena a GENESIS_INCIDENT_HASH, cada incidente
    siguiente encadena al incident_hash del anterior, y cada incident_hash
    individual sigue siendo el que build_incident() habría calculado para
    esos mismos campos (detecta tanto una alteración de campos como una
    ruptura de encadenado)."""
    expected_previous = GENESIS_INCIDENT_HASH
    for incident in session.incidents:
        if incident.previous_incident_hash != expected_previous:
            return False
        fields = _canonical_incident_fields(
            incident_id=incident.incident_id, session_id=incident.session_id,
            attack_round=incident.attack_round, raw_attack_payload=incident.raw_attack_payload,
            rejection_reason=incident.rejection_reason, blocked=incident.blocked,
            timestamp=incident.timestamp, previous_incident_hash=incident.previous_incident_hash,
        )
        if _compute_incident_hash(fields) != incident.incident_hash:
            return False
        expected_previous = incident.incident_hash
    return True


def session_to_dict(session: RedTeamSession) -> dict:
    return {
        "session_id": session.session_id,
        "started_at": session.started_at,
        "goal": session.goal,
        "incidents": [incident_to_dict(incident) for incident in session.incidents],
    }


def session_to_json(session: RedTeamSession) -> str:
    return json.dumps(
        session_to_dict(session), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


__all__ = (
    "GENESIS_INCIDENT_HASH",
    "RedTeamIncidentError",
    "InvalidRedTeamIncidentError",
    "RedTeamIncident",
    "build_incident",
    "incident_to_dict",
    "incident_to_json",
    "RedTeamSession",
    "build_session",
    "verify_session_chain",
    "session_to_dict",
    "session_to_json",
)
