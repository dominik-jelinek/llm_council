"""Branch persistence, model context, HTTP editing, and in-flight isolation."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend import branches, council
from backend.main import create_app
from backend.runs import RunError
from tests.fixtures import CONFIGURATION, parallel_reply, single_reply


class BranchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app(self.directory.name)
        self.app.state.council_settings.save(CONFIGURATION)
        self.store = self.app.state.store
        self.runs = self.app.state.runs
        self.conversation_id = self.store.create()["id"]
        self.url = f"/conversations/{self.conversation_id}"
        self.headers = {"HX-Request": "true"}
        for target, reply in (
            ("query_models_parallel", parallel_reply),
            ("query_model", single_reply),
        ):
            mock = patch.object(council, target, AsyncMock(side_effect=reply))
            mock.start()
            self.addCleanup(mock.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.runs.close()
        await self.client.aclose()

    def saved(self):
        return self.store.get(self.conversation_id)

    async def turn(self, content, **kwargs):
        run = self.runs.start(self.conversation_id, content, **kwargs)
        await run.task
        self.assertIsNone(run.error)
        return run

    async def test_edit_preserves_original_and_uses_only_the_shared_context(self):
        await self.turn("First")
        original = await self.turn("Second", council_enabled=False)
        await self.turn("Original follow-up")
        original_messages = self.saved()["messages"]
        council.query_models_parallel.reset_mock()
        council.query_model.reset_mock()

        edited = await self.turn("Edited second", edit_message_id=original.message_id)
        self.assertFalse(edited.council_enabled)
        council.query_models_parallel.assert_not_awaited()
        prompt = council.query_model.call_args.args[1]
        self.assertEqual([item["role"] for item in prompt], ["system", "user", "assistant", "user"])
        self.assertEqual(prompt[1]["content"], "First")
        self.assertEqual(prompt[-1]["content"], "Edited second")
        self.assertEqual(edited.history, original_messages[:2])
        edited_messages = self.saved()["messages"]
        self.assertEqual(len(edited_messages), 4)
        self.assertEqual(edited_messages[:2], original_messages[:2])

        council.query_model.reset_mock()
        self.runs.select_branch(self.conversation_id, edited.message_id, original.message_id)
        self.assertEqual(self.saved()["messages"], original_messages)
        council.query_model.assert_not_awaited()
        council.query_models_parallel.assert_not_awaited()
        await self.turn("More on the original", council_enabled=False)
        original_extended = self.saved()["messages"]
        self.runs.select_branch(self.conversation_id, original.message_id, edited.message_id)
        self.assertEqual(self.saved()["messages"], edited_messages)
        self.runs.select_branch(self.conversation_id, edited.message_id, original.message_id)
        self.assertEqual(self.saved()["messages"], original_extended)

    async def test_nested_branches_remain_available_after_restart(self):
        first = await self.turn("First")
        second = await self.turn("Second")
        second_edit = await self.turn("Second v2", edit_message_id=second.message_id)
        second_edit_again = await self.turn("Second v3", edit_message_id=second_edit.message_id)
        nested = await self.turn("Nested follow-up")
        nested_edit = await self.turn("Nested v2", edit_message_id=nested.message_id)
        chosen_path = self.saved()["messages"]
        nav = branches.navigation(self.saved())
        self.assertEqual(nav[second_edit_again.message_id]["index"], 3)
        self.assertEqual(nav[second_edit_again.message_id]["count"], 3)
        self.assertEqual(nav[nested_edit.message_id]["count"], 2)

        root_edit = await self.turn("Different first", edit_message_id=first.message_id)
        self.assertEqual(root_edit.history, [])
        self.assertEqual(len(self.saved()["messages"]), 2)
        restored = create_app(self.directory.name).state.runs
        restored.select_branch(self.conversation_id, root_edit.message_id, first.message_id)
        self.assertEqual(self.saved()["messages"], chosen_path)
        restored.select_branch(self.conversation_id, nested_edit.message_id, nested.message_id)
        self.assertEqual(self.saved()["messages"][-2]["content"], "Nested follow-up")
        self.assertEqual(len(self.store.list()), 1)

    async def test_opening_defaults_to_newest_activity_and_nested_versions(self):
        first = await self.turn("First")
        nested = await self.turn("Nested")
        nested_edit = await self.turn("Nested latest", edit_message_id=nested.message_id)
        self.runs.select_branch(self.conversation_id, nested_edit.message_id, nested.message_id)
        latest_root = await self.turn("New root", edit_message_id=first.message_id)
        self.runs.select_branch(self.conversation_id, latest_root.message_id, first.message_id)
        self.assertEqual(self.saved()["messages"][-2]["id"], nested_edit.message_id)
        # Visiting an older version is not new activity.
        await self.client.get(self.url)
        self.assertEqual(self.saved()["messages"][0]["id"], latest_root.message_id)
        # An explicit branch link restores that path and its newest nested version.
        await self.client.get(self.url, params={"branch": first.message_id})
        self.assertEqual(self.saved()["messages"][-2]["id"], nested_edit.message_id)
        continued = await self.turn("New follow-up on older root")
        self.runs.select_branch(self.conversation_id, first.message_id, latest_root.message_id)
        await self.client.get(self.url)
        self.assertEqual(self.saved()["messages"][-2]["id"], continued.message_id)

    async def test_switch_during_generation_saves_reply_on_its_own_branch(self):
        original = await self.turn("Original", council_enabled=False)
        original_messages = self.saved()["messages"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(model, messages, timeout=120.0):
            if timeout != 30:
                entered.set()
                await release.wait()
            return await single_reply(model, messages, timeout)

        with patch.object(council, "query_model", AsyncMock(side_effect=delayed)):
            edited = self.runs.start(
                self.conversation_id, "Edited", edit_message_id=original.message_id
            )
            await entered.wait()
            self.runs.select_branch(self.conversation_id, edited.message_id, original.message_id)
            page = await self.client.get(self.url, params={"branch": original.message_id})
            self.assertIn("A response is running on another branch.", page.text)
            self.assertEqual(self.saved()["messages"], original_messages)
            with self.assertRaises(RunError) as raised:
                self.runs.start(self.conversation_id, "Concurrent message")
            self.assertEqual(raised.exception.status_code, 409)
            release.set()
            await edited.task
        self.assertEqual(self.saved()["messages"], original_messages)
        self.runs.select_branch(self.conversation_id, original.message_id, edited.message_id)
        saved = self.saved()["messages"]
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[-1]["parent_id"], edited.message_id)
        self.assertEqual(saved[0]["content"], "Edited")

    async def test_failed_edit_keeps_both_branches_and_retry_uses_edited_message(self):
        original = await self.turn("Original", council_enabled=False)
        original_messages = self.saved()["messages"]
        with patch.object(council, "query_model", AsyncMock(return_value=None)):
            with self.assertLogs("backend.runs", level="ERROR"):
                edited = self.runs.start(
                    self.conversation_id, "Edited", edit_message_id=original.message_id
                )
                await edited.task
        self.runs.select_branch(self.conversation_id, edited.message_id, original.message_id)
        self.assertEqual(self.saved()["messages"], original_messages)
        self.runs.select_branch(self.conversation_id, original.message_id, edited.message_id)
        restored = create_app(self.directory.name).state.runs
        retry = restored.start(self.conversation_id)
        await retry.task
        self.assertEqual(retry.message_id, edited.message_id)
        self.assertFalse(retry.council_enabled)
        self.assertEqual(len(self.saved()["messages"]), 2)

    async def test_failure_is_announced_after_title_cleanup_releases_editing(self):
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow_title(model, messages, timeout=120.0):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.set()
                await release.wait()
                raise

        async def fail_models(models, messages):
            await entered.wait()
            return [(model, None) for model in models]

        async def busy_when_failure_is_announced(run):
            async for item in run.listen():
                if item and item[1]["type"] == "error":
                    return self.conversation_id in self.runs.active
            self.fail("Run never announced its failure")

        with (
            patch.object(council, "query_model", side_effect=slow_title),
            patch.object(council, "query_models_parallel", side_effect=fail_models),
            self.assertLogs("backend.runs", level="ERROR"),
        ):
            run = self.runs.start(self.conversation_id, "Question")
            subscriber = asyncio.create_task(busy_when_failure_is_announced(run))
            await cleaning.wait()
            await asyncio.sleep(0)
            release.set()
            await run.task
            self.assertFalse(await subscriber)

    async def test_edit_and_switch_validation_do_not_mutate_the_tree(self):
        original = await self.turn("Original")
        edited = await self.turn("Edited", edit_message_id=original.message_id)
        await self.turn("Follow-up")
        saved = self.saved()
        for message_id, content in (
            (original.message_id, "Stale form"),
            (edited.message_id, "  "),
            (saved["messages"][-1]["id"], "An assistant message"),
            ("missing", "Unknown message"),
        ):
            with self.subTest(message_id=message_id):
                with self.assertRaises(RunError):
                    self.runs.start(self.conversation_id, content, edit_message_id=message_id)
                self.assertEqual(self.saved(), saved)
        for target in ("missing", saved["messages"][-1]["id"], saved["messages"][-2]["id"]):
            with self.assertRaises(RunError):
                self.runs.select_branch(self.conversation_id, edited.message_id, target)
        self.assertEqual(self.saved(), saved)

    async def test_failed_save_and_invalid_settings_leave_original_untouched(self):
        original = await self.turn("Original")
        saved = self.saved()
        with patch("backend.persistence.os.replace", side_effect=OSError("Disk failure")):
            with self.assertRaises(OSError):
                self.runs.start(self.conversation_id, "Edited", edit_message_id=original.message_id)
        self.app.state.council_settings.path.write_text("{invalid")
        with self.assertRaises(RunError):
            self.runs.start(self.conversation_id, "Edited", edit_message_id=original.message_id)
        self.assertEqual(self.saved(), saved)
        self.assertFalse(self.runs.active)

    async def test_legacy_file_ids_are_stable_and_edit_migrates_without_losing_answers(self):
        legacy = {
            "id": self.conversation_id,
            "created_at": "2026-01-01T00:00:00+00:00",
            "title": "Legacy",
            "messages": [
                {"role": "user", "content": "Original"},
                {"role": "assistant", "stage1": [], "stage2": [], "stage3": {"response": "Saved"}},
            ],
        }
        path = Path(self.directory.name, f"{self.conversation_id}.json")
        path.write_text(json.dumps(legacy))
        loaded = self.saved()
        self.assertEqual(self.saved(), loaded)
        self.assertEqual(json.loads(path.read_text()), legacy)
        original_id = loaded["messages"][0]["id"]
        edited = await self.turn("Edited", edit_message_id=original_id)
        self.runs.select_branch(self.conversation_id, edited.message_id, original_id)
        self.assertEqual(self.saved()["messages"], loaded["messages"])
        persisted = json.loads(path.read_text())
        self.assertNotIn("messages", persisted)
        self.assertEqual(len(persisted["message_nodes"]), 4)

    async def test_edit_form_cancel_submit_and_branch_controls(self):
        source = "**Čau**\n</textarea><script>alert(1)</script>"
        original = await self.turn(source)
        edit_url = f"{self.url}/messages/{original.message_id}/edit"
        form = await self.client.get(edit_url, headers=self.headers)
        self.assertEqual(form.status_code, 200)
        self.assertIn("&lt;/textarea&gt;&lt;script&gt;", form.text)
        self.assertNotIn("<script>", form.text)
        self.assertIn("Save &amp; send", form.text)
        cancelled = await self.client.get(edit_url.removesuffix("/edit"), headers=self.headers)
        self.assertNotIn("edit-message-form", cancelled.text)
        self.assertEqual(len(self.saved()["message_nodes"]), 2)
        blank = await self.client.post(edit_url, data={"content": " "}, headers=self.headers)
        self.assertEqual(blank.status_code, 400)
        self.assertEqual(blank.headers["HX-Retarget"], f"#edit-error-{original.message_id}")

        edited = await self.client.post(edit_url, data={"content": "Edited"}, headers=self.headers)
        self.assertEqual(edited.status_code, 200)
        self.assertIn('id="workspace"', edited.text)
        self.assertIn('aria-label="Previous branch"', edited.text)
        self.assertIn("2 / 2", edited.text)
        run = self.runs.active[self.conversation_id]
        self.assertEqual(
            json.loads(edited.headers["HX-Trigger-After-Swap"]), {"focusMessage": run.message_id}
        )
        await run.task
        branch_url = f"{self.url}/messages/{run.message_id}/branch"
        switched = await self.client.post(
            branch_url, data={"target_id": original.message_id}, headers=self.headers
        )
        self.assertIn("1 / 2", switched.text)
        self.assertIn('aria-label="Next branch"', switched.text)
        native = await self.client.post(edit_url, data={"content": "Native edited"})
        self.assertEqual(native.status_code, 303)
        self.assertIn(self.url + "?branch=", native.headers["Location"])
        await self.runs.active[self.conversation_id].task

    async def test_cross_origin_edits_and_branch_changes_are_rejected(self):
        original = await self.turn("Original")
        saved = self.saved()
        for action, data in (
            ("edit", {"content": "Edited"}),
            ("branch", {"target_id": original.message_id}),
        ):
            response = await self.client.post(
                f"{self.url}/messages/{original.message_id}/{action}",
                data=data,
                headers={"Origin": "https://unrelated.example"},
            )
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.saved(), saved)
