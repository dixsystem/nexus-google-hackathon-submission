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
        subject._QUARANTINE_STORE.clear()

    def tearDown(self):
        subject._QUARANTINE_STORE.clear()

    def test_offline_redteam_session_completes_and_populates_quarantine_store(self):
        result = subject.run_cloud_redteam_session(rounds=1, environ={})
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["rounds"], 1)
        self.assertEqual(result["incident_count"], 1)
        self.assertEqual(result["authority_effects"], "NONE")
        self.assertIn("quarantine_report", result)
        # El backend offline siempre devuelve el mismo candidato legítimo
        # fijo (google_agentic_demo._offline_candidate_json()) sin importar
        # el prompt -- así que en este modo cada ronda es un
        # VALIDATION_BYPASS determinista, nunca ejecutado.
        self.assertEqual(result["validation_bypass_count"], 1)
        self.assertEqual(len(subject._QUARANTINE_STORE), 1)

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
        subject._QUARANTINE_STORE.clear()

    def tearDown(self):
        subject._QUARANTINE_STORE.clear()

    def test_get_quarantine_returns_stored_report(self):
        subject._QUARANTINE_STORE["sess-x:round-1"] = "# quarantine report body"
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
        subject._QUARANTINE_STORE["sess-y:round-2"] = "# report"
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


if __name__ == "__main__":
    unittest.main()

