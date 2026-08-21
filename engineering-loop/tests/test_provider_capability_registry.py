"""Fail-first tests for the closed provider capability registry."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_execution_session as session_fixture


ROOT = Path(__file__).parents[1]


def load_registry():
    path = ROOT / "provider_capability_registry.py"
    if not path.exists():
        raise AssertionError(
            "fail-first expected missing module: "
            "provider_capability_registry.py"
        )
    spec = importlib.util.spec_from_file_location(
        "provider_capability_registry_tested", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProviderCapabilityRegistryTest(unittest.TestCase):
    def module(self):
        if not hasattr(self, "_module"):
            self._module = load_registry()
        return self._module

    def registration(
        self, capability_id="external.providers.health.v1",
        provider_id="local-supervised-bridge",
        operation="providers health",
        implementation_id="governed-external-execution-adapter",
    ):
        module = self.module()
        return module.ProviderRegistration(
            module.CapabilityDefinition(
                capability_id, operation, 1,
            ),
            module.ProviderDescriptor(
                provider_id, implementation_id, 1,
            ),
        )

    def test_01_all_models_and_registry_are_immutable(self):
        module = self.module()
        registry = module.build_provider_capability_registry((
            self.registration(),
        ))
        with self.assertRaises(FrozenInstanceError):
            registry.registrations = ()
        with self.assertRaises(FrozenInstanceError):
            registry.registrations[0].provider.provider_id = "changed"
        self.assertFalse(hasattr(registry, "register"))

    def test_02_registration_order_is_canonical_and_deterministic(self):
        module = self.module()
        second = self.registration(
            "external.system.status.v1", "local-status-provider",
            "system status", "status-adapter",
        )
        first = self.registration()
        left = module.build_provider_capability_registry((second, first))
        right = module.build_provider_capability_registry((first, second))
        self.assertEqual(left, right)
        self.assertEqual(
            left.capability_ids,
            ("external.providers.health.v1", "external.system.status.v1"),
        )

    def test_03_exact_capability_lookup_returns_immutable_resolution(self):
        module = self.module()
        registry = module.build_provider_capability_registry((
            self.registration(),
        ))
        resolved = registry.resolve("external.providers.health.v1")
        self.assertEqual(resolved.capability.operation, "providers health")
        self.assertEqual(
            resolved.provider.provider_id, "local-supervised-bridge"
        )
        with self.assertRaises(FrozenInstanceError):
            resolved.capability_id = "changed"

    def test_04_unknown_capability_fails_closed_without_fallback(self):
        module = self.module()
        registry = module.default_provider_capability_registry()
        before = registry.registrations
        with self.assertRaises(module.UnknownCapabilityError):
            registry.resolve("external.unknown.v1")
        self.assertEqual(registry.registrations, before)

    def test_05_exact_provider_operation_binding_is_required(self):
        module = self.module()
        registry = module.default_provider_capability_registry()
        resolved = registry.require_registration(
            "local-supervised-bridge", "providers health"
        )
        self.assertEqual(
            resolved.capability_id, "external.providers.health.v1"
        )
        for provider, operation in (
            ("other", "providers health"),
            ("local-supervised-bridge", "other"),
        ):
            with self.assertRaises(module.UnknownCapabilityError):
                registry.require_registration(provider, operation)

    def test_06_duplicate_capability_or_provider_operation_is_rejected(self):
        module = self.module()
        first = self.registration()
        duplicates = (
            replace(first, provider=module.ProviderDescriptor(
                "another-provider", "another-adapter", 1
            )),
            self.registration(
                "external.providers.other.v1",
                operation="providers health",
            ),
        )
        for duplicate in duplicates:
            with self.assertRaises(module.ClosedWorldRegistrationError):
                module.build_provider_capability_registry(
                    (first, duplicate)
                )

    def test_07_invalid_identifiers_versions_and_operations_are_rejected(self):
        module = self.module()
        invalid = (
            module.CapabilityDefinition("UPPER", "providers health", 1),
            module.CapabilityDefinition("valid.id.v1", "", 1),
            module.CapabilityDefinition("valid.id.v1", "providers health", 0),
            module.ProviderDescriptor("bad id", "adapter", 1),
            module.ProviderDescriptor("provider", "Bad Adapter", 1),
            module.ProviderDescriptor("provider", "adapter", 0),
        )
        for value in invalid:
            with self.assertRaises(module.InvalidCapabilityDefinitionError):
                module.build_provider_capability_registry((
                    module.ProviderRegistration(
                        value,
                        module.ProviderDescriptor("provider", "adapter", 1),
                    )
                    if isinstance(value, module.CapabilityDefinition)
                    else module.ProviderRegistration(
                        module.CapabilityDefinition(
                            "valid.id.v1", "providers health", 1
                        ),
                        value,
                    )
                ,))

    def test_08_registration_requires_exact_closed_model_types(self):
        module = self.module()
        for invalid in ({}, object(), ()):
            with self.assertRaises(module.ClosedWorldRegistrationError):
                module.build_provider_capability_registry((invalid,))

    def test_09_default_registry_is_closed_and_reproducible(self):
        module = self.module()
        first = module.default_provider_capability_registry()
        second = module.default_provider_capability_registry()
        self.assertEqual(first, second)
        self.assertEqual(len(first.registrations), 11)
        self.assertEqual(
            first.resolve("nexus.repository.mutation.v1").provider,
            second.resolve("nexus.repository.mutation.v1").provider,
        )
        self.assertEqual(
            first.resolve("external.providers.health.v1").provider,
            second.resolve("external.providers.health.v1").provider,
        )
        self.assertEqual(
            first.resolve("np1.governed.corrective.write.v1").provider,
            second.resolve("np1.governed.corrective.write.v1").provider,
        )
        self.assertEqual(
            first.resolve(
                "np1.governed.corrective.multi-artifact.publish.v1").provider,
            second.resolve(
                "np1.governed.corrective.multi-artifact.publish.v1").provider,
        )
        self.assertEqual(
            first.resolve("nexus.llm.reasoning.night-senior.v1").provider,
            module.ProviderDescriptor(
                "ollama-qwen-night-senior",
                "governed-llm-execution-adapter-ollama-night-senior",
                1,
            ),
        )

    def test_10_runtime_authorization_is_the_registry_consumer(self):
        session_source = (ROOT / "execution_session.py").read_text()
        runtime_source = (
            ROOT / "governed_runtime_authorization_transition.py"
        ).read_text()
        self.assertNotIn("provider_capability_registry", session_source)
        self.assertIn(
            "self.capability_registry.require_registration", runtime_source
        )

    def test_11_execution_session_has_no_registry_failure_policy(self):
        source = (ROOT / "execution_session.py").read_text()
        self.assertNotIn("CAPABILITY_RESOLUTION", source)
        self.assertNotIn("require_registration", source)

    def test_12_registry_is_pure_replay_and_journal_neutral(self):
        source = (ROOT / "provider_capability_registry.py").read_text()
        for forbidden in (
            "importlib", "socket", "requests", "subprocess",
            "journal_", "replay", "plugin", "entry_point",
        ):
            self.assertNotIn(forbidden, source)


class GovernedTypedExecutionExtensionTest(unittest.TestCase):
    @staticmethod
    def fixture():
        import fcr004_governed_capability_adapter as adapter
        import fcr004_verification_profile as profile
        import governed_typed_execution_contract as typed
        import np1_deterministic_verifier as verifier
        import np1_functional_independence_contracts as contracts
        frozen_manifest = Path("/home/alons/DixKeeper/NP1_CANONICAL_BASELINE_MANIFEST_V1.json")
        registry = Path("/home/alons/nexus-v1/NEXUS_PRODUCT_NP1_ARCHITECTURE_CORRECTION_INCREMENT_2_EVIDENCE_REGISTRY_V1.md")
        report = Path("/home/alons/nexus-v1/NEXUS_PRODUCT_NP1_ARCHITECTURE_CORRECTION_INCREMENT_2_EXECUTION_REPORT_V1.md")
        normative = Path("/home/alons/nexus-v1/NEXUS_PRODUCT_NP1_FCR-004_FINAL_CLOSURE_RESOLUTION_V1.md")
        registry_v2 = Path("/home/alons/nexus-v1/NEXUS_PRODUCT_NP1_COMPLETE_NORMATIVE_REGISTRY_V2.md")
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        manifest = root / "NP1_CURRENT_POST_FCR_BASELINE_MANIFEST.json"
        output = root / "FCR004_TYPED_EXTENSION_TEST_EVIDENCE.md"
        manifest_value = json.loads(frozen_manifest.read_text(encoding="utf-8"))
        for entry in manifest_value["entries"]:
            entry["sha256"] = hashlib.sha256(
                Path(entry["path"]).read_bytes()
            ).hexdigest()
        identity = dict(manifest_value)
        identity.pop("generated_at", None)
        identity.pop("canonical_manifest_hash", None)
        manifest_value["canonical_manifest_hash"] = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode("utf-8") + b"\n"
        ).hexdigest()
        manifest.write_text(
            json.dumps(manifest_value, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        historical = contracts.freeze_historical_input_v1(
            artifact_paths=(registry, report), provenance=("fixture",))
        prepared = contracts.prepare_authority_receipt_v1(
            authority_receipt_id="AR-FCR004-TYPED-TEST", issuer_id="human:alons",
            issuer_role="HUMAN_AUTHORITY", mission_id="NP1-FCR004-TYPED-TEST",
            gate_id="FCR-004-PRESERVATION",
            baseline_hash=profile.canonical_manifest_identity(manifest),
            input_hash=historical.historical_input_hash,
            read_paths=(str(manifest), str(registry), str(report), str(normative), str(registry_v2)),
            write_paths=(str(output),),
            allowed_operations=(adapter.OPERATION,), forbidden_operations=("shell",),
            acceptance_criteria=("33/33",), verification_rules_hash=profile.rules_hash_v1(),
            required_verifier_capability=verifier.VERIFIER_CAPABILITY,
            producer_capability=adapter.CAPABILITY_ID, max_attempts=1,
            attempt_index=0, issued_at="2026-08-09T00:00:00Z")
        activation_values = {
            "prepared_receipt_hash": prepared.authority_receipt_hash,
            "human_authority_reference": "TEST-FIXTURE-NOT-REAL-AUTHORITY",
            "decision": "AUTHORIZED",
        }
        activation = contracts.HumanAuthorityActivationV1(
            **activation_values,
            activation_hash=contracts.domain_hash(contracts.ACTIVATION_DOMAIN, activation_values))
        active = contracts.activate_authority_receipt_v1(prepared, activation)
        payload = adapter.FCR004PreservationPayloadV1(
            "FCR-004-PRESERVATION", active.baseline_hash,
            historical.historical_input_hash, historical.artifact_set_hash,
            active.verification_rules_hash, adapter.CAPABILITY_ID,
            str(manifest), str(registry), str(report), str(normative),
            str(registry_v2), str(output))
        context = typed.build_context(
            mission_id=active.mission_id, proposal_id=None, project_id="PRJ-NEXUS",
            capability_id=adapter.CAPABILITY_ID, operation=adapter.OPERATION,
            payload_schema_id=adapter.PAYLOAD_SCHEMA_ID, payload_schema_version=1,
            typed_payload=payload, authority_receipt=active, provenance=("fixture",))
        return temporary, typed, adapter, verifier, contracts, prepared, active, context

    def test_13_v1_provider_health_backward_compatibility(self):
        import governed_typed_result_verification as routing
        self.assertEqual(routing.verify_provider_health_payload(
            b'{"providers":{"local":{"status":"ready"}}}'), "PASS")

    def test_14_v2_context_is_deterministic_and_hash_bound(self):
        _tmp, typed, _a, _v, _c, _p, active, context = self.fixture()
        rebuilt = typed.build_context(
            mission_id=context.mission_id, proposal_id=None, project_id="PRJ-NEXUS",
            capability_id=context.capability_id, operation=context.operation,
            payload_schema_id=context.payload_schema_id, payload_schema_version=1,
            typed_payload=context.typed_payload, authority_receipt=active,
            provenance=("fixture",))
        self.assertEqual(context, rebuilt)

    def test_15_free_payload_and_tamper_are_rejected(self):
        _tmp, typed, _a, _v, _c, _p, _active, context = self.fixture()
        with self.assertRaises(typed.TypedExecutionContractError):
            typed.payload_hash({"command": "sh"})
        with self.assertRaises(typed.TypedExecutionContractError):
            typed.validate_context(replace(context, payload_hash="0" * 64))

    def test_16_inactive_or_mismatched_authority_is_rejected(self):
        _tmp, typed, _a, _v, _c, prepared, _active, context = self.fixture()
        with self.assertRaises(typed.TypedExecutionContractError):
            typed.build_context(
                mission_id=prepared.mission_id, proposal_id=None, project_id="PRJ-NEXUS",
                capability_id=context.capability_id, operation=context.operation,
                payload_schema_id=context.payload_schema_id, payload_schema_version=1,
                typed_payload=context.typed_payload, authority_receipt=prepared,
                provenance=("fixture",))

    def test_17_repeated_authority_consumption_fails_closed(self):
        _tmp, _t, _a, verifier, contracts, _p, active, _context = self.fixture()
        ledger = verifier.ReplayLedgerV1(); ledger.consume(active.authority_receipt_hash)
        with self.assertRaises(contracts.FunctionalIndependenceError):
            ledger.consume(active.authority_receipt_hash)

    def test_18_registered_verifier_routing_rejects_unknown_result(self):
        import governed_operation_profile_registry as profiles
        registry = profiles.default_operation_profile_registry()
        self.assertEqual(registry.resolve_capability(
            "np1.fcr004.preservation.verify.v1").verifier_id,
            "fcr004-deterministic-verifier-v1")
        with self.assertRaises(profiles.OperationProfileRegistryError):
            registry.resolve_result("np1.fcr004.preservation.verify.v1", "unknown")

    def test_19_real_fcr004_preflight_reaches_existing_verifier(self):
        _tmp, _t, adapter, _v, _c, _p, _active, context = self.fixture()
        historical = adapter.preflight(context)
        self.assertEqual(historical.producer_identity_status, "UNKNOWN")
        self.assertEqual(len(historical.artifacts), 2)

    def test_20_arbitrary_capability_and_shell_are_not_routable(self):
        import governed_operation_profile_registry as profiles
        registry = profiles.default_operation_profile_registry()
        with self.assertRaises(profiles.OperationProfileRegistryError):
            registry.resolve_capability("arbitrary.shell.v1")
        self.assertFalse(any("shell" in p.operation for p in registry.profiles))

    def test_21_prepared_receipt_chain_preflight_does_not_activate(self):
        _tmp, typed, adapter, _v, _c, prepared, _active, active_context = self.fixture()
        prepared_context = typed.build_context(
            mission_id=prepared.mission_id, proposal_id=None, project_id="PRJ-NEXUS",
            capability_id=adapter.CAPABILITY_ID, operation=adapter.OPERATION,
            payload_schema_id=adapter.PAYLOAD_SCHEMA_ID, payload_schema_version=1,
            typed_payload=active_context.typed_payload, authority_receipt=prepared,
            provenance=("fixture",), require_active=False)
        historical = adapter.preflight(prepared_context, require_active=False)
        self.assertEqual(prepared_context.authority_receipt.decision, "PREPARED")
        self.assertEqual(historical.producer_identity_status, "UNKNOWN")

    def test_22_authority_identity_mutations_fail_closed(self):
        _tmp, typed, _adapter, _v, _contracts, _prepared, _active, context = self.fixture()
        mutations = (
            replace(context, mission_id="WRONG"),
            replace(context, capability_id="external.providers.health.v1"),
            replace(context, payload_hash="0" * 64),
            replace(context, authority_receipt_hash="0" * 64),
            replace(context, envelope_hash="0" * 64),
        )
        for value in mutations:
            with self.assertRaises(typed.TypedExecutionContractError):
                typed.validate_context(value)

    def test_23_consumed_receipt_is_rejected_during_preflight(self):
        _tmp, _typed, adapter, verifier, _contracts, _prepared, active, context = self.fixture()
        ledger = verifier.ReplayLedgerV1()
        ledger.consume(active.authority_receipt_hash)
        with self.assertRaises(adapter.FCR004GovernedAdapterError):
            adapter.preflight(context, replay_ledger=ledger)


if __name__ == "__main__":
    unittest.main()
