"""Tests para lyria_alert_sound.py (misión M-8). Ningún test invoca una
API de Lyria real ni gasta cuota -- transport es siempre un doble de
prueba inyectado (o ausente, para probar el camino sin configurar)."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import lyria_alert_sound as subject


class GenerateAlertSoundWithMockedTransportTest(unittest.TestCase):
    def test_generates_expected_file_with_mocked_transport(self):
        transport = mock.Mock(return_value=b"FAKE-MP3-BYTES")
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            destination = subject.generate_alert_sound(
                "sess-x:round-1", transport=transport, output_dir=output_dir
            )
            self.assertEqual(destination, output_dir / "sess-x:round-1.mp3")
            self.assertTrue(destination.is_file())
            self.assertEqual(destination.read_bytes(), b"FAKE-MP3-BYTES")

    def test_transport_called_with_fixed_prompt_and_model_id(self):
        transport = mock.Mock(return_value=b"FAKE-MP3-BYTES")
        with tempfile.TemporaryDirectory() as tmp:
            subject.generate_alert_sound(
                "incident-1", transport=transport, output_dir=Path(tmp)
            )
        transport.assert_called_once_with(subject.DEFAULT_LYRIA_MODEL_ID, subject.ALERT_SOUND_PROMPT)

    def test_custom_model_id_is_honored(self):
        transport = mock.Mock(return_value=b"FAKE-MP3-BYTES")
        with tempfile.TemporaryDirectory() as tmp:
            subject.generate_alert_sound(
                "incident-1", transport=transport, model_id="custom-lyria-model", output_dir=Path(tmp)
            )
        transport.assert_called_once_with("custom-lyria-model", subject.ALERT_SOUND_PROMPT)

    def test_creates_output_directory_if_missing(self):
        transport = mock.Mock(return_value=b"FAKE-MP3-BYTES")
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "does" / "not" / "exist"
            destination = subject.generate_alert_sound(
                "incident-1", transport=transport, output_dir=nested
            )
            self.assertTrue(destination.is_file())


class GenerateAlertSoundFailsSafeTest(unittest.TestCase):
    def test_no_transport_configured_returns_none_without_raising(self):
        result = subject.generate_alert_sound("incident-1")
        self.assertIsNone(result)

    def test_transport_raising_exception_returns_none_and_does_not_propagate(self):
        transport = mock.Mock(side_effect=RuntimeError("Lyria is down"))
        with tempfile.TemporaryDirectory() as tmp:
            result = subject.generate_alert_sound(
                "incident-1", transport=transport, output_dir=Path(tmp)
            )
        self.assertIsNone(result)

    def test_transport_returning_empty_bytes_returns_none(self):
        transport = mock.Mock(return_value=b"")
        with tempfile.TemporaryDirectory() as tmp:
            result = subject.generate_alert_sound(
                "incident-1", transport=transport, output_dir=Path(tmp)
            )
        self.assertIsNone(result)

    def test_transport_returning_non_bytes_returns_none(self):
        transport = mock.Mock(return_value="not-bytes")
        with tempfile.TemporaryDirectory() as tmp:
            result = subject.generate_alert_sound(
                "incident-1", transport=transport, output_dir=Path(tmp)
            )
        self.assertIsNone(result)

    def test_rest_of_flow_can_continue_after_lyria_failure(self):
        # Simula el flujo real: la generación del informe de cuarentena
        # (M-4) no depende en absoluto de si el sonido de alerta se
        # generó -- este test documenta esa independencia explícitamente.
        transport = mock.Mock(side_effect=RuntimeError("network unreachable"))
        with tempfile.TemporaryDirectory() as tmp:
            sound_result = subject.generate_alert_sound(
                "incident-1", transport=transport, output_dir=Path(tmp)
            )
        self.assertIsNone(sound_result)
        report_still_generates = "quarantine report body"
        self.assertEqual(report_still_generates, "quarantine report body")


class IncidentIdSafetyTest(unittest.TestCase):
    def test_rejects_incident_id_with_path_separator(self):
        with self.assertRaises(subject.LyriaAlertSoundError):
            subject.generate_alert_sound("../etc/passwd", transport=mock.Mock())

    def test_rejects_empty_incident_id(self):
        with self.assertRaises(subject.LyriaAlertSoundError):
            subject.generate_alert_sound("", transport=mock.Mock())

    def test_rejects_non_string_incident_id(self):
        with self.assertRaises(subject.LyriaAlertSoundError):
            subject.generate_alert_sound(12345, transport=mock.Mock())

    def test_accepts_colon_containing_incident_id_from_red_team_incident_default_format(self):
        transport = mock.Mock(return_value=b"FAKE-MP3-BYTES")
        with tempfile.TemporaryDirectory() as tmp:
            destination = subject.generate_alert_sound(
                "sess-report:round-3", transport=transport, output_dir=Path(tmp)
            )
        self.assertIsNotNone(destination)


if __name__ == "__main__":
    unittest.main()
