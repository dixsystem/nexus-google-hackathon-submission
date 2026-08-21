from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys
import unittest
from unittest import mock

import antigravity_isolated_child as child
import google_agentic_demo as subject
import mission_generator_llm_producer as producer_module
import provider_capability_registry as capability_registry


class _FakeRealResult:
    text = "{}"
    response_model_id = "gemini-explicit"
    response_id = "response-1"
    prompt_token_count = 1
    candidates_token_count = 1
    total_token_count = 2


class DemoBackendSelectionTests(unittest.TestCase):
    def test_offline_is_default_and_mock_still_works(self):
        backend = subject.select_child_backend("offline", environ={})
        result = backend(child.ChildRequest(
            request_id="r", model_id=subject.OFFLINE_MODEL_ID, prompt="p",
            format=None, timeout_seconds=1.0, max_response_chars=100_000,
        ))
        self.assertEqual(result.response_model_id, subject.OFFLINE_MODEL_ID)
        self.assertIn('"candidates"', result.text)

    def test_real_without_credential_fails_closed_before_backend_creation(self):
        called = []
        with self.assertRaises(subject.DemoConfigurationError):
            subject.select_child_backend(
                "real", environ={}, real_backend_factory=lambda **kwargs: called.append(kwargs)
            )
        self.assertEqual(called, [])

    def test_real_backend_is_explicitly_selected_and_adapted(self):
        captured = []

        def factory(**kwargs):
            captured.append(kwargs)
            return lambda request: _FakeRealResult()

        backend = subject.select_child_backend(
            "real",
            environ={subject.REAL_CREDENTIAL_ENV: "sentinel-not-real"},
            real_backend_factory=factory,
        )
        result = backend(child.ChildRequest(
            request_id="r", model_id="gemini-explicit", prompt="p",
            format=None, timeout_seconds=1.0, max_response_chars=100,
        ))
        self.assertEqual(captured, [{"api_key": "sentinel-not-real"}])
        self.assertIsInstance(result, child.BackendResult)
        self.assertEqual(result.response_model_id, "gemini-explicit")


class DemoPipelineTests(unittest.TestCase):
    def test_demo_requests_exactly_one_candidate(self):
        with tempfile.TemporaryDirectory(prefix="google-demo-test-") as tmp:
            with mock.patch.object(
                subject,
                "MissionGeneratorCandidateProducer",
                wraps=subject.MissionGeneratorCandidateProducer,
            ) as constructor:
                subject.run_demo(output_dir=Path(tmp), environ={})

        constructor.assert_called_once()
        self.assertEqual(
            constructor.call_args.kwargs["max_candidates"],
            subject.DEMO_MAX_CANDIDATES,
        )
        self.assertEqual(subject.DEMO_MAX_CANDIDATES, 1)

    def test_demo_cardinality_contract_rejects_two_candidates(self):
        candidate = {
            "mission_name": "Provider health re-check",
            "objective": "Verify provider health",
            "capability_id": "external.providers.health.v1",
            "parameters": [],
            "depends_on_batch_index": [],
            "acceptance_criteria": ["provider health returns PASS"],
            "rationale": "health verification",
        }
        response = mock.Mock(
            provider_id="test-provider",
            model_id="test-model",
            content=json.dumps({"schema_version": 1, "candidates": [candidate, candidate]}),
        )
        provider = mock.Mock()
        provider.evaluate.return_value = response
        producer = producer_module.MissionGeneratorCandidateProducer(
            provider,
            registry=capability_registry.default_provider_capability_registry(),
            max_candidates=subject.DEMO_MAX_CANDIDATES,
        )

        with self.assertRaises(producer_module.MissionGeneratorLLMError) as caught:
            producer.produce_batch(goal="health check", available_mission_ids=("M-901",))

        self.assertEqual(caught.exception.category, "LIMIT")
        sent_prompt = provider.evaluate.call_args.args[0]
        sent_schema = provider.evaluate.call_args.kwargs["format"]
        self.assertIn("Propose at most 1 candidates.", sent_prompt)
        self.assertEqual(sent_schema["properties"]["candidates"]["minItems"], 1)
        self.assertEqual(sent_schema["properties"]["candidates"]["maxItems"], 1)

    def test_offline_proposal_validation_and_staging_end_to_end(self):
        with tempfile.TemporaryDirectory(prefix="google-demo-test-") as tmp:
            batch = subject.run_demo(output_dir=Path(tmp), environ={})
            self.assertEqual(len(batch.candidates), 1)
            self.assertEqual(batch.candidates[0].mission_id, "M-901")
            self.assertTrue(batch.proposals_path.is_file())
            self.assertTrue(batch.contracts_path.is_file())
            self.assertTrue((Path(tmp) / "MISSION_PROPOSAL_CANDIDATES.json").is_file())
            self.assertFalse(hasattr(batch.candidates[0], "approved"))
            self.assertFalse(hasattr(batch.candidates[0], "promoted"))

    def test_real_transport_requires_explicit_model_and_credential(self):
        with self.assertRaises(subject.DemoConfigurationError):
            subject.build_transport("real", model_id=None, environ={})
        with self.assertRaises(subject.DemoConfigurationError):
            subject.build_transport("real", model_id="gemini-explicit", environ={})

    def test_real_transport_uses_isolated_google_sdk_interpreter(self):
        transport, selected_model = subject.build_transport(
            "real",
            model_id="gemini-flash-latest",
            environ={subject.REAL_CREDENTIAL_ENV: "sentinel-not-real"},
        )
        self.assertEqual(selected_model, "gemini-flash-latest")
        self.assertEqual(transport._child_argv[0], str(subject.REAL_CHILD_PYTHON))

    def test_real_transport_fails_closed_when_isolated_interpreter_is_missing(self):
        missing = Path("/definitely/missing/isolated-python")
        with mock.patch.object(subject, "REAL_CHILD_PYTHON", missing):
            with self.assertRaisesRegex(
                subject.DemoConfigurationError,
                "isolated Google SDK interpreter",
            ):
                subject.build_transport(
                    "real",
                    model_id="gemini-flash-latest",
                    environ={subject.REAL_CREDENTIAL_ENV: "sentinel-not-real"},
                )

    def test_offline_transport_keeps_current_interpreter(self):
        transport, _ = subject.build_transport("offline", model_id=None, environ={})
        self.assertEqual(transport._child_argv[0], sys.executable)


if __name__ == "__main__":
    unittest.main()
