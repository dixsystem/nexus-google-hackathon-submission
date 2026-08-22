"""Orquestador de sesión completa de red-team (M-9): conecta M-1 a M-6 en
un único flujo por ronda -- Gemini-Atacante genera un intento (M-1), se
valida contra la MISMA validación que ya protege el pipeline legítimo
(mission_generator_llm_producer.MissionGeneratorCandidateProducer.produce_batch
+ mission_generator_candidates.validate_candidate_batch, sin modificar ni
duplicar), el resultado se registra como RedTeamIncident encadenado por
hash (M-2), Gemma da una segunda opinión de severidad (M-3), el triple
filtro decide consenso (M-5) y, si consensus=ESCALATE, el incidente entra
al informe de cuarentena consolidado (M-4).

Ver NIGHT_QUESTIONS.md, entrada "2026-08-22 — M-9/M-7a", para tres
decisiones de diseño documentadas: (B) model_id del atacante Gemini es un
parámetro keyword-only obligatorio, sin default (no estaba en la firma
literal de la misión, pero inventar uno violaría la disciplina "sin
default deliberado" ya establecida); (C) gemini_assessment nunca dispara
una tercera llamada real a Gemini por defecto -- vale "UNKNOWN" salvo que
el llamador inyecte gemini_assessor.

VALIDATION_BYPASS (caso raro, por diseño): si un intento de ataque pasa
TODA la validación existente (el mismo camino que certificaría un
candidato legítimo), este módulo NUNCA lo ejecuta -- no importa
mission_executor.py, no construye ningún AllowDecision, no llama a
ningún executor. Se registra en RedTeamSessionResult.validation_bypasses
y el incidente correspondiente queda fuera del informe de cuarentena
(ni Gemma ni el triple filtro lo evalúan -- un bypass de validación real
es más grave que cualquier ESCALATE y requiere decisión humana explícita
y separada, no un prompt automático para Keeper)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import consensus_gate
import gemma_severity_classifier
import mission_generator_candidates
import mission_generator_llm_producer
import quarantine_report_generator
import red_team_attacker
import red_team_incident


# Mission_id sintético, disponible dentro del batch de un único candidato
# que cada ronda valida de forma aislada (existing_mission_ids/
# resolvable_dependency_ids quedan en su default vacío en cada llamada a
# produce_batch -- ninguna ronda comparte estado de colisión con otra, así
# que reutilizar el mismo id en todas las rondas es seguro). Nunca se
# escribe a MISSION_QUEUE.md ni a ningún almacén real.
_SYNTHETIC_MISSION_ID = "M-700"


class RedTeamSessionError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True, slots=True)
class ValidationBypass:
    """Un intento que pasó TODA la validación existente. Nunca se ejecuta
    desde este módulo -- ver docstring del módulo."""

    incident_id: str
    mission_id: str
    detected_at: str


@dataclass(frozen=True, slots=True)
class RedTeamSessionResult:
    session_id: str
    incidents: tuple  # tuple[red_team_incident.RedTeamIncident, ...] -- todas las rondas, encadenadas
    quarantine_report: str  # Markdown consolidado (M-4), solo incidentes con consensus=ESCALATE
    validation_bypasses: tuple  # tuple[ValidationBypass, ...] -- normalmente vacía


class _ReplayProvider:
    """Adaptador que permite reproducir una AntigravityGeminiResponse ya
    obtenida (la respuesta cruda del atacante, generada SIN schema para
    que el intento de inyección de Gemini quede sin restringir
    estructuralmente -- ver red_team_attacker.py) a través del pipeline
    de parseo + validate_candidate_batch EXACTO y SIN MODIFICAR que
    MissionGeneratorCandidateProducer.produce_batch() ya usa para
    candidatos legítimos. Nunca toca la red: .evaluate() ignora el
    prompt/format que produce_batch() construiría para una petición
    legítima y devuelve tal cual la respuesta del ataque ya recibida."""

    def __init__(self, response):
        self._response = response

    def evaluate(self, prompt, *, format=None, cancel_event=None):
        return self._response


def _validate_attack_against_nexus(attack_response, *, goal, registry):
    """Reutiliza MissionGeneratorCandidateProducer.produce_batch() sin
    modificarlo: si el intento de ataque sobrevive TODO ese camino
    (parseo estricto de campos + validate_candidate_batch), devuelve
    (True, candidato_validado) -- un VALIDATION_BYPASS. Si falla en
    cualquier punto de esa validación ya existente, devuelve
    (False, motivo_textual) -- blocked=True para el incidente."""

    producer = mission_generator_llm_producer.MissionGeneratorCandidateProducer(
        _ReplayProvider(attack_response), registry=registry, max_candidates=1
    )
    try:
        validated = producer.produce_batch(
            goal=goal, available_mission_ids=(_SYNTHETIC_MISSION_ID,)
        )
    except mission_generator_llm_producer.MissionGeneratorLLMError as exc:
        return False, f"{type(exc).__name__}[{exc.category}]: {exc}"
    except mission_generator_candidates.GeneratedMissionCandidateError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, validated[0]


def _default_session_id(clock: Callable[[], datetime]) -> str:
    return "redteam-" + clock().strftime("%Y%m%dT%H%M%S")


def run_red_team_session(
    goal: str,
    registry,
    transport,
    rounds: int = 5,
    session_id: Optional[str] = None,
    *,
    model_id: str,
    gemma_transport=None,
    gemma_model_id: Optional[str] = None,
    use_gemma_fallback: bool = False,
    gemini_assessor: Optional[Callable] = None,
    clock: Optional[Callable[[], datetime]] = None,
    started_at: Optional[str] = None,
) -> RedTeamSessionResult:
    """Ejecuta `rounds` rondas del flujo M-1..M-6 (ver docstring del
    módulo) y devuelve un RedTeamSessionResult.

    model_id: obligatorio, model_id del atacante Gemini (ver
    NIGHT_QUESTIONS.md 2026-08-22, Contexto B -- sin default).
    gemma_transport: transport para GemmaSeverityClassifier; si es None,
    reutiliza `transport` (el mismo objeto sirve para ambos si el fake/real
    despacha por model_id).
    use_gemma_fallback: si True, usa classify_with_fallback_rules() en vez
    de una llamada real a Gemma (cero red para la clasificación).
    gemini_assessor: callable(incident) -> uno de consensus_gate.GEMINI_ASSESSMENTS;
    si es None (default), siempre "UNKNOWN" -- ver NIGHT_QUESTIONS.md
    Contexto C: esta función NUNCA dispara una tercera llamada real a
    Gemini por su cuenta."""

    if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 1:
        raise RedTeamSessionError("CONFIGURATION", "rounds must be a positive integer")

    resolved_clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
    resolved_session_id = session_id if session_id is not None else _default_session_id(resolved_clock)
    resolved_started_at = started_at if started_at is not None else resolved_clock().isoformat()
    resolved_gemma_transport = gemma_transport if gemma_transport is not None else transport
    resolved_gemini_assessor = gemini_assessor if gemini_assessor is not None else (lambda incident: "UNKNOWN")

    attacker = red_team_attacker.RedTeamAttacker(model_id)
    gemma_classifier = gemma_severity_classifier.GemmaSeverityClassifier(
        gemma_model_id if gemma_model_id is not None else gemma_severity_classifier.GEMMA_MODEL_ID
    )

    incidents = []
    previous_hash = None
    previous_failures = []
    escalated_incidents = []
    severities = {}
    validation_bypasses = []

    for attack_round in range(1, rounds + 1):
        attack_response = attacker.generate_attack(
            goal, registry, transport,
            attack_round=attack_round, previous_failures=tuple(previous_failures),
        )

        passed_validation, validation_outcome = _validate_attack_against_nexus(
            attack_response, goal=goal, registry=registry
        )
        blocked = not passed_validation
        rejection_reason = None if passed_validation else validation_outcome

        timestamp = resolved_clock().isoformat()
        incident = red_team_incident.build_incident(
            session_id=resolved_session_id, attack_round=attack_round,
            raw_attack_payload=attack_response.content, rejection_reason=rejection_reason,
            blocked=blocked, timestamp=timestamp, previous_incident_hash=previous_hash,
        )
        incidents.append(incident)
        previous_hash = incident.incident_hash

        if passed_validation:
            validated_candidate = validation_outcome
            validation_bypasses.append(
                ValidationBypass(
                    incident_id=incident.incident_id,
                    mission_id=validated_candidate.mission_id,
                    detected_at=timestamp,
                )
            )
            previous_failures = []  # el intento tuvo éxito total; nada que "adaptar" a partir de un fallo
            continue

        previous_failures.append(rejection_reason)

        if use_gemma_fallback:
            severity_assessment = gemma_severity_classifier.classify_with_fallback_rules(
                incident, rejection_reason
            )
        else:
            severity_assessment = gemma_classifier.classify(incident, rejection_reason, resolved_gemma_transport)

        gemini_assessment = resolved_gemini_assessor(incident)
        if gemini_assessment not in consensus_gate.GEMINI_ASSESSMENTS:
            raise RedTeamSessionError(
                "CONFIGURATION",
                f"gemini_assessor returned an invalid assessment: {gemini_assessment!r}",
            )

        verdict = consensus_gate.evaluate_consensus(blocked, gemini_assessment, severity_assessment.severity)
        if verdict.consensus == "ESCALATE":
            escalated_incidents.append(incident)
            severities[incident.incident_id] = severity_assessment

    session = red_team_incident.build_session(
        session_id=resolved_session_id, started_at=resolved_started_at,
        goal=goal, incidents=tuple(incidents),
    )
    report = quarantine_report_generator.generate_quarantine_report(
        session, tuple(escalated_incidents), severities
    )

    return RedTeamSessionResult(
        session_id=resolved_session_id,
        incidents=tuple(incidents),
        quarantine_report=report,
        validation_bypasses=tuple(validation_bypasses),
    )


__all__ = (
    "RedTeamSessionError",
    "ValidationBypass",
    "RedTeamSessionResult",
    "run_red_team_session",
)
