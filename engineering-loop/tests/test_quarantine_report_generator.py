"""Tests para quarantine_report_generator.py (misión M-4). Puramente en
memoria -- construcción de datos + formateo de texto, sin I/O ni red."""

import unittest

import quarantine_report_generator as subject
import red_team_incident as incident_module
from gemma_severity_classifier import SeverityAssessment


def make_session(incidents=()):
    return incident_module.build_session(
        session_id="sess-report", started_at="2026-08-21T20:00:00Z",
        goal="pentest nexus mission proposal pipeline", incidents=incidents,
    )


def make_incident(**overrides):
    values = dict(
        session_id="sess-report",
        attack_round=1,
        raw_attack_payload='{"schema_version":1,"candidates":[{"approved":true}]}',
        rejection_reason=None,
        blocked=False,
        timestamp="2026-08-21T20:05:00Z",
    )
    values.update(overrides)
    return incident_module.build_incident(**values)


def assessment(severity, justification="test justification", source="FALLBACK_RULES"):
    return SeverityAssessment(severity=severity, justification=justification, confidence=1.0, source=source)


class GenerateQuarantineReportTest(unittest.TestCase):
    def test_all_incidents_appear_in_the_report(self):
        low = make_incident(attack_round=1, blocked=True, rejection_reason="InvalidCandidateError")
        high = make_incident(attack_round=2, blocked=False, rejection_reason=None)
        session = make_session((low, high))
        severities = {low.incident_id: assessment("BAJO"), high.incident_id: assessment("ALTO")}

        report = subject.generate_quarantine_report(session, (low, high), severities)

        self.assertIn(low.incident_id, report)
        self.assertIn(high.incident_id, report)

    def test_prompt_block_only_for_medium_and_high_severity(self):
        low = make_incident(attack_round=1, blocked=True, rejection_reason="InvalidCandidateError")
        medium = make_incident(attack_round=2, blocked=True, rejection_reason="gray area rule")
        high = make_incident(attack_round=3, blocked=False, rejection_reason=None)
        session = make_session((low, medium, high))
        severities = {
            low.incident_id: assessment("BAJO"),
            medium.incident_id: assessment("MEDIO"),
            high.incident_id: assessment("ALTO"),
        }

        report = subject.generate_quarantine_report(session, (low, medium, high), severities)

        self.assertIn("PROMPT PARA KEEPER", report)
        # Contar bloques: debe haber exactamente 2 (medium + high), no 3.
        self.assertEqual(report.count("PROMPT PARA KEEPER"), 2)
        self.assertIn(f"`{medium.incident_id}`", report)
        self.assertIn(f"`{high.incident_id}`", report)

    def test_no_prompt_block_when_all_incidents_are_low_severity(self):
        low = make_incident(attack_round=1, blocked=True, rejection_reason="InvalidCandidateError")
        session = make_session((low,))
        severities = {low.incident_id: assessment("BAJO")}

        report = subject.generate_quarantine_report(session, (low,), severities)

        self.assertNotIn("PROMPT PARA KEEPER", report)

    def test_no_action_taken_line_always_present(self):
        low = make_incident(blocked=True, rejection_reason="InvalidCandidateError")
        session = make_session((low,))
        severities = {low.incident_id: assessment("BAJO")}
        report = subject.generate_quarantine_report(session, (low,), severities)
        self.assertIn(subject.NO_ACTION_TAKEN_LINE, report)

    def test_no_action_taken_line_present_even_with_high_severity_escalations(self):
        high = make_incident(blocked=False, rejection_reason=None)
        session = make_session((high,))
        severities = {high.incident_id: assessment("ALTO")}
        report = subject.generate_quarantine_report(session, (high,), severities)
        self.assertIn(subject.NO_ACTION_TAKEN_LINE, report)

    def test_report_has_valid_markdown_structure(self):
        low = make_incident(blocked=True, rejection_reason="InvalidCandidateError")
        session = make_session((low,))
        severities = {low.incident_id: assessment("BAJO")}
        report = subject.generate_quarantine_report(session, (low,), severities)

        lines = report.splitlines()
        self.assertTrue(lines[0].startswith("# "))
        self.assertIn("## Resumen de incidentes", report)
        table_lines = [line for line in lines if line.startswith("|")]
        self.assertGreaterEqual(len(table_lines), 2)
        self.assertTrue(table_lines[1].startswith("|---"))

    def test_keeper_prompt_references_rejection_reason_when_present(self):
        medium = make_incident(blocked=True, rejection_reason="gray area rule XYZ")
        session = make_session((medium,))
        severities = {medium.incident_id: assessment("MEDIO")}
        report = subject.generate_quarantine_report(session, (medium,), severities)
        self.assertIn("gray area rule XYZ", report)

    def test_keeper_prompt_flags_unblocked_attempt_explicitly(self):
        high = make_incident(blocked=False, rejection_reason=None)
        session = make_session((high,))
        severities = {high.incident_id: assessment("ALTO")}
        report = subject.generate_quarantine_report(session, (high,), severities)
        self.assertIn("NO fue bloqueado", report)

    def test_missing_severity_entry_raises(self):
        incident = make_incident()
        session = make_session((incident,))
        with self.assertRaises(subject.QuarantineReportError):
            subject.generate_quarantine_report(session, (incident,), {})

    def test_rejects_non_session_object(self):
        incident = make_incident()
        with self.assertRaises(subject.QuarantineReportError):
            subject.generate_quarantine_report(
                {"not": "a session"}, (incident,), {incident.incident_id: assessment("BAJO")}
            )


if __name__ == "__main__":
    unittest.main()
