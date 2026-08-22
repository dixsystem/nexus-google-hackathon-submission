"""Tests para red_team_session.py (misión M-9). Ningún test hace red real
-- attacker/gemma transports son siempre dobles de prueba (QueueTransport)
inyectados, igual que en cada test de este proyecto."""

import ast
import json
import unittest
from pathlib import Path

import antigravity_gemini_provider as provider_module
import provider_capability_registry as capability_registry
import red_team_incident as incident_module
import red_team_session as subject


ATTACKER_MODEL_ID = "gemini-redteam-test-model"
GEMMA_MODEL_ID = "gemma-4-26b-a4b-it"


def make_registry():
    return capability_registry.default_provider_capability_registry()


def raw_result(model_id, text):
    return provider_module.RawGeminiResult(
        text=text, response_model_id=model_id, response_id="resp-1",
        prompt_token_count=10, candidates_token_count=5, total_token_count=15,
    )


def blocked_attack_json(capability_id="not.a.real.capability.v1"):
    return json.dumps({
        "schema_version": 1,
        "candidates": [{
            "mission_name": "Attack attempt",
            "objective": "Try to escalate privileges",
            "capability_id": capability_id,
            "parameters": [],
            "depends_on_batch_index": [],
            "acceptance_criteria": ["n/a"],
            "rationale": "attack",
        }],
    })


def valid_attack_json(capability_id="external.providers.health.v1"):
    return json.dumps({
        "schema_version": 1,
        "candidates": [{
            "mission_name": "Looks legitimate",
            "objective": "Perfectly valid objective",
            "capability_id": capability_id,
            "parameters": [],
            "depends_on_batch_index": [],
            "acceptance_criteria": ["passes validation"],
            "rationale": "bypass test",
        }],
    })


def gemma_response_json(severity, justification="test", confidence=0.9):
    return json.dumps({"severity": severity, "justification": justification, "confidence": confidence})


def gemini_assessment_response_json(assessment):
    return json.dumps({"assessment": assessment})


class QueueTransport:
    """Doble de prueba: agota una cola de RawGeminiResult en orden y
    registra cada model_id con el que fue llamado."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, model_id, prompt, format, timeout):
        self.calls.append(model_id)
        if not self._results:
            raise AssertionError("QueueTransport exhausted -- unexpected extra call")
        return self._results.pop(0)


class NormalSessionTest(unittest.TestCase):
    def test_session_with_multiple_blocked_incidents_is_chained_and_report_has_no_escalations(self):
        registry = make_registry()
        attacker_transport = QueueTransport(
            [raw_result(ATTACKER_MODEL_ID, blocked_attack_json()) for _ in range(3)]
        )
        gemma_transport = QueueTransport(
            [raw_result(GEMMA_MODEL_ID, gemma_response_json("BAJO")) for _ in range(3)]
        )

        result = subject.run_red_team_session(
            "test goal", registry, attacker_transport, rounds=3,
            session_id="sess-normal", model_id=ATTACKER_MODEL_ID,
            gemma_transport=gemma_transport,
            # Explícito "UNKNOWN": este test verifica chain integrity y
            # ausencia de escalación, no el comportamiento de
            # gemini_assess_attack (cubierto en GeminiAssessAttackTest) --
            # sin esto, el default real consumiría de attacker_transport.
            gemini_assessor=lambda incident: "UNKNOWN",
        )

        self.assertEqual(result.session_id, "sess-normal")
        self.assertEqual(len(result.incidents), 3)
        self.assertEqual(len(result.validation_bypasses), 0)
        for incident in result.incidents:
            self.assertTrue(incident.blocked)
            self.assertIsNotNone(incident.rejection_reason)

        # gemini_assessor="UNKNOWN" -> siempre NO_CONSENSUS -> nunca escala
        self.assertNotIn("PROMPT PARA KEEPER", result.quarantine_report)
        self.assertEqual(result.escalated_incident_ids, ())

        session = incident_module.build_session(
            session_id="sess-normal", started_at="2026-08-22T00:00:00Z",
            goal="test goal", incidents=result.incidents,
        )
        self.assertTrue(incident_module.verify_session_chain(session))

    def test_default_session_id_and_started_at_are_auto_generated_when_omitted(self):
        registry = make_registry()
        attacker_transport = QueueTransport([raw_result(ATTACKER_MODEL_ID, blocked_attack_json())])
        gemma_transport = QueueTransport([raw_result(GEMMA_MODEL_ID, gemma_response_json("BAJO"))])

        result = subject.run_red_team_session(
            "test goal", registry, attacker_transport, rounds=1,
            model_id=ATTACKER_MODEL_ID, gemma_transport=gemma_transport,
            gemini_assessor=lambda incident: "UNKNOWN",
        )
        self.assertTrue(result.session_id.startswith("redteam-"))


class ArchiveLowInterestSessionTest(unittest.TestCase):
    def test_trivial_gemini_assessment_with_fallback_gemma_archives_everything(self):
        registry = make_registry()
        attacker_transport = QueueTransport(
            [raw_result(ATTACKER_MODEL_ID, blocked_attack_json()) for _ in range(2)]
        )

        result = subject.run_red_team_session(
            "test goal", registry, attacker_transport, rounds=2,
            session_id="sess-archive", model_id=ATTACKER_MODEL_ID,
            use_gemma_fallback=True,
            gemini_assessor=lambda incident: "TRIVIAL",
        )

        self.assertEqual(len(result.incidents), 2)
        self.assertEqual(len(result.validation_bypasses), 0)
        self.assertNotIn("PROMPT PARA KEEPER", result.quarantine_report)


class EscalateSessionTest(unittest.TestCase):
    def test_sophisticated_gemini_and_high_gemma_severity_escalates_and_appears_in_report(self):
        registry = make_registry()
        attacker_transport = QueueTransport([raw_result(ATTACKER_MODEL_ID, blocked_attack_json())])
        gemma_transport = QueueTransport(
            [raw_result(GEMMA_MODEL_ID, gemma_response_json("ALTO", "very sophisticated attempt"))]
        )

        result = subject.run_red_team_session(
            "test goal", registry, attacker_transport, rounds=1,
            session_id="sess-escalate", model_id=ATTACKER_MODEL_ID,
            gemma_transport=gemma_transport,
            gemini_assessor=lambda incident: "SOPHISTICATED",
        )

        self.assertEqual(len(result.incidents), 1)
        self.assertIn("PROMPT PARA KEEPER", result.quarantine_report)
        self.assertIn(result.incidents[0].incident_id, result.quarantine_report)
        self.assertIn("very sophisticated attempt", result.quarantine_report)
        self.assertEqual(result.escalated_incident_ids, (result.incidents[0].incident_id,))

    def test_mixed_session_only_escalated_incidents_get_prompt_blocks(self):
        registry = make_registry()
        attacker_transport = QueueTransport(
            [raw_result(ATTACKER_MODEL_ID, blocked_attack_json()) for _ in range(2)]
        )
        gemma_transport = QueueTransport([
            raw_result(GEMMA_MODEL_ID, gemma_response_json("ALTO")),
            raw_result(GEMMA_MODEL_ID, gemma_response_json("BAJO")),
        ])
        assessments = iter(["SOPHISTICATED", "TRIVIAL"])

        result = subject.run_red_team_session(
            "test goal", registry, attacker_transport, rounds=2,
            session_id="sess-mixed", model_id=ATTACKER_MODEL_ID,
            gemma_transport=gemma_transport,
            gemini_assessor=lambda incident: next(assessments),
        )

        self.assertEqual(result.quarantine_report.count("PROMPT PARA KEEPER"), 1)
        self.assertIn(result.incidents[0].incident_id, result.quarantine_report)


class ValidationBypassTest(unittest.TestCase):
    def test_fully_valid_attack_is_recorded_as_bypass_and_never_reaches_gemma(self):
        registry = make_registry()
        attacker_transport = QueueTransport([raw_result(ATTACKER_MODEL_ID, valid_attack_json())])
        gemma_transport = QueueTransport([])  # NUNCA debe consumirse

        result = subject.run_red_team_session(
            "test goal", registry, attacker_transport, rounds=1,
            session_id="sess-bypass", model_id=ATTACKER_MODEL_ID,
            gemma_transport=gemma_transport,
        )

        self.assertEqual(len(result.validation_bypasses), 1)
        bypass = result.validation_bypasses[0]
        self.assertEqual(bypass.incident_id, result.incidents[0].incident_id)
        self.assertFalse(result.incidents[0].blocked)
        self.assertIsNone(result.incidents[0].rejection_reason)
        self.assertEqual(gemma_transport.calls, [])
        self.assertNotIn("PROMPT PARA KEEPER", result.quarantine_report)

    def test_bypass_incident_id_matches_synthetic_mission_id_pool(self):
        registry = make_registry()
        attacker_transport = QueueTransport([raw_result(ATTACKER_MODEL_ID, valid_attack_json())])
        result = subject.run_red_team_session(
            "test goal", registry, attacker_transport, rounds=1,
            session_id="sess-bypass-2", model_id=ATTACKER_MODEL_ID,
        )
        self.assertEqual(result.validation_bypasses[0].mission_id, subject._SYNTHETIC_MISSION_ID)


class GeminiAssessAttackTest(unittest.TestCase):
    """Tests dedicados de gemini_assess_attack() (sesión supervisada,
    paso 1) -- transport siempre mockeado, ninguna llamada real a Gemini."""

    def test_returns_sophisticated_from_mocked_transport(self):
        transport = QueueTransport(
            [raw_result(subject.DEFAULT_GEMINI_ASSESSOR_MODEL_ID, gemini_assessment_response_json("SOPHISTICATED"))]
        )
        result = subject.gemini_assess_attack(blocked_attack_json(), "some rejection reason", transport)
        self.assertEqual(result, "SOPHISTICATED")

    def test_returns_trivial_from_mocked_transport(self):
        transport = QueueTransport(
            [raw_result(subject.DEFAULT_GEMINI_ASSESSOR_MODEL_ID, gemini_assessment_response_json("TRIVIAL"))]
        )
        result = subject.gemini_assess_attack(blocked_attack_json(), "some rejection reason", transport)
        self.assertEqual(result, "TRIVIAL")

    def test_falls_back_to_unknown_when_transport_raises(self):
        def failing_transport(model_id, prompt, format, timeout):
            raise RuntimeError("Gemini is unreachable")

        result = subject.gemini_assess_attack(blocked_attack_json(), "reason", failing_transport)
        self.assertEqual(result, "UNKNOWN")

    def test_falls_back_to_unknown_on_malformed_json_response(self):
        transport = QueueTransport(
            [raw_result(subject.DEFAULT_GEMINI_ASSESSOR_MODEL_ID, "not json at all { broken")]
        )
        result = subject.gemini_assess_attack(blocked_attack_json(), "reason", transport)
        self.assertEqual(result, "UNKNOWN")

    def test_falls_back_to_unknown_on_unexpected_fields(self):
        transport = QueueTransport(
            [raw_result(subject.DEFAULT_GEMINI_ASSESSOR_MODEL_ID, json.dumps({"assessment": "SOPHISTICATED", "extra": True}))]
        )
        result = subject.gemini_assess_attack(blocked_attack_json(), "reason", transport)
        self.assertEqual(result, "UNKNOWN")

    def test_falls_back_to_unknown_on_invalid_assessment_value(self):
        transport = QueueTransport(
            [raw_result(subject.DEFAULT_GEMINI_ASSESSOR_MODEL_ID, json.dumps({"assessment": "MALICIOUS"}))]
        )
        result = subject.gemini_assess_attack(blocked_attack_json(), "reason", transport)
        self.assertEqual(result, "UNKNOWN")

    def test_empty_attack_payload_returns_unknown_without_calling_transport(self):
        transport = QueueTransport([])
        result = subject.gemini_assess_attack("", "reason", transport)
        self.assertEqual(result, "UNKNOWN")
        self.assertEqual(transport.calls, [])

    def test_uses_expected_model_id_and_includes_payload_and_reason_in_prompt(self):
        transport = QueueTransport(
            [raw_result(subject.DEFAULT_GEMINI_ASSESSOR_MODEL_ID, gemini_assessment_response_json("TRIVIAL"))]
        )
        attack_payload = '{"marker":"XYZ-PAYLOAD"}'
        subject.gemini_assess_attack(attack_payload, "XYZ-REASON", transport)
        self.assertEqual(transport.calls, [subject.DEFAULT_GEMINI_ASSESSOR_MODEL_ID])

    def test_custom_model_id_is_honored(self):
        transport = QueueTransport(
            [raw_result("custom-gemini-model", gemini_assessment_response_json("SOPHISTICATED"))]
        )
        result = subject.gemini_assess_attack(
            blocked_attack_json(), "reason", transport, model_id="custom-gemini-model"
        )
        self.assertEqual(result, "SOPHISTICATED")
        self.assertEqual(transport.calls, ["custom-gemini-model"])


class DefaultGeminiAssessorWiringTest(unittest.TestCase):
    """Prueba de integración: sin gemini_assessor explícito,
    run_red_team_session() ahora dispara gemini_assess_attack() de verdad
    (vía transport mockeado) en vez del "UNKNOWN" fijo anterior."""

    def test_default_assessor_calls_gemini_and_can_escalate(self):
        registry = make_registry()
        attacker_transport = QueueTransport([raw_result(ATTACKER_MODEL_ID, blocked_attack_json())])
        gemma_transport = QueueTransport(
            [raw_result(GEMMA_MODEL_ID, gemma_response_json("ALTO", "escalation-worthy"))]
        )
        # Reutiliza attacker_transport para el assessor (default de
        # gemini_assessor_transport=None -> transport) -- necesita un
        # segundo item en cola para la llamada del assessor tras el
        # ataque.
        attacker_transport._results.append(
            raw_result(subject.DEFAULT_GEMINI_ASSESSOR_MODEL_ID, gemini_assessment_response_json("SOPHISTICATED"))
        )

        result = subject.run_red_team_session(
            "test goal", registry, attacker_transport, rounds=1,
            session_id="sess-default-assessor", model_id=ATTACKER_MODEL_ID,
            gemma_transport=gemma_transport,
        )

        self.assertEqual(attacker_transport.calls, [ATTACKER_MODEL_ID, subject.DEFAULT_GEMINI_ASSESSOR_MODEL_ID])
        self.assertIn("PROMPT PARA KEEPER", result.quarantine_report)

    def test_default_assessor_transport_failure_degrades_to_no_consensus_not_a_crash(self):
        registry = make_registry()

        class FailingAfterFirstCall:
            def __init__(self, first_result):
                self._first = first_result
                self.calls = 0

            def __call__(self, model_id, prompt, format, timeout):
                self.calls += 1
                if self.calls == 1:
                    return self._first
                raise RuntimeError("assessor call unreachable")

        attacker_transport = FailingAfterFirstCall(raw_result(ATTACKER_MODEL_ID, blocked_attack_json()))
        gemma_transport = QueueTransport([raw_result(GEMMA_MODEL_ID, gemma_response_json("ALTO"))])

        result = subject.run_red_team_session(
            "test goal", registry, attacker_transport, rounds=1,
            session_id="sess-assessor-fails", model_id=ATTACKER_MODEL_ID,
            gemma_transport=gemma_transport,
        )

        # gemini_assess_attack degrada a UNKNOWN -> NO_CONSENSUS -> no escala,
        # pero la sesión completa sin lanzar.
        self.assertEqual(len(result.incidents), 1)
        self.assertNotIn("PROMPT PARA KEEPER", result.quarantine_report)


class NeverExecutesTest(unittest.TestCase):
    """Verificación estática: red_team_session.py nunca importa ni
    invoca mission_executor.py -- un VALIDATION_BYPASS se reporta, nunca
    se ejecuta."""

    @classmethod
    def setUpClass(cls):
        cls.source_text = Path(subject.__file__).read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source_text)

    def test_mission_executor_never_referenced_as_a_real_identifier(self):
        identifiers = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.alias):
                identifiers.add(node.name.split(".")[0])
                if node.asname:
                    identifiers.add(node.asname)
        self.assertNotIn("mission_executor", identifiers)
        self.assertNotIn("execute_allowed_mission", identifiers)
        self.assertNotIn("authorize_allow_decision", identifiers)


class ConfigurationTest(unittest.TestCase):
    def test_rejects_non_positive_rounds(self):
        registry = make_registry()
        with self.assertRaises(subject.RedTeamSessionError):
            subject.run_red_team_session(
                "goal", registry, QueueTransport([]), rounds=0, model_id=ATTACKER_MODEL_ID
            )

    def test_model_id_is_a_required_keyword_argument(self):
        registry = make_registry()
        with self.assertRaises(TypeError):
            subject.run_red_team_session("goal", registry, QueueTransport([]))

    def test_invalid_gemini_assessor_return_value_raises(self):
        registry = make_registry()
        attacker_transport = QueueTransport([raw_result(ATTACKER_MODEL_ID, blocked_attack_json())])
        gemma_transport = QueueTransport([raw_result(GEMMA_MODEL_ID, gemma_response_json("BAJO"))])
        with self.assertRaises(subject.RedTeamSessionError):
            subject.run_red_team_session(
                "goal", registry, attacker_transport, rounds=1, model_id=ATTACKER_MODEL_ID,
                gemma_transport=gemma_transport, gemini_assessor=lambda incident: "MALICIOUS",
            )


if __name__ == "__main__":
    unittest.main()
