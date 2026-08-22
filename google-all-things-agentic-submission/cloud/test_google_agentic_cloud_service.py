from __future__ import annotations

from contextlib import contextmanager
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

import google_agentic_cloud_service as subject
import red_team_session


class CloudServiceTests(unittest.TestCase):
    def test_offline_reaches_staging_without_authority(self):
        result = subject.run_cloud_demo(mode="offline", environ={})
        self.assertEqual(result["status"], "STAGED")
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["authority_effects"], "NONE")
        self.assertEqual(len(result["artifacts"]), 3)

    def test_real_requires_explicit_model_before_transport(self):
        with self.assertRaises(subject.CloudDemoConfigurationError):
            subject.run_cloud_demo(mode="real", environ={})


# -- M-7a: red team endpoints (code only, not deployed) -----------------


@contextmanager
def _running_server():
    """Servidor HTTP real en loopback (127.0.0.1, puerto efímero) -- NO es
    un despliegue (ni gcloud, ni Cloud Run): es la forma estándar de
    probar un http.server.BaseHTTPRequestHandler sin fabricar objetos
    request/rfile/wfile a mano."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), subject.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _http_request(url, *, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class RedTeamOfflineFunctionTests(unittest.TestCase):
    """Ejercita run_cloud_redteam_session() directamente, igual estilo que
    CloudServiceTests ya hace con run_cloud_demo() -- sin red real: el modo
    offline usa el mismo backend determinista local que /demo/offline."""

    def setUp(self):
        subject._QUARANTINE_STORE = subject.QuarantineStore()

    def tearDown(self):
        subject._QUARANTINE_STORE = subject.QuarantineStore()

    def test_offline_redteam_session_completes_with_no_escalations_to_persist(self):
        result = subject.run_cloud_redteam_session(rounds=1, environ={})
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(result["incident_count"], 1)
        self.assertEqual(result["authority_effects"], "NONE")
        self.assertIn("quarantine_report", result)
        # El backend offline siempre devuelve el mismo candidato legítimo
        # fijo (google_agentic_demo._offline_candidate_json()) sin importar
        # el prompt -- así que en este modo cada ronda es un
        # VALIDATION_BYPASS determinista, nunca ejecutado, y NUNCA pasa por
        # el triple filtro -- nada se persiste a cuarentena en este modo.
        self.assertEqual(result["validation_bypass_count"], 1)
        self.assertEqual(result["escalated_incident_ids"], [])

    def test_rejects_rounds_above_hard_cap(self):
        with self.assertRaises(subject.CloudDemoConfigurationError):
            subject.run_cloud_redteam_session(rounds=subject.MAX_REDTEAM_ROUNDS + 1, environ={})

    def test_rejects_non_positive_rounds(self):
        with self.assertRaises(subject.CloudDemoConfigurationError):
            subject.run_cloud_redteam_session(rounds=0, environ={})

    def test_default_and_hard_cap_constants(self):
        self.assertEqual(subject.DEFAULT_REDTEAM_ROUNDS, 5)
        self.assertEqual(subject.MAX_REDTEAM_ROUNDS, 15)


class RedTeamEndpointHTTPTests(unittest.TestCase):
    """POST /redteam -- siempre mockea run_cloud_redteam_session para
    probar el contrato HTTP (rutas, límite de rondas, fail-closed) sin
    reejecutar el pipeline completo en cada test."""

    def test_post_redteam_uses_default_rounds_when_no_body(self):
        fake_result = {"status": "COMPLETED", "session_id": "s1"}
        with mock.patch.object(subject, "run_cloud_redteam_session", return_value=fake_result) as mocked:
            with _running_server() as base_url:
                status, payload = _http_request(f"{base_url}/redteam", method="POST")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "COMPLETED")
        mocked.assert_called_once_with(rounds=subject.DEFAULT_REDTEAM_ROUNDS)

    def test_post_redteam_honors_rounds_in_body(self):
        with mock.patch.object(
            subject, "run_cloud_redteam_session", return_value={"status": "COMPLETED"}
        ) as mocked:
            with _running_server() as base_url:
                status, _ = _http_request(f"{base_url}/redteam", method="POST", body={"rounds": 3})
        self.assertEqual(status, 200)
        mocked.assert_called_once_with(rounds=3)

    def test_post_redteam_rejects_rounds_above_hard_cap_without_calling_orchestrator(self):
        with mock.patch.object(subject, "run_cloud_redteam_session") as mocked:
            with _running_server() as base_url:
                status, payload = _http_request(f"{base_url}/redteam", method="POST", body={"rounds": 16})
        self.assertEqual(status, 400)
        self.assertEqual(payload["status"], "FAILED")
        mocked.assert_not_called()

    def test_post_redteam_rejects_non_positive_rounds_without_calling_orchestrator(self):
        with mock.patch.object(subject, "run_cloud_redteam_session") as mocked:
            with _running_server() as base_url:
                status, payload = _http_request(f"{base_url}/redteam", method="POST", body={"rounds": 0})
        self.assertEqual(status, 400)
        mocked.assert_not_called()

    def test_post_redteam_rejects_malformed_json_body(self):
        with mock.patch.object(subject, "run_cloud_redteam_session") as mocked:
            with _running_server() as base_url:
                request = urllib.request.Request(
                    f"{base_url}/redteam", data=b"not json", method="POST"
                )
                try:
                    urllib.request.urlopen(request, timeout=10)
                    status = 200
                except urllib.error.HTTPError as exc:
                    status = exc.code
        self.assertEqual(status, 400)
        mocked.assert_not_called()

    def test_post_redteam_fails_closed_without_leaking_internal_diagnostics(self):
        with mock.patch.object(
            subject, "run_cloud_redteam_session",
            side_effect=RuntimeError("secret internal detail should never leak"),
        ):
            with _running_server() as base_url:
                status, payload = _http_request(f"{base_url}/redteam", method="POST")
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "FAILED")
        self.assertEqual(payload["authority_effects"], "NONE")
        self.assertNotIn("secret internal detail", json.dumps(payload))

    def test_post_redteam_fails_closed_with_structured_category_when_available(self):
        class _FakeError(Exception):
            category = "CONFIGURATION"

        with mock.patch.object(subject, "run_cloud_redteam_session", side_effect=_FakeError("boom")):
            with _running_server() as base_url:
                status, payload = _http_request(f"{base_url}/redteam", method="POST")
        self.assertEqual(status, 503)
        self.assertEqual(payload["category"], "CONFIGURATION")


class QuarantineEndpointHTTPTests(unittest.TestCase):
    def setUp(self):
        subject._QUARANTINE_STORE = subject.QuarantineStore()

    def tearDown(self):
        subject._QUARANTINE_STORE = subject.QuarantineStore()

    def test_get_quarantine_returns_stored_report(self):
        subject._QUARANTINE_STORE.put("sess-x:round-1", "# quarantine report body")
        with _running_server() as base_url:
            status, payload = _http_request(f"{base_url}/quarantine/sess-x:round-1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["incident_id"], "sess-x:round-1")
        self.assertEqual(payload["quarantine_report"], "# quarantine report body")

    def test_get_quarantine_returns_404_for_unknown_incident(self):
        with _running_server() as base_url:
            status, payload = _http_request(f"{base_url}/quarantine/does-not-exist")
        self.assertEqual(status, 404)
        self.assertEqual(payload["status"], "NOT_FOUND")

    def test_get_quarantine_url_decodes_the_incident_id(self):
        subject._QUARANTINE_STORE.put("sess-y:round-2", "# report")
        with _running_server() as base_url:
            status, payload = _http_request(f"{base_url}/quarantine/sess-y%3Around-2")
        self.assertEqual(status, 200)
        self.assertEqual(payload["incident_id"], "sess-y:round-2")

    def test_unknown_path_still_404s(self):
        with _running_server() as base_url:
            status, payload = _http_request(f"{base_url}/nope")
        self.assertEqual(status, 404)
        self.assertEqual(payload["status"], "NOT_FOUND")

    def test_health_endpoint_still_works(self):
        with _running_server() as base_url:
            status, payload = _http_request(f"{base_url}/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "LIVE")


# -- M-7a paso 2: persistencia real de cuarentena en Cloud Storage ------


class FakeBlob:
    def __init__(self, client, bucket_name, path):
        self._client = client
        self.bucket_name = bucket_name
        self.path = path
        self.content_type = None

    def upload_from_string(self, data, content_type=None):
        self.content_type = content_type
        self._client.data.setdefault(self.bucket_name, {})[self.path] = data

    def download_as_bytes(self):
        try:
            return self._client.data[self.bucket_name][self.path]
        except KeyError:
            raise FileNotFoundError(f"no such object: {self.bucket_name}/{self.path}")


class FakeBucket:
    def __init__(self, client, name):
        self._client = client
        self.name = name

    def blob(self, path):
        return FakeBlob(self._client, self.name, path)


class FakeStorageClient:
    """Mismo estilo que test_mission_executor.py, adaptado al contrato
    .bucket(name) (no .create_bucket) que QuarantineStore usa -- data se
    guarda a nivel de cliente para que dos llamadas separadas a .bucket()
    (una al escribir, otra al leer) vean el mismo contenido, igual que un
    Client de GCS real contra el mismo bucket."""

    def __init__(self):
        self.data = {}  # {bucket_name: {object_path: bytes}}
        self.bucket_calls = []

    def bucket(self, bucket_name):
        self.bucket_calls.append(bucket_name)
        self.data.setdefault(bucket_name, {})
        return FakeBucket(self, bucket_name)


def _fake_session_result(*, session_id, escalated_incident_ids, quarantine_report):
    return red_team_session.RedTeamSessionResult(
        session_id=session_id,
        incidents=(),
        quarantine_report=quarantine_report,
        validation_bypasses=(),
        escalated_incident_ids=tuple(escalated_incident_ids),
    )


class QuarantineStorePersistenceTest(unittest.TestCase):
    """Tests unitarios de QuarantineStore contra un storage_client fake --
    nunca toca Cloud Storage real."""

    def test_put_then_get_round_trips_the_same_content(self):
        store = subject.QuarantineStore(FakeStorageClient())
        store.put("sess-a:round-1", "# report A")
        self.assertEqual(store.get("sess-a:round-1"), "# report A")

    def test_get_returns_none_for_missing_incident(self):
        store = subject.QuarantineStore(FakeStorageClient())
        self.assertIsNone(store.get("does-not-exist"))

    def test_writes_land_in_the_shared_default_bucket(self):
        client = FakeStorageClient()
        store = subject.QuarantineStore(client)
        store.put("sess-a:round-1", "# report A")
        self.assertIn(subject.DEFAULT_QUARANTINE_BUCKET_NAME, client.data)
        self.assertIn("incidents/sess-a:round-1.json", client.data[subject.DEFAULT_QUARANTINE_BUCKET_NAME])

    def test_get_rejects_unsafe_incident_id_without_touching_storage(self):
        client = FakeStorageClient()
        store = subject.QuarantineStore(client)
        self.assertIsNone(store.get("../../etc/passwd"))
        self.assertEqual(client.bucket_calls, [])

    def test_in_memory_fallback_used_when_storage_client_is_none(self):
        store = subject.QuarantineStore(storage_client=None)
        store.put("sess-a:round-1", "# report A")
        self.assertEqual(store.get("sess-a:round-1"), "# report A")


class RedTeamPersistenceIntegrationTest(unittest.TestCase):
    """Escribe en /redteam (con run_red_team_session mockeado para forzar
    un ESCALATE, ya que el modo offline real nunca escala) y lee después
    con /quarantine/<id> -- ambos contra el MISMO storage_client fake, para
    probar la persistencia real de punta a punta, no solo el fallback
    in-memory."""

    def setUp(self):
        self.fake_client = FakeStorageClient()
        subject._QUARANTINE_STORE = subject.QuarantineStore(self.fake_client)

    def tearDown(self):
        subject._QUARANTINE_STORE = subject.QuarantineStore()

    def test_run_cloud_redteam_session_persists_only_escalated_incidents(self):
        fake_result = _fake_session_result(
            session_id="sess-escalate", escalated_incident_ids=("sess-escalate:round-1",),
            quarantine_report="# quarantine report with an escalation",
        )
        with mock.patch.object(subject, "run_red_team_session", return_value=fake_result):
            result = subject.run_cloud_redteam_session(rounds=1, environ={})

        self.assertEqual(result["escalated_incident_ids"], ["sess-escalate:round-1"])
        self.assertEqual(
            subject._QUARANTINE_STORE.get("sess-escalate:round-1"),
            "# quarantine report with an escalation",
        )
        self.assertIn(subject.DEFAULT_QUARANTINE_BUCKET_NAME, self.fake_client.data)

    def test_run_cloud_redteam_session_persists_nothing_when_no_escalations(self):
        fake_result = _fake_session_result(
            session_id="sess-quiet", escalated_incident_ids=(), quarantine_report="# nothing escalated",
        )
        with mock.patch.object(subject, "run_red_team_session", return_value=fake_result):
            subject.run_cloud_redteam_session(rounds=1, environ={})
        self.assertEqual(self.fake_client.data, {})

    def test_write_via_redteam_then_read_via_quarantine_endpoint_matches(self):
        fake_result = _fake_session_result(
            session_id="sess-e2e", escalated_incident_ids=("sess-e2e:round-1",),
            quarantine_report="# end to end quarantine report",
        )
        with mock.patch.object(subject, "run_red_team_session", return_value=fake_result):
            with _running_server() as base_url:
                post_status, _ = _http_request(f"{base_url}/redteam", method="POST")
                self.assertEqual(post_status, 200)
                get_status, payload = _http_request(f"{base_url}/quarantine/sess-e2e:round-1")

        self.assertEqual(get_status, 200)
        self.assertEqual(payload["quarantine_report"], "# end to end quarantine report")
        # Confirma que la lectura vino del storage fake, no de un fallback
        # in-memory paralelo -- el mismo objeto quedó en fake_client.data.
        self.assertIn(
            "incidents/sess-e2e:round-1.json",
            self.fake_client.data[subject.DEFAULT_QUARANTINE_BUCKET_NAME],
        )


if __name__ == "__main__":
    unittest.main()

