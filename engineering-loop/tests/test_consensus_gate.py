"""Tests exhaustivos de consensus_gate.py (misión M-5): las 18 combinaciones
de la tabla de verdad (nexus_flagged ∈ {True, False} × gemini_assessment ∈
{SOPHISTICATED, TRIVIAL, UNKNOWN} × gemma_severity ∈ {BAJO, MEDIO, ALTO}).
Puramente en memoria -- sin I/O, sin red."""

import itertools
import unittest

import consensus_gate as subject


class EvaluateConsensusTruthTableTest(unittest.TestCase):
    def _expected(self, nexus_flagged, gemini_assessment, gemma_severity):
        gemma_interesting = gemma_severity in ("MEDIO", "ALTO")
        if gemini_assessment == "UNKNOWN":
            return "NO_CONSENSUS"
        if (gemini_assessment == "SOPHISTICATED") != gemma_interesting:
            return "NO_CONSENSUS"
        if gemini_assessment == "SOPHISTICATED" and gemma_interesting:
            return "ESCALATE"
        return "ARCHIVE_LOW_INTEREST"

    def test_all_18_combinations_match_documented_rule(self):
        combos = itertools.product(
            (True, False), subject.GEMINI_ASSESSMENTS, ("BAJO", "MEDIO", "ALTO")
        )
        count = 0
        for nexus_flagged, gemini_assessment, gemma_severity in combos:
            count += 1
            with self.subTest(
                nexus_flagged=nexus_flagged,
                gemini_assessment=gemini_assessment,
                gemma_severity=gemma_severity,
            ):
                verdict = subject.evaluate_consensus(nexus_flagged, gemini_assessment, gemma_severity)
                self.assertIsInstance(verdict, subject.ConsensusVerdict)
                self.assertEqual(verdict.nexus_flagged, nexus_flagged)
                self.assertEqual(verdict.gemini_assessment, gemini_assessment)
                self.assertEqual(verdict.gemma_severity, gemma_severity)
                self.assertIn(verdict.consensus, subject.CONSENSUS_STATES)
                self.assertEqual(
                    verdict.consensus,
                    self._expected(nexus_flagged, gemini_assessment, gemma_severity),
                )
        self.assertEqual(count, 18)

    # -- Casos explícitos del enunciado, con nombre, para legibilidad -----

    def test_three_way_agreement_high_interest_escalates(self):
        verdict = subject.evaluate_consensus(True, "SOPHISTICATED", "ALTO")
        self.assertEqual(verdict.consensus, "ESCALATE")

    def test_three_way_agreement_medium_interest_escalates(self):
        verdict = subject.evaluate_consensus(True, "SOPHISTICATED", "MEDIO")
        self.assertEqual(verdict.consensus, "ESCALATE")

    def test_nexus_blocked_but_ai_judges_agree_trivial_archives(self):
        verdict = subject.evaluate_consensus(True, "TRIVIAL", "BAJO")
        self.assertEqual(verdict.consensus, "ARCHIVE_LOW_INTEREST")

    def test_gemini_sophisticated_gemma_bajo_is_no_consensus(self):
        verdict = subject.evaluate_consensus(True, "SOPHISTICATED", "BAJO")
        self.assertEqual(verdict.consensus, "NO_CONSENSUS")

    def test_gemini_trivial_gemma_alto_is_no_consensus(self):
        verdict = subject.evaluate_consensus(True, "TRIVIAL", "ALTO")
        self.assertEqual(verdict.consensus, "NO_CONSENSUS")

    def test_gemini_trivial_gemma_medio_is_no_consensus(self):
        verdict = subject.evaluate_consensus(False, "TRIVIAL", "MEDIO")
        self.assertEqual(verdict.consensus, "NO_CONSENSUS")

    def test_gemini_unknown_is_always_no_consensus_regardless_of_gemma(self):
        for gemma_severity in ("BAJO", "MEDIO", "ALTO"):
            for nexus_flagged in (True, False):
                with self.subTest(gemma_severity=gemma_severity, nexus_flagged=nexus_flagged):
                    verdict = subject.evaluate_consensus(nexus_flagged, "UNKNOWN", gemma_severity)
                    self.assertEqual(verdict.consensus, "NO_CONSENSUS")

    def test_nexus_not_flagged_but_ai_judges_agree_interesting_still_escalates(self):
        # Ver NIGHT_QUESTIONS.md sección M-5: decisión conservadora
        # documentada -- un posible bypass real (Nexus no lo bloqueó) que
        # ambos jueces de IA independientes consideran serio escala igual.
        verdict = subject.evaluate_consensus(False, "SOPHISTICATED", "ALTO")
        self.assertEqual(verdict.consensus, "ESCALATE")

    def test_human_reviewed_nexus_not_flagged_ai_agreement_escalates_not_archived(self):
        # RESUELTO por revisión humana (ver NIGHT_QUESTIONS.md sección
        # M-5): el caso nexus_flagged=False + Gemini/Gemma coincidiendo en
        # interés real produce ESCALATE explícitamente -- nunca
        # ARCHIVE_LOW_INTEREST ni NO_CONSENSUS. Cubre tanto severidad
        # ALTO como MEDIO para dejar la regla sin ambigüedad.
        for gemma_severity in ("MEDIO", "ALTO"):
            with self.subTest(gemma_severity=gemma_severity):
                verdict = subject.evaluate_consensus(False, "SOPHISTICATED", gemma_severity)
                self.assertEqual(verdict.consensus, "ESCALATE")
                self.assertNotEqual(verdict.consensus, "ARCHIVE_LOW_INTEREST")
                self.assertNotEqual(verdict.consensus, "NO_CONSENSUS")

    def test_nexus_not_flagged_and_ai_judges_agree_trivial_archives(self):
        verdict = subject.evaluate_consensus(False, "TRIVIAL", "BAJO")
        self.assertEqual(verdict.consensus, "ARCHIVE_LOW_INTEREST")


class EvaluateConsensusValidationTest(unittest.TestCase):
    def test_rejects_non_bool_nexus_flagged(self):
        with self.assertRaises(subject.ConsensusGateError):
            subject.evaluate_consensus("yes", "SOPHISTICATED", "ALTO")

    def test_rejects_invalid_gemini_assessment(self):
        with self.assertRaises(subject.ConsensusGateError):
            subject.evaluate_consensus(True, "MALICIOUS", "ALTO")

    def test_rejects_invalid_gemma_severity(self):
        with self.assertRaises(subject.ConsensusGateError):
            subject.evaluate_consensus(True, "SOPHISTICATED", "CRITICO")


if __name__ == "__main__":
    unittest.main()
