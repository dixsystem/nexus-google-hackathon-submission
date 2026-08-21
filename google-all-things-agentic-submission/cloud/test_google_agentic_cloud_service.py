from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()

