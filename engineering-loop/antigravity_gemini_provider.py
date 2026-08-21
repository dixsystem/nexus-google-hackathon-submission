"""Frontera de proveedor local, sin tools de efecto, fail-closed para
Google Antigravity/Gemini (M-AG006, evidence/M-AG005 design report).

Rol arquitectónico (M-AG004/M-AG005): Antigravity/Gemini es un PRODUCTOR
DE PROPUESTAS NO CONFIABLE. Este módulo es la única superficie donde una
respuesta cruda del modelo se convierte en el objeto (provider_id,
model_id, content) que mission_generator_llm_producer.py ya espera de
cualquier proveedor -- exactamente el mismo protocolo duck-typed que hoy
satisface OllamaQwenProvider.evaluate(), sin que
MissionGeneratorCandidateProducer, mission_generator_candidates.py ni
ningún componente de autoridad/aprobación/ejecución corriente aguas abajo
necesiten cambiar una sola línea.

Lo que este módulo NO hace, a propósito:
- No parsea ni valida el JSON de mission-candidates -- eso sigue siendo
  trabajo exclusivo, sin modificar, de mission_generator_llm_producer.py
  (_parse_exact_json_object, _RESPONSE_FIELDS/_CANDIDATE_FIELDS con
  additionalProperties=False). Un intento del modelo de inyectar campos
  como "approved", "authorization_token", "capability_grant" o
  "evidence_verdict" dentro de ese JSON se rechaza ahí, por la misma
  frontera que ya protege al proveedor Ollama -- no se duplica aquí.
- No posee ni puede otorgar autoridad: el tipo de retorno de evaluate()
  no tiene ningún campo de aprobación/token/permiso/veredicto -- es
  estructuralmente irrepresentable, igual que en GeneratedMissionCandidateV1.
- No ejecuta nada: cero mutación de archivos, cero subprocess, cero red,
  cero MCP, cero invocación de authorize_and_run ni de ningún ejecutor
  de Keeper. Su trabajo termina al devolver texto.

Identidad del modelo (M-AG003 Fase 6/Fase 9, M-AG005 Fase 6 -- brecha de
evidencia ya señalada: la identidad del modelo NUNCA debe depender solo
de lo que el propio modelo afirma en su texto): la SDK real instalada
(google-genai 2.18.1, inspeccionada offline en
nexus-antigravity-lab/.venv) expone GenerateContentResponse.model_version
y .response_id como metadata reportada por el servidor, separada del
texto de la respuesta (types.py:8546 y :8554 de esa instalación). Este
módulo preserva esa metadata independiente cuando el transport la
suministra (model_identity_source="SDK_METADATA"); cuando no está
disponible, NO se fabrica una prueba de identidad -- se marca
"UNKNOWN" explícitamente. Nunca se deriva identidad del contenido de
texto generado por el propio modelo.

Sin transport por defecto, a propósito: este módulo no importa
google.genai ni google.antigravity en ningún punto, así que cargarlo no
requiere esos paquetes instalados en este entorno (no lo están fuera del
venv aislado del lab) ni dispara actividad de red al importarlo. El
constructor exige un transport explícito -- no hay transport real
implementado en esta misión (M-AG006 no autoriza ninguna invocación real
a Gemini); cablear un transport real que llame a google.genai.Client
queda diferido a una misión posterior con autorización humana explícita,
igual que ya hace default_producer() en mission_generator_llm_producer.py
para el tier Ollama ("No production caller invokes this yet").

Endurecimiento M-AG008 (auditoría independiente M-AG007, Fase 5/Fase de
timeout): dos brechas encontradas por M-AG007 y cerradas aquí, ambas sin
tocar mission_generator_llm_producer.py ni ningún otro componente aguas
abajo -- el fix vive enteramente en esta frontera:

1. Binding de identidad de modelo. Antes: response_model_id se
   capturaba pero era "dato muerto" -- nada lo comparaba contra
   config.model_id, así que un transport que reportara un modelo
   completamente distinto al configurado producía una respuesta
   indistinguible de una correcta. Ahora evaluate() compara
   explícitamente response_model_id contra config.model_id:
   coinciden -> model_identity_status="VERIFIED_MATCH"; falta
   response_model_id -> "UNVERIFIED" (la respuesta se sigue emitiendo,
   pero jamás reclama identidad verificada -- ver nota de reconciliación
   de fases más abajo); no coinciden -> FAIL CLOSED inmediato con
   AntigravityGeminiProviderError(category="MODEL_ID_MISMATCH") antes
   de construir cualquier respuesta -- nunca se emite una propuesta con
   identidad de modelo contradictoria.

   Nota de reconciliación (M-AG008 Fase 2 vs Fase 6 del brief de la
   misión): la Fase 6 lista "MODEL_ID_UNVERIFIED" junto a
   MODEL_ID_MISMATCH/TIMEOUT/CANCELLED/EXCEPTION como "estados de fallo
   estructurados ... que no deben convertirse en una propuesta válida".
   Pero la Fase 2, más específica y explícita, define el comportamiento
   exacto: ausencia de response_model_id -> status UNVERIFIED, "no
   reclames identidad verificada" -- no dice "falla cerrado". Se
   resuelve a favor de la Fase 2 (más detallada y con máquina de
   estados explícita) sin introducir una excepción para el caso
   UNVERIFIED: la propuesta se sigue emitiendo (así ya se comportaba
   M-AG006, con tests 14/15 que dependen de eso), simplemente el campo
   model_identity_status dice la verdad ("UNVERIFIED") en vez de
   fabricar una verificación que no ocurrió. Lo que sí es imposible
   ahora es que un UNVERIFIED se confunda con un VERIFIED_MATCH aguas
   abajo, porque son dos strings distintos y explícitos en la
   evidencia.

2. Enforcement real de timeout + resultado tardío estructuralmente
   inalcanzable. Antes: timeout_seconds se pasaba al transport como
   argumento, pero nada de este módulo lo hacía cumplir -- un transport
   que ignorara ese argumento podía colgar evaluate() indefinidamente.
   Ahora la llamada al transport corre en un hilo daemon dedicado a
   *esa* invocación de evaluate() (nunca reusado entre llamadas); el
   hilo principal espera como máximo timeout_seconds (con un poll
   corto que también vigila un cancel_event opcional) y, si el hilo no
   terminó a tiempo, evaluate() lanza TRANSPORT_TIMEOUT (o
   TRANSPORT_CANCELLED si el cancel_event se activó primero) y
   retorna/lanza inmediatamente -- sin esperar a que el hilo termine.
   El resultado tardío que ese hilo eventualmente produzca se escribe
   en un objeto _TransportOutcome que fue creado localmente para esa
   llamada y del que ya nadie vuelve a leer: no existe ningún estado
   compartido entre invocaciones de evaluate() que un hilo abandonado
   pudiera contaminar. Es una imposibilidad estructural (el marco de
   la función ya fue abandonado vía excepción), no solo una convención
   de no leerlo. Los hilos se crean con daemon=True deliberadamente
   para que un transport colgado no bloquee el cierre del intérprete
   (a diferencia de concurrent.futures.ThreadPoolExecutor, cuyos
   workers no son daemon y sí pueden bloquear la salida del proceso
   esperando a un hilo que nunca termina)."""

from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
from typing import Callable, Optional, Union

from antigravity_transport_protocol import CancellableTransport


PROVIDER_ID = "antigravity-gemini"

# Sin default deliberado: a diferencia de OllamaQwenConfig.DEFAULT_MODEL_ID,
# aquí no se hardcodea ninguna versión de Gemini como "la" correcta. M-000 y
# M-AG003 (Fase 7 de esta última) ya señalaron que una cadena de versión de
# modelo sin verificar, copiada de memoria o de un experimento previo, es
# exactamente el tipo de dato que este proveedor no debe inventar. Quien
# instancie AntigravityGeminiConfig debe suministrar model_id explícitamente.
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_GEMINI_FLASH_LATEST_ALIAS = "gemini-flash-latest"
_REPORTED_FLASH_MODEL_RE = re.compile(r"^gemini-[A-Za-z0-9][A-Za-z0-9._-]*-flash\Z")

# Categorías de fallo estructurado (M-AG008 Fase 6). MODEL_ID_MISMATCH,
# TRANSPORT_TIMEOUT, TRANSPORT_CANCELLED y TRANSPORT_EXCEPTION son estados
# que impiden que se emita ninguna propuesta -- evaluate() siempre lanza
# antes de construir una AntigravityGeminiResponse. TRANSPORT_CANCELLED
# requiere que el llamador pase un cancel_event explícito a evaluate();
# sin uno, ese camino simplemente no puede ocurrir (no hay cancelación
# implícita/automática).
CATEGORY_MODEL_ID_MISMATCH = "MODEL_ID_MISMATCH"
CATEGORY_TRANSPORT_TIMEOUT = "TRANSPORT_TIMEOUT"
CATEGORY_TRANSPORT_CANCELLED = "TRANSPORT_CANCELLED"
CATEGORY_TRANSPORT_EXCEPTION = "TRANSPORT_EXCEPTION"

# model_identity_status (M-AG008 Fase 2): valores del campo de evidencia,
# no categorías de excepción -- UNVERIFIED es un estado válido de una
# respuesta que SÍ se emite (ver nota de reconciliación en el docstring
# del módulo). MISMATCH nunca aparece aquí porque ese caso lanza
# MODEL_ID_MISMATCH antes de construir la respuesta.
MODEL_IDENTITY_VERIFIED_MATCH = "VERIFIED_MATCH"
MODEL_IDENTITY_UNVERIFIED = "UNVERIFIED"

_TIMEOUT_POLL_SECONDS = 0.01


def _model_ids_compatible(configured_model_id: str, reported_model_id: str) -> bool:
    """Accept exact identities, plus the one explicitly supported alias.

    Concrete configured IDs remain strict.  ``gemini-flash-latest`` may
    resolve only to a Google-reported ID in the exact ``gemini-*-flash``
    family; broader Gemini families and suffixes remain mismatches.
    """

    if reported_model_id == configured_model_id:
        return True
    return (
        configured_model_id == _GEMINI_FLASH_LATEST_ALIAS
        and _REPORTED_FLASH_MODEL_RE.fullmatch(reported_model_id) is not None
    )


class AntigravityGeminiProviderError(Exception):
    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


class _TransportOutcome:
    """Contenedor mutable local a UNA llamada de evaluate(). Nunca se
    comparte entre invocaciones ni se retiene tras retornar/lanzar --
    esa es la propiedad que hace estructuralmente imposible que un
    resultado tardío de un hilo abandonado contamine una llamada
    posterior (M-AG008 Fase 5)."""

    __slots__ = ("raw", "error")

    def __init__(self):
        self.raw = None
        self.error = None


@dataclass(frozen=True)
class AntigravityGeminiConfig:
    """Configuración SYSTEM_CONSTANT/CONFIGURATION -- nunca MODEL_SUPPLIED."""

    model_id: str
    timeout_seconds: float
    max_input_chars: int
    max_response_chars: int

    def validated(self) -> "AntigravityGeminiConfig":
        if type(self.model_id) is not str or not _MODEL_ID_RE.fullmatch(self.model_id):
            raise AntigravityGeminiProviderError("CONFIGURATION", "invalid Gemini model id")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 300:
            raise AntigravityGeminiProviderError("CONFIGURATION", "invalid timeout")
        if self.max_input_chars <= 0:
            raise AntigravityGeminiProviderError("CONFIGURATION", "invalid input limit")
        if self.max_response_chars <= 0:
            raise AntigravityGeminiProviderError("CONFIGURATION", "invalid response size limit")
        return self


@dataclass(frozen=True)
class RawGeminiResult:
    """Lo que un transport debe devolver: el sobre de respuesta de la SDK,
    sin parsear a nivel del JSON de mission-candidates. `text` es la salida
    cruda del modelo; el resto es metadata que la SDK reporta de forma
    independiente de ese texto (ver google.genai.types.GenerateContentResponse
    .model_version / .response_id / .usage_metadata), mantenida separada a
    propósito para que la evidencia de identidad nunca dependa de confiar en
    las palabras del propio modelo."""

    text: Optional[str]
    response_model_id: Optional[str]
    response_id: Optional[str]
    prompt_token_count: Optional[int]
    candidates_token_count: Optional[int]
    total_token_count: Optional[int]


@dataclass(frozen=True)
class AntigravityGeminiResponse:
    """Cumple exactamente el protocolo duck-typed que
    MissionGeneratorCandidateProducer.produce_batch() ya consume vía
    getattr(response, "provider_id"/"model_id"/"content") -- ningún llamador
    existente necesita cambiar."""

    provider_id: str
    model_id: str
    response_model_id: Optional[str]
    model_identity_source: str  # "SDK_METADATA" o "UNKNOWN" -- nunca "SELF_REPORTED"
    model_identity_status: str  # "VERIFIED_MATCH" o "UNVERIFIED" -- MISMATCH nunca llega aquí, falla cerrado antes
    content: str
    response_id: Optional[str]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    total_tokens: Optional[int]
    latency_ms: int
    attempts: int


# transport(model_id, prompt, format, timeout) -> RawGeminiResult
Transport = Callable[[str, str, Union[str, dict, None], float], RawGeminiResult]


def _optional_token_count(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AntigravityGeminiProviderError("PROTOCOL", "token usage must be non-negative integers")
    return value


def _optional_text(value: object, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise AntigravityGeminiProviderError("PROTOCOL", f"{field} must be a non-empty string or None")
    return value


class AntigravityGeminiProvider:
    """Frontera fail-closed: nunca recupera silenciosamente un valor
    malformado ni una identidad sensible a autoridad. transport es
    obligatorio -- ver nota de módulo sobre por qué no hay default real."""

    def __init__(
        self,
        config: AntigravityGeminiConfig,
        *,
        transport: Transport,
        clock: Callable[[], float] = time.monotonic,
    ):
        if transport is None:
            raise AntigravityGeminiProviderError(
                "CONFIGURATION",
                "transport must be supplied explicitly; this module ships no "
                "default live transport (no Gemini invocation is authorized "
                "by M-AG006)",
            )
        self.config = config.validated()
        self._transport = transport
        self._clock = clock

    def evaluate(
        self,
        prompt: str,
        *,
        format: Union[str, dict, None] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> AntigravityGeminiResponse:
        if not isinstance(prompt, str):
            raise AntigravityGeminiProviderError("CONFIGURATION", "prompt must be text")
        if len(prompt) > self.config.max_input_chars:
            raise AntigravityGeminiProviderError("INPUT_LIMIT", "prompt exceeds configured input limit")

        if format is not None:
            if isinstance(format, dict):
                if not format:
                    raise AntigravityGeminiProviderError(
                        "CONFIGURATION", "format schema must be a non-empty object"
                    )
            elif isinstance(format, str):
                raise AntigravityGeminiProviderError(
                    "CONFIGURATION",
                    "format must be a JSON schema object or None; this provider "
                    "always requests structured output, never the bare 'json' keyword",
                )
            else:
                raise AntigravityGeminiProviderError(
                    "CONFIGURATION", "format must be a JSON schema object or None"
                )

        start = self._clock()
        raw = self._run_transport_with_real_timeout(prompt, format, cancel_event)
        latency_ms = self._latency_since(start)

        if not isinstance(raw, RawGeminiResult):
            raise AntigravityGeminiProviderError("PROTOCOL", "transport must return a RawGeminiResult")

        if not isinstance(raw.text, str) or not raw.text:
            raise AntigravityGeminiProviderError("PROTOCOL", "Gemini response text is invalid")
        if len(raw.text) > self.config.max_response_chars:
            raise AntigravityGeminiProviderError("LIMIT", "Gemini response exceeds configured size limit")

        response_model_id = _optional_text(raw.response_model_id, "response_model_id")
        response_id = _optional_text(raw.response_id, "response_id")

        if response_model_id is None:
            model_identity_source = "UNKNOWN"
            model_identity_status = MODEL_IDENTITY_UNVERIFIED
        elif _model_ids_compatible(self.config.model_id, response_model_id):
            model_identity_source = "SDK_METADATA"
            model_identity_status = MODEL_IDENTITY_VERIFIED_MATCH
        else:
            # Fail closed: el transport reportó un modelo distinto al
            # configurado. Nunca se construye una respuesta para esto --
            # M-AG007 demostró que sin este chequeo, un mismatch producía
            # un generation_id de evidencia idéntico al de una corrida
            # legítima (ver docstring del módulo).
            raise AntigravityGeminiProviderError(
                CATEGORY_MODEL_ID_MISMATCH,
                f"transport reported response_model_id={response_model_id!r} "
                f"which does not match configured model_id={self.config.model_id!r}",
            )

        prompt_tokens = _optional_token_count(raw.prompt_token_count)
        completion_tokens = _optional_token_count(raw.candidates_token_count)
        total_tokens = _optional_token_count(raw.total_token_count)

        return AntigravityGeminiResponse(
            provider_id=PROVIDER_ID,
            model_id=self.config.model_id,
            response_model_id=response_model_id,
            model_identity_source=model_identity_source,
            model_identity_status=model_identity_status,
            content=raw.text,
            response_id=response_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            attempts=1,
        )

    def _latency_since(self, start: float) -> int:
        return round((self._clock() - start) * 1000)

    def _run_transport_with_real_timeout(
        self,
        prompt: str,
        format: Union[str, dict, None],
        cancel_event: Optional[threading.Event],
    ) -> RawGeminiResult:
        """Corre self._transport en un hilo daemon dedicado a esta llamada y
        hace cumplir config.timeout_seconds de verdad -- ver nota de
        endurecimiento M-AG008 en el docstring del módulo. outcome/done son
        locales a esta invocación: si hay timeout/cancelación, se lanza y se
        retorna sin esperar al hilo, y cualquier resultado que ese hilo
        escriba después ya no lo lee nadie."""

        outcome = _TransportOutcome()
        done = threading.Event()

        def _run():
            try:
                # M-AG010: si el transport inyectado declara soporte real de
                # cancel_event (isinstance() contra la ABC CancellableTransport
                # -- nunca hasattr()/duck-typing, que produciria un falso
                # positivo contra unittest.mock.Mock() y rompería los tests
                # existentes de M-AG006/M-AG008), se le propaga EL MISMO
                # cancel_event que este evaluate() recibio de su llamador, para
                # que una cancelacion externa llegue hasta la terminacion real
                # del recurso subyacente (p.ej. matar el proceso hijo aislado)
                # en vez de quedar atrapada esperando el deadline completo de
                # este hilo daemon. cancel_event es un parametro de esta
                # invocacion, nunca estado global/compartido entre llamadas.
                if isinstance(self._transport, CancellableTransport):
                    outcome.raw = self._transport.run(
                        self.config.model_id,
                        prompt,
                        format,
                        self.config.timeout_seconds,
                        cancel_event=cancel_event,
                    )
                else:
                    outcome.raw = self._transport(
                        self.config.model_id, prompt, format, self.config.timeout_seconds
                    )
            except BaseException as exc:  # noqa: BLE001 -- capturado para reenviar, nunca silenciado
                outcome.error = exc
            finally:
                done.set()

        worker = threading.Thread(target=_run, name="antigravity-gemini-transport", daemon=True)
        worker.start()

        deadline = time.monotonic() + self.config.timeout_seconds
        cancelled = False
        while True:
            if done.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            done.wait(min(remaining, _TIMEOUT_POLL_SECONDS))

        if done.is_set():
            if outcome.error is not None:
                if isinstance(outcome.error, AntigravityGeminiProviderError):
                    raise outcome.error
                raise AntigravityGeminiProviderError(
                    CATEGORY_TRANSPORT_EXCEPTION, "Gemini transport failed"
                ) from outcome.error
            return outcome.raw

        if cancelled:
            raise AntigravityGeminiProviderError(
                CATEGORY_TRANSPORT_CANCELLED, "Gemini transport call was cancelled before completion"
            )
        raise AntigravityGeminiProviderError(
            CATEGORY_TRANSPORT_TIMEOUT,
            f"Gemini transport exceeded {self.config.timeout_seconds}s timeout",
        )


__all__ = (
    "PROVIDER_ID",
    "AntigravityGeminiProviderError",
    "AntigravityGeminiConfig",
    "RawGeminiResult",
    "AntigravityGeminiResponse",
    "AntigravityGeminiProvider",
    "CATEGORY_MODEL_ID_MISMATCH",
    "CATEGORY_TRANSPORT_TIMEOUT",
    "CATEGORY_TRANSPORT_CANCELLED",
    "CATEGORY_TRANSPORT_EXCEPTION",
    "MODEL_IDENTITY_VERIFIED_MATCH",
    "MODEL_IDENTITY_UNVERIFIED",
)
