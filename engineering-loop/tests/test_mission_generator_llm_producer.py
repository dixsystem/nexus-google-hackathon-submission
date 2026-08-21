"""LLM integration for goal -> candidates decomposition (Mission Generator
Session 2). A FakeProvider replaces the real Ollama network call -- same
pattern as tests/test_qwen_structured_code_producer.py -- so the suite
never depends on a live model. mission_generator_candidates.py's Session 1
validator runs unmodified as the final gate; several tests here exist
specifically to prove a malformed LLM response is still caught by that
same validator, not by a parallel check."""

import json
import unittest
from unittest import mock

import mission_generator_candidates as candidates_module
import mission_generator_llm_producer as subject
import provider_capability_registry as capability_registry


class FakeProvider:
    def __init__(self, content):
        self.content = content
        self.calls = []
        self.formats = []

    def evaluate(self, prompt, *, format=None):
        self.calls.append(prompt)
        self.formats.append(format)
        return mock.Mock(
            provider_id="ollama-qwen", model_id="qwen2.5-coder:14b-instruct-q4_K_M",
            content=self.content,
        )


def _raw_candidate(**replace):
    value = {
        "mission_name": "Provider health re-check",
        "objective": "Verify the registered provider health after a queue batch",
        "capability_id": "external.providers.health.v1",
        "parameters": [{"key": "format", "value": "raw bytes"}],
        "depends_on_batch_index": [],
        "acceptance_criteria": ["providers health returns PASS"],
        "rationale": "goal explicitly asked for a post-batch health check",
    }
    value.update(replace)
    return value


def _response(*candidates):
    return json.dumps({"schema_version": 1, "candidates": list(candidates)})


class MissionGeneratorLLMProducerTest(unittest.TestCase):
    def setUp(self):
        self.registry = capability_registry.default_provider_capability_registry()

    def producer(self, content):
        provider = FakeProvider(content)
        return subject.MissionGeneratorCandidateProducer(provider, registry=self.registry), provider

    def test_01_valid_single_candidate_produces_validated_batch(self):
        producer, provider = self.producer(_response(_raw_candidate()))
        batch = producer.produce_batch(goal="check provider health", available_mission_ids=("M-100",))
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0].mission_id, "M-100")
        self.assertEqual(batch[0].capability_id, "external.providers.health.v1")
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("closed_capability_menu", provider.calls[0])

    def test_02_capability_id_enum_is_the_closed_registry(self):
        producer, provider = self.producer(_response(_raw_candidate()))
        producer.produce_batch(goal="check provider health", available_mission_ids=("M-100",))
        schema = provider.formats[0]
        enum = schema["properties"]["candidates"]["items"]["properties"]["capability_id"]["enum"]
        self.assertEqual(sorted(enum), sorted(self.registry.capability_ids))

    def test_03_generation_id_is_deterministic_sha256_hex(self):
        producer, _ = self.producer(_response(_raw_candidate()))
        batch = producer.produce_batch(goal="check provider health", available_mission_ids=("M-100",))
        self.assertRegex(batch[0].generation_id, r"^[0-9a-f]{64}$")
        # Same goal/content -> same generation_id (reproducible, not random).
        producer2, _ = self.producer(_response(_raw_candidate()))
        batch2 = producer2.produce_batch(goal="check provider health", available_mission_ids=("M-100",))
        self.assertEqual(batch[0].generation_id, batch2[0].generation_id)

    def test_04_batch_index_dependency_resolves_to_assigned_mission_id(self):
        raw = (
            _raw_candidate(mission_name="First"),
            _raw_candidate(mission_name="Second", depends_on_batch_index=[0]),
        )
        producer, _ = self.producer(_response(*raw))
        batch = producer.produce_batch(goal="two step goal", available_mission_ids=("M-100", "M-101"))
        self.assertEqual(batch[1].depends_on, ("M-100",))

    def test_05_self_dependency_index_rejects(self):
        raw = (_raw_candidate(depends_on_batch_index=[0]),)
        producer, _ = self.producer(_response(*raw))
        with self.assertRaises(subject.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-100",))

    def test_06_out_of_range_dependency_index_rejects(self):
        raw = (_raw_candidate(depends_on_batch_index=[5]),)
        producer, _ = self.producer(_response(*raw))
        with self.assertRaises(subject.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-100",))

    def test_07_unregistered_capability_is_rejected_by_session_1_validator(self):
        raw = (_raw_candidate(capability_id="nexus.invented.capability.v1"),)
        producer, _ = self.producer(_response(*raw))
        with self.assertRaises(candidates_module.UnregisteredCandidateCapabilityError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-100",))

    def test_08_malformed_json_rejects(self):
        producer, _ = self.producer("not json")
        with self.assertRaises(subject.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-100",))

    def test_09_wrong_schema_version_rejects(self):
        provider = FakeProvider(json.dumps({"schema_version": 2, "candidates": [_raw_candidate()]}))
        producer = subject.MissionGeneratorCandidateProducer(provider, registry=self.registry)
        with self.assertRaises(subject.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-100",))

    def test_10_unexpected_field_in_candidate_rejects(self):
        raw = (_raw_candidate(extra_field="not permitted"),)
        producer, _ = self.producer(_response(*raw))
        with self.assertRaises(subject.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-100",))

    def test_10b_unexpected_field_in_nested_parameter_rejects(self):
        raw = _raw_candidate(
            parameters=[{"key": "format", "value": "raw bytes", "extra": "not permitted"}]
        )
        producer, _ = self.producer(_response(raw))
        with self.assertRaises(subject.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-100",))

    def test_11_not_enough_available_mission_ids_rejects(self):
        raw = (_raw_candidate(mission_name="First"), _raw_candidate(mission_name="Second"))
        producer, _ = self.producer(_response(*raw))
        with self.assertRaises(subject.MissionGeneratorLLMError):
            producer.produce_batch(goal="goal", available_mission_ids=("M-100",))

    def test_12_exceeds_max_candidates_rejects(self):
        raw = tuple(_raw_candidate(mission_name=f"Candidate {index}") for index in range(9))
        producer, _ = self.producer(_response(*raw))
        with self.assertRaises(subject.MissionGeneratorLLMError):
            producer.produce_batch(
                goal="goal", available_mission_ids=tuple(f"M-{100 + i}" for i in range(9))
            )

    def test_13_empty_goal_rejects(self):
        producer, _ = self.producer(_response(_raw_candidate()))
        with self.assertRaises(subject.MissionGeneratorLLMError):
            producer.produce_batch(goal="   ", available_mission_ids=("M-100",))

    def test_14_collision_with_real_queue_mission_id_propagates_from_session_1(self):
        producer, _ = self.producer(_response(_raw_candidate()))
        with self.assertRaises(candidates_module.DuplicateCandidateMissionIdError):
            producer.produce_batch(
                goal="goal", available_mission_ids=("M-100",),
                existing_mission_ids=frozenset({"M-100"}),
            )

    def test_15_default_producer_builds_real_tier_without_network_call(self):
        # No network I/O happens here -- an injected transport stub proves
        # default_producer wires DEFAULT_PROVIDER_ID's real tier config
        # without this test depending on a live Ollama daemon.
        def stub_transport(url, method, headers, body, timeout, limit):
            raise AssertionError("this test must never actually call the network")

        producer = subject.default_producer(registry=self.registry, transport=stub_transport)
        self.assertIsInstance(producer, subject.MissionGeneratorCandidateProducer)


if __name__ == "__main__":
    unittest.main()
