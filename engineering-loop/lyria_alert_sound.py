"""Genera un clip corto de audio de alerta con Lyria cuando se confirma un
incidente ESCALATE (M-8) -- feature puramente decorativa: nunca debe
bloquear ni degradar la generación del informe de cuarentena
(quarantine_report_generator.py) si Lyria no está disponible o falla.

Nota de diseño (ver NIGHT_QUESTIONS.md, sección M-8): no hay ningún
precedente de Lyria en este repo (`grep -rni "lyria"` no encontró nada).
DEFAULT_LYRIA_MODEL_ID usa el nombre sugerido por la propia misión
("lyria-3-clip-preview") pero SIN VERIFICAR contra documentación en vivo
-- transport es siempre inyectado, nunca real por defecto (misma
disciplina "sin default real" que red_team_attacker.py /
gemma_severity_classifier.py). Si transport es None, esta función se
salta por completo y devuelve None -- ningún test ni ningún código de
este módulo invoca una API de Lyria real."""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Callable, Optional


_logger = logging.getLogger(__name__)

ALERT_SOUND_PROMPT = (
    "tense short electronic security alert sound, staccato, urgent, "
    "3 seconds, no vocals"
)
DEFAULT_LYRIA_MODEL_ID = "lyria-3-clip-preview"  # NO VERIFICADO -- ver NIGHT_QUESTIONS.md
DEFAULT_OUTPUT_DIR = Path("/tmp/lyria-alerts")

_SAFE_INCIDENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")

# transport(model_id, prompt) -> bytes (contenido del clip de audio)
Transport = Callable[[str, str], bytes]


class LyriaAlertSoundError(Exception):
    """Solo se lanza para errores de configuración del LLAMADOR (p.ej. un
    incident_id inválido para nombre de archivo) -- nunca para fallos de
    Lyria en sí, que siempre se degradan a None (ver generate_alert_sound)."""


def _require_safe_incident_id(incident_id) -> str:
    if not isinstance(incident_id, str) or _SAFE_INCIDENT_ID.fullmatch(incident_id) is None:
        raise LyriaAlertSoundError(
            "invalid incident_id for filename construction (no path separators allowed)"
        )
    return incident_id


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
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def generate_alert_sound(
    incident_id: str,
    *,
    transport: Optional[Transport] = None,
    model_id: str = DEFAULT_LYRIA_MODEL_ID,
    output_dir=DEFAULT_OUTPUT_DIR,
) -> Optional[Path]:
    """Best-effort: intenta generar /tmp/lyria-alerts/<incident_id>.mp3 con
    Lyria. Nunca lanza por indisponibilidad o fallo de Lyria -- devuelve
    None en su lugar y registra un warning; el llamador (p.ej. un futuro
    orquestador que arma el informe de cuarentena) debe seguir su flujo
    normal sin este archivo si no se pudo generar.

    incident_id inválido para nombre de archivo SÍ lanza
    LyriaAlertSoundError -- eso es un error de programación del llamador,
    no una indisponibilidad de Lyria, y merece fallar alto igual que el
    resto de este repo hace con datos de configuración malformados."""

    _require_safe_incident_id(incident_id)

    if transport is None:
        _logger.debug(
            "lyria alert sound skipped for incident_id=%s: no transport configured "
            "(best-effort, non-blocking)",
            incident_id,
        )
        return None

    try:
        audio_bytes = transport(model_id, ALERT_SOUND_PROMPT)
        if not isinstance(audio_bytes, (bytes, bytearray)) or not audio_bytes:
            raise LyriaAlertSoundError("transport returned no audio bytes")
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / f"{incident_id}.mp3"
        _atomic_write_bytes(destination, bytes(audio_bytes))
        return destination
    except Exception:  # noqa: BLE001 -- decorativo: cualquier fallo se degrada a None, nunca propaga
        _logger.warning(
            "lyria alert sound generation failed for incident_id=%s; continuing without it",
            incident_id, exc_info=True,
        )
        return None


__all__ = (
    "ALERT_SOUND_PROMPT",
    "DEFAULT_LYRIA_MODEL_ID",
    "DEFAULT_OUTPUT_DIR",
    "LyriaAlertSoundError",
    "generate_alert_sound",
)
