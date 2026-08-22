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


class RedTeamRealModeFunctionTests(unittest.TestCase):
    """M-7a paso 3 (sesión supervisada, modo real): ejercita
    run_cloud_redteam_session(mode="real") con build_transport y
    run_red_team_session mockeados a nivel de módulo -- nunca se levanta
    el intérprete aislado real (.antigravity_isolated_venv) ni se gasta
    cuota real de Gemini/Gemma en tests."""

    def setUp(self):
        subject._QUARANTINE_STORE = subject.QuarantineStore()

    def tearDown(self):
        subject._QUARANTINE_STORE = subject.QuarantineStore()

    def test_real_mode_requires_gemini_model(self):
        with self.assertRaises(subject.CloudDemoConfigurationError) as ctx:
            subject.run_cloud_redteam_session(rounds=1, mode="real", environ={})
        self.assertEqual(ctx.exception.category, "CONFIGURATION")

    def test_real_mode_requires_gemini_api_key(self):
        with self.assertRaises(subject.CloudDemoConfigurationError) as ctx:
            subject.run_cloud_redteam_session(
                rounds=1, mode="real", environ={"GEMINI_MODEL": "gemini-3.5-flash"}
            )
        self.assertEqual(ctx.exception.category, "CONFIGURATION")

    def test_real_mode_hard_cap_is_lower_than_offline_hard_cap(self):
        self.assertLess(subject.MAX_REDTEAM_ROUNDS_REAL, subject.MAX_REDTEAM_ROUNDS)
        self.assertEqual(subject.MAX_REDTEAM_ROUNDS_REAL, 5)

    def test_real_mode_rejects_rounds_above_its_own_hard_cap_even_with_full_config(self):
        # rounds=6 sería válido en modo offline (cap 15) pero debe seguir
        # rechazándose en modo real (cap 5), incluso con GEMINI_MODEL y
        # GEMINI_API_KEY ya configurados -- la validación de rounds ocurre
        # antes de intentar construir ningún transport.
        with self.assertRaises(subject.CloudDemoConfigurationError):
            subject.run_cloud_redteam_session(
                rounds=subject.MAX_REDTEAM_ROUNDS_REAL + 1, mode="real",
                environ={"GEMINI_MODEL": "gemini-3.5-flash", "GEMINI_API_KEY": "fake-key"},
            )

    def test_real_mode_rejects_unsupported_mode_value(self):
        with self.assertRaises(subject.CloudDemoConfigurationError):
            subject.run_cloud_redteam_session(rounds=1, mode="bogus", environ={})

    def test_real_mode_wires_the_real_transport_for_attacker_and_lets_assessor_reuse_it(self):
        fake_transport = object()
        captured = {}

        def fake_build_transport(mode, *, model_id, environ):
            captured["build_transport_mode"] = mode
            captured["build_transport_model_id"] = model_id
            return fake_transport, model_id

        fake_result = _fake_session_result(
            session_id="sess-real", escalated_incident_ids=(), quarantine_report="",
        )

        def fake_run_red_team_session(goal, registry, transport, **kwargs):
            captured["transport"] = transport
            captured["rounds"] = kwargs.get("rounds")
            captured["model_id"] = kwargs.get("model_id")
            captured["use_gemma_fallback"] = kwargs.get("use_gemma_fallback")
            # gemini_assessor no se pasa explícitamente desde
            # run_cloud_redteam_session -- por diseño, run_red_team_session
            # reutiliza el `transport` recibido (el mismo real) cuando
            # gemini_assessor es None (ver docstring de red_team_session.py).
            captured["gemini_assessor"] = kwargs.get("gemini_assessor")
            captured["gemini_assessor_transport"] = kwargs.get("gemini_assessor_transport")
            return fake_result

        with mock.patch.object(subject, "build_transport", side_effect=fake_build_transport):
            with mock.patch.object(subject, "run_red_team_session", side_effect=fake_run_red_team_session):
                result = subject.run_cloud_redteam_session(
                    rounds=2, mode="real",
                    environ={"GEMINI_MODEL": "gemini-3.5-flash", "GEMINI_API_KEY": "fake-key"},
                )

        self.assertEqual(captured["build_transport_mode"], "real")
        self.assertEqual(captured["build_transport_model_id"], "gemini-3.5-flash")
        self.assertIs(captured["transport"], fake_transport)
        self.assertEqual(captured["rounds"], 2)
        self.assertTrue(captured["use_gemma_fallback"])
        self.assertIsNone(captured["gemini_assessor"])
        self.assertIsNone(captured["gemini_assessor_transport"])
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(result["mode"], "real")


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
        mocked.assert_called_once_with(rounds=subject.DEFAULT_REDTEAM_ROUNDS, mode=subject.DEFAULT_REDTEAM_MODE)

    def test_post_redteam_honors_rounds_in_body(self):
        with mock.patch.object(
            subject, "run_cloud_redteam_session", return_value={"status": "COMPLETED"}
        ) as mocked:
            with _running_server() as base_url:
                status, _ = _http_request(f"{base_url}/redteam", method="POST", body={"rounds": 3})
        self.assertEqual(status, 200)
        mocked.assert_called_once_with(rounds=3, mode=subject.DEFAULT_REDTEAM_MODE)

    def test_post_redteam_honors_mode_in_body(self):
        with mock.patch.object(
            subject, "run_cloud_redteam_session", return_value={"status": "COMPLETED"}
        ) as mocked:
            with _running_server() as base_url:
                status, _ = _http_request(
                    f"{base_url}/redteam", method="POST", body={"mode": "real", "rounds": 2}
                )
        self.assertEqual(status, 200)
        mocked.assert_called_once_with(rounds=2, mode="real")

    def test_post_redteam_rejects_unsupported_mode_without_calling_orchestrator(self):
        with mock.patch.object(subject, "run_cloud_redteam_session") as mocked:
            with _running_server() as base_url:
                status, payload = _http_request(f"{base_url}/redteam", method="POST", body={"mode": "bogus"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["category"], "CONFIGURATION")
        mocked.assert_not_called()

    def test_post_redteam_rejects_rounds_above_real_hard_cap_without_calling_orchestrator(self):
        # 6 rondas es válido en modo offline (cap 15) pero debe rechazarse
        # con 400 en modo real (cap 5, MAX_REDTEAM_ROUNDS_REAL) -- la capa
        # HTTP debe aplicar el mismo límite más bajo antes de invocar el
        # orquestador, no solo la capa de función.
        with mock.patch.object(subject, "run_cloud_redteam_session") as mocked:
            with _running_server() as base_url:
                status, payload = _http_request(
                    f"{base_url}/redteam", method="POST",
                    body={"mode": "real", "rounds": subject.MAX_REDTEAM_ROUNDS_REAL + 1},
                )
        self.assertEqual(status, 400)
        self.assertEqual(payload["category"], "CONFIGURATION")
        mocked.assert_not_called()

    def test_post_redteam_accepts_real_mode_rounds_up_to_its_hard_cap(self):
        with mock.patch.object(
            subject, "run_cloud_redteam_session", return_value={"status": "COMPLETED"}
        ) as mocked:
            with _running_server() as base_url:
                status, _ = _http_request(
                    f"{base_url}/redteam", method="POST",
                    body={"mode": "real", "rounds": subject.MAX_REDTEAM_ROUNDS_REAL},
                )
        self.assertEqual(status, 200)
        mocked.assert_called_once_with(rounds=subject.MAX_REDTEAM_ROUNDS_REAL, mode="real")

    def test_post_redteam_fails_closed_with_configuration_category_when_gemini_api_key_missing_in_real_mode(self):
        # Integración de extremo a extremo del requisito de fallo cerrado:
        # sin mockear run_cloud_redteam_session, solo el entorno del
        # proceso (sin GEMINI_API_KEY) -- nunca llega a build_transport.
        with mock.patch.dict("os.environ", {"GEMINI_MODEL": "gemini-3.5-flash"}, clear=True):
            with _running_server() as base_url:
                status, payload = _http_request(f"{base_url}/redteam", method="POST", body={"mode": "real"})
        self.assertEqual(status, 503)
        self.assertEqual(payload["category"], "CONFIGURATION")

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


# -- PASO A (sesión supervisada, despliegue): cliente real de Cloud -----
# Storage detrás de un flag explícito de entorno --------------------------


class FakeStorageModule:
    """Sustituye al paquete google.cloud.storage real -- inyectado vía
    storage_module=, nunca importado. Client() se registra como llamada
    (construct_calls) pero nunca toca red: es un stand-in puro, igual que
    FakeStorageClient sustituye a una instancia ya construida en los tests
    de QuarantineStorePersistenceTest más arriba."""

    def __init__(self):
        self.construct_calls = 0
        self.client_instance = object()

    def Client(self):
        self.construct_calls += 1
        return self.client_instance


class BuildStorageClientTests(unittest.TestCase):
    def test_returns_none_when_flag_absent(self):
        fake_module = FakeStorageModule()
        result = subject._build_storage_client({}, storage_module=fake_module)
        self.assertIsNone(result)
        self.assertEqual(fake_module.construct_calls, 0)

    def test_returns_none_when_flag_is_not_exactly_true(self):
        fake_module = FakeStorageModule()
        for value in ("false", "0", "1", "yes", "truee", ""):
            with self.subTest(value=value):
                result = subject._build_storage_client(
                    {"ENABLE_REAL_STORAGE": value}, storage_module=fake_module
                )
                self.assertIsNone(result)
        self.assertEqual(fake_module.construct_calls, 0)

    def test_constructs_client_when_flag_is_true(self):
        fake_module = FakeStorageModule()
        result = subject._build_storage_client(
            {"ENABLE_REAL_STORAGE": "true"}, storage_module=fake_module
        )
        self.assertIs(result, fake_module.client_instance)
        self.assertEqual(fake_module.construct_calls, 1)

    def test_flag_is_case_and_whitespace_tolerant(self):
        for value in ("True", "TRUE ", " true"):
            with self.subTest(value=value):
                fake_module = FakeStorageModule()
                result = subject._build_storage_client(
                    {"ENABLE_REAL_STORAGE": value}, storage_module=fake_module
                )
                self.assertIs(result, fake_module.client_instance)

    def test_default_environ_without_flag_returns_none(self):
        # Sin pasar environ explícito, usa os.environ real -- en el entorno
        # de test/CI la variable no está definida, así que debe devolver
        # None, igual que el comportamiento de hoy antes de este cambio.
        with mock.patch.dict("os.environ", {}, clear=False):
            import os as os_module

            os_module.environ.pop("ENABLE_REAL_STORAGE", None)
            result = subject._build_storage_client(storage_module=FakeStorageModule())
        self.assertIsNone(result)

    def test_module_level_quarantine_store_defaults_to_in_memory_fallback(self):
        # El singleton _QUARANTINE_STORE se construye una sola vez al
        # importar el módulo, con el os.environ real de ese momento (sin
        # ENABLE_REAL_STORAGE en CI/test) -- confirma que sigue siendo el
        # mismo fallback in-memory de siempre, sin regresión de
        # comportamiento por defecto.
        store = subject.QuarantineStore(subject._build_storage_client())
        self.assertIsNone(store._storage_client)


if __name__ == "__main__":
    unittest.main()

