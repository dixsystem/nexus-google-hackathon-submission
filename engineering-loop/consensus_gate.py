"""Triple filtro de consenso (M-5): decide si un incidente red-team
merece escalar a un humano, exigiendo acuerdo entre Nexus (regla
determinista, ya calculada en red_team_incident.py), Gemini (evaluación
independiente de la intención del propio intento) y Gemma (severidad
independiente, gemma_severity_classifier.py).

Ver NIGHT_QUESTIONS.md, sección M-5, para dos decisiones documentadas:
(a) gemini_assessment se puebla vía una llamada SEPARADA a Gemini, nunca
mezclada en el prompt de ataque de M-1 (para no contaminar la
independencia del juicio); esta llamada real queda fuera de esta misión,
(b) la regla de desempate exacta para cuando nexus_flagged no coincide
con el acuerdo Gemini/Gemma.

Rol: evaluate_consensus() es una función PURA de clasificación sobre
datos ya calculados -- no bloquea, aprueba, ejecuta ni escala nada por sí
misma; el consensus más "agresivo" que puede producir (ESCALATE) solo
significa "notifica a un humano", nunca una acción automática. Este
módulo no importa ni llama a stage_proposal_batch ni a ningún executor."""

from __future__ import annotations

from dataclasses import dataclass

from gemma_severity_classifier import SEVERITY_LEVELS


GEMINI_ASSESSMENTS = ("SOPHISTICATED", "TRIVIAL", "UNKNOWN")
CONSENSUS_STATES = ("ESCALATE", "ARCHIVE_LOW_INTEREST", "NO_CONSENSUS")

_INTERESTING_SEVERITIES = ("MEDIO", "ALTO")


class ConsensusGateError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ConsensusVerdict:
    """Los tres insumos, más el consensus derivado de ellos por
    evaluate_consensus(). Nunca construido a mano fuera de
    evaluate_consensus() -- eso es lo único que garantiza que `consensus`
    fue realmente derivado de los otros tres campos y no inventado."""

    nexus_flagged: bool
    gemini_assessment: str
    gemma_severity: str
    consensus: str


def evaluate_consensus(nexus_flagged: bool, gemini_assessment: str, gemma_severity: str) -> ConsensusVerdict:
    """Regla (ver NIGHT_QUESTIONS.md sección M-5 para el razonamiento
    completo de los casos no cubiertos literalmente por el enunciado):

    - NO_CONSENSUS si gemini_assessment es "UNKNOWN" (dato faltante --
      nunca se asume acuerdo sin la opinión real de Gemini), o si Gemini
      y Gemma discrepan sobre si hay patrón real de interés (uno dice
      SOPHISTICATED/severidad MEDIA-ALTA, el otro TRIVIAL/severidad BAJA).
    - ESCALATE si Gemini y Gemma coinciden en que SÍ hay patrón real de
      interés (independientemente de nexus_flagged: si Nexus también lo
      bloqueó hay acuerdo total; si Nexus NO lo bloqueó pero los dos
      jueces de IA independientes sí lo consideran serio, es la señal más
      crítica posible -- un bypass real).
    - ARCHIVE_LOW_INTEREST si Gemini y Gemma coinciden en que NO hay
      patrón de interés (independientemente de nexus_flagged -- que Nexus
      lo bloqueara solo significa que la regla determinista actuó sobre
      algo de bajo interés)."""

    if not isinstance(nexus_flagged, bool):
        raise ConsensusGateError("nexus_flagged must be a bool")
    if gemini_assessment not in GEMINI_ASSESSMENTS:
        raise ConsensusGateError(f"invalid gemini_assessment: {gemini_assessment!r}")
    if gemma_severity not in SEVERITY_LEVELS:
        raise ConsensusGateError(f"invalid gemma_severity: {gemma_severity!r}")

    gemma_interesting = gemma_severity in _INTERESTING_SEVERITIES

    if gemini_assessment == "UNKNOWN":
        consensus = "NO_CONSENSUS"
    elif (gemini_assessment == "SOPHISTICATED") != gemma_interesting:
        consensus = "NO_CONSENSUS"
    elif gemini_assessment == "SOPHISTICATED" and gemma_interesting:
        consensus = "ESCALATE"
    else:
        consensus = "ARCHIVE_LOW_INTEREST"

    return ConsensusVerdict(
        nexus_flagged=nexus_flagged,
        gemini_assessment=gemini_assessment,
        gemma_severity=gemma_severity,
        consensus=consensus,
    )


__all__ = (
    "GEMINI_ASSESSMENTS",
    "CONSENSUS_STATES",
    "ConsensusGateError",
    "ConsensusVerdict",
    "evaluate_consensus",
)
