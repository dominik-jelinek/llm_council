"""Council editing, durable settings, and configuration boundaries between runs."""

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend import council
from backend.config import CouncilConfig, get_settings
from backend.credentials import OpenRouterCredentials
from backend.main import create_app
from tests.fixtures import CONFIGURATION, configure_test_app, parallel_reply, single_reply


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app(self.directory.name)
        self.settings = self.app.state.council_settings
        configure_test_app(self.app)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self.values = {
            "member_model": ["example/alpha", "example/alpha"],
            "member_effort": ["low", "high"],
            "chairman_model": "example/chairman",
            "chairman_effort": "medium",
            "title_model": "example/title",
            "title_effort": "",
        }

    async def asyncTearDown(self):
        await self.app.state.runs.close()
        await self.client.aclose()

    async def test_defaults_native_page_and_htmx_editor(self):
        self.settings.path.unlink()
        self.assertEqual(self.settings.get(), CouncilConfig())
        page = await self.client.get("/council/setup")
        self.assertIn("<!doctype html>", page.text)
        self.assertIn("Setup", page.text)
        partial = await self.client.get("/council/setup", headers={"HX-Request": "true"})
        self.assertNotIn("<!doctype", partial.text)
        self.assertIn('value="' + CouncilConfig().chairman[0] + '"', partial.text)

    async def test_ui_save_survives_restart_and_drives_model_calls(self):
        response = await self.client.post(
            "/council/setup", data=self.values, headers={"HX-Request": "true"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Settings saved", response.text)
        restored = create_app(self.directory.name)
        configuration = restored.state.council_settings.get()
        self.assertEqual(
            configuration.members, (("example/alpha", "low"), ("example/alpha", "high"))
        )
        self.assertEqual(configuration.chairman, ("example/chairman", "medium"))
        self.assertEqual(configuration.title_model, ("example/title", None))
        conversation_id = restored.state.store.create()["id"]
        with (
            patch.object(
                council, "query_models_parallel", AsyncMock(side_effect=parallel_reply)
            ) as parallel,
            patch.object(council, "query_model", AsyncMock(side_effect=single_reply)) as single,
        ):
            run = restored.state.runs.start(conversation_id, "Question")
            await run.task
        self.assertIsNone(run.error)
        self.assertEqual(parallel.await_count, 2)
        self.assertTrue(
            all(call.args[0] == configuration.members for call in parallel.call_args_list)
        )
        self.assertEqual(
            {call.args[0] for call in single.call_args_list},
            {configuration.chairman, configuration.title_model},
        )
        self.assertEqual(len(restored.state.store.list()), 1)

    async def test_invalid_settings_do_not_replace_saved_configuration(self):
        invalid_values = (
            {"member_model": [], "member_effort": []},
            {"member_effort": ["invalid", "high"]},
            {"member_model": ["no-provider", "example/alpha"]},
            {"chairman_model": " "},
            {"member_model": [f"example/model-{i}" for i in range(27)], "member_effort": [""] * 27},
            {"member_effort": ["high"]},
        )
        for values in invalid_values:
            with self.subTest(values=values):
                response = await self.client.post(
                    "/council/setup", data={**self.values, **values}, headers={"HX-Request": "true"}
                )
                self.assertEqual(response.status_code, 422)
                self.assertIn('role="alert"', response.text)
                self.assertEqual(response.headers["HX-Retarget"], "#council-settings")
                self.assertEqual(self.settings.get(), CONFIGURATION)

    async def test_identical_members_can_be_saved_and_restored(self):
        response = await self.client.post(
            "/council/setup", data={**self.values, "member_effort": ["high", "high"]}
        )
        self.assertEqual(response.status_code, 200)
        restored = create_app(self.directory.name).state.council_settings.get()
        self.assertEqual(restored.members, (("example/alpha", "high"), ("example/alpha", "high")))

    async def test_failed_atomic_save_preserves_previous_configuration(self):
        with (
            patch("backend.persistence.os.replace", side_effect=OSError("disk failure")),
            self.assertLogs("backend.setup_views", level="ERROR"),
        ):
            response = await self.client.post(
                "/council/setup", data=self.values, headers={"HX-Request": "true"}
            )
        self.assertEqual(response.status_code, 500)
        self.assertIn("Settings could not be saved", response.text)
        self.assertEqual(self.settings.get(), CONFIGURATION)
        self.assertFalse(list(self.settings.path.parent.glob("*.tmp")))

    async def test_configuration_change_does_not_affect_active_run(self):
        conversation_id = self.app.state.store.create()["id"]
        with (
            patch.object(
                council, "query_models_parallel", AsyncMock(side_effect=parallel_reply)
            ) as parallel,
            patch.object(council, "query_model", AsyncMock(side_effect=single_reply)) as single,
        ):
            first = self.app.state.runs.start(conversation_id, "First")
            await self.client.post("/council/setup", data=self.values)
            await first.task
            self.assertTrue(
                all(call.args[0] == CONFIGURATION.members for call in parallel.call_args_list)
            )
            self.assertEqual(first.result["stage3"]["reasoning_effort"], None)
            parallel.reset_mock()
            single.reset_mock()
            followup = self.app.state.runs.start(conversation_id, "Next", council_enabled=False)
            await followup.task
            parallel.assert_not_awaited()
            self.assertEqual(single.call_args.args[0], ("example/chairman", "medium"))

    async def test_corrupt_configuration_is_visible_and_can_be_repaired(self):
        self.settings.path.write_text("{broken", encoding="utf-8")
        with self.assertLogs("backend.setup_views", level="ERROR"):
            response = await self.client.get("/council/setup")
        self.assertIn("Saved settings could not be read", response.text)
        conversation_id = self.app.state.store.create()["id"]
        response = await self.client.post(
            f"/api/conversations/{conversation_id}/message", json={"content": "Question"}
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.app.state.store.get(conversation_id)["messages"], [])
        response = await self.client.post("/council/setup", data=self.values)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.settings.get().chairman, ("example/chairman", "medium"))

    async def test_cross_origin_form_cannot_change_settings(self):
        response = await self.client.post(
            "/council/setup", data=self.values, headers={"Origin": "https://unrelated.example"}
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.settings.get(), CONFIGURATION)

    async def test_key_is_saved_separately_hidden_and_kept_when_field_is_blank(self):
        key = "sk-or-test-ui-secret"
        response = await self.client.post(
            "/council/setup",
            data={**self.values, "openrouter_api_key": key},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(key, response.text)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn('type="password"', response.text)
        self.assertIn("Key configured", response.text)
        self.assertNotIn(key, self.settings.path.read_text())
        restored = create_app(self.directory.name)
        self.assertEqual(
            restored.state.runs.credentials.get().openrouter_api_key.get_secret_value(), key
        )
        for value in ("", "   "):
            response = await self.client.post(
                "/council/setup", data={**self.values, "openrouter_api_key": value}
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                self.app.state.runs.credentials.get().openrouter_api_key.get_secret_value(), key
            )
        for headers in ({}, {"HX-Request": "true"}):
            response = await self.client.get("/council/setup", headers=headers)
            self.assertNotIn(key, response.text)
            self.assertIn("Key configured", response.text)

    async def test_saved_key_is_not_exposed_by_pages_api_or_file_urls(self):
        key = "sk-or-test-not-public"
        self.app.state.runs.credentials.save(OpenRouterCredentials(openrouter_api_key=key))
        conversation_id = self.app.state.store.create()["id"]
        for url in (
            "/",
            "/council/setup",
            "/api/conversations",
            f"/api/conversations/{conversation_id}",
            f"/conversations/{conversation_id}",
            "/health",
            "/openapi.json",
        ):
            response = await self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(key, response.text)
        for url in (
            "/data/conversations/settings/credentials.json",
            "/settings/credentials.json",
            "/static/settings/credentials.json",
            "/static/../../data/conversations/settings/credentials.json",
            "/static/%2e%2e/%2e%2e/data/conversations/settings/credentials.json",
        ):
            response = await self.client.get(url)
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(key, response.text)

    async def test_rejected_forms_never_replace_or_echo_the_key(self):
        credentials = self.app.state.runs.credentials
        credentials.save(OpenRouterCredentials(openrouter_api_key="old-test-key"))
        for values, headers, expected_status in (
            ({"openrouter_api_key": "new-test-key", "chairman_model": "invalid"}, {}, 422),
            ({"openrouter_api_key": "secret with spaces"}, {}, 422),
            ({"openrouter_api_key": "secret\nwith-newline"}, {}, 422),
            ({"openrouter_api_key": "secret\x00control"}, {}, 422),
            ({"openrouter_api_key": "new-test-key"}, {"Origin": "https://unrelated.example"}, 403),
        ):
            with self.subTest(status=expected_status):
                response = await self.client.post(
                    "/council/setup", data={**self.values, **values}, headers=headers
                )
                self.assertEqual(response.status_code, expected_status)
                self.assertNotIn(values["openrouter_api_key"], response.text)
                self.assertEqual(
                    credentials.get().openrouter_api_key.get_secret_value(), "old-test-key"
                )
                self.assertEqual(self.settings.get(), CONFIGURATION)

    async def test_key_write_failure_is_visible_and_keeps_previous_key(self):
        credentials = self.app.state.runs.credentials
        credentials.save(OpenRouterCredentials(openrouter_api_key="old-test-key"))
        with (
            patch.object(credentials, "save", side_effect=OSError("secret-error-detail")),
            self.assertLogs("backend.setup_views", level="ERROR") as logs,
        ):
            response = await self.client.post(
                "/council/setup", data={**self.values, "openrouter_api_key": "new-test-key"}
            )
        self.assertEqual(response.status_code, 500)
        self.assertIn("API key could not be saved", response.text)
        self.assertNotIn("new-test-key", response.text)
        self.assertNotIn("secret-error-detail", "\n".join(logs.output))
        self.assertEqual(credentials.get().openrouter_api_key.get_secret_value(), "old-test-key")

    async def test_corrupt_key_can_be_replaced_without_exposing_contents(self):
        path = self.app.state.runs.credentials.path
        path.write_text('{"openrouter_api_key": "broken secret"}', encoding="utf-8")
        response = await self.client.get("/council/setup")
        self.assertIn("Saved API key could not be read", response.text)
        self.assertNotIn("broken secret", response.text)
        response = await self.client.post(
            "/council/setup", data={**self.values, "openrouter_api_key": "repaired-test-key"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.app.state.runs.credentials.get().openrouter_api_key.get_secret_value(),
            "repaired-test-key",
        )

    async def test_key_changes_apply_to_new_runs_including_title_and_all_stages(self):
        keys = []

        async def record_parallel(*args, **kwargs):
            keys.append(get_settings().openrouter_api_key.get_secret_value())
            return await parallel_reply(*args, **kwargs)

        async def record_single(*args, **kwargs):
            keys.append(get_settings().openrouter_api_key.get_secret_value())
            return await single_reply(*args, **kwargs)

        self.app.state.runs.credentials.save(
            OpenRouterCredentials(openrouter_api_key="first-test-key")
        )
        with (
            patch.object(council, "query_models_parallel", side_effect=record_parallel),
            patch.object(council, "query_model", side_effect=record_single),
        ):
            first = self.app.state.runs.start(None, "First")
            response = await self.client.post(
                "/council/setup", data={**self.values, "openrouter_api_key": "second-test-key"}
            )
            self.assertEqual(response.status_code, 200)
            await first.task
            self.assertIsNone(first.error)
            self.assertEqual(keys, ["first-test-key"] * 4)
            keys.clear()
            # A restored app uses the saved key immediately without restarting this app.
            restored = create_app(self.directory.name)
            run = restored.state.runs.start(None, "Next", council_enabled=False)
            await run.task
            self.assertIsNone(run.error)
            self.assertEqual(keys, ["second-test-key"] * 2)
            self.assertNotIn("test-key", str(restored.state.store.get(run.conversation_id)))


if __name__ == "__main__":
    unittest.main()
