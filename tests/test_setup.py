"""Council editing, durable settings, and configuration boundaries between runs."""

import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend import council
from backend.config import CouncilConfig
from backend.main import create_app
from tests.fixtures import CONFIGURATION, parallel_reply, single_reply


class SetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app(self.directory.name)
        self.settings = self.app.state.council_settings
        self.settings.save(CONFIGURATION)
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
        self.assertIn("Setup Council", page.text)
        partial = await self.client.get("/council/setup", headers={"HX-Request": "true"})
        self.assertNotIn("<!doctype", partial.text)
        self.assertIn('value="' + CouncilConfig().chairman[0] + '"', partial.text)

    async def test_ui_save_survives_restart_and_drives_model_calls(self):
        response = await self.client.post(
            "/council/setup", data=self.values, headers={"HX-Request": "true"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Council saved", response.text)
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


if __name__ == "__main__":
    unittest.main()
