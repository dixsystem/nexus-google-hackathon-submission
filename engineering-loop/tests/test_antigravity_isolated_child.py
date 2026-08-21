"""Tests offline unitarios de antigravity_isolated_child.py (M-AG010 Fase
2/9/13/14). Estos tests llaman las funciones del modulo directamente (sin
spawnear un subprocess real) para cubrir rapido el espacio de casos del
protocolo/backend; la prueba de punta a punta con un subprocess REAL del
propio archivo vive en test_antigravity_e2e_isolated_child.py (Fase 15)."""

from __future__ import annotations

import io
import json
import unittest

import antigravity_isolated_child as subject


def _request_line(**overrides) -> bytes:
    payload = {
        "schema_version": 1,
        "request_id": "11111111-1111-4111-8111-111111111111",
        "model_id": "gemini-flash-latest",
        "prompt": "hola",
        "format": None,
        "timeout_seconds": 5.0,
        "max_response_chars": 1000,
    }
    payload.update(overrides)
    return (json.dumps(payload) + "\n").encode("utf-8")


class ParseRequestLineTests(unittest.TestCase):
    def test_valid_request_parsed(self):
        req = subject.parse_request_line(_request_line())
        self.assertEqual(req.request_id, "11111111-1111-4111-8111-111111111111")
        self.assertEqual(req.model_id, "gemini-flash-latest")
        self.assertEqual(req.prompt, "hola")

    def test_malformed_json_rejected(self):
        with self.assertRaises(subject.ChildProtocolError) as ctx:
            subject.parse_request_line(b"esto no es json\n")
        self.assertEqual(ctx.exception.category, "MALFORMED_REQUEST")

    def test_non_object_json_rejected(self):
        with self.assertRaises(subject.ChildProtocolError):
            subject.parse_request_line(b"[1,2,3]\n")

    def test_wrong_schema_version_rejected(self):
        with self.assertRaises(subject.ChildProtocolError):
            subject.parse_request_line(_request_line(schema_version=999))

    def test_missing_request_id_rejected(self):
        payload = json.loads(_request_line())
        del payload["request_id"]
        with self.assertRaises(subject.ChildProtocolError):
            subject.parse_request_line((json.dumps(payload) + "\n").encode("utf-8"))

    def test_empty_model_id_rejected(self):
        with self.assertRaises(subject.ChildProtocolError):
            subject.parse_request_line(_request_line(model_id=""))

    def test_wrong_type_prompt_rejected(self):
        with self.assertRaises(subject.ChildProtocolError):
            subject.parse_request_line(_request_line(prompt=12345))

    def test_non_object_format_rejected(self):
        with self.assertRaises(subject.ChildProtocolError):
            subject.parse_request_line(_request_line(format="not-an-object"))

    def test_bool_as_timeout_rejected(self):
        with self.assertRaises(subject.ChildProtocolError):
            subject.parse_request_line(_request_line(timeout_seconds=True))

    def test_unknown_field_rejected(self):
        payload = json.loads(_request_line())
        payload["approved"] = True
        with self.assertRaises(subject.ChildProtocolError):
            subject.parse_request_line((json.dumps(payload) + "\n").encode("utf-8"))

    def test_empty_request_rejected(self):
        with self.assertRaises(subject.ChildProtocolError):
            subject.parse_request_line(b"")

    def test_oversized_request_rejected(self):
        with self.assertRaises(subject.ChildProtocolError):
            subject.parse_request_line(b"x" * (subject._MAX_REQUEST_BYTES + 1))


class RunChildSuccessTests(unittest.TestCase):
    def _run(self, backend, request_line=None):
        stdin = io.BytesIO(request_line or _request_line())
        stdout = io.StringIO()
        code = subject.run_child(backend, stdin=stdin, stdout=stdout)
        return code, stdout.getvalue()

    def test_success_writes_exactly_one_ok_line(self):
        backend = subject.make_offline_mock_backend(response_model_id="gemini-flash-latest")
        code, out = self._run(backend)
        self.assertEqual(code, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1)
        envelope = json.loads(lines[0])
        self.assertTrue(envelope["ok"])
        self.assertEqual(envelope["response_model_id"], "gemini-flash-latest")
        self.assertEqual(envelope["request_id"], "11111111-1111-4111-8111-111111111111")

    def test_missing_response_model_id_preserved_as_null_never_fabricated(self):
        backend = subject.make_offline_mock_backend(response_model_id=None)
        code, out = self._run(backend)
        envelope = json.loads(out.strip())
        self.assertIsNone(envelope["response_model_id"])

    def test_mismatched_response_model_id_is_transported_unmodified(self):
        # El hijo NUNCA decide match/mismatch (invariante N.4) -- solo
        # transporta lo que el backend devuelve, tal cual, sin comparar
        # contra config.model_id (eso vive exclusivamente en el provider).
        backend = subject.make_offline_mock_backend(response_model_id="un-modelo-completamente-distinto")
        code, out = self._run(backend)
        envelope = json.loads(out.strip())
        self.assertEqual(envelope["response_model_id"], "un-modelo-completamente-distinto")

    def test_malformed_request_exits_nonzero_without_writing_anything(self):
        stdin = io.BytesIO(b"not json\n")
        stdout = io.StringIO()
        code = subject.run_child(subject.make_offline_mock_backend(), stdin=stdin, stdout=stdout)
        self.assertNotEqual(code, 0)
        self.assertEqual(stdout.getvalue(), "")


class RunChildBackendFailureTests(unittest.TestCase):
    def _run(self, backend):
        stdin = io.BytesIO(_request_line())
        stdout = io.StringIO()
        code = subject.run_child(backend, stdin=stdin, stdout=stdout)
        return code, stdout.getvalue()

    def test_backend_error_reported_as_structured_failure_exit_zero(self):
        def boom(request):
            raise subject.BackendError("SDK_EXCEPTION", "fallo simulado")
        code, out = self._run(boom)
        self.assertEqual(code, 0)  # el proceso termina limpio; el fallo va en el envelope, no en el exit code
        envelope = json.loads(out.strip())
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error_category"], "SDK_EXCEPTION")

    def test_unexpected_backend_exception_mapped_to_sdk_exception(self):
        def boom(request):
            raise ValueError("cosa inesperada")
        code, out = self._run(boom)
        envelope = json.loads(out.strip())
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["error_category"], "SDK_EXCEPTION")

    def test_backend_error_message_truncated_at_4096_chars(self):
        def boom(request):
            raise subject.BackendError("SDK_EXCEPTION", "x" * 10_000)
        _, out = self._run(boom)
        envelope = json.loads(out.strip())
        self.assertLessEqual(len(envelope["error_message"]), 4096)

    def test_backend_error_rejects_unknown_category_at_construction(self):
        with self.assertRaises(ValueError):
            subject.BackendError("NOT_A_REAL_CATEGORY", "x")

    def test_only_one_line_ever_written_even_on_failure(self):
        def boom(request):
            raise subject.BackendError("SDK_EMPTY_RESPONSE", "vacio")
        _, out = self._run(boom)
        self.assertEqual(len(out.splitlines()), 1)


class ModuleSelfAuditTests(unittest.TestCase):
    def test_module_never_imports_google_sdk(self):
        import inspect
        source = inspect.getsource(subject)
        for forbidden in ("import google", "from google", "import requests", "import urllib", "import http.client"):
            self.assertNotIn(forbidden, source)

    def test_module_never_uses_pickle_eval_exec(self):
        import inspect
        source = inspect.getsource(subject)
        for forbidden in ("import pickle", "pickle.loads", "pickle.dumps", "eval(", "exec("):
            self.assertNotIn(forbidden, source)

    def test_module_import_requires_no_network_or_google_env(self):
        import os
        import subprocess
        import sys as _sys

        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            [_sys.executable, "-c", "import antigravity_isolated_child"],
            cwd=repo_root,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))

    def test_offline_backend_never_fabricates_identity_when_none_requested(self):
        backend = subject.make_offline_mock_backend()  # sin response_model_id explicito
        result = backend(subject.ChildRequest(
            request_id="r", model_id="gemini-flash-latest", prompt="p",
            format=None, timeout_seconds=5.0, max_response_chars=100,
        ))
        self.assertIsNone(result.response_model_id)


if __name__ == "__main__":
    unittest.main()
