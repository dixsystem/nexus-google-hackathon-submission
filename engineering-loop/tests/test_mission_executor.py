"""Tests para mission_executor.py (misión M-6). Ningún test toca Cloud
Storage real -- storage_client es siempre un fake en memoria inyectado,
igual que el transport de Gemini en el resto del proyecto."""

import unittest

import mission_executor as subject
import mission_generator_candidates as candidates_module


def make_candidate(**overrides):
    values = dict(
        mission_id="M-901",
        mission_name="Verify governed proposal pipeline",
        objective="Prove the pipeline reaches a verifiable executed state",
        capability_id="external.providers.health.v1",
        parameters=(),
        depends_on=(),
        acceptance_criteria=("result is uploaded and publicly verifiable",),
        rationale="M-6 real executor smoke test",
        generation_id="a" * 64,
    )
    values.update(overrides)
    return candidates_module.GeneratedMissionCandidateV1(**values)


class FakeBlob:
    def __init__(self, bucket_name, path):
        self.bucket_name = bucket_name
        self.path = path
        self.uploaded_data = None
        self.content_type = None

    def upload_from_string(self, data, content_type=None):
        self.uploaded_data = data
        self.content_type = content_type


class FakeBucket:
    def __init__(self, name):
        self.name = name
        self.blobs = {}

    def blob(self, path):
        blob = FakeBlob(self.name, path)
        self.blobs[path] = blob
        return blob


class FakeStorageClient:
    def __init__(self):
        self.buckets = {}
        self.create_bucket_calls = []

    def create_bucket(self, bucket_name):
        self.create_bucket_calls.append(bucket_name)
        bucket = FakeBucket(bucket_name)
        self.buckets[bucket_name] = bucket
        return bucket


class BucketCreateFailingStorageClient:
    def create_bucket(self, bucket_name):
        raise RuntimeError("simulated GCS outage: bucket creation failed")


class UploadFailingBlob:
    def upload_from_string(self, data, content_type=None):
        raise RuntimeError("simulated GCS outage: upload failed")


class UploadFailingBucket:
    def __init__(self, name):
        self.name = name

    def blob(self, path):
        return UploadFailingBlob()


class UploadFailingStorageClient:
    def create_bucket(self, bucket_name):
        return UploadFailingBucket(bucket_name)


def fixed_clock():
    from datetime import datetime, timezone
    return datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


class SuccessfulExecutionTest(unittest.TestCase):
    def test_valid_allow_generates_bucket_object_and_url(self):
        candidate = make_candidate()
        allow_decision = subject.authorize_allow_decision(candidate, approved_candidates=(candidate,))
        storage_client = FakeStorageClient()
        executor = subject.MissionExecutor(storage_client, clock=fixed_clock)

        result = executor.execute_allowed_mission(candidate, allow_decision)

        self.assertEqual(result.status, subject.STATUS_EXECUTED)
        self.assertIsNone(result.failure_reason)
        self.assertEqual(result.mission_id, "M-901")
        self.assertEqual(result.bucket_name, "nexus-mission-m-901-aaaaaaaa")
        self.assertEqual(result.object_path, "result.json")
        self.assertEqual(
            result.public_url,
            "https://storage.googleapis.com/nexus-mission-m-901-aaaaaaaa/result.json",
        )
        self.assertIsNotNone(result.result_hash)
        self.assertEqual(len(result.result_hash), 64)

        self.assertIn(result.bucket_name, storage_client.buckets)
        uploaded_blob = storage_client.buckets[result.bucket_name].blobs["result.json"]
        self.assertEqual(uploaded_blob.content_type, "application/json")
        self.assertIn(b'"mission_id":"M-901"', uploaded_blob.uploaded_data)
        self.assertIn(b'"result_hash":', uploaded_blob.uploaded_data)

    def test_executed_at_uses_injected_clock(self):
        candidate = make_candidate()
        allow_decision = subject.authorize_allow_decision(candidate, approved_candidates=(candidate,))
        executor = subject.MissionExecutor(FakeStorageClient(), clock=fixed_clock)
        result = executor.execute_allowed_mission(candidate, allow_decision)
        self.assertEqual(result.executed_at, fixed_clock().isoformat())


class FailsClosedWithoutValidAllowTest(unittest.TestCase):
    def test_bare_bool_true_is_never_accepted(self):
        candidate = make_candidate()
        executor = subject.MissionExecutor(FakeStorageClient())
        with self.assertRaises(subject.MissionExecutorError) as caught:
            executor.execute_allowed_mission(candidate, True)
        self.assertEqual(caught.exception.category, "NOT_ALLOWED")

    def test_none_allow_decision_is_never_accepted(self):
        candidate = make_candidate()
        executor = subject.MissionExecutor(FakeStorageClient())
        with self.assertRaises(subject.MissionExecutorError):
            executor.execute_allowed_mission(candidate, None)

    def test_duck_typed_object_pretending_to_be_an_allow_decision_is_rejected(self):
        class FakeAllow:
            mission_id = "M-901"
            decision_hash = "0" * 64

        candidate = make_candidate()
        executor = subject.MissionExecutor(FakeStorageClient())
        with self.assertRaises(subject.MissionExecutorError):
            executor.execute_allowed_mission(candidate, FakeAllow())

    def test_authorize_allow_decision_rejects_candidate_not_in_approved_tuple(self):
        candidate = make_candidate()
        other_candidate = make_candidate(mission_id="M-902", generation_id="b" * 64)
        with self.assertRaises(subject.MissionExecutorError) as caught:
            subject.authorize_allow_decision(candidate, approved_candidates=(other_candidate,))
        self.assertEqual(caught.exception.category, "NOT_ALLOWED")

    def test_authorize_allow_decision_rejects_non_tuple_approved_candidates(self):
        candidate = make_candidate()
        with self.assertRaises(subject.MissionExecutorError):
            subject.authorize_allow_decision(candidate, approved_candidates=[candidate])

    def test_allow_decision_cannot_be_reused_against_a_different_mission_id(self):
        candidate = make_candidate()
        allow_decision = subject.authorize_allow_decision(candidate, approved_candidates=(candidate,))
        other_candidate = make_candidate(mission_id="M-902", generation_id="b" * 64)
        executor = subject.MissionExecutor(FakeStorageClient())
        with self.assertRaises(subject.MissionExecutorError) as caught:
            executor.execute_allowed_mission(other_candidate, allow_decision)
        self.assertEqual(caught.exception.category, "NOT_ALLOWED")

    def test_allow_decision_cannot_be_reused_against_a_tampered_candidate_with_same_id(self):
        candidate = make_candidate()
        allow_decision = subject.authorize_allow_decision(candidate, approved_candidates=(candidate,))
        tampered_candidate = make_candidate(objective="a completely different, unapproved objective")
        executor = subject.MissionExecutor(FakeStorageClient())
        with self.assertRaises(subject.MissionExecutorError):
            executor.execute_allowed_mission(tampered_candidate, allow_decision)

    def test_hand_built_allow_decision_with_invented_hash_is_rejected(self):
        candidate = make_candidate()
        forged = subject.AllowDecision(mission_id="M-901", decision_hash="f" * 64)
        executor = subject.MissionExecutor(FakeStorageClient())
        with self.assertRaises(subject.MissionExecutorError):
            executor.execute_allowed_mission(candidate, forged)

    def test_storage_client_is_never_touched_when_allow_is_invalid(self):
        candidate = make_candidate()
        storage_client = FakeStorageClient()
        executor = subject.MissionExecutor(storage_client)
        with self.assertRaises(subject.MissionExecutorError):
            executor.execute_allowed_mission(candidate, True)
        self.assertEqual(storage_client.create_bucket_calls, [])


class StorageFailureDegradesGracefullyTest(unittest.TestCase):
    def test_create_bucket_failure_returns_failed_status_without_raising(self):
        candidate = make_candidate()
        allow_decision = subject.authorize_allow_decision(candidate, approved_candidates=(candidate,))
        executor = subject.MissionExecutor(BucketCreateFailingStorageClient(), clock=fixed_clock)

        result = executor.execute_allowed_mission(candidate, allow_decision)

        self.assertEqual(result.status, subject.STATUS_FAILED)
        self.assertIsNotNone(result.failure_reason)
        self.assertIn("simulated GCS outage", result.failure_reason)
        self.assertIsNone(result.public_url)
        self.assertIsNone(result.object_path)
        self.assertIsNone(result.result_hash)
        self.assertEqual(result.bucket_name, "nexus-mission-m-901-aaaaaaaa")

    def test_upload_failure_returns_failed_status_without_raising(self):
        candidate = make_candidate()
        allow_decision = subject.authorize_allow_decision(candidate, approved_candidates=(candidate,))
        executor = subject.MissionExecutor(UploadFailingStorageClient(), clock=fixed_clock)

        result = executor.execute_allowed_mission(candidate, allow_decision)

        self.assertEqual(result.status, subject.STATUS_FAILED)
        self.assertIn("upload failed", result.failure_reason)
        self.assertIsNone(result.public_url)


class BucketNamingTest(unittest.TestCase):
    def test_bucket_name_is_deterministic_for_the_same_candidate(self):
        candidate = make_candidate()
        allow_decision = subject.authorize_allow_decision(candidate, approved_candidates=(candidate,))
        executor = subject.MissionExecutor(FakeStorageClient(), clock=fixed_clock)
        first = executor.execute_allowed_mission(candidate, allow_decision)
        second = executor.execute_allowed_mission(candidate, allow_decision)
        self.assertEqual(first.bucket_name, second.bucket_name)

    def test_bucket_name_differs_by_mission_id(self):
        candidate_a = make_candidate(mission_id="M-901", generation_id="a" * 64)
        candidate_b = make_candidate(mission_id="M-902", generation_id="a" * 64)
        allow_a = subject.authorize_allow_decision(candidate_a, approved_candidates=(candidate_a,))
        allow_b = subject.authorize_allow_decision(candidate_b, approved_candidates=(candidate_b,))
        executor = subject.MissionExecutor(FakeStorageClient(), clock=fixed_clock)
        result_a = executor.execute_allowed_mission(candidate_a, allow_a)
        result_b = executor.execute_allowed_mission(candidate_b, allow_b)
        self.assertNotEqual(result_a.bucket_name, result_b.bucket_name)

    def test_bucket_name_differs_by_generation_id_hash_for_same_mission_id(self):
        candidate_a = make_candidate(mission_id="M-901", generation_id="a" * 64)
        candidate_b = make_candidate(mission_id="M-901", generation_id="b" * 64)
        allow_a = subject.authorize_allow_decision(candidate_a, approved_candidates=(candidate_a,))
        allow_b = subject.authorize_allow_decision(candidate_b, approved_candidates=(candidate_b,))
        executor = subject.MissionExecutor(FakeStorageClient(), clock=fixed_clock)
        result_a = executor.execute_allowed_mission(candidate_a, allow_a)
        result_b = executor.execute_allowed_mission(candidate_b, allow_b)
        self.assertNotEqual(result_a.bucket_name, result_b.bucket_name)

    def test_bucket_name_matches_expected_naming_convention(self):
        candidate = make_candidate(mission_id="M-905", generation_id="deadbeef" + "0" * 56)
        allow_decision = subject.authorize_allow_decision(candidate, approved_candidates=(candidate,))
        executor = subject.MissionExecutor(FakeStorageClient(), clock=fixed_clock)
        result = executor.execute_allowed_mission(candidate, allow_decision)
        self.assertEqual(result.bucket_name, "nexus-mission-m-905-deadbeef")


class ConfigurationTest(unittest.TestCase):
    def test_storage_client_is_mandatory(self):
        with self.assertRaises(subject.MissionExecutorError) as caught:
            subject.MissionExecutor(None)
        self.assertEqual(caught.exception.category, "CONFIGURATION")

    def test_rejects_non_candidate_object(self):
        executor = subject.MissionExecutor(FakeStorageClient())
        with self.assertRaises(subject.MissionExecutorError):
            executor.execute_allowed_mission({"not": "a candidate"}, True)


if __name__ == "__main__":
    unittest.main()
