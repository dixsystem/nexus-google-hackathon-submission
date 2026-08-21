"""Tests para red_team_incident.py (misión M-2): hash-chaining, hash
génesis, construcción válida y serialización determinista. Puramente en
memoria -- sin I/O, sin red."""

import json
import unittest

import red_team_incident as subject


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
    return subject.build_incident(**values)


class BuildIncidentTest(unittest.TestCase):
    def test_valid_construction_produces_expected_fields(self):
        incident = make_incident()
        self.assertEqual(incident.session_id, "sess-001")
        self.assertEqual(incident.attack_round, 1)
        self.assertEqual(incident.timestamp, "2026-08-21T22:00:00Z")
        self.assertFalse(incident.blocked)
        self.assertIsNone(incident.rejection_reason)
        self.assertEqual(len(incident.incident_hash), 64)

    def test_first_incident_of_session_chains_to_genesis_hash(self):
        incident = make_incident()
        self.assertEqual(incident.previous_incident_hash, subject.GENESIS_INCIDENT_HASH)

    def test_explicit_previous_incident_hash_is_preserved(self):
        first = make_incident(attack_round=1)
        second = make_incident(attack_round=2, previous_incident_hash=first.incident_hash)
        self.assertEqual(second.previous_incident_hash, first.incident_hash)
        self.assertNotEqual(second.previous_incident_hash, subject.GENESIS_INCIDENT_HASH)

    def test_default_incident_id_is_deterministic(self):
        incident = make_incident(session_id="sess-XYZ", attack_round=3)
        self.assertEqual(incident.incident_id, "sess-XYZ:round-3")

    def test_explicit_incident_id_is_honored(self):
        incident = make_incident(incident_id="custom-id-1")
        self.assertEqual(incident.incident_id, "custom-id-1")

    def test_rejects_non_positive_attack_round(self):
        with self.assertRaises(subject.InvalidRedTeamIncidentError):
            make_incident(attack_round=0)

    def test_rejects_empty_session_id(self):
        with self.assertRaises(subject.InvalidRedTeamIncidentError):
            make_incident(session_id="")

    def test_rejects_non_bool_blocked(self):
        with self.assertRaises(subject.InvalidRedTeamIncidentError):
            make_incident(blocked="yes")

    def test_rejects_malformed_previous_incident_hash(self):
        with self.assertRaises(subject.InvalidRedTeamIncidentError):
            make_incident(previous_incident_hash="not-a-sha256")

    def test_blocked_incident_with_rejection_reason(self):
        incident = make_incident(blocked=True, rejection_reason="UnregisteredCandidateCapabilityError")
        self.assertTrue(incident.blocked)
        self.assertEqual(incident.rejection_reason, "UnregisteredCandidateCapabilityError")


class HashChainingTest(unittest.TestCase):
    def _build_chain(self, count=3):
        incidents = []
        previous_hash = None
        for round_number in range(1, count + 1):
            incident = make_incident(
                attack_round=round_number,
                raw_attack_payload=f'{{"attempt":{round_number}}}',
                previous_incident_hash=previous_hash,
            )
            incidents.append(incident)
            previous_hash = incident.incident_hash
        return tuple(incidents)

    def test_valid_chain_verifies(self):
        session = subject.build_session(
            session_id="sess-chain", started_at="2026-08-21T21:00:00Z",
            goal="pentest nexus", incidents=self._build_chain(),
        )
        self.assertTrue(subject.verify_session_chain(session))

    def test_altering_an_incident_breaks_the_chain_of_following_incidents(self):
        incidents = list(self._build_chain())
        # Alterar el payload del primer incidente sin recalcular su hash --
        # simula manipulación posterior a la construcción original.
        import dataclasses
        tampered_first = dataclasses.replace(incidents[0], raw_attack_payload='{"attempt":"TAMPERED"}')
        incidents[0] = tampered_first
        session = subject.build_session(
            session_id="sess-chain", started_at="2026-08-21T21:00:00Z",
            goal="pentest nexus", incidents=tuple(incidents),
        )
        self.assertFalse(subject.verify_session_chain(session))

    def test_reordering_incidents_breaks_the_chain(self):
        incidents = list(self._build_chain())
        incidents[0], incidents[1] = incidents[1], incidents[0]
        session = subject.build_session(
            session_id="sess-chain", started_at="2026-08-21T21:00:00Z",
            goal="pentest nexus", incidents=tuple(incidents),
        )
        self.assertFalse(subject.verify_session_chain(session))

    def test_single_incident_session_chain_is_valid(self):
        session = subject.build_session(
            session_id="sess-single", started_at="2026-08-21T21:00:00Z",
            goal="pentest nexus", incidents=(make_incident(),),
        )
        self.assertTrue(subject.verify_session_chain(session))

    def test_empty_session_chain_is_trivially_valid(self):
        session = subject.build_session(
            session_id="sess-empty", started_at="2026-08-21T21:00:00Z", goal="pentest nexus",
        )
        self.assertTrue(subject.verify_session_chain(session))


class SerializationTest(unittest.TestCase):
    def test_incident_to_json_is_deterministic(self):
        incident = make_incident()
        first = subject.incident_to_json(incident)
        second = subject.incident_to_json(incident)
        self.assertEqual(first, second)
        parsed = json.loads(first)
        self.assertEqual(parsed["session_id"], "sess-001")
        self.assertEqual(parsed["incident_hash"], incident.incident_hash)

    def test_incident_to_dict_round_trips_through_json(self):
        incident = make_incident(rejection_reason="blocked by validator")
        as_dict = subject.incident_to_dict(incident)
        round_tripped = json.loads(json.dumps(as_dict, sort_keys=True))
        self.assertEqual(round_tripped["rejection_reason"], "blocked by validator")

    def test_session_to_json_includes_all_incidents_in_order(self):
        session = subject.build_session(
            session_id="sess-ser", started_at="2026-08-21T21:00:00Z", goal="pentest nexus",
            incidents=(make_incident(attack_round=1), make_incident(attack_round=2, previous_incident_hash=None)),
        )
        payload = json.loads(subject.session_to_json(session))
        self.assertEqual(len(payload["incidents"]), 2)
        self.assertEqual(payload["incidents"][0]["attack_round"], 1)
        self.assertEqual(payload["incidents"][1]["attack_round"], 2)

    def test_session_to_json_is_byte_deterministic_across_calls(self):
        session = subject.build_session(
            session_id="sess-ser", started_at="2026-08-21T21:00:00Z", goal="pentest nexus",
            incidents=(make_incident(),),
        )
        self.assertEqual(subject.session_to_json(session), subject.session_to_json(session))


if __name__ == "__main__":
    unittest.main()
