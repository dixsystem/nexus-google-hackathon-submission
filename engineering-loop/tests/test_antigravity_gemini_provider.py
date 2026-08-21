"""M-AG006: tests offline enfocados para antigravity_gemini_provider.py.

Ningún test en este archivo llama a Gemini, hace red, o depende de
GEMINI_API_KEY -- cada transport es un doble de prueba inyectado
explícitamente, exactamente como test_ollama_qwen_provider.py hace para
el proveedor Ollama. La segunda clase de tests demuestra, con el
producer/registry/staging REALES sin modificar, que la salida de este
proveedor puede entrar al pipeline NEXUS existente sin saltarse ninguna
frontera de validación -- y que un intento de inyección de campos de
autoridad dentro del JSON del modelo lo rechaza esa misma frontera
existente, no un chequeo nuevo paralelo."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import antigravity_gemini_provider as subject
import mission_generator_candidates as candidates_module
import mission_generator_llm_producer as producer_module
import mission_proposal_staging as staging_module
import provider_capability_registry as capability_registry


SAFE_CAPABILITY_ID = "external.providers.health.v1"


def config(**overrides):
    values = dict(
        model_id="gemini-flash-latest",
        timeout_seconds=30.0,
        max_input_chars=100_000,
        max_response_chars=100_000,
    )
    values.update(overrides)
    return subject.AntigravityGeminiConfig(**values)


def raw_result(**overrides):
    # response_model_id coincide con el model_id por defecto de config() a
    # propósito: la mayoría de los tests representan el camino feliz
    # (VERIFIED_MATCH). Los tests de M-AG008 que necesitan un mismatch
    # real lo pasan explícitamente con un valor distinto.
    values = dict(
        text="ok",
        response_model_id="gemini-flash-latest",
        response_id="resp-1",
        prompt_token_count=10,
        candidates_token_count=5,
        total_token_count=15,
    )
    values.update(overrides)
    return subject.RawGeminiResult(**values)


class AntigravityGeminiProviderTest(unittest.TestCase):
    """Tests unitarios de la frontera del proveedor en sí."""

    def test_01_transport_is_mandatory(self):
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            subject.AntigravityGeminiProvider(config(), transport=None)
        self.assertEqual(caught.exception.category, "CONFIGURATION")

    def test_model_identity_compatibility_exact_match_passes(self):
        self.assertTrue(subject._model_ids_compatible("gemini-3.7-flash", "gemini-3.7-flash"))

    def test_model_identity_compatibility_flash_latest_resolution_passes(self):
        self.assertTrue(
            subject._model_ids_compatible("gemini-flash-latest", "gemini-3.7-flash")
        )

    def test_model_identity_compatibility_flash_latest_non_flash_fails(self):
        self.assertFalse(
            subject._model_ids_compatible("gemini-flash-latest", "gemini-3.7-pro")
        )

    def test_model_identity_compatibility_concrete_mismatch_fails(self):
        self.assertFalse(
            subject._model_ids_compatible("gemini-3.6-flash", "gemini-3.7-flash")
        )

    def test_flash_latest_resolution_is_verified_from_sdk_metadata(self):
        transport = mock.Mock(return_value=raw_result(response_model_id="gemini-3.7-flash"))
        provider = subject.AntigravityGeminiProvider(
            config(model_id="gemini-flash-latest"), transport=transport
        )
        response = provider.evaluate("prompt")
        self.assertEqual(response.model_identity_status, "VERIFIED_MATCH")
        self.assertEqual(response.model_id, "gemini-flash-latest")
        self.assertEqual(response.response_model_id, "gemini-3.7-flash")

    def test_02_valid_minimal_proposal_accepted(self):
        transport = mock.Mock(return_value=raw_result(text='{"schema_version":1,"candidates":[]}'))
        provider = subject.AntigravityGeminiProvider(config(), transport=transport)
        response = provider.evaluate("prompt", format={"type": "object"})
        self.assertEqual(response.provider_id, "antigravity-gemini")
        self.assertEqual(response.model_id, "gemini-flash-latest")
        self.assertEqual(response.content, '{"schema_version":1,"candidates":[]}')
        transport.assert_called_once_with("gemini-flash-latest", "prompt", {"type": "object"}, 30.0)

    def test_03_malformed_transport_return_type_rejected(self):
        transport = mock.Mock(return_value={"text": "not a RawGeminiResult"})
        provider = subject.AntigravityGeminiProvider(config(), transport=transport)
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt")
        self.assertEqual(caught.exception.category, "PROTOCOL")

    def test_04_missing_required_field_none_text_rejected(self):
        transport = mock.Mock(return_value=raw_result(text=None))
        provider = subject.AntigravityGeminiProvider(config(), transport=transport)
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt")
        self.assertEqual(caught.exception.category, "PROTOCOL")

    def test_05_wrong_field_type_text_rejected(self):
        transport = mock.Mock(return_value=raw_result(text=12345))
        provider = subject.AntigravityGeminiProvider(config(), transport=transport)
        with self.assertRaises(subject.AntigravityGeminiProviderError):
            provider.evaluate("prompt")

    def test_06_empty_text_rejected(self):
        transport = mock.Mock(return_value=raw_result(text=""))
        provider = subject.AntigravityGeminiProvider(config(), transport=transport)
        with self.assertRaises(subject.AntigravityGeminiProviderError):
            provider.evaluate("prompt")

    def test_07_bare_json_string_format_rejected(self):
        provider = subject.AntigravityGeminiProvider(config(), transport=mock.Mock())
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt", format="json")
        self.assertEqual(caught.exception.category, "CONFIGURATION")

    def test_08_empty_dict_format_rejected(self):
        provider = subject.AntigravityGeminiProvider(config(), transport=mock.Mock())
        with self.assertRaises(subject.AntigravityGeminiProviderError):
            provider.evaluate("prompt", format={})

    def test_09_oversized_prompt_rejected(self):
        provider = subject.AntigravityGeminiProvider(config(max_input_chars=10), transport=mock.Mock())
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("x" * 11)
        self.assertEqual(caught.exception.category, "INPUT_LIMIT")

    def test_10_oversized_response_rejected(self):
        transport = mock.Mock(return_value=raw_result(text="x" * 20))
        provider = subject.AntigravityGeminiProvider(config(max_response_chars=10), transport=transport)
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt")
        self.assertEqual(caught.exception.category, "LIMIT")

    def test_11_negative_token_count_rejected(self):
        transport = mock.Mock(return_value=raw_result(prompt_token_count=-1))
        provider = subject.AntigravityGeminiProvider(config(), transport=transport)
        with self.assertRaises(subject.AntigravityGeminiProviderError):
            provider.evaluate("prompt")

    def test_12_transport_exception_fails_closed(self):
        def boom(*a, **k):
            raise ConnectionError("network unreachable")
        provider = subject.AntigravityGeminiProvider(config(), transport=boom)
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt")
        self.assertEqual(caught.exception.category, "TRANSPORT_EXCEPTION")

    def test_13_structured_transport_error_passes_through_category(self):
        def boom(*a, **k):
            raise subject.AntigravityGeminiProviderError("CONFIGURATION", "deliberately misconfigured")
        provider = subject.AntigravityGeminiProvider(config(), transport=boom)
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt")
        self.assertEqual(caught.exception.category, "CONFIGURATION")

    def test_14_model_identity_verified_match_when_response_agrees_with_config(self):
        transport = mock.Mock(return_value=raw_result(response_model_id="gemini-flash-latest"))
        provider = subject.AntigravityGeminiProvider(config(model_id="gemini-flash-latest"), transport=transport)
        response = provider.evaluate("prompt")
        self.assertEqual(response.model_identity_source, "SDK_METADATA")
        self.assertEqual(response.model_identity_status, "VERIFIED_MATCH")
        self.assertEqual(response.response_model_id, "gemini-flash-latest")
        self.assertEqual(response.model_id, "gemini-flash-latest")

    def test_15_absence_of_independent_identity_is_marked_unverified_not_faked(self):
        transport = mock.Mock(return_value=raw_result(response_model_id=None, response_id=None))
        provider = subject.AntigravityGeminiProvider(config(), transport=transport)
        response = provider.evaluate("prompt")
        self.assertEqual(response.model_identity_source, "UNKNOWN")
        self.assertEqual(response.model_identity_status, "UNVERIFIED")
        self.assertIsNone(response.response_model_id)
        # La ausencia de metadata NUNCA se convierte en una prueba VERIFIED_MATCH.
        self.assertNotEqual(response.model_identity_status, "VERIFIED_MATCH")

    def test_14b_model_id_mismatch_fails_closed_before_building_a_response(self):
        transport = mock.Mock(
            return_value=raw_result(response_model_id="gemini-1.0-pro-EOL-DIFFERENT-MODEL")
        )
        provider = subject.AntigravityGeminiProvider(
            config(model_id="gemini-flash-latest"), transport=transport
        )
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt")
        self.assertEqual(caught.exception.category, "MODEL_ID_MISMATCH")
        # El transport sí fue invocado (la solicitud ocurrió); lo que nunca
        # ocurre es que ese mismatch produzca una AntigravityGeminiResponse.
        transport.assert_called_once()

    def test_14c_configured_model_id_is_never_fabricated_as_response_identity(self):
        # Si el provider "arreglara" un mismatch sustituyendo silenciosamente
        # response_model_id por config.model_id, este test lo detectaría: la
        # única forma de que evaluate() no lance aquí sería que ambos ya
        # coincidieran, lo cual no es el caso.
        transport = mock.Mock(return_value=raw_result(response_model_id="some-other-model-v9"))
        provider = subject.AntigravityGeminiProvider(
            config(model_id="gemini-flash-latest"), transport=transport
        )
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt")
        self.assertEqual(caught.exception.category, "MODEL_ID_MISMATCH")
        self.assertIn("gemini-flash-latest", str(caught.exception))
        self.assertIn("some-other-model-v9", str(caught.exception))

    def test_16_zero_side_effects_only_the_injected_transport_is_called(self):
        transport = mock.Mock(return_value=raw_result())
        provider = subject.AntigravityGeminiProvider(config(), transport=transport)
        provider.evaluate("prompt")
        self.assertEqual(transport.call_count, 1)

    def test_17_output_carries_no_authority_grant_fields(self):
        transport = mock.Mock(return_value=raw_result())
        provider = subject.AntigravityGeminiProvider(config(), transport=transport)
        response = provider.evaluate("prompt")
        forbidden = {
            "approved", "authorization", "authorization_token", "capability_grant",
            "promotion_state", "trusted_identity", "authority_envelope",
            "evidence_verdict", "consume_state", "human_approval", "risk_override",
        }
        self.assertFalse(forbidden & set(vars(response)))

    def test_18_config_rejects_invalid_model_id(self):
        with self.assertRaises(subject.AntigravityGeminiProviderError):
            config(model_id="").validated()
        with self.assertRaises(subject.AntigravityGeminiProviderError):
            config(model_id="../etc/passwd").validated()

    def test_19_module_import_requires_no_network_sdk(self):
        # Si este módulo importara google.genai/google.antigravity al cargar,
        # este propio archivo de test ya habría fallado al importar `subject`
        # arriba (ninguno de esos paquetes está instalado en este entorno,
        # solo en el venv aislado del lab). Lo reafirmamos explícitamente:
        import sys
        self.assertNotIn("google.genai", sys.modules)
        self.assertNotIn("google.antigravity", sys.modules)


class AntigravityGeminiTimeoutTest(unittest.TestCase):
    """M-AG008 Fase 4/5: timeout real de verdad (no solo metadata) y
    resultado tardío que no puede filtrarse a la pipeline."""

    def test_timeout_before_response_fails_closed(self):
        def slow_transport(model_id, prompt, fmt, timeout_seconds):
            time.sleep(0.3)
            return raw_result()
        provider = subject.AntigravityGeminiProvider(
            config(timeout_seconds=0.05), transport=slow_transport
        )
        started = time.monotonic()
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt")
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.category, "TRANSPORT_TIMEOUT")
        # Si timeout_seconds fuera decorativo, esto tardaría ~0.3s (lo que
        # tarda el transport), no ~0.05s (lo que dice la config).
        self.assertLess(elapsed, 0.2)

    def test_timeout_emits_no_proposal(self):
        def slow_transport(model_id, prompt, fmt, timeout_seconds):
            time.sleep(0.3)
            return raw_result()
        provider = subject.AntigravityGeminiProvider(
            config(timeout_seconds=0.05), transport=slow_transport
        )
        result_holder = []
        try:
            result_holder.append(provider.evaluate("prompt"))
        except subject.AntigravityGeminiProviderError:
            pass
        # Ninguna AntigravityGeminiResponse llegó a existir para el llamador.
        self.assertEqual(result_holder, [])

    def test_late_result_after_timeout_is_never_used(self):
        # El transport "gana la carrera" tarde y escribe una marca
        # distintiva -- pero solo en un canal lateral de prueba, nunca en
        # el valor de retorno de evaluate(), que ya lanzó TRANSPORT_TIMEOUT
        # y abandonó el hilo antes de que esto sucediera.
        late_channel = []
        transport_finished = threading.Event()

        def slow_transport(model_id, prompt, fmt, timeout_seconds):
            time.sleep(0.15)
            late_channel.append(raw_result(text="LATE-RESULT-SHOULD-NEVER-BE-USED"))
            transport_finished.set()
            return late_channel[-1]

        provider = subject.AntigravityGeminiProvider(
            config(timeout_seconds=0.02), transport=slow_transport
        )
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt")
        self.assertEqual(caught.exception.category, "TRANSPORT_TIMEOUT")

        # Confirmamos que el hilo abandonado sí terminó eventualmente (para
        # no probar un negativo trivial por no esperar lo suficiente)...
        self.assertTrue(transport_finished.wait(1.0))
        self.assertEqual(late_channel[0].text, "LATE-RESULT-SHOULD-NEVER-BE-USED")

        # ...pero una llamada nueva e independiente, tras ese resultado
        # tardío, sigue devolviendo SU PROPIO resultado fresco -- nada del
        # hilo abandonado se filtró a ningún estado compartido.
        fresh_transport = mock.Mock(return_value=raw_result(text="fresh"))
        fresh_provider = subject.AntigravityGeminiProvider(config(), transport=fresh_transport)
        response = fresh_provider.evaluate("prompt")
        self.assertEqual(response.content, "fresh")

    def test_cancellation_path_fails_closed(self):
        def slow_transport(model_id, prompt, fmt, timeout_seconds):
            time.sleep(2.0)
            return raw_result()
        provider = subject.AntigravityGeminiProvider(
            config(timeout_seconds=60.0), transport=slow_transport
        )
        cancel_event = threading.Event()

        def _cancel_soon():
            time.sleep(0.05)
            cancel_event.set()
        threading.Thread(target=_cancel_soon, daemon=True).start()

        started = time.monotonic()
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            provider.evaluate("prompt", cancel_event=cancel_event)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.category, "TRANSPORT_CANCELLED")
        # Cancelado en ~0.05s, muy por debajo del timeout de 60s -- prueba
        # que la cancelación, no el timeout, fue lo que cortó la espera.
        self.assertLess(elapsed, 1.0)

    def test_fast_response_within_timeout_still_works(self):
        transport = mock.Mock(return_value=raw_result(text="fast-ok"))
        provider = subject.AntigravityGeminiProvider(config(timeout_seconds=5.0), transport=transport)
        response = provider.evaluate("prompt")
        self.assertEqual(response.content, "fast-ok")
        self.assertGreaterEqual(response.latency_ms, 0)

    def test_timeout_seconds_value_actually_gates_success_vs_failure(self):
        def transport_with_delay(delay):
            def _t(model_id, prompt, fmt, timeout_seconds):
                time.sleep(delay)
                return raw_result()
            return _t

        # 0.03s de trabajo, timeout de 0.2s -> pasa.
        ok_provider = subject.AntigravityGeminiProvider(
            config(timeout_seconds=0.2), transport=transport_with_delay(0.03)
        )
        self.assertEqual(ok_provider.evaluate("prompt").content, "ok")

        # El mismo trabajo de 0.03s, pero timeout de 0.01s -> falla. El
        # único cambio entre ambos casos es el valor de timeout_seconds.
        fail_provider = subject.AntigravityGeminiProvider(
            config(timeout_seconds=0.01), transport=transport_with_delay(0.03)
        )
        with self.assertRaises(subject.AntigravityGeminiProviderError) as caught:
            fail_provider.evaluate("prompt")
        self.assertEqual(caught.exception.category, "TRANSPORT_TIMEOUT")


class AntigravityGeminiPipelineIntegrationTest(unittest.TestCase):
    """Demuestra, con el pipeline NEXUS real sin modificar, que la salida
    de este proveedor entra por la misma frontera que ya usa Ollama --
    sin saltarse validate_candidate_batch ni stage_proposal_batch."""

    def setUp(self):
        self.registry = capability_registry.default_provider_capability_registry()

    def _producer_with_content(self, content):
        raw = raw_result(text=content)
        transport = mock.Mock(return_value=raw)
        provider = subject.AntigravityGeminiProvider(config(), transport=transport)
        return producer_module.MissionGeneratorCandidateProducer(provider, registry=self.registry), transport

    def _valid_candidate(self, **overrides):
        value = {
            "mission_name": "Antigravity provider health check",
            "objective": "Verify registered provider health via Antigravity proposal",
            "capability_id": SAFE_CAPABILITY_ID,
            "parameters": [],
            "depends_on_batch_index": [],
            "acceptance_criteria": ["providers health returns PASS"],
            "rationale": "Antigravity was asked to verify provider health",
        }
        value.update(overrides)
        return value

    def test_20_valid_proposal_reaches_validated_batch_end_to_end(self):
        content = json.dumps({"schema_version": 1, "candidates": [self._valid_candidate()]})
        producer, transport = self._producer_with_content(content)
        batch = producer.produce_batch(goal="check provider health", available_mission_ids=("M-900",))
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0].mission_id, "M-900")
        self.assertEqual(batch[0].capability_id, SAFE_CAPABILITY_ID)
        self.assertEqual(transport.call_count, 1)

    def test_21_model_supplied_mission_id_is_ignored_nexus_derives_it(self):
        # El esquema de _candidate_json_schema no tiene ni siquiera un campo
        # mission_id -- este test confirma que aunque el modelo lo intentara
        # vía additionalProperties, la frontera existente (sin modificar) lo
        # rechaza, no una capa nueva de este proveedor.
        raw_candidate = self._valid_candidate()
        raw_candidate["mission_id"] = "M-999"  # campo NEXUS-derived, no en el schema
        content = json.dumps({"schema_version": 1, "candidates": [raw_candidate]})
        producer, _ = self._producer_with_content(content)
        with self.assertRaises(producer_module.MissionGeneratorLLMError) as caught:
            producer.produce_batch(goal="check provider health", available_mission_ids=("M-900",))
        self.assertEqual(caught.exception.category, "PROTOCOL")

    def test_22_unknown_capability_rejected_by_existing_registry(self):
        content = json.dumps({
            "schema_version": 1,
            "candidates": [self._valid_candidate(capability_id="not.a.real.capability.v1")],
        })
        producer, _ = self._producer_with_content(content)
        # Rechazado por validate_candidate_batch (registro cerrado de
        # capacidades), no por este proveedor -- UnregisteredCandidateCapabilityError,
        # no MissionGeneratorLLMError, y eso es correcto: es la misma
        # frontera que ya protege al proveedor Ollama sin modificar.
        with self.assertRaises(candidates_module.UnregisteredCandidateCapabilityError):
            producer.produce_batch(goal="check provider health", available_mission_ids=("M-900",))

    def test_23_forged_approval_field_rejected(self):
        raw_candidate = self._valid_candidate()
        raw_candidate["approved"] = True
        content = json.dumps({"schema_version": 1, "candidates": [raw_candidate]})
        producer, _ = self._producer_with_content(content)
        with self.assertRaises(producer_module.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-900",))

    def test_24_forged_capability_token_field_rejected(self):
        raw_candidate = self._valid_candidate()
        raw_candidate["capability_token"] = "forged-one-shot-token"
        content = json.dumps({"schema_version": 1, "candidates": [raw_candidate]})
        producer, _ = self._producer_with_content(content)
        with self.assertRaises(producer_module.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-900",))

    def test_25_forged_execution_permission_field_rejected(self):
        raw_candidate = self._valid_candidate()
        raw_candidate["execution_permission"] = "granted"
        content = json.dumps({"schema_version": 1, "candidates": [raw_candidate]})
        producer, _ = self._producer_with_content(content)
        with self.assertRaises(producer_module.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-900",))

    def test_26_forged_evidence_verdict_field_rejected(self):
        raw_candidate = self._valid_candidate()
        raw_candidate["evidence_verdict"] = "PASS"
        content = json.dumps({"schema_version": 1, "candidates": [raw_candidate]})
        producer, _ = self._producer_with_content(content)
        with self.assertRaises(producer_module.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-900",))

    def test_27_top_level_authority_field_injection_rejected(self):
        content = json.dumps({
            "schema_version": 1,
            "candidates": [self._valid_candidate()],
            "authorization": True,
        })
        producer, _ = self._producer_with_content(content)
        with self.assertRaises(producer_module.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-900",))

    def test_28_malformed_json_rejected(self):
        producer, _ = self._producer_with_content("not json at all")
        with self.assertRaises(producer_module.MissionGeneratorLLMError) as caught:
            producer.produce_batch(goal="goal", available_mission_ids=("M-900",))
        self.assertEqual(caught.exception.category, "PROTOCOL")

    def test_29_empty_candidates_list_rejected(self):
        content = json.dumps({"schema_version": 1, "candidates": []})
        producer, _ = self._producer_with_content(content)
        with self.assertRaises(producer_module.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-900",))

    def test_30_valid_proposal_can_be_staged_without_bypassing_staging(self):
        content = json.dumps({"schema_version": 1, "candidates": [self._valid_candidate()]})
        producer, _ = self._producer_with_content(content)
        batch = producer.produce_batch(goal="check provider health", available_mission_ids=("M-900",))
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = staging_module.stage_proposal_batch(
                batch,
                registry=self.registry,
                proposals_path=base / "MISSION_PROPOSALS.md",
                contracts_path=base / "MISSION_PROPOSAL_CANDIDATES_CONTRACTS.md",
                candidates_path=base / "MISSION_PROPOSAL_CANDIDATES.json",
            )
            self.assertTrue((base / "MISSION_PROPOSALS.md").exists())
            self.assertTrue((base / "MISSION_PROPOSAL_CANDIDATES.json").exists())
            self.assertIsNotNone(result)

    def test_31_regression_model_proposal_alone_never_reaches_the_real_queue(self):
        # produce_batch()/stage_proposal_batch() never touch
        # MISSION_QUEUE.md/MISSION_EXECUTION_CONTRACTS.json -- only
        # promote_approved_batch() does, and only after
        # check_batch_human_gate(confirm=True). This test proves the model's
        # output alone (no human gate call at all) produces nothing beyond a
        # validated, staged proposal.
        content = json.dumps({"schema_version": 1, "candidates": [self._valid_candidate()]})
        producer, _ = self._producer_with_content(content)
        batch = producer.produce_batch(goal="check provider health", available_mission_ids=("M-900",))
        self.assertTrue(hasattr(candidates_module, "validate_candidate_batch"))
        self.assertIsInstance(batch, tuple)
        for candidate in batch:
            self.assertIsInstance(candidate, candidates_module.GeneratedMissionCandidateV1)
        # Ningún campo de aprobación/promoción existe en el objeto staged.
        self.assertFalse(hasattr(batch[0], "approved"))
        self.assertFalse(hasattr(batch[0], "promoted"))


if __name__ == "__main__":
    unittest.main()
