"""Tests para gemma_severity_classifier.py (misión M-3). Ningún test hace
red ni gasta cuota real -- classify() siempre usa un transport doble de
prueba inyectado; classify_with_fallback_rules() no toca red en absoluto."""

import unittest
from unittest import mock

import antigravity_gemini_provider as provider_module
import gemma_severity_classifier as subject
import red_team_incident as incident_module


def make_incident(**overrides):
    values = dict(
        session_id="sess-001",
        attack_round=1,
        raw_attack_payload='{"schema_version":1,"candidates":[]}',
        rejection_reason=None,
        blocked=False,
        timestamp="2026-08-21T22:00:00Z",
    )
    values.update(overrides)
    return incident_module.build_incident(**values)


def raw_result(**overrides):
    values = dict(
        text='{"severity":"ALTO","justification":"looks bad","confidence":0.9}',
        response_model_id="gemma-classifier-model",
        response_id="resp-classify-1",
        prompt_token_count=10,
        candidates_token_count=5,
        total_token_count=15,
    )
    values.update(overrides)
    return provider_module.RawGeminiResult(**values)


class BuildClassificationPromptTest(unittest.TestCase):
    def test_prompt_includes_three_severity_levels(self):
        prompt = subject.build_classification_prompt(make_incident(), None)
        self.assertIn("BAJO", prompt)
        self.assertIn("MEDIO", prompt)
        self.assertIn("ALTO", prompt)

    def test_prompt_includes_raw_attack_payload(self):
        incident = make_incident(raw_attack_payload='{"marker":"XYZ"}')
        prompt = subject.build_classification_prompt(incident, None)
        self.assertIn("XYZ", prompt)

    def test_rejects_non_incident_object(self):
        with self.assertRaises(subject.GemmaSeverityClassifierError):
            subject.build_classification_prompt({"not": "an incident"}, None)


class GemmaSeverityClassifierClassifyTest(unittest.TestCase):
    def test_classify_returns_valid_severity_with_gemma_source(self):
        transport = mock.Mock(
            return_value=raw_result(
                text='{"severity":"MEDIO","justification":"partially clever","confidence":0.6}',
                response_model_id="gemma-classifier-model",
            )
        )
        classifier = subject.GemmaSeverityClassifier("gemma-classifier-model")
        result = classifier.classify(make_incident(), "some rejection", transport)

        self.assertIsInstance(result, subject.SeverityAssessment)
        self.assertEqual(result.severity, "MEDIO")
        self.assertEqual(result.justification, "partially clever")
        self.assertAlmostEqual(result.confidence, 0.6)
        self.assertEqual(result.source, subject.SOURCE_GEMMA)
        transport.assert_called_once()

    def test_classify_rejects_invalid_severity_level(self):
        transport = mock.Mock(
            return_value=raw_result(
                text='{"severity":"CRITICO","justification":"x","confidence":0.5}',
                response_model_id="gemma-classifier-model",
            )
        )
        classifier = subject.GemmaSeverityClassifier("gemma-classifier-model")
        with self.assertRaises(subject.GemmaSeverityClassifierError):
            classifier.classify(make_incident(), None, transport)

    def test_classify_rejects_out_of_range_confidence(self):
        transport = mock.Mock(
            return_value=raw_result(
                text='{"severity":"BAJO","justification":"x","confidence":1.5}',
                response_model_id="gemma-classifier-model",
            )
        )
        classifier = subject.GemmaSeverityClassifier("gemma-classifier-model")
        with self.assertRaises(subject.GemmaSeverityClassifierError):
            classifier.classify(make_incident(), None, transport)

    def test_classify_rejects_malformed_json(self):
        transport = mock.Mock(
            return_value=raw_result(text="not json", response_model_id="gemma-classifier-model")
        )
        classifier = subject.GemmaSeverityClassifier("gemma-classifier-model")
        with self.assertRaises(subject.GemmaSeverityClassifierError):
            classifier.classify(make_incident(), None, transport)

    def test_classify_rejects_extra_fields(self):
        transport = mock.Mock(
            return_value=raw_result(
                text='{"severity":"BAJO","justification":"x","confidence":0.1,"extra":true}',
                response_model_id="gemma-classifier-model",
            )
        )
        classifier = subject.GemmaSeverityClassifier("gemma-classifier-model")
        with self.assertRaises(subject.GemmaSeverityClassifierError):
            classifier.classify(make_incident(), None, transport)

    def test_classify_uses_same_isolation_contract_as_attacker_provider(self):
        transport = mock.Mock(return_value=raw_result(response_model_id="gemma-classifier-model"))
        classifier = subject.GemmaSeverityClassifier("gemma-classifier-model", timeout_seconds=20.0)
        classifier.classify(make_incident(), None, transport)
        transport.assert_called_once_with("gemma-classifier-model", mock.ANY, mock.ANY, 20.0)


class FallbackRulesClassifierTest(unittest.TestCase):
    def test_unblocked_with_no_rejection_reason_is_high_severity(self):
        result = subject.classify_with_fallback_rules(make_incident(blocked=False), None)
        self.assertEqual(result.severity, "ALTO")
        self.assertEqual(result.source, subject.SOURCE_FALLBACK_RULES)

    def test_blocked_with_rejection_reason_is_low_severity(self):
        result = subject.classify_with_fallback_rules(
            make_incident(blocked=True), "UnregisteredCandidateCapabilityError"
        )
        self.assertEqual(result.severity, "BAJO")
        self.assertEqual(result.source, subject.SOURCE_FALLBACK_RULES)

    def test_inconsistent_state_blocked_without_reason_is_medium_severity(self):
        result = subject.classify_with_fallback_rules(make_incident(blocked=True), None)
        self.assertEqual(result.severity, "MEDIO")

    def test_inconsistent_state_unblocked_with_reason_is_medium_severity(self):
        result = subject.classify_with_fallback_rules(
            make_incident(blocked=False), "some reason present anyway"
        )
        self.assertEqual(result.severity, "MEDIO")

    def test_fallback_confidence_is_always_one(self):
        result = subject.classify_with_fallback_rules(make_incident(blocked=False), None)
        self.assertEqual(result.confidence, 1.0)

    def test_fallback_never_touches_network(self):
        # No hay ningún transport/provider inyectado -- si esta función
        # intentara red, fallaría por falta de argumentos/atributos.
        result = subject.classify_with_fallback_rules(make_incident(), "x")
        self.assertIsInstance(result, subject.SeverityAssessment)


if __name__ == "__main__":
    unittest.main()
