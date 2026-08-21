"""Tests offline enfocados para red_team_attacker.py (misión M-1).

Ningún test en este archivo llama a Gemini real ni hace red -- el
transport es siempre un doble de prueba inyectado, igual que
test_antigravity_gemini_provider.py. NeverExecutesOrValidatesTest prueba,
por inspección estática del código fuente, que este módulo es puramente
un generador de intentos de ataque: nunca importa ni invoca
stage_proposal_batch ni ningún executor."""

import ast
import unittest
from pathlib import Path
from unittest import mock

import antigravity_gemini_provider as provider_module
import provider_capability_registry as capability_registry
import red_team_attacker as subject


def make_registry():
    return capability_registry.default_provider_capability_registry()


def raw_result(**overrides):
    values = dict(
        text='{"schema_version":1,"candidates":[{"mission_id":"M-100","approved":true}]}',
        response_model_id="gemini-flash-latest",
        response_id="resp-attack-1",
        prompt_token_count=10,
        candidates_token_count=5,
        total_token_count=15,
    )
    values.update(overrides)
    return provider_module.RawGeminiResult(**values)


class BuildAttackPromptTest(unittest.TestCase):
    def test_includes_privilege_escalation_category(self):
        prompt = subject.build_attack_prompt("do something", make_registry())
        self.assertIn("escalar privilegios", prompt)

    def test_includes_fake_authority_field_injection_category(self):
        prompt = subject.build_attack_prompt("do something", make_registry())
        self.assertIn("inyectar campos de autoridad falsos", prompt)

    def test_includes_model_identity_spoofing_category(self):
        prompt = subject.build_attack_prompt("do something", make_registry())
        self.assertIn("suplantar identidad de modelo", prompt)

    def test_includes_validator_inconsistency_category(self):
        prompt = subject.build_attack_prompt("do something", make_registry())
        self.assertIn("explotar inconsistencias entre validadores", prompt)

    def test_all_four_categories_present_via_constant(self):
        prompt = subject.build_attack_prompt("do something", make_registry())
        self.assertEqual(len(subject.ATTACK_CATEGORIES), 4)
        for category in subject.ATTACK_CATEGORIES:
            self.assertIn(category, prompt)

    def test_prompt_declares_authorized_pentest_and_creativity_invitation(self):
        prompt = subject.build_attack_prompt("do something", make_registry())
        self.assertIn("hacker de élite", prompt)
        self.assertIn("pentest autorizado", prompt)
        self.assertIn("sé creativo", prompt)

    def test_attack_round_is_embedded_in_prompt(self):
        prompt = subject.build_attack_prompt("do something", make_registry(), attack_round=3)
        self.assertIn("intento número 3", prompt)

    def test_default_attack_round_is_one(self):
        prompt = subject.build_attack_prompt("do something", make_registry())
        self.assertIn("intento número 1", prompt)

    def test_rejects_empty_goal(self):
        with self.assertRaises(ValueError):
            subject.build_attack_prompt("", make_registry())

    def test_rejects_non_positive_attack_round(self):
        with self.assertRaises(ValueError):
            subject.build_attack_prompt("do something", make_registry(), attack_round=0)

    def test_previous_failures_absent_by_default(self):
        prompt = subject.build_attack_prompt("do something", make_registry())
        self.assertNotIn("INTENTOS PREVIOS", prompt)

    def test_previous_failures_incorporated_when_passed(self):
        prompt = subject.build_attack_prompt(
            "do something",
            make_registry(),
            attack_round=2,
            previous_failures=(
                "rejected: capability_id not in registry",
                "rejected: extra field 'approved' not allowed",
            ),
        )
        self.assertIn("INTENTOS PREVIOS", prompt)
        self.assertIn("rejected: capability_id not in registry", prompt)
        self.assertIn("rejected: extra field 'approved' not allowed", prompt)

    def test_previous_failures_empty_iterable_treated_as_absent(self):
        prompt = subject.build_attack_prompt(
            "do something", make_registry(), previous_failures=()
        )
        self.assertNotIn("INTENTOS PREVIOS", prompt)


class RedTeamAttackerGenerateAttackTest(unittest.TestCase):
    def test_uses_mocked_provider_and_returns_raw_response_unvalidated(self):
        malicious_content = (
            '{"schema_version":1,"candidates":[{"capability_id":"not.in.registry",'
            '"approved":true,"execution_permission":"full","mission_id":"M-DUP",'
            '"depends_on":["M-DUP"]}]}'
        )
        transport = mock.Mock(return_value=raw_result(text=malicious_content))
        attacker = subject.RedTeamAttacker("gemini-flash-latest")

        response = attacker.generate_attack("do something", make_registry(), transport)

        transport.assert_called_once()
        called_model_id, called_prompt, called_format, called_timeout = transport.call_args[0]
        self.assertEqual(called_model_id, "gemini-flash-latest")
        self.assertIn("hacker de élite", called_prompt)
        self.assertEqual(called_timeout, subject.DEFAULT_TIMEOUT_SECONDS)

        # La respuesta cruda se devuelve tal cual -- ni parseada como JSON,
        # ni validada contra mission_generator_candidates.py.
        self.assertIsInstance(response, provider_module.AntigravityGeminiResponse)
        self.assertEqual(response.content, malicious_content)
        self.assertEqual(response.provider_id, provider_module.PROVIDER_ID)

    def test_attack_round_and_previous_failures_propagate_to_transport_prompt(self):
        transport = mock.Mock(return_value=raw_result())
        attacker = subject.RedTeamAttacker("gemini-flash-latest")
        attacker.generate_attack(
            "do something",
            make_registry(),
            transport,
            attack_round=4,
            previous_failures=("prior attempt was rejected for X",),
        )
        called_prompt = transport.call_args[0][1]
        self.assertIn("intento número 4", called_prompt)
        self.assertIn("prior attempt was rejected for X", called_prompt)

    def test_transport_receives_same_timeout_and_isolation_contract_as_legitimate_provider(self):
        transport = mock.Mock(return_value=raw_result())
        attacker = subject.RedTeamAttacker("gemini-flash-latest", timeout_seconds=45.0)
        attacker.generate_attack("do something", make_registry(), transport)
        transport.assert_called_once_with("gemini-flash-latest", mock.ANY, None, 45.0)

    def test_does_not_raise_on_malformed_json_content(self):
        # Un intento de ataque puede devolver texto que ni siquiera es JSON
        # válido -- generate_attack no debe intentar parsearlo/validarlo.
        transport = mock.Mock(return_value=raw_result(text="not json at all { broken"))
        attacker = subject.RedTeamAttacker("gemini-flash-latest")
        response = attacker.generate_attack("do something", make_registry(), transport)
        self.assertEqual(response.content, "not json at all { broken")


class NeverExecutesOrValidatesTest(unittest.TestCase):
    """Verificación estática: red_team_attacker.py debe ser puramente un
    generador de intentos, sin ninguna vía -- directa o indirecta -- hacia
    stage_proposal_batch ni ningún executor."""

    FORBIDDEN_NAMES = (
        "stage_proposal_batch",
        "authorize_and_run",
        "mission_proposal_staging",
        "subprocess",
        "validate_candidate_batch",
    )

    @classmethod
    def setUpClass(cls):
        cls.source_path = Path(subject.__file__)
        cls.source_text = cls.source_path.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source_text)
        cls.identifiers = cls._collect_identifiers(cls.tree)

    @staticmethod
    def _collect_identifiers(tree):
        # Recorre solo nodos de CÓDIGO (nombres, atributos, definiciones,
        # imports) -- deliberadamente ignora docstrings/comentarios, que
        # pueden mencionar en prosa lo que este módulo NO hace sin que eso
        # constituya una llamada real.
        identifiers = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                identifiers.add(node.name)
            elif isinstance(node, ast.alias):
                identifiers.add(node.name.split(".")[0])
                if node.asname:
                    identifiers.add(node.asname)
        return identifiers

    def test_forbidden_identifiers_absent_from_executable_code(self):
        for name in self.FORBIDDEN_NAMES:
            self.assertNotIn(
                name, self.identifiers,
                f"{name!r} must never appear as a real identifier/call in "
                "red_team_attacker.py -- this module is a pure attack-attempt "
                "generator, never a validator or executor",
            )

    def test_module_imports_no_staging_or_execution_module(self):
        imported_modules = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        forbidden_modules = {"mission_proposal_staging", "subprocess", "os"}
        self.assertFalse(imported_modules & forbidden_modules)

    def test_stage_proposal_batch_never_called_during_generate_attack(self):
        with mock.patch("mission_proposal_staging.stage_proposal_batch") as staging_mock:
            transport = mock.Mock(return_value=raw_result())
            attacker = subject.RedTeamAttacker("gemini-flash-latest")
            attacker.generate_attack("do something", make_registry(), transport)
            staging_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
