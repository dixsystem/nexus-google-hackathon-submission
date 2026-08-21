"""Genera el informe de cuarentena de una sesión red-team (M-4): resumen,
tabla de incidentes con severidad, y un prompt de corrección concreto
para Keeper por cada incidente de severidad MEDIA o ALTA.

Rol: PURO formateo de texto. No hace ninguna llamada a IA (el prompt de
corrección se arma con templates de texto sobre datos que M-2/M-3 ya
calcularon) y no escribe a disco por su cuenta -- devuelve un string
Markdown; el llamador decide si lo persiste. No importa ni llama a
stage_proposal_batch ni a ningún executor: es un informe de solo
lectura, nunca una vía de aplicación de cambios."""

from __future__ import annotations

import red_team_incident as incident_module
from gemma_severity_classifier import SEVERITY_LEVELS, SeverityAssessment


NO_ACTION_TAKEN_LINE = (
    "Nexus no ha aplicado ningún cambio. Esta recomendación requiere "
    "aprobación humana antes de activarse."
)

_ESCALATION_SEVERITIES = ("MEDIO", "ALTO")
_MAX_PAYLOAD_PREVIEW_CHARS = 800


class QuarantineReportError(Exception):
    pass


def _cell(text: str) -> str:
    return str(text).replace("\n", " ").replace("|", "\\|").strip()


def _require_severity_for(incident, severities):
    assessment = severities.get(incident.incident_id)
    if type(assessment) is not SeverityAssessment:
        raise QuarantineReportError(
            f"missing or invalid severity assessment for incident_id={incident.incident_id!r}"
        )
    return assessment


def _build_keeper_prompt(incident, assessment: SeverityAssessment) -> str:
    """Prompt de corrección concreto, construido por template (sin nueva
    llamada a IA) a partir de lo que ya se sabe del incidente: si hubo
    rejection_reason, pide reforzar esa regla; si no lo hubo, pide
    investigar por qué el payload no fue rechazado."""

    payload_preview = incident.raw_attack_payload[:_MAX_PAYLOAD_PREVIEW_CHARS]
    if incident.rejection_reason is not None:
        return (
            f"Revisa el validador que bloqueó el incidente {incident.incident_id} "
            f"(regla que actuó: {incident.rejection_reason}). Evaluación "
            f"independiente de severidad: {assessment.severity} -- "
            f"{assessment.justification} Refuerza esa regla o añade un test de "
            f"regresión que cubra explícitamente este vector de ataque (ronda "
            f"{incident.attack_round}). Payload que disparó el bloqueo:\n"
            f"{payload_preview}"
        )
    return (
        f"El incidente {incident.incident_id} NO fue bloqueado por ningún "
        f"validador existente. Evaluación independiente de severidad: "
        f"{assessment.severity} -- {assessment.justification} Investiga si el "
        f"siguiente payload (ronda {incident.attack_round}) debería haber sido "
        f"rechazado y, si es así, añade la regla de validación correspondiente "
        f"antes de aprobar cualquier cambio relacionado:\n{payload_preview}"
    )


def _render_summary_table(incidents, severities) -> list:
    lines = [
        "| Incidente | Ronda | Bloqueado | Regla que lo detuvo | Severidad |",
        "|---|---:|---|---|---|",
    ]
    for incident in incidents:
        assessment = _require_severity_for(incident, severities)
        lines.append(
            "| `{incident_id}` | {round} | {blocked} | {rule} | {severity} |".format(
                incident_id=incident.incident_id,
                round=incident.attack_round,
                blocked="Sí" if incident.blocked else "No",
                rule=_cell(incident.rejection_reason or "Ninguna"),
                severity=assessment.severity,
            )
        )
    return lines


def generate_quarantine_report(session, incidents, severities) -> str:
    """session: RedTeamSession (para el encabezado: session_id/goal/
    started_at). incidents: tupla/lista de RedTeamIncident a incluir en
    este informe (no necesariamente session.incidents completo).
    severities: dict[incident_id -> SeverityAssessment], una entrada por
    cada incident en `incidents` (falta una entrada -> QuarantineReportError,
    fail-closed en vez de omitir un incidente en silencio)."""

    if type(session) is not incident_module.RedTeamSession:
        raise QuarantineReportError("exact RedTeamSession required")
    incidents = tuple(incidents)
    for incident in incidents:
        if type(incident) is not incident_module.RedTeamIncident:
            raise QuarantineReportError("exact RedTeamIncident required in incidents")
    if type(severities) is not dict:
        raise QuarantineReportError("severities must be a dict keyed by incident_id")

    lines = [
        "# NEXUS RED TEAM — INFORME DE CUARENTENA",
        "",
        f"**Sesión:** `{session.session_id}`",
        f"**Objetivo declarado:** {session.goal}",
        f"**Iniciada:** {session.started_at}",
        f"**Incidentes en este informe:** {len(incidents)}",
        "",
        f"**Estado:** {NO_ACTION_TAKEN_LINE}",
        "",
        "## Resumen de incidentes",
        "",
    ]
    lines.extend(_render_summary_table(incidents, severities))
    lines.append("")

    escalation_worthy = [
        (incident, _require_severity_for(incident, severities))
        for incident in incidents
        if _require_severity_for(incident, severities).severity in _ESCALATION_SEVERITIES
    ]

    if escalation_worthy:
        lines.append("## Recomendaciones de corrección (severidad MEDIA/ALTA)")
        lines.append("")
        for incident, assessment in escalation_worthy:
            lines.append(f"### `{incident.incident_id}` — severidad {assessment.severity}")
            lines.append("")
            lines.append(f"**Justificación de severidad:** {assessment.justification}")
            lines.append("")
            lines.append("**PROMPT PARA KEEPER**")
            lines.append("")
            lines.append("```")
            lines.append(_build_keeper_prompt(incident, assessment))
            lines.append("```")
            lines.append("")

    lines.append(f"**{NO_ACTION_TAKEN_LINE}**")
    lines.append("")
    return "\n".join(lines)


__all__ = (
    "NO_ACTION_TAKEN_LINE",
    "QuarantineReportError",
    "generate_quarantine_report",
)
