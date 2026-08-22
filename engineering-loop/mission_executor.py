"""Executor real del lado ALLOW (M-6): la primera pieza de este repo que
produce un efecto verificable fuera del proceso -- un bucket de Cloud
Storage único por misión, con el resultado firmado adentro. Hasta esta
misión, el flujo entero (mission_generator_llm_producer.py ->
mission_generator_candidates.py -> mission_proposal_staging.py) termina
en "candidatos STAGED"; nada de eso ejecuta nada. Este módulo es el
primer y único lugar de engineering-loop/ que sí actúa.

Rol y límites (léanse junto con las REGLAS DURAS de la misión M-6):
- Este módulo NUNCA decide si algo se permite -- solo actúa cuando ya
  existe una AllowDecision genuina (ver authorize_allow_decision()) y
  falla cerrado (lanza MissionExecutorError) ante cualquier intento sin
  una decisión válida. No hay ningún camino que ejecute con un bool
  suelto ("allow=True").
- Nunca importa ni depende de mission_proposal_staging.stage_proposal_batch
  ni de ningún componente que genere staging/propuestas -- consume
  únicamente su salida ya materializada (GeneratedMissionCandidateV1 y el
  tuple de candidatos que check_batch_human_gate ya aprobó).
- Nunca importa google.cloud.storage directamente -- storage_client es
  SIEMPRE inyectado, sin default real, misma disciplina exacta que
  AntigravityGeminiConfig/AntigravityGeminiProvider aplican para Gemini
  ("Sin default deliberado"): este módulo se puede importar y probar
  sin el paquete google-cloud-storage instalado. El contrato duck-typed
  que exige (create_bucket(name) -> objeto con .blob(path) ->
  objeto con .upload_from_string(data, content_type=...)) es
  estructuralmente idéntico al de google.cloud.storage.Client, para que
  una instancia real de esa clase pueda inyectarse sin código adaptador.

Límite documentado de authorize_allow_decision() (instrucción explícita
de la misión M-6, punto 3.a): este constructor TRUSTS que
`approved_candidates` es genuinamente la tupla que devolvió
mission_proposal_staging.check_batch_human_gate(..., confirm=True) (o un
mecanismo de aprobación humana equivalente ya gateado) -- no vuelve a
invocar ese gate ni a re-hashear MISSION_PROPOSALS.md, porque hacerlo
acoplaría este executor a las rutas de archivo de staging, que están
fuera de su responsabilidad. Lo que SÍ verifica de forma robusta,
siempre, sin excepción: (1) approved_candidates debe ser exactamente un
tuple, (2) mission_candidate debe estar presente en ese tuple por
igualdad estructural completa (GeneratedMissionCandidateV1 es un
dataclass frozen -- la igualdad compara los 9 campos, incluido
generation_id, así que no basta con que coincida el mission_id: el
candidato debe ser byte-a-byte el mismo que fue aprobado), y (3) el
AllowDecision resultante queda criptográficamente atado
(decision_hash, dominio separado, mismo patrón que
mission_generator_llm_producer._generation_id) al contenido exacto de
mission_candidate, así que execute_allowed_mission() puede detectar --
sin volver a tocar disco -- si alguien intenta reutilizar una
AllowDecision contra un candidato distinto al que fue aprobada. No es
un token de capability criptográficamente infalsificable (quien lea
este código fuente puede recalcular el hash); es tamper-evident, igual
que cada otro hash de este repo, no un secreto."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Callable, Optional

import mission_generator_candidates as candidates_module


STATUS_EXECUTED = "EXECUTED"
STATUS_FAILED = "FAILED"

_ALLOW_DECISION_HASH_DOMAIN = b"NEXUS-MISSION-EXECUTOR-ALLOW-DECISION-V1\x00"
_RESULT_HASH_DOMAIN = b"NEXUS-MISSION-EXECUTOR-RESULT-V1\x00"

_BUCKET_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{1,61})[a-z0-9]\Z")
_OBJECT_PATH = "result.json"
_MAX_FAILURE_REASON_CHARS = 2000


class MissionExecutorError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True, slots=True)
class AllowDecision:
    """Solo debe construirse vía authorize_allow_decision() -- ver nota de
    límite documentado en el docstring del módulo. Un AllowDecision
    construido a mano con un decision_hash inventado NO pasará la
    verificación de execute_allowed_mission(), que recalcula el hash
    esperado a partir del mission_candidate real y lo compara."""

    mission_id: str
    decision_hash: str


def _decision_hash(mission_candidate) -> str:
    values = {
        "mission_id": mission_candidate.mission_id,
        "mission_name": mission_candidate.mission_name,
        "objective": mission_candidate.objective,
        "capability_id": mission_candidate.capability_id,
        "generation_id": mission_candidate.generation_id,
    }
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(_ALLOW_DECISION_HASH_DOMAIN + canonical).hexdigest()


def authorize_allow_decision(mission_candidate, *, approved_candidates) -> AllowDecision:
    """Envuelve una decisión de aprobación YA TOMADA (ver límite
    documentado en el docstring del módulo) en un AllowDecision atado
    criptográficamente al contenido exacto de mission_candidate. Esta
    función NUNCA aprueba nada por su cuenta -- si mission_candidate no
    está, byte a byte, dentro de approved_candidates, lanza
    MissionExecutorError en vez de inventar una aprobación."""

    if type(mission_candidate) is not candidates_module.GeneratedMissionCandidateV1:
        raise MissionExecutorError("CONFIGURATION", "exact GeneratedMissionCandidateV1 required")
    if type(approved_candidates) is not tuple:
        raise MissionExecutorError(
            "CONFIGURATION",
            "approved_candidates must be the tuple returned by "
            "mission_proposal_staging.check_batch_human_gate (or an equivalent "
            "already-gated approval)",
        )
    if mission_candidate not in approved_candidates:
        raise MissionExecutorError(
            "NOT_ALLOWED",
            f"mission_id={mission_candidate.mission_id!r} is not present, byte-for-byte, "
            "in approved_candidates -- no genuine ALLOW decision exists for this candidate",
        )
    return AllowDecision(mission_id=mission_candidate.mission_id, decision_hash=_decision_hash(mission_candidate))


@dataclass(frozen=True, slots=True)
class MissionExecutionResult:
    mission_id: str
    bucket_name: str
    object_path: Optional[str]
    public_url: Optional[str]
    executed_at: str
    result_hash: Optional[str]
    status: str
    failure_reason: Optional[str] = None


def _bucket_name(mission_candidate) -> str:
    """nexus-mission-<mission_id>-<hash_corto>: determinista (el mismo
    candidato siempre produce el mismo nombre) y único por candidato
    (generation_id es un sha256 sobre el contenido completo del
    candidato -- ver mission_generator_llm_producer._generation_id).
    Unicidad GLOBAL dentro del namespace de Cloud Storage (que abarca
    TODOS los proyectos de GCP, no solo este) sigue sin poder
    garantizarla este módulo -- eso es una restricción real de GCS, no
    algo que este código pueda controlar; create_bucket() del cliente
    inyectado es quien fallaría en ese caso extremadamente improbable, y
    ese fallo se captura como cualquier otro (ver execute_allowed_mission)."""

    short_hash = mission_candidate.generation_id[:8]
    name = f"nexus-mission-{mission_candidate.mission_id.lower()}-{short_hash}"
    if _BUCKET_NAME_RE.fullmatch(name) is None:
        raise MissionExecutorError("CONFIGURATION", f"derived bucket name is not a valid GCS bucket name: {name!r}")
    return name


def _compute_result_hash(*, mission_id, objective, capability_id, timestamp) -> str:
    values = {
        "mission_id": mission_id, "objective": objective,
        "capability_id": capability_id, "timestamp": timestamp,
    }
    canonical = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(_RESULT_HASH_DOMAIN + canonical).hexdigest()


class MissionExecutor:
    """storage_client es OBLIGATORIO y sin default real -- ver nota de
    módulo. Debe exponer, como mínimo, el mismo contrato que
    google.cloud.storage.Client: .create_bucket(bucket_name) -> objeto
    con .blob(object_path) -> objeto con
    .upload_from_string(data: bytes, content_type: str)."""

    def __init__(self, storage_client, *, clock: Callable[[], datetime] = None):
        if storage_client is None:
            raise MissionExecutorError(
                "CONFIGURATION",
                "storage_client must be supplied explicitly; this module ships no "
                "default live Cloud Storage client",
            )
        self._storage_client = storage_client
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))

    def execute_allowed_mission(self, mission_candidate, allow_decision) -> MissionExecutionResult:
        # -- Verificación de ALLOW: SIEMPRE lanza si no es genuina, nunca
        # degrada a un MissionExecutionResult(status=FAILED). Un intento
        # sin ALLOW válido no es "la ejecución falló" -- es "la ejecución
        # nunca debió empezar".
        if type(mission_candidate) is not candidates_module.GeneratedMissionCandidateV1:
            raise MissionExecutorError("CONFIGURATION", "exact GeneratedMissionCandidateV1 required")
        if type(allow_decision) is not AllowDecision:
            raise MissionExecutorError(
                "NOT_ALLOWED",
                "allow_decision must be an exact AllowDecision produced by "
                "authorize_allow_decision() -- a bare bool or duck-typed object is never accepted",
            )
        if allow_decision.mission_id != mission_candidate.mission_id:
            raise MissionExecutorError(
                "NOT_ALLOWED", "allow_decision.mission_id does not match mission_candidate.mission_id"
            )
        if allow_decision.decision_hash != _decision_hash(mission_candidate):
            raise MissionExecutorError(
                "NOT_ALLOWED",
                "allow_decision does not match the exact content of mission_candidate -- "
                "possible tamper, or reuse of a decision granted to a different candidate",
            )

        # -- A partir de aquí el ALLOW ya está confirmado. Cualquier fallo
        # de ejecución real de aquí en adelante se captura y se reporta
        # como MissionExecutionResult(status=FAILED, failure_reason=...) --
        # nunca una excepción sin control, y nunca un bucket a medias sin
        # que el fallo quede registrado en el resultado devuelto.
        bucket_name = _bucket_name(mission_candidate)
        timestamp = self._clock().isoformat()
        result_hash = _compute_result_hash(
            mission_id=mission_candidate.mission_id,
            objective=mission_candidate.objective,
            capability_id=mission_candidate.capability_id,
            timestamp=timestamp,
        )
        try:
            bucket = self._storage_client.create_bucket(bucket_name)
            document = {
                "mission_id": mission_candidate.mission_id,
                "objective": mission_candidate.objective,
                "capability_id": mission_candidate.capability_id,
                "timestamp": timestamp,
                "result_hash": result_hash,
            }
            document_bytes = json.dumps(
                document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            blob = bucket.blob(_OBJECT_PATH)
            blob.upload_from_string(document_bytes, content_type="application/json")
        except Exception as exc:  # noqa: BLE001 -- cualquier fallo de storage_client se degrada a FAILED, nunca se propaga sin control
            return MissionExecutionResult(
                mission_id=mission_candidate.mission_id,
                bucket_name=bucket_name,
                object_path=None,
                public_url=None,
                executed_at=timestamp,
                result_hash=None,
                status=STATUS_FAILED,
                failure_reason=str(exc)[:_MAX_FAILURE_REASON_CHARS],
            )

        # Mismo formato que google.cloud.storage.Blob.public_url para un
        # objeto público: https://storage.googleapis.com/<bucket>/<object>.
        public_url = f"https://storage.googleapis.com/{bucket_name}/{_OBJECT_PATH}"
        return MissionExecutionResult(
            mission_id=mission_candidate.mission_id,
            bucket_name=bucket_name,
            object_path=_OBJECT_PATH,
            public_url=public_url,
            executed_at=timestamp,
            result_hash=result_hash,
            status=STATUS_EXECUTED,
            failure_reason=None,
        )


__all__ = (
    "STATUS_EXECUTED",
    "STATUS_FAILED",
    "MissionExecutorError",
    "AllowDecision",
    "authorize_allow_decision",
    "MissionExecutionResult",
    "MissionExecutor",
)
