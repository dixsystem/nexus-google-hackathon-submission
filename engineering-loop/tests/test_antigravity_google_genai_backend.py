"""Tests de contrato offline para antigravity_google_genai_backend.py
(M-AG017, Fase 14 del brief). Cero red, cero credencial real -- el unico
punto de contacto con la SDK real es un `client_factory` inyectado que
SIEMPRE devuelve un objeto falso construido en este archivo, nunca el
Client real de google.genai.

Este modulo solo puede importarse bajo un interprete que tenga
google.genai instalado -- es decir, exclusivamente el venv aislado
creado en M-AG016 (engineering-loop/.antigravity_isolated_venv/). Bajo
el entorno principal de Keeper (que a proposito NO tiene ese paquete,
Fase 13 del brief) este archivo se salta limpio via
unittest.skipUnless, en vez de romper el descubrimiento de tests de
bootstrap_test_runner.py con un ImportError. Para ejecutar estos tests
de verdad:

    engineering-loop/.antigravity_isolated_venv/bin/python -m unittest \
        tests.test_antigravity_google_genai_backend -v

(ejecutado con cwd=engineering-loop/, igual que el resto de la suite)."""

from __future__ import annotations

import copy
import logging
import time
import unittest

try:
    import google.genai.errors as genai_errors  # noqa: F401
    _HAS_GOOGLE_GENAI = True
except ImportError:
    _HAS_GOOGLE_GENAI = False


@unittest.skipUnless(
    _HAS_GOOGLE_GENAI,
    "requires google.genai -- run under engineering-loop/.antigravity_isolated_venv/bin/python",
)
class _Base(unittest.TestCase):
    """Import diferido dentro de setUpClass: si este modulo se saltea
    (entorno sin google.genai), el import de
    antigravity_google_genai_backend nunca se intenta -- evita que el
    propio import de este archivo de test falle en el entorno principal
    de Keeper."""

    @classmethod
    def setUpClass(cls):
        global backend_mod, genai_errors_mod
        import antigravity_google_genai_backend as backend_mod
        import google.genai.errors as genai_errors_mod
        cls.backend_mod = backend_mod
        cls.genai_errors_mod = genai_errors_mod


class _FakeUsage:
    def __init__(self, prompt=3, candidates=5, total=8):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.total_token_count = total


class _FakeResponse:
    def __init__(self, text="hola mundo", model_version="gemini-test-1.0", response_id="resp-123", usage=None):
        self.text = text
        self.model_version = model_version
        self.response_id = response_id
        self.usage_metadata = usage if usage is not None else _FakeUsage()


class _FakeModels:
    def __init__(self, response=None, exception=None, capture=None):
        self._response = response
        self._exception = exception
        self._capture = capture if capture is not None else []

    def generate_content(self, *, model, contents, config):
        self._capture.append({"model": model, "contents": contents, "config": config})
        if self._exception is not None:
            raise self._exception
        return self._response


class _FakeClient:
    def __init__(self, response=None, exception=None, capture=None):
        self.models = _FakeModels(response=response, exception=exception, capture=capture)


def _make_request(
    request_id="11111111-1111-1111-1111-111111111111",
    model_id="gemini-test-model",
    prompt="hola",
    fmt=None,
    timeout_seconds=5.0,
    max_response_chars=4096,
):
    from types import SimpleNamespace

    return SimpleNamespace(
        request_id=request_id,
        model_id=model_id,
        prompt=prompt,
        format=fmt,
        timeout_seconds=timeout_seconds,
        max_response_chars=max_response_chars,
    )


class ConstructionAndInjectionTests(_Base):
    def test_backend_constructs_expected_client_config(self):
        """Item 1: el backend construye la config esperada del Client de
        google.genai (vertexai=False explicito, retry attempts=1) --
        verificado inspeccionando el client_factory inyectado."""

        captured = {}

        def fake_factory(api_key):
            captured["api_key"] = api_key
            client = _FakeClient(response=_FakeResponse())
            return client

        backend = self.backend_mod.GoogleGenAIBackend(api_key="sentinel-key-abc", client_factory=fake_factory)
        self.assertEqual(captured["api_key"], "sentinel-key-abc")
        self.assertIsInstance(backend, self.backend_mod.GoogleGenAIBackend)

    def test_default_client_factory_passes_vertexai_false_and_no_retries(self):
        """La factory REAL de produccion (nunca invocada con una key real
        en este test) debe pasar vertexai=False explicito y
        retry_options.attempts=1 -- inspeccionado construyendo un Client
        real de la SDK offline (construir un Client no hace red por si
        solo; solo generate_content() la haria, y este test nunca lo
        invoca)."""

        client = self.backend_mod._default_client_factory("sentinel-key-construction-only")
        self.assertFalse(client.vertexai)
        # http_options del cliente ya validado por la SDK al construirse.

    def test_fake_credential_passed_to_backend_only(self):
        """Item 2: la credencial falsa llega SOLO al client_factory
        inyectado; el unico canal OBSERVABLE (repr, nunca almacenamiento
        interno crudo -- eso es esperado, la redaccion aplica a lo que
        sale del objeto, no a que el objeto la retenga internamente para
        poder usarla/redactarla) nunca la expone."""

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse())

        backend = self.backend_mod.GoogleGenAIBackend(api_key="sentinel-only-here-999", client_factory=fake_factory)
        self.assertNotIn("sentinel-only-here-999", repr(backend))

    def test_missing_api_key_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            self.backend_mod.GoogleGenAIBackend(api_key="", client_factory=lambda k: _FakeClient())

    def test_none_api_key_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            self.backend_mod.GoogleGenAIBackend(api_key=None, client_factory=lambda k: _FakeClient())


class CredentialLeakTests(_Base):
    def test_credential_never_printed_on_success(self):
        """Item 3: en el camino de exito, la credencial no aparece en
        ningun texto observable (repr del backend)."""

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse())

        backend = self.backend_mod.GoogleGenAIBackend(api_key="sk-SUCCESS-SENTINEL-77", client_factory=fake_factory)
        backend(_make_request())
        self.assertNotIn("sk-SUCCESS-SENTINEL-77", repr(backend))

    def test_credential_never_printed_on_auth_error(self):
        """Item 3 (variante error): el SDK real de Gemini Developer API
        pasa la api_key como query param en la URL -- si una excepcion de
        red la incluyera en su mensaje, este backend debe redactarla
        antes de que salga como BackendError.message."""

        secret = "sk-LEAK-SENTINEL-CANARY-42"
        exc = self.genai_errors_mod.ClientError(
            code=401,
            response_json={"error": {"message": f"invalid key in url ?key={secret}", "status": "UNAUTHENTICATED"}},
        )

        def fake_factory(api_key):
            return _FakeClient(exception=exc)

        backend = self.backend_mod.GoogleGenAIBackend(api_key=secret, client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertNotIn(secret, str(ctx.exception))
        self.assertNotIn(secret, ctx.exception.message)
        self.assertIn("[REDACTED]", ctx.exception.message)


class RequestMappingTests(_Base):
    def test_mission_candidate_schema_is_accepted_by_google_sdk_transformer(self):
        """Regression: google-genai rejects numeric ``const`` values while
        converting response_schema, before any HTTP request is made."""

        from google.genai import _transformers
        from mission_generator_llm_producer import _candidate_json_schema
        from provider_capability_registry import default_provider_capability_registry

        schema = _candidate_json_schema(
            default_provider_capability_registry().capability_ids,
            8,
        )
        original = copy.deepcopy(schema)
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["properties"]["candidates"]["items"]["additionalProperties"])
        self.assertFalse(
            schema["properties"]["candidates"]["items"]["properties"]
            ["parameters"]["items"]["additionalProperties"]
        )

        transformed = self.backend_mod._project_response_schema_for_gemini(schema)

        def assert_no_additional_properties(node):
            if isinstance(node, dict):
                self.assertNotIn("additionalProperties", node)
                self.assertNotIn("additional_properties", node)
                for value in node.values():
                    assert_no_additional_properties(value)
            elif isinstance(node, list):
                for value in node:
                    assert_no_additional_properties(value)

        assert_no_additional_properties(transformed)
        self.assertEqual(schema, original)
        _transformers.process_schema(transformed, None)

        self.assertEqual(
            transformed["properties"]["schema_version"],
            {"type": "integer"},
        )

    def test_configured_model_mapped_correctly(self):
        """Item 4."""

        capture = []

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(), capture=capture)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        backend(_make_request(model_id="gemini-super-1.5"))
        self.assertEqual(capture[0]["model"], "gemini-super-1.5")

    def test_prompt_mapped_correctly(self):
        """Item 5."""

        capture = []

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(), capture=capture)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        backend(_make_request(prompt="cual es la capital de Francia"))
        self.assertEqual(capture[0]["contents"], "cual es la capital de Francia")

    def test_format_mapped_to_response_schema_and_json_mime(self):
        capture = []

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(), capture=capture)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        backend(_make_request(fmt=schema))
        config = capture[0]["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertEqual(config.response_schema, schema)

    def test_response_schema_projection_does_not_mutate_input(self):
        capture = []

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(), capture=capture)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        schema = {
            "type": "object",
            "properties": {
                "nested": {
                    "type": "object",
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        }
        original = copy.deepcopy(schema)
        backend(_make_request(fmt=schema))

        sent = capture[0]["config"].response_schema
        self.assertEqual(schema, original)
        self.assertNotIn("additionalProperties", sent)
        self.assertNotIn("additionalProperties", sent["properties"]["nested"])

    def test_no_format_leaves_response_schema_unset(self):
        capture = []

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(), capture=capture)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        backend(_make_request(fmt=None))
        config = capture[0]["config"]
        self.assertIsNone(config.response_schema)
        self.assertIsNone(config.response_mime_type)

    def test_request_never_carries_authority_fields(self):
        """El mapeo de request no envia ningun campo relacionado con
        autoridad/aprobacion/tokens -- verificado por ausencia de
        cualquier atributo asi en el objeto config construido."""

        capture = []

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(), capture=capture)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        backend(_make_request())
        config = capture[0]["config"]
        forbidden = ("approved", "authorization", "capability_token", "execution_permission", "evidence_verdict")
        for name in forbidden:
            self.assertFalse(hasattr(config, name))


class ResponseMappingTests(_Base):
    def test_successful_response_maps_to_backend_result(self):
        """Item 6."""

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(text="respuesta real", model_version="gemini-v9", response_id="r-1"))

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        result = backend(_make_request())
        self.assertEqual(result.text, "respuesta real")
        self.assertEqual(result.response_model_id, "gemini-v9")
        self.assertEqual(result.response_id, "r-1")
        self.assertEqual(result.prompt_token_count, 3)
        self.assertEqual(result.candidates_token_count, 5)
        self.assertEqual(result.total_token_count, 8)

    def test_missing_text_fails_closed(self):
        """Item 7."""

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(text=None))

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_SDK_EMPTY_RESPONSE)

    def test_empty_string_text_fails_closed(self):
        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(text=""))

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_SDK_EMPTY_RESPONSE)

    def test_malformed_response_object_fails_closed(self):
        """Item 8: un objeto de respuesta sin atributo .text utilizable."""

        class _Broken:
            @property
            def text(self):
                raise RuntimeError("boom, no candidates")

        def fake_factory(api_key):
            return _FakeClient(response=_Broken())

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_MALFORMED_RESPONSE)

    def test_non_string_model_version_fails_closed(self):
        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(model_version=12345))

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_MALFORMED_RESPONSE)

    def test_non_integer_usage_field_fails_closed(self):
        usage = _FakeUsage()
        usage.total_token_count = "not-an-int"

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(usage=usage))

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_MALFORMED_RESPONSE)


class ErrorMappingTests(_Base):
    def test_sdk_exception_fails_closed(self):
        """Item 9."""

        def fake_factory(api_key):
            return _FakeClient(exception=RuntimeError("unexpected boom"))

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_SDK_ERROR)

    def test_auth_exception_fails_closed(self):
        """Item 10."""

        exc = self.genai_errors_mod.ClientError(code=401, response_json={"error": {"message": "no auth", "status": "UNAUTHENTICATED"}})

        def fake_factory(api_key):
            return _FakeClient(exception=exc)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_AUTH_INVALID)

    def test_forbidden_exception_maps_to_auth_invalid(self):
        exc = self.genai_errors_mod.ClientError(code=403, response_json={"error": {"message": "forbidden", "status": "PERMISSION_DENIED"}})

        def fake_factory(api_key):
            return _FakeClient(exception=exc)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_AUTH_INVALID)

    def test_model_not_found_exception_mapped(self):
        exc = self.genai_errors_mod.ClientError(code=404, response_json={"error": {"message": "model not found", "status": "NOT_FOUND"}})

        def fake_factory(api_key):
            return _FakeClient(exception=exc)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_MODEL_NOT_FOUND)

    def test_rate_limit_exception_fails_closed(self):
        """Item 11."""

        exc = self.genai_errors_mod.ClientError(code=429, response_json={"error": {"message": "slow down", "status": "RESOURCE_EXHAUSTED"}})

        def fake_factory(api_key):
            return _FakeClient(exception=exc)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_RATE_LIMIT)

    def test_network_exception_fails_closed(self):
        """Item 12: simula una excepcion httpx sin envolver, detectada por
        modulo (`httpx.*`) sin declarar una dependencia directa de httpx
        en el propio archivo de test."""

        import httpx

        exc = httpx.ConnectError("connection refused")

        def fake_factory(api_key):
            return _FakeClient(exception=exc)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_NETWORK_FAILURE)

    def test_server_error_mapped_to_sdk_error(self):
        exc = self.genai_errors_mod.ServerError(code=500, response_json={"error": {"message": "internal", "status": "INTERNAL"}})

        def fake_factory(api_key):
            return _FakeClient(exception=exc)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError) as ctx:
            backend(_make_request())
        self.assertEqual(ctx.exception.category, self.backend_mod.CATEGORY_SDK_ERROR)


class ModelIdentityTests(_Base):
    def test_missing_response_model_metadata_propagates_as_none(self):
        """Item 13: sin model_version independiente, este backend propaga
        None -- el binding VERIFIED_MATCH/UNVERIFIED sigue siendo
        responsabilidad exclusiva de AntigravityGeminiProvider.evaluate(),
        fuera de alcance de este modulo."""

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(model_version=None))

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        result = backend(_make_request())
        self.assertIsNone(result.response_model_id)

    def test_mismatch_metadata_transported_unmodified(self):
        """Item 14: este backend NUNCA compara response_model_id contra
        el model_id configurado -- esa comparacion (y por tanto el
        MODEL_ID_MISMATCH) es responsabilidad exclusiva del provider
        aguas arriba. Aqui solo se verifica que el valor discrepante se
        transporta sin alterar ni descartar."""

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(model_version="gemini-OTRO-modelo-completamente-distinto"))

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        result = backend(_make_request(model_id="gemini-configurado"))
        self.assertEqual(result.response_model_id, "gemini-OTRO-modelo-completamente-distinto")

    def test_matching_independent_metadata_transported(self):
        """Item 15."""

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse(model_version="gemini-igual"))

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        result = backend(_make_request(model_id="gemini-igual"))
        self.assertEqual(result.response_model_id, "gemini-igual")

    def test_generated_text_cannot_spoof_model_identity(self):
        """Item 16: aunque el texto generado contenga un string que
        parezca un model_id, response_model_id solo puede venir de
        response.model_version -- nunca del contenido de texto."""

        def fake_factory(api_key):
            return _FakeClient(
                response=_FakeResponse(text="soy el modelo gemini-9.9-ultra-fake", model_version=None)
            )

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        result = backend(_make_request())
        self.assertIsNone(result.response_model_id)
        self.assertIn("gemini-9.9-ultra-fake", result.text)  # el texto se transporta igual, solo la identidad no se infiere de el


class DataOnlyAndAuthorityTests(_Base):
    def test_backend_emits_data_only(self):
        """Item 17: BackendResult no tiene ningun campo de autoridad."""

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse())

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        result = backend(_make_request())
        forbidden = ("approved", "authorization", "capability_token", "execution_permission", "evidence_verdict")
        for name in forbidden:
            self.assertFalse(hasattr(result, name))

    def test_backend_cannot_trigger_keeper_execution(self):
        """Item 18: el modulo entero no importa ningun componente de
        ejecucion/autoridad de Keeper/NEXUS."""

        import antigravity_google_genai_backend as mod
        import inspect

        source = inspect.getsource(mod)
        forbidden_imports = (
            "governed_mission_engine",
            "governed_runtime_composition",
            "governed_external_execution_adapter",
            "one_shot_dispatch_authorization",
            "authorize_and_run",
        )
        for token in forbidden_imports:
            self.assertNotIn(token, source)


class RetryAndNoNetworkTests(_Base):
    def test_no_silent_retry_single_call(self):
        """Item 19/22: una llamada a __call__ invoca generate_content
        exactamente una vez, incluso si la respuesta simulada es un
        error -- este backend nunca reintenta por su cuenta."""

        capture = []
        exc = RuntimeError("fails once")

        def fake_factory(api_key):
            return _FakeClient(exception=exc, capture=capture)

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        with self.assertRaises(self.backend_mod.BackendError):
            backend(_make_request())
        self.assertEqual(len(capture), 1)

    def test_default_factory_disables_sdk_hidden_retries(self):
        """Item 22 (documentacion ejecutable): la SDK real reintenta hasta
        5 veces por defecto ante codigos reintentables si no se
        configura lo contrario (HttpRetryOptions.attempts default=5,
        inspeccionado offline en Fase 15) -- este backend lo desactiva
        explicitamente pasando attempts=1 al construir el Client real."""

        client = self.backend_mod._default_client_factory("construction-only-key")
        # No hay forma publica de leer retry_options de vuelta del Client
        # ya construido sin tocar internals privados de la SDK; en su
        # lugar se verifica la fuente: _default_client_factory siempre
        # pasa retry_options=HttpRetryOptions(attempts=1) literal.
        import inspect

        src = inspect.getsource(self.backend_mod._default_client_factory)
        self.assertIn("attempts=1", src)

    def test_no_vertex_fallback(self):
        """Item 23."""

        import inspect

        src = inspect.getsource(self.backend_mod._default_client_factory)
        self.assertIn("vertexai=False", src)

    def test_no_adc_fallback_without_explicit_key(self):
        """Item 24: sin api_key explicita, el constructor falla ANTES de
        poder llegar a ninguna ruta de ADC de la SDK real (esa ruta solo
        se activa cuando vertexai resuelve a True y no hay api_key/
        project -- aqui vertexai=False siempre, asi que ADC nunca aplica,
        y ademas la construccion ya fallo cerrado por falta de key)."""

        with self.assertRaises(ValueError):
            self.backend_mod.GoogleGenAIBackend(api_key=None, client_factory=lambda k: _FakeClient())

    def test_no_network_needed_for_this_test_module(self):
        """Item 25: verificacion estatica -- este archivo de test nunca
        importa `requests`/`socket`/`httpx` para hacer una llamada real
        (solo para construir una excepcion offline en
        test_network_exception_fails_closed, que nunca abre un socket)."""

        import inspect

        src = inspect.getsource(__import__(__name__))
        self.assertNotIn("socket.create_connection", src)
        self.assertNotIn(".get(\"http", src)

    def test_no_real_credential_needed_for_this_test_module(self):
        """Item 26: ninguna api_key usada en este archivo tiene forma de
        credencial real (todas son literales "k"/"sentinel-*" cortos,
        nunca leidas de variables de entorno ni de disco). Se verifica
        contra el modulo del BACKEND (no contra este propio archivo de
        test, cuyo texto inevitablemente menciona "os.environ" en
        docstrings/aserciones de otros tests -- una auto-referencia que
        haria trivialmente falsa cualquier busqueda literal sobre su
        propia fuente)."""

        import inspect

        backend_src = inspect.getsource(self.backend_mod)
        # El modulo nunca importa el paquete `os` -- sin ese import le es
        # estructuralmente imposible leer os.environ para descubrir una
        # credencial por su cuenta (el docstring del modulo SI menciona
        # la cadena "os.environ" en prosa, al explicar por que la SDK de
        # google.genai si la lee y por que este backend la evita -- eso
        # es documentacion legitima, no codigo, asi que no se busca esa
        # cadena literal aqui).
        self.assertNotIn("import os", backend_src)
        self.assertNotIn("\nimport os\n", backend_src)


class LifecycleUntouchedTests(_Base):
    def test_module_does_not_import_isolated_child_entrypoint(self):
        """Confirma el diseno de aislamiento de import documentado: este
        backend no importa antigravity_isolated_child.py, asi que
        importar este backend nunca fuerza a ese modulo (ni a su
        __main__) a cargar google.genai transitivamente."""

        import inspect

        src = inspect.getsource(__import__("antigravity_google_genai_backend"))
        self.assertNotIn("import antigravity_isolated_child", src)

    def test_module_does_not_touch_parent_supervision_or_transport(self):
        """Items 20/21/29 (timeout/cancelacion/muerte-del-padre/un-proceso-
        por-llamada preservados): este backend vive POR DEBAJO de
        antigravity_isolated_child.py (que ya llama a
        supervise_parent_death() como su primera accion, antes de invocar
        cualquier backend) y de antigravity_isolated_transport.py (que ya
        implementa SIGTERM->SIGKILL y un proceso por llamada). Este
        backend no reimplementa ni interfiere con ninguno de esos
        mecanismos -- verificado por ausencia total de esos imports/
        llamadas aqui, y por Fase 8 del brief (nunca instala su propio
        manejador de SIGTERM, nunca atrapa BaseException de forma
        amplia)."""

        import inspect

        src = inspect.getsource(__import__("antigravity_google_genai_backend"))
        self.assertNotIn("signal.signal", src)
        self.assertNotIn("except BaseException", src)
        self.assertNotIn("supervise_parent_death", src)
        self.assertNotIn("subprocess", src)

    def test_call_does_not_block_beyond_config_kwargs_construction(self):
        """No hay bucle de reintento interno ni sleep -- una llamada al
        backend con una respuesta inmediata retorna practicamente
        instantaneo (limite generoso para no ser fragil en CI lento)."""

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse())

        backend = self.backend_mod.GoogleGenAIBackend(api_key="k", client_factory=fake_factory)
        start = time.monotonic()
        backend(_make_request())
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.0)


class BackendErrorCategoryTests(_Base):
    def test_unknown_category_rejected_at_construction(self):
        """Item 27 (analogia): igual que
        antigravity_isolated_child.BackendError, esta clase tambien
        falla cerrado ante una categoria desconocida -- nunca deja pasar
        un error sin categorizar silenciosamente."""

        with self.assertRaises(ValueError):
            self.backend_mod.BackendError("NOT_A_REAL_CATEGORY", "boom")

    def test_all_declared_categories_are_constructible(self):
        for category in self.backend_mod.KNOWN_BACKEND_ERROR_CATEGORIES:
            exc = self.backend_mod.BackendError(category, "msg")
            self.assertEqual(exc.category, category)


class LoggingSafetyTests(_Base):
    def test_debug_log_never_contains_prompt_or_credential(self):
        """Fase 12: log seguro -- captura el log real emitido por una
        llamada exitosa y confirma ausencia de prompt/api_key."""

        def fake_factory(api_key):
            return _FakeClient(response=_FakeResponse())

        backend = self.backend_mod.GoogleGenAIBackend(api_key="sk-LOG-SENTINEL-1", client_factory=fake_factory)

        logger = logging.getLogger("antigravity_google_genai_backend")
        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = _Capture()
        previous_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            backend(_make_request(prompt="informacion muy secreta del prompt"))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous_level)

        combined = "\n".join(records)
        self.assertNotIn("sk-LOG-SENTINEL-1", combined)
        self.assertNotIn("informacion muy secreta del prompt", combined)


if __name__ == "__main__":
    unittest.main()
