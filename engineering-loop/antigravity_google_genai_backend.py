"""Backend real de google.genai para el hijo aislado (M-AG017). Este es el
PRIMER modulo de engineering-loop/ que importa google.genai de verdad --
por diseno, solo puede cargarse con exito bajo el interprete Python del
venv aislado (engineering-loop/.antigravity_isolated_venv/, M-AG016), que
es el UNICO lugar del repo donde ese paquete esta instalado. El entorno
principal de Keeper no lo tiene ni lo necesita: ningun modulo de
produccion existente importa este archivo (verificable por grep), y
antigravity_isolated_child.py -- el unico caller legitimo de un Backend
real -- sigue sin importarlo (Fase invariante M-AG010/M-AG011: el backend
debe seguir siendo inyectado por el llamador de run_child(), nunca
instanciado incondicionalmente dentro de __main__). Cablear esta clase a
__main__ queda diferido a una mision futura con autorizacion humana
explicita para una llamada en vivo -- esta mision NO la otorga.

Rol: DATA PRODUCER ONLY. Esta clase nunca toca autoridad, aprobacion,
tokens de capability ni ningun ejecutor de Keeper -- su contrato de
entrada/salida es exactamente `Backend` (ChildRequest -> BackendResult o
BackendError), el mismo que ya usa make_offline_mock_backend() en
antigravity_isolated_child.py. No importa ni reexporta nada de ese modulo
(evita el ciclo de import que romperia la garantia de que
antigravity_isolated_child.py puede seguir importandose sin google.genai
instalado): los tipos ChildRequest/BackendResult/BackendError se
duplican aqui via un Protocol/dataclass estructuralmente compatibles y
se verifican por test de integracion cruzando ambos modulos bajo el
interprete del venv aislado.

Superficie de SDK expuesta: SOLO generate_content() de texto. Nunca:
files API, tool/code execution, caches, batch jobs, uploads, sesiones
persistentes, Vertex Agents, function calling. Verificado offline contra
la version instalada (google-genai 2.18.1, Fase 15 del brief): el unico
metodo invocado es google.genai.Client.models.generate_content().

Auth (Fase 3 del brief -- fuente de verdad: BaseApiClient.__init__ de la
SDK instalada, inspeccionado offline en esta misma mision): la SDK real
soporta descubrimiento AMBIENTE de credenciales via las variables de
entorno GOOGLE_API_KEY/GEMINI_API_KEY (get_env_api_key()) y, en modo
Vertex, via Application Default Credentials (load_auth()) cuando no se
pasa api_key/project explicitamente. Esta clase CONTROLA
deliberadamente ambas rutas:
  1. api_key es un parametro OBLIGATORIO del constructor -- ausente,
     None o cadena vacia -> ValueError inmediato, ANTES de construir
     ningun Client. Nunca se lee os.environ dentro de este modulo para
     descubrir una credencial por su cuenta.
  2. vertexai=False se pasa SIEMPRE explicito (nunca None) al
     construir el Client real -- bloquea que GOOGLE_GENAI_USE_VERTEXAI/
     GOOGLE_GENAI_USE_ENTERPRISE en el entorno del proceso hijo activen
     silenciosamente el modo Vertex/ADC. El modo previsto es
     exclusivamente Google AI Studio / Gemini Developer API con
     api_key explicito.
  3. El Client real solo se construye a traves de `client_factory`,
     inyectable en el constructor (default: la construccion real de
     genai.Client). Los tests SIEMPRE inyectan un client_factory falso
     -- cero red posible durante `python -m unittest` (Fase 4).

Reintentos (Fase 9 -- fuente de verdad: HttpRetryOptions, inspeccionada
offline en esta misma mision): la SDK real reintenta hasta 5 veces por
defecto ante codigos HTTP reintentables (408/429/5xx) si no se
configura lo contrario -- comportamiento oculto documentado, no
inventado. Esta clase lo desactiva explicitamente pasando
retry_options=HttpRetryOptions(attempts=1) (soportado oficialmente por
HttpOptions) al construir el Client real -- una peticion del padre =
un proceso hijo = una sola invocacion HTTP a la SDK, sin reintentos
silenciosos.

Redaccion de secretos (Fase 11/12): ninguna excepcion, mensaje de log o
BackendError construido por este modulo puede contener el valor
literal de api_key -- se aplica _redact_secret() a cualquier texto de
diagnostico antes de que salga de este modulo (cubre el caso real de
que la SDK de Gemini Developer API pasa la key como query param
`?key=...` de la URL, que puede terminar dentro del texto de una
excepcion de red/HTTP)."""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

import google.genai as genai
import google.genai.errors as genai_errors
import google.genai.types as genai_types

_logger = logging.getLogger(__name__)

# Mismas categorias de fallo estructurado que antigravity_isolated_child.py
# ya conoce (SDK_EXCEPTION/SDK_EMPTY_RESPONSE/MALFORMED_REQUEST) mas las
# nuevas que este backend introduce (Fase 10 del brief M-AG017). Este
# modulo nunca importa antigravity_isolated_child.py (evitaria el ciclo
# de import descrito en el docstring) -- BackendError aqui es una clase
# propia, estructuralmente compatible (misma forma: .category/.message),
# que el caller de run_child() (fuera de alcance de esta mision) debe
# traducir/re-lanzar como antigravity_isolated_child.BackendError si
# decide cablear este backend en produccion.
CATEGORY_AUTH_MISSING = "AUTH_MISSING"
CATEGORY_AUTH_INVALID = "AUTH_INVALID"
CATEGORY_MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
CATEGORY_RATE_LIMIT = "RATE_LIMIT"
CATEGORY_NETWORK_FAILURE = "NETWORK_FAILURE"
CATEGORY_SDK_ERROR = "SDK_ERROR"
CATEGORY_MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
CATEGORY_SDK_EMPTY_RESPONSE = "SDK_EMPTY_RESPONSE"

KNOWN_BACKEND_ERROR_CATEGORIES = frozenset(
    {
        CATEGORY_AUTH_MISSING,
        CATEGORY_AUTH_INVALID,
        CATEGORY_MODEL_NOT_FOUND,
        CATEGORY_RATE_LIMIT,
        CATEGORY_NETWORK_FAILURE,
        CATEGORY_SDK_ERROR,
        CATEGORY_MALFORMED_RESPONSE,
        CATEGORY_SDK_EMPTY_RESPONSE,
    }
)

_REDACTED = "[REDACTED]"


def _project_response_schema_for_gemini(schema: dict) -> dict:
    """Copy a canonical JSON Schema and remove only the unsupported
    additionalProperties keyword before sending it to Gemini Developer API.

    The canonical schema remains untouched and continues to govern validation
    after generation.  Both spellings denote the same SDK field: google-genai
    normalizes ``additionalProperties`` to ``additional_properties``.
    """

    projected = copy.deepcopy(schema)

    def _strip(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("additionalProperties", None)
            node.pop("additional_properties", None)
            for value in node.values():
                _strip(value)
        elif isinstance(node, list):
            for value in node:
                _strip(value)

    _strip(projected)
    return projected


class BackendError(Exception):
    """Estructuralmente identica a antigravity_isolated_child.BackendError
    (misma forma .category/.message) pero definida aqui de forma
    independiente para que este modulo -- el unico que importa
    google.genai -- nunca necesite importar antigravity_isolated_child.py
    (evita forzar ese import, y por tanto la disponibilidad de
    google.genai, sobre el entorno principal de Keeper)."""

    def __init__(self, category: str, message: str):
        if category not in KNOWN_BACKEND_ERROR_CATEGORIES:
            raise ValueError(f"unknown backend error category: {category!r}")
        super().__init__(message)
        self.category = category
        self.message = message


class SupportsChildRequest(Protocol):
    """Forma estructural minima que este backend necesita leer de un
    ChildRequest real (antigravity_isolated_child.ChildRequest ya cumple
    esto) -- Protocol en vez de importar esa clase, por la misma razon de
    aislamiento de import descrita en el docstring del modulo."""

    request_id: str
    model_id: str
    prompt: str
    format: Optional[dict]
    timeout_seconds: float
    max_response_chars: int


@dataclass(frozen=True)
class BackendResult:
    """Misma forma exacta que antigravity_isolated_child.BackendResult."""

    text: str
    response_model_id: Optional[str]
    response_id: Optional[str]
    prompt_token_count: Optional[int]
    candidates_token_count: Optional[int]
    total_token_count: Optional[int]


def _redact_secret(text: str, secret: Optional[str]) -> str:
    """Elimina cualquier ocurrencia literal de `secret` de `text` antes de
    que el texto pueda salir de este modulo (excepcion, log, BackendError).
    Nunca falla si secret es None/vacio -- simplemente no hay nada que
    redactar (eso solo puede ocurrir antes de que __init__ ya haya
    fallado cerrado por falta de api_key, asi que en la practica esta
    rama nunca se alcanza con secret vacio desde generate())."""

    if not secret:
        return text
    return text.replace(secret, _REDACTED)


def _default_client_factory(api_key: str) -> Any:
    """Construccion REAL de produccion del Client de google.genai -- nunca
    invocada durante los tests de este modulo (Fase 4), que siempre
    inyectan un client_factory falso. vertexai=False explicito (Fase 3):
    bloquea que variables de entorno del proceso hijo activen Vertex/ADC
    en silencio. retry_options=attempts=1 (Fase 9): desactiva el
    reintento oculto por defecto de la SDK (hasta 5 intentos) -- una
    llamada del padre debe producir como maximo UNA invocacion HTTP
    real."""

    return genai.Client(
        api_key=api_key,
        vertexai=False,
        http_options=genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(attempts=1)
        ),
    )


class GoogleGenAIBackend:
    """Backend inyectable, production-shaped, para un unico proceso hijo
    desechable (Backend = Callable[[ChildRequest], BackendResult]). NUNCA
    se auto-registra en __main__ de antigravity_isolated_child.py -- debe
    ser construido e inyectado explicitamente por el caller de
    run_child(), en una mision futura con autorizacion humana explicita
    para una llamada en vivo. Esta mision (M-AG017) solo la deja lista,
    probada offline con un client_factory falso."""

    def __init__(
        self,
        *,
        api_key: str,
        client_factory: Callable[[str], Any] = _default_client_factory,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(api_key, str) or not api_key:
            # Fail-closed ANTES de construir ningun Client -- este es el
            # unico punto donde AUTH_MISSING puede ocurrir en este
            # backend; nunca se llega a __call__() sin una api_key no
            # vacia, asi que __call__() nunca necesita volver a
            # comprobarlo ni reportarlo como fallo de tiempo de ejecucion.
            raise ValueError("api_key must be a non-empty string; no ambient credential discovery is performed")
        self._api_key = api_key
        self._clock = clock
        self._client = client_factory(api_key)

    def __repr__(self) -> str:
        # Nunca incluye api_key ni el objeto Client crudo (Fase 12).
        return f"{self.__class__.__name__}(api_key=[REDACTED])"

    def __call__(self, request: SupportsChildRequest) -> BackendResult:
        start = self._clock()
        try:
            response = self._generate(request)
        except BackendError:
            raise
        except genai_errors.ClientError as exc:
            category, message = self._classify_client_error(exc)
            self._log(request, "error", start, category)
            raise BackendError(category, _redact_secret(message, self._api_key)) from None
        except genai_errors.ServerError as exc:
            self._log(request, "error", start, CATEGORY_SDK_ERROR)
            raise BackendError(CATEGORY_SDK_ERROR, _redact_secret(str(exc), self._api_key)) from None
        except genai_errors.APIError as exc:
            self._log(request, "error", start, CATEGORY_SDK_ERROR)
            raise BackendError(CATEGORY_SDK_ERROR, _redact_secret(str(exc), self._api_key)) from None
        except Exception as exc:  # noqa: BLE001 -- cualquier excepcion no prevista se mapea, nunca crashea silenciosamente ni se re-intenta
            category = self._classify_generic_exception(exc)
            self._log(request, "error", start, category)
            raise BackendError(category, _redact_secret(str(exc), self._api_key)) from None

        result = self._extract_result(request, response)
        self._log(request, "ok", start, None)
        return result

    # -- internals -----------------------------------------------------

    def _generate(self, request: SupportsChildRequest):
        config_kwargs: dict = {
            "http_options": genai_types.HttpOptions(timeout=int(request.timeout_seconds * 1000)),
        }
        if request.format is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_schema"] = _project_response_schema_for_gemini(request.format)

        config = genai_types.GenerateContentConfig(**config_kwargs)
        return self._client.models.generate_content(
            model=request.model_id,
            contents=request.prompt,
            config=config,
        )

    def _extract_result(self, request: SupportsChildRequest, response: Any) -> BackendResult:
        # Nunca confia en que `response` tenga la forma esperada solo
        # porque no se lanzo una excepcion -- un client_factory de prueba
        # (o una version futura de la SDK) podria devolver cualquier cosa.
        try:
            text = response.text
        except Exception as exc:  # noqa: BLE001 -- acceder a .text puede lanzar si la respuesta esta mal formada
            raise BackendError(
                CATEGORY_MALFORMED_RESPONSE, f"could not extract response text: {exc}"
            ) from None

        if not isinstance(text, str) or not text:
            raise BackendError(CATEGORY_SDK_EMPTY_RESPONSE, "Gemini response contained no text")

        # Identidad de modelo (Fase 7): response_model_id SOLO viene de
        # metadata independiente reportada por la SDK (.model_version) --
        # nunca se deriva de request.model_id ni del propio texto
        # generado. Si la SDK no la expone, se propaga None sin fabricar
        # nada; el binding VERIFIED_MATCH/UNVERIFIED sigue siendo
        # responsabilidad exclusiva de AntigravityGeminiProvider.evaluate()
        # aguas arriba (invariante ya establecido, sin cambios aqui).
        response_model_id = getattr(response, "model_version", None)
        if response_model_id is not None and not isinstance(response_model_id, str):
            raise BackendError(
                CATEGORY_MALFORMED_RESPONSE, "response.model_version was present but not a string"
            )

        response_id = getattr(response, "response_id", None)
        if response_id is not None and not isinstance(response_id, str):
            raise BackendError(CATEGORY_MALFORMED_RESPONSE, "response.response_id was present but not a string")

        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = self._optional_int(usage, "prompt_token_count")
        candidates_tokens = self._optional_int(usage, "candidates_token_count")
        total_tokens = self._optional_int(usage, "total_token_count")

        return BackendResult(
            text=text,
            response_model_id=response_model_id,
            response_id=response_id,
            prompt_token_count=prompt_tokens,
            candidates_token_count=candidates_tokens,
            total_token_count=total_tokens,
        )

    @staticmethod
    def _optional_int(usage: Any, field: str) -> Optional[int]:
        if usage is None:
            return None
        value = getattr(usage, field, None)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise BackendError(CATEGORY_MALFORMED_RESPONSE, f"usage_metadata.{field} was not an integer")
        return value

    @staticmethod
    def _classify_client_error(exc: "genai_errors.ClientError") -> tuple:
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return CATEGORY_AUTH_INVALID, str(exc)
        if code == 404:
            return CATEGORY_MODEL_NOT_FOUND, str(exc)
        if code == 429:
            return CATEGORY_RATE_LIMIT, str(exc)
        return CATEGORY_SDK_ERROR, str(exc)

    @staticmethod
    def _classify_generic_exception(exc: BaseException) -> str:
        # Excepciones de red de httpx (usadas internamente por la SDK)
        # sin envolver en un APIError propio de google.genai -- se
        # detectan por nombre de modulo, sin declarar una dependencia
        # directa de httpx en este archivo (la SDK ya la trae; importarla
        # aqui solo para isinstance() ampliaria la superficie sin
        # necesidad real).
        module = type(exc).__module__
        if module.startswith("httpx"):
            return CATEGORY_NETWORK_FAILURE
        return CATEGORY_SDK_ERROR

    def _log(self, request: SupportsChildRequest, status: str, start: float, error_category: Optional[str]) -> None:
        # Fase 12: solo campos seguros. Nunca prompt/content, nunca el
        # objeto Client, nunca api_key.
        latency_ms = round((self._clock() - start) * 1000)
        _logger.debug(
            "antigravity google.genai backend call request_id=%s configured_model_id=%s "
            "status=%s latency_ms=%s error_category=%s",
            request.request_id,
            request.model_id,
            status,
            latency_ms,
            error_category,
        )


__all__ = (
    "GoogleGenAIBackend",
    "BackendResult",
    "BackendError",
    "SupportsChildRequest",
    "KNOWN_BACKEND_ERROR_CATEGORIES",
    "CATEGORY_AUTH_MISSING",
    "CATEGORY_AUTH_INVALID",
    "CATEGORY_MODEL_NOT_FOUND",
    "CATEGORY_RATE_LIMIT",
    "CATEGORY_NETWORK_FAILURE",
    "CATEGORY_SDK_ERROR",
    "CATEGORY_MALFORMED_RESPONSE",
    "CATEGORY_SDK_EMPTY_RESPONSE",
)
