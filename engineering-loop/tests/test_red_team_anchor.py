from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from unittest import mock

import red_team_anchor as subject
import red_team_incident
import red_team_merkle

# subprocess es un módulo singleton -- subject.subprocess y cualquier otro
# "import subprocess" son EL MISMO objeto. Capturar la referencia real a
# .run ANTES de que ningún test la parchee es la única forma de poder
# invocar el subprocess.run genuino (para mkdir, en los tests de
# anchor_session) mientras se mockean gh/git en el mismo mock.patch.object.
_REAL_SUBPROCESS_RUN = subject.subprocess.run


def _build_test_session(session_id="redteam-20260101T000000", n=3):
    previous = red_team_incident.GENESIS_INCIDENT_HASH
    incidents = []
    for round_number in range(1, n + 1):
        incident = red_team_incident.build_incident(
            session_id=session_id, attack_round=round_number,
            raw_attack_payload=f'{{"round":{round_number}}}',
            rejection_reason="UnregisteredCandidateCapabilityError: x",
            blocked=True, timestamp=f"2026-01-01T00:0{round_number}:00+00:00",
            previous_incident_hash=previous,
        )
        incidents.append(incident)
        previous = incident.incident_hash
    session = red_team_incident.build_session(
        session_id=session_id, started_at="2026-01-01T00:00:00+00:00",
        goal="test", incidents=tuple(incidents),
    )
    return session


def _fake_urlopen_response(payload_bytes):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.read.return_value = payload_bytes
    return response


class FetchSessionDocumentTests(unittest.TestCase):
    def test_fetch_session_document_parses_nested_json_on_found(self):
        session = _build_test_session()
        session_doc = red_team_incident.session_to_dict(session)
        wrapper = json.dumps(
            {"status": "FOUND", "incident_id": "session-x", "quarantine_report": json.dumps(session_doc)}
        ).encode("utf-8")

        with mock.patch.object(subject.urllib.request, "urlopen", return_value=_fake_urlopen_response(wrapper)):
            result = subject.fetch_session_document(session.session_id)

        self.assertEqual(result["session_id"], session.session_id)
        self.assertEqual(len(result["incidents"]), 3)

    def test_fetch_session_document_raises_on_http_404(self):
        with mock.patch.object(
            subject.urllib.request, "urlopen",
            side_effect=urllib.error.HTTPError("url", 404, "not found", None, None),
        ):
            with self.assertRaises(subject.AnchorError):
                subject.fetch_session_document("redteam-20260101T000000")

    def test_fetch_session_document_raises_when_status_not_found(self):
        wrapper = json.dumps({"status": "NOT_FOUND", "incident_id": "session-x"}).encode("utf-8")
        with mock.patch.object(subject.urllib.request, "urlopen", return_value=_fake_urlopen_response(wrapper)):
            with self.assertRaises(subject.AnchorError):
                subject.fetch_session_document("redteam-20260101T000000")


class BuildAnchorRecordTests(unittest.TestCase):
    def test_build_anchor_record_computes_root_and_leaves_from_incident_hashes(self):
        session = _build_test_session()
        session_doc = red_team_incident.session_to_dict(session)
        record = subject.build_anchor_record(session_doc, clock=lambda: mock.Mock(isoformat=lambda: "T"))

        expected_leaves = [incident.incident_hash for incident in session.incidents]
        self.assertEqual(record["leaf_hashes"], expected_leaves)
        self.assertEqual(record["merkle_root"], red_team_merkle.merkle_root(expected_leaves))
        self.assertEqual(record["incident_count"], 3)
        self.assertEqual(record["session_id"], session.session_id)


class VerifySessionTests(unittest.TestCase):
    def setUp(self):
        self.session = _build_test_session()
        self.session_doc = red_team_incident.session_to_dict(self.session)
        self.anchor_record = subject.build_anchor_record(self.session_doc)

    def test_verify_session_match_when_nothing_changed(self):
        with mock.patch.object(subject, "fetch_session_document", return_value=self.session_doc), \
             mock.patch.object(subject, "fetch_anchor_record", return_value=self.anchor_record):
            result = subject.verify_session(self.session.session_id)

        self.assertEqual(result["status"], "MATCH")
        self.assertEqual(result["merkle_root"], self.anchor_record["merkle_root"])

    def test_verify_session_detects_and_localizes_stale_hash_tamper(self):
        # Ataque simple: se edita un campo (raw_attack_payload) del
        # incidente del medio SIN recomputar su incident_hash guardado --
        # la capa Merkle no ve nada (los hashes guardados no cambiaron),
        # pero la recomputación por campo sí.
        tampered_doc = json.loads(json.dumps(self.session_doc))
        tampered_doc["incidents"][1]["raw_attack_payload"] = '{"round":999}'

        with mock.patch.object(subject, "fetch_session_document", return_value=tampered_doc), \
             mock.patch.object(subject, "fetch_anchor_record", return_value=self.anchor_record):
            result = subject.verify_session(self.session.session_id)

        self.assertEqual(result["status"], "TAMPER_DETECTED")
        self.assertFalse(result["chain_intact"])
        self.assertTrue(result["merkle_match"])  # hashes guardados no cambiaron
        self.assertEqual(result["leaf_index"], 1)
        self.assertEqual(result["incident_id"], self.session.incidents[1].incident_id)

    def test_verify_session_detects_and_localizes_anchor_mismatch_tamper(self):
        # Ataque sofisticado: campo Y hash reescritos de forma
        # autoconsistente (pasaría verify_session_chain) -- solo el
        # anchor externo (leaf_hashes ya publicado) lo detecta.
        forged_incident = red_team_incident.build_incident(
            session_id=self.session.session_id, attack_round=3,
            raw_attack_payload='{"round":999}',
            rejection_reason="UnregisteredCandidateCapabilityError: x",
            blocked=True, timestamp="2026-01-01T00:03:00+00:00",
            previous_incident_hash=self.session.incidents[1].incident_hash,
        )
        tampered_doc = json.loads(json.dumps(self.session_doc))
        tampered_doc["incidents"][2] = red_team_incident.incident_to_dict(forged_incident)

        with mock.patch.object(subject, "fetch_session_document", return_value=tampered_doc), \
             mock.patch.object(subject, "fetch_anchor_record", return_value=self.anchor_record):
            result = subject.verify_session(self.session.session_id)

        self.assertEqual(result["status"], "TAMPER_DETECTED")
        self.assertTrue(result["chain_intact"])  # autoconsistente -- la cadena interna no lo ve
        self.assertFalse(result["merkle_match"])
        self.assertEqual(result["leaf_index"], 2)
        self.assertEqual(result["expected_hash"], self.session.incidents[2].incident_hash)
        self.assertEqual(result["actual_hash"], forged_incident.incident_hash)


class AnchorSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = _build_test_session()
        self.session_doc = red_team_incident.session_to_dict(self.session)

    def test_anchor_session_success_commits_and_pushes(self):
        # Solo gh/git se mockean (nunca tocan red ni un repo real); mkdir
        # se deja pasar al subprocess.run REAL contra un tempdir real, para
        # que el open()/json.dump() posterior (también real) tenga un
        # directorio válido donde escribir -- ver docstring de anchor_session.
        calls = []

        def fake_run(args, **kwargs):
            if args[0] in ("gh", "git"):
                calls.append(args)
                result = mock.Mock()
                result.returncode = 0
                result.stdout = "abc123deadbeef\n" if args[:2] == ["git", "rev-parse"] else ""
                result.stderr = ""
                return result
            return _REAL_SUBPROCESS_RUN(args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp_dir:
            clone_dir = f"{tmp_dir}/clone"
            with mock.patch.object(subject, "fetch_session_document", return_value=self.session_doc), \
                 mock.patch.object(subject.subprocess, "run", side_effect=fake_run):
                result = subject.anchor_session(self.session.session_id, clone_dir=clone_dir)

        self.assertEqual(result["session_id"], self.session.session_id)
        self.assertEqual(result["commit_sha"], "abc123deadbeef")
        self.assertIn("abc123deadbeef", result["anchor_url"])
        git_push_calls = [c for c in calls if c[:2] == ["git", "push"]]
        self.assertEqual(len(git_push_calls), 1)

    def test_anchor_session_fails_closed_when_gh_clone_fails(self):
        def fake_run(args, **kwargs):
            if args[:2] == ["gh", "repo"]:
                result = mock.Mock()
                result.returncode = 1
                result.stderr = "gh: authentication required"
                return result
            return _REAL_SUBPROCESS_RUN(args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp_dir:
            clone_dir = f"{tmp_dir}/clone"
            with mock.patch.object(subject, "fetch_session_document", return_value=self.session_doc), \
                 mock.patch.object(subject.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(subject.AnchorError):
                    subject.anchor_session(self.session.session_id, clone_dir=clone_dir)


if __name__ == "__main__":
    unittest.main()
