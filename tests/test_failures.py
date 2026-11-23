"""Configuration checks and recoverable failures across HTTP, runs, and storage."""

import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend import council
from backend.config import Settings
from backend.failures import ProviderError
from backend.main import create_app
from backend.usage import begin_request, finish_request
from tests.fixtures import CONFIGURATION, configure_test_app, parallel_reply, single_reply


def failed_request(model, code):
    error = ProviderError(code)
    request = begin_request(model)
    request.update(status="failed", error=error.as_dict())
    finish_request()
    return error


class FailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app(self.directory.name)
        configure_test_app(self.app)
        self.runs = self.app.state.runs
        self.store = self.app.state.store
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        for target, reply in (
            ("query_models_parallel", parallel_reply),
            ("query_model", single_reply),
        ):
            mock = patch.object(council, target, AsyncMock(side_effect=reply))
            mock.start()
            self.addCleanup(mock.stop)

    async def asyncTearDown(self):
        await self.runs.close()
        await self.client.aclose()

    async def test_missing_key_blocks_new_questions_before_saving_or_calling_models(self):
        for key in (None, "", "  "):
            with (
                self.subTest(key=key),
                patch.object(
                    self.runs.credentials,
                    "get",
                    return_value=Settings(_env_file=None, openrouter_api_key=key),
                ),
            ):
                response = await self.client.post(
                    "/conversations/messages",
                    data={"content": "Keep my draft"},
                    headers={"HX-Request": "true"},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.headers["HX-Retarget"], "#request-error")
                self.assertIn("Add an OpenRouter API key", response.text)
                self.assertIn('href="/council/setup"', response.text)
                self.assertNotIn('id="message-input"', response.text)
                self.assertEqual(self.store.list(), [])
                self.assertFalse(self.runs.runs)
        council.query_models_parallel.assert_not_awaited()
        council.query_model.assert_not_awaited()

    async def test_missing_key_preserves_existing_history_for_send_and_edit(self):
        run = self.runs.start(None, "Original question")
        await run.task
        before = self.store.get(run.conversation_id)
        missing = Settings(_env_file=None, openrouter_api_key=None)
        with patch.object(self.runs.credentials, "get", return_value=missing):
            response = await self.client.post(
                f"/api/conversations/{run.conversation_id}/message",
                json={"content": "Follow-up"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertTrue(response.json()["setup_required"])
            response = await self.client.post(
                f"/conversations/{run.conversation_id}/messages/{run.message_id}/edit",
                data={"content": "Edited question"},
                headers={"HX-Request": "true"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.headers["HX-Retarget"], f"#edit-error-{run.message_id}")
            self.assertIn("Open Setup", response.text)
        self.assertEqual(self.store.get(run.conversation_id), before)

    async def test_invalid_model_settings_are_blocked_and_can_be_repaired_in_setup(self):
        for changes in (
            {"members": []},
            {"chairman": ["", None]},
            {"title_model": ["", None]},
        ):
            with self.subTest(changes=changes):
                configuration = CONFIGURATION.model_dump(mode="json") | changes
                self.runs.settings.path.write_text(json.dumps(configuration))
                response = await self.client.post(
                    "/conversations/messages",
                    data={"content": "Question"},
                    headers={"HX-Request": "true"},
                )
                self.assertEqual(response.status_code, 503)
                self.assertIn("Open Setup", response.text)
                if "members" in changes:
                    self.assertIn("at least one council model", response.text)
                with self.assertLogs("backend.setup_views", level="ERROR"):
                    setup = await self.client.get("/council/setup")
                self.assertEqual(setup.status_code, 200)
                self.assertIn("save to replace them", setup.text)
        self.assertEqual(self.store.list(), [])
        council.query_models_parallel.assert_not_awaited()
        council.query_model.assert_not_awaited()

    async def test_question_storage_failure_does_not_start_a_run(self):
        with patch.object(self.store, "save", side_effect=OSError("Private disk details")):
            response = await self.client.post(
                "/conversations/messages",
                data={"content": "Question"},
                headers={"HX-Request": "true"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("Your question could not be saved", response.text)
        self.assertNotIn("Private disk details", response.text)
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.runs.runs)
        council.query_model.assert_not_awaited()
        council.query_models_parallel.assert_not_awaited()

    async def test_provider_failure_survives_restart_and_retry_reuses_the_question(self):
        async def rejected(model, messages, timeout=120.0):
            raise failed_request(model, "authentication")

        with (
            patch.object(council, "query_model", side_effect=rejected),
            self.assertLogs("backend.runs", level="ERROR"),
        ):
            run = self.runs.start(None, "Question", council_enabled=False)
            await run.task
        self.assertIn("rejected the API key", run.error)
        self.assertTrue(run.error_setup_required)
        self.assertFalse(self.runs.active)
        self.assertEqual(len(self.store.get(run.conversation_id)["messages"]), 1)
        events = await self.client.get(f"/runs/{run.id}/events")
        self.assertIn("rejected the API key", events.text)
        self.assertIn("Open Setup", events.text)
        self.assertIn("Retry", events.text)
        self.assertIn("event: done", events.text)
        restarted = create_app(self.directory.name)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted), base_url="http://test"
        ) as client:
            page = await client.get(f"/conversations/{run.conversation_id}")
            self.assertIn("rejected the API key", page.text)
            self.assertIn("Open Setup", page.text)
            response = await client.post(f"/api/conversations/{run.conversation_id}/retry")
            self.assertEqual(response.status_code, 200)
        await restarted.state.runs.close()
        saved = self.store.get(run.conversation_id)
        self.assertEqual([message["role"] for message in saved["messages"]], ["user", "assistant"])
        self.assertEqual(saved["messages"][0]["id"], run.message_id)
        self.assertNotIn("last_error", saved["messages"][0])
        self.assertGreater(len(saved["request_usage"][run.message_id]), len(run.result["requests"]))

    async def test_partial_answers_and_failed_rankings_do_not_discard_successful_answer(self):
        async def partial(models, messages):
            if "FINAL RANKING:" in messages[-1]["content"]:
                for model in models:
                    error = failed_request(model, "rate_limit")
                raise error
            failed_request(models[0], "model")
            responses = await parallel_reply(models[1:], messages)
            return [(models[0], None), *responses]

        with patch.object(council, "query_models_parallel", side_effect=partial):
            run = self.runs.start(None, "Question")
            await run.task
        self.assertIsNone(run.error)
        self.assertEqual(len(run.result["stage1"]), 1)
        self.assertEqual(run.result["stage2"], [])
        self.assertTrue(run.result["stage3"])
        page = await self.client.get(f"/conversations/{run.conversation_id}")
        self.assertIn("Some model requests failed", page.text)
        self.assertIn("selected model is unavailable", page.text)
        self.assertIn("rate limiting requests", page.text)
        self.assertNotIn('class="retry-form"', page.text)

    async def test_title_failure_keeps_answer_and_records_failure_details(self):
        async def reply(model, messages, timeout=120.0):
            if timeout == 30.0:
                raise failed_request(model, "model")
            return await single_reply(model, messages, timeout)

        with patch.object(council, "query_model", side_effect=reply):
            run = self.runs.start(None, "Question")
            await run.task
        self.assertIsNone(run.error)
        saved = self.store.get(run.conversation_id)
        self.assertEqual(saved["title"], "Question")
        self.assertEqual(len(saved["messages"]), 2)
        title = next(row for row in run.result["requests"] if row["phase"] == "title")
        self.assertEqual(title["error"]["code"], "model")

    async def test_unexpected_failure_is_sanitized_and_keeps_retry_available(self):
        with (
            patch.object(council, "query_models_parallel", side_effect=RuntimeError("secret-key")),
            self.assertLogs("backend.runs", level="ERROR") as logs,
        ):
            run = self.runs.start(None, "Question")
            await run.task
        self.assertIn("could not be completed", run.error)
        page = await self.client.get(f"/conversations/{run.conversation_id}")
        self.assertIn("could not be completed", page.text)
        self.assertIn('class="retry-form"', page.text)
        self.assertNotIn("secret-key", page.text + "\n".join(logs.output))
        self.assertFalse(self.runs.active)

    async def test_response_storage_failure_keeps_question_for_retry(self):
        with (
            patch.object(self.store, "append_message", side_effect=OSError("disk full")),
            self.assertLogs("backend.runs", level="ERROR"),
        ):
            run = self.runs.start(None, "Question")
            await run.task
        self.assertIn("response could not be saved", run.error)
        self.assertEqual(len(self.store.get(run.conversation_id)["messages"]), 1)
        retry = self.runs.start(run.conversation_id)
        await retry.task
        self.assertIsNone(retry.error)
        self.assertEqual(len(self.store.get(run.conversation_id)["messages"]), 2)
