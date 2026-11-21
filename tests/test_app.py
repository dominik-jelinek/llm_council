import asyncio
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from backend import council
from backend.api import completed_result
from backend.events import encode_event
from backend.main import create_app
from backend.rendering import render, render_markdown
from backend.runs import RunError
from backend.storage import ConversationStore
from backend.usage import begin_request, finish_request
from tests.fixtures import CONFIGURATION, COUNCIL_MEMBERS, MODELS, parallel_reply, single_reply


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app(self.directory.name)
        self.app.state.council_settings.save(CONFIGURATION)
        self.runs = self.app.state.runs
        self.store = self.app.state.store
        self.conversation_id = self.store.create()["id"]
        for target, value in (
            ("query_models_parallel", AsyncMock(side_effect=parallel_reply)),
            ("query_model", AsyncMock(side_effect=single_reply)),
        ):
            mock = patch.object(council, target, value)
            mock.start()
            self.addCleanup(mock.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://test",
        )

    async def asyncTearDown(self):
        await self.runs.close()
        await self.client.aclose()

    async def test_full_turn_saves_rankings_and_generated_title(self):
        run = self.runs.start(self.conversation_id, "Explain this")
        await run.task
        saved = self.store.get(self.conversation_id)
        self.assertEqual(len(saved["messages"]), 2)
        self.assertEqual(saved["title"], "A useful conversation")
        answer = saved["messages"][-1]
        self.assertEqual(answer["metadata"]["label_to_model"]["Response A"], MODELS[0])
        self.assertEqual(answer["metadata"]["aggregate_rankings"][0]["average_rank"], 1.0)
        self.assertEqual(answer["stage3"]["model"], "example/chairman")
        self.assertTrue(run.done)
        self.assertNotIn(self.conversation_id, self.runs.active)

    async def test_followup_only_calls_chairman_and_uses_final_answers(self):
        first = self.runs.start(self.conversation_id, "First question")
        await first.task
        council.query_models_parallel.reset_mock()
        council.query_model.reset_mock()
        followup = self.runs.start(self.conversation_id, "Explain more", council_enabled=False)
        await followup.task
        council.query_models_parallel.assert_not_awaited()
        council.query_model.assert_awaited_once()
        messages = council.query_model.call_args.args[1]
        self.assertEqual(
            [item["role"] for item in messages], ["system", "user", "assistant", "user"]
        )
        self.assertEqual(messages[-1]["content"], "Explain more")
        self.assertNotIn("Alpha answer", messages[2]["content"])
        self.assertEqual(followup.result["stage1"], [])
        self.assertEqual(len(self.store.get(self.conversation_id)["messages"]), 4)

    async def test_followup_defaults_to_full_council_with_context_at_every_stage(self):
        first = self.runs.start(self.conversation_id, "First question")
        await first.task
        council.query_models_parallel.reset_mock()
        council.query_model.reset_mock()
        response = await self.client.post(
            f"/api/conversations/{self.conversation_id}/message",
            json={"content": "Explain more"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["stage1"]), len(MODELS))
        self.assertEqual(len(response.json()["stage2"]), len(MODELS))
        self.assertEqual(council.query_models_parallel.await_count, 2)
        council.query_model.assert_awaited_once()
        calls = [*council.query_models_parallel.call_args_list, council.query_model.call_args]
        for call in calls:
            messages = call.args[1]
            self.assertEqual([item["role"] for item in messages], ["user", "assistant", "user"])
            self.assertEqual(messages[0]["content"], "First question")
            self.assertEqual(messages[1]["content"], first.result["stage3"]["response"])
            self.assertIn("Explain more", messages[-1]["content"])
            self.assertNotIn("Alpha answer", messages[1]["content"])

    async def test_council_off_on_first_turn_survives_failure_and_retry(self):
        async def fail_answer(model, messages, timeout=120.0):
            return await single_reply(model, messages, timeout) if timeout == 30 else None

        with patch.object(council, "query_model", AsyncMock(side_effect=fail_answer)):
            with self.assertLogs("backend.runs", level="ERROR"):
                failed = self.runs.start(self.conversation_id, "Question", council_enabled=False)
                await failed.task
        saved = self.store.get(self.conversation_id)
        self.assertFalse(saved["messages"][0]["council_enabled"])
        # Reconstruct the app to verify that retry uses persisted mode, not memory.
        restored = create_app(self.directory.name).state.runs
        retry = restored.start(self.conversation_id)
        await retry.task
        self.assertFalse(retry.council_enabled)
        self.assertIsNone(retry.error)
        council.query_models_parallel.assert_not_awaited()
        self.assertEqual(retry.result["stage1"], [])
        self.assertEqual(len(self.store.get(self.conversation_id)["messages"]), 2)

    async def test_same_model_with_distinct_efforts_keeps_separate_answers_and_rankings(self):
        members = [(MODELS[0], "low"), (MODELS[0], "high")]
        self.app.state.council_settings.save(
            CONFIGURATION.model_copy(
                update={
                    "members": tuple(members),
                    "chairman": ("example/chairman", "medium"),
                }
            )
        )
        run = self.runs.start(self.conversation_id, "Compare")
        await run.task
        saved = self.store.get(self.conversation_id)["messages"][-1]
        self.assertEqual([item["reasoning_effort"] for item in saved["stage1"]], ["low", "high"])
        self.assertEqual([item["reasoning_effort"] for item in saved["stage2"]], ["low", "high"])
        self.assertEqual(saved["stage3"]["reasoning_effort"], "medium")
        self.assertEqual(
            saved["metadata"]["label_to_model"],
            {
                "Response A": "example/alpha (low)",
                "Response B": "example/alpha (high)",
            },
        )
        self.assertEqual(len(saved["metadata"]["aggregate_rankings"]), 2)
        for call in council.query_models_parallel.call_args_list:
            self.assertEqual(call.args[0], tuple(members))
        # Rendering and reconstructed metadata retain identity even in older-style records.
        conversation = self.store.get(self.conversation_id)
        del conversation["messages"][-1]["metadata"]
        self.store.save(conversation)
        page = await self.client.get(f"/conversations/{self.conversation_id}")
        self.assertIn("alpha (low)", page.text)
        self.assertIn("alpha (high)", page.text)
        self.assertIn("chairman (medium)", page.text)

    async def test_html_switch_defaults_on_and_unchecked_form_uses_chairman(self):
        url = f"/conversations/{self.conversation_id}"
        page = await self.client.get(url)
        switch = re.search(r'<input id="council-enabled"[^>]+>', page.text)[0]
        self.assertIn("checked", switch)
        response = await self.client.post(url + "/messages", data={"content": "Question"})
        self.assertEqual(response.status_code, 303)
        run = self.runs.active[self.conversation_id]
        await run.task
        council.query_models_parallel.assert_not_awaited()
        page = await self.client.get(url)
        switch = re.search(r'<input id="council-enabled"[^>]+>', page.text)[0]
        self.assertNotIn("checked", switch)
        self.assertIn("Chairman Answer", page.text)
        self.assertIn('aria-label="Copy your message"', page.text)
        self.assertIn('aria-label="Copy final answer"', page.text)

    async def test_identical_members_keep_separate_votes_and_labels_after_partial_failure(self):
        members = ((MODELS[0], None), (MODELS[0], None))
        self.app.state.council_settings.save(CONFIGURATION.model_copy(update={"members": members}))
        for fail_first in (False, True):
            with self.subTest(fail_first=fail_first):

                async def respond(models, messages):
                    responses = await parallel_reply(models, messages)
                    if fail_first and "FINAL RANKING:" not in messages[-1]["content"]:
                        responses[0] = (models[0], None)
                    return responses

                conversation_id = self.store.create()["id"]
                with patch.object(council, "query_models_parallel", side_effect=respond):
                    run = self.runs.start(conversation_id, "Question")
                    await run.task
                self.assertIsNone(run.error)
                answer = self.store.get(conversation_id)["messages"][-1]
                expected = (
                    ["example/alpha #2"] if fail_first else ["example/alpha #1", "example/alpha #2"]
                )
                self.assertEqual([result["member_label"] for result in answer["stage1"]], expected)
                self.assertEqual(len(answer["stage2"]), 2)
                self.assertEqual(list(answer["metadata"]["label_to_model"].values()), expected)
                rankings = answer["metadata"]["aggregate_rankings"]
                self.assertEqual([ranking["model"] for ranking in rankings], expected)
                self.assertTrue(all(ranking["rankings_count"] == 2 for ranking in rankings))
                page = await self.client.get(f"/conversations/{conversation_id}")
                self.assertIn("alpha #2", page.text)

    async def test_failure_leaves_pending_message_and_retry_does_not_duplicate_it(self):
        with patch.object(
            council,
            "query_models_parallel",
            AsyncMock(return_value=[(model, None) for model in COUNCIL_MEMBERS]),
        ):
            with self.assertLogs("backend.runs", level="ERROR"):
                failed = self.runs.start(self.conversation_id, "Please answer")
                await failed.task
        self.assertIn("None of the council models", failed.error)
        self.assertEqual(len(self.store.get(self.conversation_id)["messages"]), 1)
        retry = self.runs.start(self.conversation_id)
        await retry.task
        self.assertIsNone(retry.error)
        self.assertEqual(len(self.store.get(self.conversation_id)["messages"]), 2)

    async def test_chairman_failure_remains_retryable(self):
        async def fail_chairman(model, messages, timeout=120.0):
            return await single_reply(model, messages, timeout) if timeout == 30 else None

        with patch.object(council, "query_model", AsyncMock(side_effect=fail_chairman)):
            with self.assertLogs("backend.runs", level="ERROR"):
                run = self.runs.start(self.conversation_id, "Question")
                await run.task
        self.assertIn("chairman", run.error)
        self.assertEqual(len(self.store.get(self.conversation_id)["messages"]), 1)

    async def test_duplicate_submissions_are_rejected(self):
        run = self.runs.start(self.conversation_id, "Question")
        with self.assertRaises(RunError) as raised:
            self.runs.start(self.conversation_id, "Another question")
        self.assertEqual(raised.exception.status_code, 409)
        with self.assertRaises(RunError):
            self.runs.start(self.conversation_id)
        await run.task
        self.assertEqual(len(self.store.get(self.conversation_id)["messages"]), 2)

    async def test_disconnected_subscriber_does_not_cancel_work_and_replay_resumes(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(models, messages):
            started.set()
            await release.wait()
            return await parallel_reply(models, messages)

        with patch.object(council, "query_models_parallel", AsyncMock(side_effect=delayed)):
            run = self.runs.start(self.conversation_id, "Question")
            listener = run.listen()
            sequence, event = await anext(listener)
            self.assertEqual(event["type"], "stage1_start")
            await started.wait()
            await listener.aclose()
            self.assertFalse(run.task.done())
            release.set()
            await run.task
        replay = [item async for item in run.listen(sequence)]
        self.assertEqual(replay[0][0], sequence + 1)
        self.assertEqual(replay[-1][1]["type"], "complete")
        self.assertEqual(len(self.store.get(self.conversation_id)["messages"]), 2)

    async def test_shutdown_keeps_question_for_retry(self):
        entered = asyncio.Event()

        async def wait_forever(models, messages):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(council, "query_models_parallel", side_effect=wait_forever):
            run = self.runs.start(self.conversation_id, "Question")
            await entered.wait()
            await self.runs.close()
        self.assertTrue(run.done)
        self.assertEqual(len(self.store.get(self.conversation_id)["messages"]), 1)

    async def test_shutdown_before_task_starts_closes_subscribers(self):
        run = self.runs.start(self.conversation_id, "Question")
        await self.runs.close()
        self.assertTrue(run.done)
        self.assertFalse(self.runs.active)
        events = [item async for item in run.listen()]
        self.assertEqual(events[-1][1]["type"], "error")

    async def test_json_api_and_stream_share_the_pipeline(self):
        created = (await self.client.post("/api/conversations", json={})).json()
        url = f"/api/conversations/{created['id']}"
        response = await self.client.post(url + "/message/stream", json={"content": "Question"})
        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        self.assertEqual(events[-1]["type"], "complete")
        self.assertIn("stage2_complete", [event["type"] for event in events])
        followup = await self.client.post(
            url + "/message", json={"content": "More please", "council_enabled": False}
        )
        self.assertEqual(followup.status_code, 200)
        self.assertEqual(followup.json()["stage1"], [])
        self.assertIn("contextual", followup.json()["stage3"]["response"])
        saved = (await self.client.get(url)).json()
        self.assertEqual(len(saved["messages"]), 4)

    async def test_html_fragments_and_named_events(self):
        headers = {"HX-Request": "true"}
        created = await self.client.post("/conversations", headers=headers)
        self.assertIn("HX-Push-Url".lower(), created.headers)
        self.assertNotIn("<!doctype", created.text)
        self.assertEqual(created.headers["HX-Push-Url"], "/")
        response = await self.client.post(
            "/conversations/messages",
            data={"content": "Question", "council_enabled": "true"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("sse-connect=", response.text)
        self.assertIn('id="workspace"', response.text)
        url = response.headers["HX-Push-Url"]
        run_url = re.search(r'sse-connect="([^"]+)"', response.text)[1].replace("&amp;", "&")
        stream = await self.client.get(run_url)
        self.assertIn("event: stage1", stream.text)
        self.assertIn("event: stage2", stream.text)
        self.assertIn("event: stage3", stream.text)
        self.assertIn("event: done", stream.text)
        self.assertIn("Aggregate Rankings", stream.text)
        self.assertIn('hx-swap-oob="outerHTML"', stream.text)
        page = await self.client.get(url)
        self.assertIn("<!doctype html>", page.text)
        self.assertEqual(page.text.count('class="stage stage3"'), 1)

    async def test_home_and_new_conversation_do_not_save_empty_drafts(self):
        existing = self.store.list()
        for method, url in (("GET", "/"), ("POST", "/conversations"), ("GET", "/")):
            response = await self.client.request(method, url, headers={"HX-Request": "true"})
            self.assertEqual(response.status_code, 200)
            self.assertIn('id="message-input"', response.text)
            self.assertIn('action="/conversations/messages"', response.text)
            self.assertIn('data-conversation-id=""', response.text)
            self.assertEqual(self.store.list(), existing)
        await self.client.get(f"/conversations/{self.conversation_id}")
        self.assertEqual(self.store.list(), existing)

    async def test_first_message_validation_does_not_leave_an_empty_conversation(self):
        existing = self.store.list()
        response = await self.client.post(
            "/conversations/messages", data={"content": " \n "}, headers={"HX-Request": "true"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["HX-Retarget"], "#request-error")
        with patch.object(self.runs.settings, "get", side_effect=ValueError("Invalid settings")):
            response = await self.client.post(
                "/conversations/messages", data={"content": "A valid question"}
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.list(), existing)
        council.query_models_parallel.assert_not_awaited()
        council.query_model.assert_not_awaited()

    async def test_native_first_message_is_saved_and_redirects_to_its_conversation(self):
        response = await self.client.post(
            "/conversations/messages", data={"content": "First question"}
        )
        self.assertEqual(response.status_code, 303)
        conversation_id = response.headers["Location"].rsplit("/", 1)[1]
        run = self.runs.active[conversation_id]
        saved = self.store.get(conversation_id)
        self.assertEqual(saved["messages"][0]["content"], "First question")
        self.assertFalse(saved["messages"][0]["council_enabled"])
        await run.task
        self.assertEqual(len(self.store.get(conversation_id)["messages"]), 2)
        home = await self.client.get("/")
        self.assertIn('data-conversation-id=""', home.text)
        self.assertNotIn('class="user-message"', home.text)
        self.assertEqual(len(self.store.list()), 2)

    async def test_sse_last_event_id_does_not_repeat_stages(self):
        run = self.runs.start(self.conversation_id, "Question")
        await run.task
        last_stage = next(
            index for index, event in enumerate(run.events, 1) if event["type"] == "stage3_complete"
        )
        response = await self.client.get(
            f"/runs/{run.id}/events", headers={"Last-Event-ID": str(last_stage)}
        )
        self.assertNotIn("event: stage", response.text)
        self.assertIn("event: done", response.text)
        self.assertEqual(len(self.store.get(self.conversation_id)["messages"]), 2)

    async def test_legacy_metadata_is_reconstructed_on_display(self):
        run = self.runs.start(self.conversation_id, "Question")
        await run.task
        saved = self.store.get(self.conversation_id)
        del saved["messages"][-1]["metadata"]
        self.store.save(saved)
        response = await self.client.get(f"/conversations/{self.conversation_id}")
        self.assertIn("Aggregate Rankings", response.text)
        self.assertIn("<strong>alpha</strong>", response.text)
        self.assertIn("Avg: 1.00", response.text)

    async def test_validation_and_missing_resources(self):
        response = await self.client.post(
            f"/conversations/{self.conversation_id}/messages",
            data={"content": "  "},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["HX-Retarget"], "#request-error")
        self.assertEqual(self.store.get(self.conversation_id)["messages"], [])
        self.assertEqual((await self.client.get("/conversations/not-an-id")).status_code, 404)
        self.assertEqual((await self.client.get("/api/conversations/not-an-id")).status_code, 404)
        self.assertEqual((await self.client.get("/runs/expired/events")).status_code, 204)
        self.assertEqual((await self.client.get("/health")).status_code, 200)

    async def test_cross_origin_forms_cannot_start_paid_model_work(self):
        response = await self.client.post(
            f"/conversations/{self.conversation_id}/messages",
            data={"content": "Question"},
            headers={"Origin": "https://unrelated.example"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.get(self.conversation_id)["messages"], [])
        council.query_models_parallel.assert_not_awaited()

    async def test_retry_routes_and_native_form_redirects(self):
        created = await self.client.post("/conversations")
        self.assertEqual(created.status_code, 303)
        self.assertEqual(created.headers["Location"], "/")
        self.store.append_message(self.conversation_id, {"role": "user", "content": "Question"})
        response = await self.client.post(f"/api/conversations/{self.conversation_id}/retry")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.store.get(self.conversation_id)["messages"]), 2)
        self.assertEqual(
            (
                await self.client.post(f"/api/conversations/{self.conversation_id}/retry")
            ).status_code,
            400,
        )

    async def test_delete_removes_all_branches_usage_and_retained_runs(self):
        original = self.runs.start(self.conversation_id, "Original question")
        await original.task
        edited = self.runs.start(
            self.conversation_id, "Edited question", edit_message_id=original.message_id
        )
        await edited.task
        self.assertEqual(len(self.store.get(self.conversation_id)["message_nodes"]), 4)
        other = self.store.create()
        response = await self.client.post(
            f"/conversations/{self.conversation_id}/delete",
            data={"current_conversation_id": other["id"]},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('id="workspace"', response.text)
        self.assertNotIn("HX-Replace-Url", response.headers)
        self.assertEqual(
            json.loads(response.headers["HX-Trigger"]),
            {"conversationDeleted": self.conversation_id},
        )
        self.assertIsNone(self.store.get(self.conversation_id))
        self.assertFalse(Path(self.directory.name, f"{self.conversation_id}.json").exists())
        self.assertEqual(self.store.get(other["id"]), other)
        for run in (original, edited):
            self.assertNotIn(run.id, self.runs.runs)
            self.assertEqual((await self.client.get(f"/runs/{run.id}/events")).status_code, 204)
        response = await self.client.get(f"/api/conversations/{self.conversation_id}")
        self.assertEqual(response.status_code, 404)

    async def test_delete_current_conversation_returns_new_workspace(self):
        response = await self.client.post(
            f"/conversations/{self.conversation_id}/delete",
            data={"current_conversation_id": self.conversation_id},
            headers={"HX-Request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["HX-Retarget"], "#workspace")
        self.assertEqual(response.headers["HX-Replace-Url"], "/")
        self.assertIn('data-conversation-id=""', response.text)
        self.assertIn("No conversations yet", response.text)
        self.assertEqual(self.store.list(), [])

    async def test_delete_cancels_models_and_title_without_recreating_history(self):
        models_started, title_started = asyncio.Event(), asyncio.Event()
        stopped = set()

        async def wait_for_deletion(event, name):
            begin_request(COUNCIL_MEMBERS[0])
            event.set()
            try:
                await asyncio.Event().wait()
            finally:
                finish_request()
                stopped.add(name)

        async def models(*args, **kwargs):
            await wait_for_deletion(models_started, "models")

        async def title(*args, **kwargs):
            await wait_for_deletion(title_started, "title")

        with (
            patch.object(council, "query_models_parallel", side_effect=models),
            patch.object(council, "query_model", side_effect=title),
        ):
            run = self.runs.start(self.conversation_id, "Question")
            await asyncio.wait_for(models_started.wait(), 1)
            await asyncio.wait_for(title_started.wait(), 1)
            listener = run.listen(len(run.events))
            waiting = asyncio.create_task(anext(listener, None))
            await asyncio.sleep(0)
            await self.runs.delete(self.conversation_id)
            self.assertIsNone(await asyncio.wait_for(waiting, 1))
        self.assertEqual(stopped, {"models", "title"})
        self.assertTrue(run.task.cancelled())
        self.assertTrue(run.done)
        self.assertNotIn(self.conversation_id, self.runs.active)
        self.assertIsNone(self.store.get(self.conversation_id))
        self.assertEqual(self.store.list(), [])

    async def test_delete_before_task_starts_unblocks_api_waiters(self):
        run = self.runs.start(self.conversation_id, "Question")
        await self.runs.delete(self.conversation_id)
        self.assertTrue(run.done)
        self.assertEqual([item async for item in run.listen()], [])
        with self.assertRaises(RunError) as raised:
            await completed_result(run)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertIsNone(self.store.get(self.conversation_id))

    async def test_failed_delete_keeps_history_and_run_available(self):
        run = self.runs.start(self.conversation_id, "Question")
        await run.task
        before = self.store.get(self.conversation_id)
        with patch.object(self.store, "delete", side_effect=PermissionError("Read only")):
            response = await self.client.post(
                f"/conversations/{self.conversation_id}/delete", headers={"HX-Request": "true"}
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers["HX-Retarget"], "#request-error")
        self.assertNotIn("HX-Trigger", response.headers)
        self.assertEqual(self.store.get(self.conversation_id), before)
        self.assertIn(run.id, self.runs.runs)
        self.assertFalse(run.deleted)

    async def test_delete_rejects_cross_origin_and_missing_ids_and_supports_native_forms(self):
        url = f"/conversations/{self.conversation_id}/delete"
        response = await self.client.post(url, headers={"Origin": "https://unrelated.example"})
        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(self.store.get(self.conversation_id))
        self.assertEqual(
            (await self.client.post("/conversations/not-an-id/delete")).status_code, 404
        )
        response = await self.client.post(url)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["Location"], "/")
        self.assertEqual((await self.client.post(url)).status_code, 404)


class StorageAndRenderingTests(unittest.TestCase):
    def test_copy_source_is_escaped_and_preserves_original_markdown(self):
        source = '</span><script>alert(1)</script>\n\n**Čau** & "hello"'
        html = render("message.html", message={"role": "user", "content": source})
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;/span&gt;&lt;script&gt;", html)
        self.assertIn('class="copy-source" hidden hx-disable', html)
        self.assertIn("**Čau** &amp;", html)

    def test_atomic_write_failure_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConversationStore(directory)
            conversation = store.create()
            original = store.get(conversation["id"])
            conversation["title"] = "A changed title"
            with patch("backend.persistence.os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    store.save(conversation)
            self.assertEqual(store.get(conversation["id"]), original)
            self.assertFalse(list(Path(directory).glob("*.tmp")))
            self.assertIsNone(store.get("../../outside"))

    def test_unicode_round_trip_and_invalid_file_does_not_hide_history(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConversationStore(directory)
            conversation = store.create()
            store.append_message(conversation["id"], {"role": "user", "content": "Čau 👋"})
            self.assertEqual(store.get(conversation["id"])["messages"][0]["content"], "Čau 👋")
            Path(directory, "11111111-1111-1111-1111-111111111111.json").write_text("{bad json")
            with self.assertLogs("backend.storage", level="WARNING"):
                self.assertEqual(len(store.list()), 1)

    def test_markdown_does_not_execute_model_html_or_unsafe_links(self):
        html = str(
            render_markdown(
                "<script>alert(1)</script>\n\n[bad](javascript:alert(1))\n\n<img src=x onerror=alert(1)>"
            )
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img", html)
        self.assertNotIn('href="javascript:', html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("<code>", str(render_markdown("`code`")))

    def test_sse_formats_multiline_html_as_one_event(self):
        event = encode_event("<p>first</p>\n<p>second</p>", event="stage1", event_id=3)
        self.assertEqual(event, "id: 3\nevent: stage1\ndata: <p>first</p>\ndata: <p>second</p>\n\n")


if __name__ == "__main__":
    unittest.main()
