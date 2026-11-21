"""Settings and provider authentication checks without real credentials or requests."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.config import Settings


class SettingsTests(unittest.TestCase):
    def test_environment_overrides_dotenv_and_unrelated_entries_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "OPENROUTER_API_KEY=dotenv-test-key\nUNRELATED_SETTING=ignored\n",
                encoding="utf-8",
            )
            settings = Settings(_env_file=env_file)
            self.assertEqual(settings.openrouter_api_key.get_secret_value(), "dotenv-test-key")
            self.assertNotIn("OPENROUTER_API_KEY", os.environ)
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "environment-test-key"}):
                settings = Settings(_env_file=env_file)
                self.assertEqual(
                    settings.openrouter_api_key.get_secret_value(), "environment-test-key"
                )

    def test_secret_is_masked_in_settings_output(self):
        secret = "private-test-key"
        settings = Settings(_env_file=None, openrouter_api_key=secret)
        for output in (str(settings), repr(settings), settings.model_dump_json()):
            self.assertNotIn(secret, output)
            self.assertIn("**********", output)


if __name__ == "__main__":
    unittest.main()
