"""Validador offline del envelope JSON del boundary de transporte aislado
(M-AG009 design report: M_AG009_ISOLATED_TRANSPORT_BOUNDARY_DESIGN_V1.md,
Secciones D/E/N). NO es el transporte real -- no spawnea procesos, no
importa google.genai, no toca red. Es la única pieza de código que M-AG009
autoriza a escribir: la lógica de validación fail-closed del schema de
request/response que consumirá el cliente real de M-AG010, separada para
poder probarla offline hoy contra fixtures deterministas.

Invariante N.3 del diseño: este módulo deserializa exclusivamente con
json.loads() -- nunca pickle, nunca eval/exec sobre los bytes recibidos.
Verificable por ausencia de esos imports/llamadas en este archivo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional


SCHEMA_VERSION = 1
_REQUEST_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE
)

# error_category conocidas que el hijo puede reportar en ok:false
# (Sección D del diseño) -- cualquier otra cosa es MALFORMED_RESPONSE.
# Ampliado en M-AG017 (Fase 10 del brief) con las categorías que un
# backend real de google.genai puede reportar -- puramente aditivo,
# ninguna categoría existente se remueve ni cambia de significado, así
# que los 27 tests existentes de este módulo (M-AG009) siguen intactos.
_KNOWN_CHILD_ERROR_CATEGORIES = frozenset(
    {
        "SDK_EXCEPTION",
        "SDK_EMPTY_RESPONSE",
        "MALFORMED_REQUEST",
        "AUTH_MISSING",
        "AUTH_INVALID",
        "MODEL_NOT_FOUND",
        "RATE_LIMIT",
        "NETWORK_FAILURE",
        "SDK_ERROR",
        "MALFORMED_RESPONSE",
    }
)

# Categorías de fallo de ESTE boundary (Sección E de la matriz de fallos).
# Nunca se traducen a una propuesta válida -- el llamador (M-AG010) las
# mapea a las categorías ya existentes en antigravity_gemini_provider.py
# donde corresponda (p.ej. TRANSPORT_TIMEOUT ya existe ahí).
CATEGORY_MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
CATEGORY_RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
CATEGORY_CHILD_REPORTED_FAILURE = "CHILD_REPORTED_FAILURE"


class TransportEnvelopeError(Exception):
    """Fail-closed: cualquier desviación del schema exacto lanza esto.
    Nunca se recupera un envelope parcial ni se adivina un valor
    faltante."""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class ValidatedSuccessEnvelope:
    """Resultado de un envelope ok:true ya validado -- mapea 1:1 a los
    campos de RawGeminiResult (antigravity_gemini_provider.py:196-211),
    sin depender de esa clase para mantener este módulo independiente."""

    request_id: str
    text: str
    response_model_id: Optional[str]
    response_id: Optional[str]
    prompt_token_count: Optional[int]
    candidates_token_count: Optional[int]
    total_token_count: Optional[int]


@dataclass(frozen=True)
class ValidatedFailureEnvelope:
    """Resultado de un envelope ok:false ya validado."""

    request_id: str
    error_category: str
    error_message: str


def build_request_envelope(
    *,
    request_id: str,
    model_id: str,
    prompt: str,
    format: Optional[dict],
    timeout_seconds: float,
    max_response_chars: int,
) -> str:
    """Serializa un request saliente (padre -> hijo). No valida semántica
    de negocio (eso ya lo hace AntigravityGeminiConfig/Provider aguas
    arriba) -- solo construye el envelope de transporte con
    schema_version fijo."""

    if not _REQUEST_ID_RE.match(request_id):
        raise TransportEnvelopeError("CONFIGURATION", "request_id must be a UUID4 string")
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "model_id": model_id,
        "prompt": prompt,
        "format": format,
        "timeout_seconds": timeout_seconds,
        "max_response_chars": max_response_chars,
    }
    line = json.dumps(envelope, ensure_ascii=True, separators=(",", ":"))
    if len(line.encode("utf-8")) > 1_048_576:  # 1 MiB, límite duro (Sección D)
        raise TransportEnvelopeError("INPUT_LIMIT", "request envelope exceeds 1 MiB transport limit")
    return line + "\n"


def parse_response_envelope(
    raw_line: bytes,
    *,
    expected_request_id: str,
    max_response_bytes: int = 4 * 1024 * 1024,
):
    """Deserializa y valida estrictamente una línea de respuesta (hijo ->
    padre). Devuelve ValidatedSuccessEnvelope o ValidatedFailureEnvelope;
    nunca devuelve un dict crudo ni un objeto parcialmente poblado.
    Cualquier desviación del schema exacto -> TransportEnvelopeError.
    Deserialización EXCLUSIVAMENTE vía json.loads (invariante N.3)."""

    if not isinstance(raw_line, (bytes, bytearray)):
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, "response must be raw bytes")
    if len(raw_line) > max_response_bytes:
        raise TransportEnvelopeError(
            CATEGORY_RESPONSE_TOO_LARGE,
            f"response exceeds {max_response_bytes} byte transport limit",
        )
    if not raw_line.strip():
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, "empty response")

    try:
        text = raw_line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, "response is not valid UTF-8") from exc

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, "response is not valid JSON") from exc

    if not isinstance(obj, dict):
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, "response must be a JSON object")

    if obj.get("schema_version") != SCHEMA_VERSION:
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, "unknown or missing schema_version")

    request_id = obj.get("request_id")
    if not isinstance(request_id, str) or request_id != expected_request_id:
        # Nunca se acepta una respuesta que no declare el request_id exacto
        # que el padre generó y envió (Sección R18 / fila de la matriz de
        # fallos "request_id no coincide") -- protección contra una
        # respuesta de otra invocación filtrándose a esta.
        raise TransportEnvelopeError(
            CATEGORY_MALFORMED_RESPONSE, "response request_id does not match the request that was sent"
        )

    ok = obj.get("ok")
    if ok is True:
        return _validate_success_fields(obj, request_id)
    if ok is False:
        return _validate_failure_fields(obj, request_id)
    raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, "response 'ok' field must be true or false")


def _optional_str(obj: dict, field: str) -> Optional[str]:
    value = obj.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise TransportEnvelopeError(
            CATEGORY_MALFORMED_RESPONSE, f"{field} must be a non-empty string or null"
        )
    return value


def _optional_nonneg_int(obj: dict, field: str) -> Optional[int]:
    value = obj.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TransportEnvelopeError(
            CATEGORY_MALFORMED_RESPONSE, f"{field} must be a non-negative integer or null"
        )
    return value


def _validate_success_fields(obj: dict, request_id: str) -> ValidatedSuccessEnvelope:
    allowed = {
        "schema_version",
        "request_id",
        "ok",
        "text",
        "response_model_id",
        "response_id",
        "prompt_token_count",
        "candidates_token_count",
        "total_token_count",
    }
    unknown = set(obj) - allowed
    if unknown:
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, f"unknown fields in success envelope: {sorted(unknown)}")

    text = obj.get("text")
    if not isinstance(text, str) or not text:
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, "text must be a non-empty string")

    return ValidatedSuccessEnvelope(
        request_id=request_id,
        text=text,
        response_model_id=_optional_str(obj, "response_model_id"),
        response_id=_optional_str(obj, "response_id"),
        prompt_token_count=_optional_nonneg_int(obj, "prompt_token_count"),
        candidates_token_count=_optional_nonneg_int(obj, "candidates_token_count"),
        total_token_count=_optional_nonneg_int(obj, "total_token_count"),
    )


def _validate_failure_fields(obj: dict, request_id: str) -> ValidatedFailureEnvelope:
    allowed = {"schema_version", "request_id", "ok", "error_category", "error_message"}
    unknown = set(obj) - allowed
    if unknown:
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, f"unknown fields in failure envelope: {sorted(unknown)}")

    category = obj.get("error_category")
    if category not in _KNOWN_CHILD_ERROR_CATEGORIES:
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, f"unknown error_category: {category!r}")

    message = obj.get("error_message")
    if not isinstance(message, str):
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, "error_message must be a string")
    if len(message) > 4096:
        raise TransportEnvelopeError(CATEGORY_MALFORMED_RESPONSE, "error_message exceeds sanitization length limit")

    return ValidatedFailureEnvelope(request_id=request_id, error_category=category, error_message=message)


__all__ = (
    "SCHEMA_VERSION",
    "TransportEnvelopeError",
    "ValidatedSuccessEnvelope",
    "ValidatedFailureEnvelope",
    "CATEGORY_MALFORMED_RESPONSE",
    "CATEGORY_RESPONSE_TOO_LARGE",
    "CATEGORY_CHILD_REPORTED_FAILURE",
    "build_request_envelope",
    "parse_response_envelope",
)
