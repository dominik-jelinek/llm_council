"""Settings and provider authentication checks without real credentials or requests."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from backend.config import Settings
from backend.credentials import CredentialsStore, OpenRouterCredentials
from backend.main import create_app, main
from backend.rendering import FRONTEND_DIR


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

    def test_ui_key_is_private_persistent_and_overrides_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings" / "credentials.json"
            store = CredentialsStore(path)
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "environment-test-key"}):
                self.assertEqual(
                    store.get().openrouter_api_key.get_secret_value(), "environment-test-key"
                )
                store.save(OpenRouterCredentials(openrouter_api_key="  saved-test-key  "))
                restored = CredentialsStore(path).get()
                self.assertIsInstance(restored, Settings)
                self.assertEqual(restored.openrouter_api_key.get_secret_value(), "saved-test-key")
                self.assertNotIn("saved-test-key", repr(restored))
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_failed_credential_replacement_preserves_previous_key(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialsStore(Path(directory) / "credentials.json")
            store.save(OpenRouterCredentials(openrouter_api_key="original-test-key"))
            with (
                patch("backend.persistence.os.replace", side_effect=OSError("disk failure")),
                self.assertRaises(OSError),
            ):
                store.save(OpenRouterCredentials(openrouter_api_key="replacement-test-key"))
            self.assertEqual(store.get().openrouter_api_key.get_secret_value(), "original-test-key")
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_data_directory_and_server_settings_load_from_dotenv_and_environment(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            env_file = Path(directory) / ".env"
            env_file.write_text("DATA_DIR=./custom-chats\nHOST=localhost\nPORT=8123\n")
            settings = Settings(_env_file=env_file)
            self.assertEqual(settings.data_dir, Path("custom-chats").resolve())
            self.assertEqual(settings.host, "localhost")
            self.assertEqual(settings.port, 8123)
            with patch.dict(os.environ, {"DATA_DIR": directory, "PORT": "8124"}):
                settings = Settings(_env_file=env_file)
                self.assertEqual(settings.data_dir, Path(directory))
                self.assertEqual(settings.port, 8124)
            self.assertEqual(Settings(_env_file=None).host, "127.0.0.1")
            self.assertEqual(
                Settings(_env_file=None).data_dir, Path("data/conversations").resolve()
            )
            self.assertEqual(
                Settings(_env_file=None, data_dir="~/council").data_dir, Path.home() / "council"
            )
            for value in ("", "   "):
                with self.assertRaises(ValidationError):
                    Settings(_env_file=None, data_dir=value)
            for port in (0, 65536):
                with self.assertRaises(ValidationError):
                    Settings(_env_file=None, port=port)

    def test_configured_directory_owns_all_persistent_data_and_survives_restart(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"DATA_DIR": directory}),
        ):
            app = create_app()
            conversation = app.state.store.create()
            project = app.state.projects.create("Test project")
            app.state.council_settings.save(app.state.council_settings.get())
            app.state.runs.credentials.save(
                OpenRouterCredentials(openrouter_api_key="test-local-key")
            )
            for path in (
                app.state.council_settings.path,
                app.state.projects.path,
                app.state.runs.credentials.path,
            ):
                self.assertTrue(path.is_relative_to(directory))
                self.assertTrue(path.is_file())
            restored = create_app()
            self.assertEqual(restored.state.store.list()[0]["id"], conversation["id"])
            self.assertEqual(restored.state.projects.list()[0]["id"], project["id"])
            self.assertEqual(
                restored.state.runs.credentials.get().openrouter_api_key.get_secret_value(),
                "test-local-key",
            )
            other = create_app(Path(directory) / "other")
            self.assertEqual(other.state.store.directory, Path(directory) / "other")
            self.assertEqual(other.state.store.list(), [])

    def test_data_cannot_be_saved_in_the_public_static_directory(self):
        for path in (FRONTEND_DIR / "static", FRONTEND_DIR / "static" / "private-data"):
            with self.assertRaisesRegex(ValueError, "public static directory"):
                create_app(path)
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / "static-link"
            link.symlink_to(FRONTEND_DIR / "static", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "public static directory"):
                create_app(link / "private-data")

    def test_server_uses_configured_host_and_port(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"HOST": "127.0.0.1", "PORT": "8123"}),
        ):
            app = create_app(directory)
            with patch("backend.main.app", app), patch("uvicorn.run") as serve:
                main()
            serve.assert_called_once_with(app, host="127.0.0.1", port=8123)

    @unittest.skipUnless(os.name == "posix", "Unix file permissions")
    def test_reading_existing_key_restricts_file_and_directory_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = CredentialsStore(Path(directory) / "settings" / "credentials.json")
            store.save(OpenRouterCredentials(openrouter_api_key="test-private-key"))
            store.path.chmod(0o644)
            store.path.parent.chmod(0o755)
            self.assertEqual(store.get().openrouter_api_key.get_secret_value(), "test-private-key")
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(store.path.parent.stat().st_mode & 0o777, 0o700)

    def test_credentials_reject_symbolic_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials = OpenRouterCredentials(openrouter_api_key="test-link-key")
            store = CredentialsStore(root / "settings" / "credentials.json")
            store.save(credentials)
            (root / "linked.json").symlink_to(store.path)
            (root / "linked-directory").symlink_to(store.path.parent, target_is_directory=True)
            for path in (root / "linked.json", root / "linked-directory" / "credentials.json"):
                linked = CredentialsStore(path)
                with self.assertRaises(OSError):
                    linked.get()
                with self.assertRaises(OSError):
                    linked.save(credentials)


if __name__ == "__main__":
    unittest.main()
