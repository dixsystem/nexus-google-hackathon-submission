"""Staging a validated Mission Generator batch to MISSION_PROPOSALS.md plus
a parallel candidate-contract document, and the batch human-approval gate
(evidence/MISSION_GENERATOR_DESIGN_V1.md sections 2.5/3/4.1, piece 3).

Real files under tempfile.TemporaryDirectory(), no mocks of governed logic
-- same discipline as every other test in this suite that touches staging.
Synthetic candidates only: Session 1 has no LLM integration yet (sign-off
#12)."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import mission_execution_contracts as execution_contracts
import mission_generator_candidates as candidates_module
import mission_proposal_staging as subject
import provider_capability_registry as capability_registry


def _candidate(**replace):
    values = dict(
        mission_id="M-100", mission_name="Provider health re-check",
        objective="Verify the registered provider health after a queue batch",
        capability_id="external.providers.health.v1",
        parameters=(("format", "raw bytes"),),
        depends_on=(),
        acceptance_criteria=("providers health returns PASS",),
        rationale="synthetic candidate for Session 1 staging validation only",
        generation_id="a" * 64,
    )
    values.update(replace)
    return candidates_module.GeneratedMissionCandidateV1(**values)


class MissionProposalStagingTest(unittest.TestCase):
    def setUp(self):
        self.registry = capability_registry.default_provider_capability_registry()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.proposals_path = Path(self.directory.name) / "MISSION_PROPOSALS.md"
        self.contracts_path = Path(self.directory.name) / "MISSION_PROPOSAL_CONTRACTS.json"
        self.candidates_path = Path(self.directory.name) / "MISSION_PROPOSAL_CANDIDATES.json"

    def stage(self, candidates, **kwargs):
        return subject.stage_proposal_batch(
            candidates, registry=self.registry,
            proposals_path=self.proposals_path, contracts_path=self.contracts_path,
            candidates_path=self.candidates_path,
            **kwargs,
        )

    def load(self, **kwargs):
        return subject.load_proposal_batch(
            registry=self.registry,
            proposals_path=self.proposals_path, contracts_path=self.contracts_path,
            candidates_path=self.candidates_path,
            **kwargs,
        )

    def test_01_valid_batch_stages_both_documents_on_disk(self):
        batch = self.stage((_candidate(),))
        self.assertTrue(self.proposals_path.exists())
        self.assertTrue(self.contracts_path.exists())
        self.assertEqual(len(batch.candidates), 1)
        self.assertEqual(len(batch.contracts), 1)

    def test_02_proposals_document_contains_expected_row_shape(self):
        self.stage((_candidate(),))
        text = self.proposals_path.read_text(encoding="utf-8")
        self.assertIn("| M-100 |", text)
        self.assertIn("`PROPOSED`", text)
        self.assertIn("`GENERATED`", text)
        self.assertIn("generation_id: " + "a" * 64, text)

    def test_03_proposals_sha256_matches_exact_bytes_on_disk(self):
        batch = self.stage((_candidate(),))
        on_disk = hashlib.sha256(self.proposals_path.read_bytes()).hexdigest()
        self.assertEqual(batch.proposals_sha256, on_disk)

    def test_04_candidate_contract_binds_queue_sha256_to_proposals_hash(self):
        batch = self.stage((_candidate(),))
        self.assertEqual(len(batch.contracts), 1)
        self.assertEqual(batch.contracts[0].queue_sha256, batch.proposals_sha256)
        self.assertEqual(batch.contracts[0].mission_id, "M-100")

    def test_05_contract_document_round_trips_through_real_load_contracts(self):
        batch = self.stage((_candidate(),))
        reloaded = execution_contracts.load_contracts(self.contracts_path)
        self.assertEqual(reloaded, batch.contracts)

    def test_06_staging_never_writes_outside_the_three_supplied_paths(self):
        directory = Path(self.directory.name)
        self.stage((_candidate(),))
        self.assertEqual(
            sorted(path.name for path in directory.iterdir()),
            sorted({self.proposals_path.name, self.contracts_path.name, self.candidates_path.name}),
        )

    def test_07_invalid_batch_never_touches_disk(self):
        bad = _candidate(capability_id="nexus.invented.capability.v1")
        with self.assertRaises(candidates_module.UnregisteredCandidateCapabilityError):
            self.stage((bad,))
        self.assertFalse(self.proposals_path.exists())
        self.assertFalse(self.contracts_path.exists())
        self.assertFalse(self.candidates_path.exists())

    def test_08_batch_gate_rejects_without_literal_confirm_true(self):
        batch = self.stage((_candidate(),))
        for bad_confirm in (False, 1, "true", None):
            with self.assertRaises(subject.BatchHumanConfirmationRequiredError):
                subject.check_batch_human_gate(batch, ("M-100",), confirm=bad_confirm)

    def test_09_batch_gate_approves_full_batch(self):
        batch = self.stage((_candidate(mission_id="M-100"), _candidate(mission_id="M-101", generation_id="b" * 64)))
        approved = subject.check_batch_human_gate(
            batch, ("M-100", "M-101"), confirm=True
        )
        self.assertEqual({candidate.mission_id for candidate in approved}, {"M-100", "M-101"})

    def test_10_batch_gate_approves_partial_subset(self):
        batch = self.stage((_candidate(mission_id="M-100"), _candidate(mission_id="M-101", generation_id="b" * 64)))
        approved = subject.check_batch_human_gate(batch, ("M-101",), confirm=True)
        self.assertEqual([candidate.mission_id for candidate in approved], ["M-101"])

    def test_11_batch_gate_rejects_mission_id_outside_batch(self):
        batch = self.stage((_candidate(),))
        with self.assertRaises(subject.InvalidBatchApprovalError):
            subject.check_batch_human_gate(batch, ("M-999",), confirm=True)

    def test_12_batch_gate_rejects_empty_approval(self):
        batch = self.stage((_candidate(),))
        with self.assertRaises(subject.InvalidBatchApprovalError):
            subject.check_batch_human_gate(batch, (), confirm=True)

    def test_13_batch_gate_rejects_stale_proposals_file(self):
        batch = self.stage((_candidate(),))
        self.proposals_path.write_text("tampered after staging", encoding="utf-8")
        with self.assertRaises(subject.StaleProposalBatchError):
            subject.check_batch_human_gate(batch, ("M-100",), confirm=True)

    def test_14_gate_never_writes_mission_queue_or_execution_contracts_files(self):
        # No file named MISSION_QUEUE.md or MISSION_EXECUTION_CONTRACTS.json
        # exists anywhere the staging/gate functions could have written --
        # sign-off #6's CSI-ceiling boundary, proved by absence, not intent.
        directory = Path(self.directory.name)
        batch = self.stage((_candidate(),))
        subject.check_batch_human_gate(batch, ("M-100",), confirm=True)
        names = {path.name for path in directory.iterdir()}
        self.assertNotIn("MISSION_QUEUE.md", names)
        self.assertNotIn("MISSION_EXECUTION_CONTRACTS.json", names)

    def test_15_source_never_mentions_the_canonical_queue_or_contract_paths(self):
        source = (Path(__file__).parents[1] / "mission_proposal_staging.py").read_text(encoding="utf-8")
        self.assertNotIn('"MISSION_QUEUE.md"', source)
        self.assertNotIn('"MISSION_EXECUTION_CONTRACTS.json"', source)

    # -- Session 4: load_proposal_batch (two-phase, separate-process approval) --

    def test_16_load_proposal_batch_reconstructs_full_fidelity_candidate(self):
        original = _candidate(
            depends_on=(), acceptance_criteria=("a", "b"), rationale="why this one",
        )
        self.stage((original,))
        batch = self.load()
        self.assertEqual(len(batch.candidates), 1)
        reloaded = batch.candidates[0]
        self.assertEqual(reloaded, original)

    def test_17_loaded_batch_feeds_check_batch_human_gate_unmodified(self):
        self.stage((_candidate(),))
        batch = self.load()
        approved = subject.check_batch_human_gate(batch, ("M-100",), confirm=True)
        self.assertEqual([candidate.mission_id for candidate in approved], ["M-100"])

    def test_18_load_rejects_tampered_candidate_field(self):
        self.stage((_candidate(),))
        document = json.loads(self.candidates_path.read_text(encoding="utf-8"))
        document["candidates"][0]["mission_id"] = "not-a-valid-id"
        self.candidates_path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaises(candidates_module.InvalidGeneratedMissionCandidateError):
            self.load()

    def test_19_load_rejects_stale_proposals_file(self):
        self.stage((_candidate(),))
        self.proposals_path.write_text("tampered after staging", encoding="utf-8")
        with self.assertRaises(subject.StaleProposalBatchError):
            self.load()

    def test_20_load_rejects_stale_contracts_file(self):
        self.stage((_candidate(),))
        self.contracts_path.write_bytes(self.contracts_path.read_bytes() + b" ")
        with self.assertRaises(subject.StaleProposalBatchError):
            self.load()

    def test_21_load_rejects_malformed_document_schema(self):
        self.stage((_candidate(),))
        self.candidates_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
        with self.assertRaises(subject.InvalidProposalCandidateDocumentError):
            self.load()

    def test_22_load_re_validates_against_fresh_existing_mission_ids(self):
        # existing_mission_ids is supplied fresh at load time, never inherited
        # from stage time -- a collision that did not exist at stage time
        # must still be caught here (sign-off #3).
        self.stage((_candidate(),))
        with self.assertRaises(candidates_module.DuplicateCandidateMissionIdError):
            self.load(existing_mission_ids=frozenset({"M-100"}))

    def test_23_load_detects_documents_edited_independently(self):
        # Mutating only MISSION_PROPOSAL_CANDIDATES.json's objective (a field
        # that also lives in MISSION_EXECUTION_CONTRACTS.json) must be caught
        # even though neither file's own hash changed relative to itself --
        # this is a cross-document consistency check, not a staleness check.
        self.stage((_candidate(),))
        document = json.loads(self.candidates_path.read_text(encoding="utf-8"))
        tampered_objective = document["candidates"][0]["objective"] + " (edited)"
        document["candidates"][0]["objective"] = tampered_objective
        tampered_bytes = json.dumps(document).encode("utf-8")
        self.candidates_path.write_bytes(tampered_bytes)
        with self.assertRaises(subject.InconsistentProposalDocumentsError):
            self.load()


if __name__ == "__main__":
    unittest.main()
