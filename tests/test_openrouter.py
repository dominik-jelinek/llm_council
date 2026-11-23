"""Exercise the actual SDK against a mock HTTP transport, never a paid provider."""

import asyncio
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openrouter import OpenRouter

from backend import openrouter
from backend.config import Settings, get_settings
from backend.credentials import OpenRouterCredentials
from backend.failures import ProviderError
from backend.main import create_app
from backend.usage import track_requests


def completion(content="Answer"):
    return {
        "id": "test-completion",
        "model": "example/model",
        "object": "chat.completion",
        "created": 0,
        "system_fingerprint": None,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


class OpenRouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.response = httpx.Response(200, json=completion())

        def respond(request):
            self.requests.append(request)
            return self.response

        transport = httpx.MockTransport(respond)
        self.http_client = httpx.AsyncClient(transport=transport)
        self.sync_client = httpx.Client(transport=transport)
        self.addCleanup(self.sync_client.close)
        settings = Settings(_env_file=None, openrouter_api_key="sdk-test-key")
        for mock in (
            patch.object(openrouter, "get_settings", return_value=settings),
            patch.object(
                openrouter,
                "OpenRouter",
                side_effect=lambda **kwargs: OpenRouter(
                    **kwargs, async_client=self.http_client, client=self.sync_client
                ),
            ),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    async def asyncTearDown(self):
        await self.http_client.aclose()

    async def test_missing_or_blank_key_never_creates_sdk_client(self):
        for key in (None, "", "  "):
            with self.subTest(key=key):
                settings = Settings(_env_file=None, openrouter_api_key=key)
                with (
                    patch.object(openrouter, "get_settings", return_value=settings),
                    patch.object(openrouter, "OpenRouter") as client,
                    self.assertRaises(ProviderError) as error,
                ):
                    await openrouter.query_model(("example/model", None), [])
                self.assertEqual(error.exception.code, "missing_key")
                client.assert_not_called()

    async def test_sdk_sends_key_payload_and_timeout_without_overriding_default_reasoning(self):
        messages = [{"role": "user", "content": "Question"}]
        result = await openrouter.query_model(("example/model", None), messages, timeout=30)
        self.assertEqual(result["content"], "Answer")
        self.assertIsNone(result["reasoning_details"])
        self.assertIsNone(result["request"]["usage"]["cost_usd"])
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request.headers["Authorization"], "Bearer sdk-test-key")
        self.assertEqual(str(request.url), "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.extensions["timeout"]["read"], 30)
        self.assertEqual(
            json.loads(request.content),
            {
                "model": "example/model",
                "messages": messages,
                "stream": False,
            },
        )

    async def test_sdk_sends_explicit_effort_and_returns_serializable_reasoning_details(self):
        details = [{"type": "reasoning.text", "text": "A test reasoning summary"}]
        payload = completion()
        payload["choices"][0]["message"]["reasoning_details"] = details
        self.response = httpx.Response(200, json=payload)
        for effort in ("high", "none"):
            with self.subTest(effort=effort):
                result = await openrouter.query_model(("example/model", effort), [])
                self.assertEqual(result["reasoning_details"], details)
                self.assertEqual(
                    json.loads(self.requests[-1].content)["reasoning"], {"effort": effort}
                )
                json.dumps(result)

    async def test_sdk_preserves_actual_usage_including_billed_empty_responses(self):
        for content in ("Answer", " "):
            with self.subTest(content=content):
                payload = completion(content)
                payload["usage"] = {
                    "prompt_tokens": 1000,
                    "completion_tokens": 300,
                    "total_tokens": 1300,
                    "cost": 0.012345,
                    "prompt_tokens_details": {"cached_tokens": 750, "cache_write_tokens": 100},
                    "completion_tokens_details": {"reasoning_tokens": 200},
                    "cost_details": {
                        "upstream_inference_prompt_cost": 0.004,
                        "upstream_inference_completions_cost": 0.008345,
                    },
                }
                self.response = httpx.Response(200, json=payload)
                records = []
                changed = []
                with track_requests(records, lambda: changed.append(True), "attempt-1"):
                    if content.strip():
                        result = await openrouter.query_model(("example/model", "high"), [])
                    else:
                        with self.assertRaises(ProviderError) as error:
                            await openrouter.query_model(("example/model", "high"), [])
                        self.assertEqual(error.exception.code, "empty")
                self.assertEqual(len(records), 1)
                self.assertEqual(len(changed), 1)
                record = records[0]
                self.assertEqual(record["generation_id"], "test-completion")
                self.assertEqual(record["usage"]["cost_usd"], 0.012345)
                self.assertEqual(record["usage"]["cached_tokens"], 750)
                self.assertEqual(record["usage"]["cache_write_tokens"], 100)
                self.assertEqual(record["usage"]["reasoning_tokens"], 200)
                self.assertEqual(record["usage"]["output_tokens"], 300)
                if content.strip():
                    self.assertEqual(result["request"], record)
                    self.assertEqual(record["status"], "complete")
                else:
                    self.assertEqual(record["status"], "failed")
                    self.assertEqual(record["error"]["code"], "empty")

    async def test_sdk_errors_malformed_and_empty_responses_are_isolated_without_retries(self):
        for response in (
            httpx.Response(401, json={"error": {"code": 401, "message": "Invalid credentials"}}),
            httpx.Response(503, json={"error": {"code": 503, "message": "Unavailable"}}),
            httpx.Response(200, json={"choices": "invalid"}),
            httpx.Response(200, json=completion(" ")),
        ):
            with self.subTest(status=response.status_code, body=response.text):
                self.response = response
                before = len(self.requests)
                with (
                    self.assertLogs("backend.openrouter", level="WARNING"),
                    self.assertRaises(ProviderError),
                ):
                    await openrouter.query_model(("example/model", None), [])
                self.assertEqual(len(self.requests), before + 1)

    async def test_transport_timeout_fails_but_task_cancellation_propagates(self):
        with patch.object(
            self.http_client, "send", AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        ):
            with (
                self.assertLogs("backend.openrouter", level="WARNING"),
                self.assertRaises(ProviderError) as error,
            ):
                await openrouter.query_model(("example/model", None), [])
            self.assertEqual(error.exception.code, "timeout")
        with patch.object(self.http_client, "send", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await openrouter.query_model(("example/model", None), [])

    async def test_parallel_calls_keep_identical_members_and_different_efforts(self):
        members = [("example/model", "low"), ("example/model", "low"), ("example/model", "high")]
        responses = await openrouter.query_models_parallel(members, [])
        self.assertEqual([model for model, _ in responses], members)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(
            {json.loads(request.content)["reasoning"]["effort"] for request in self.requests},
            {"low", "high"},
        )

    async def test_app_credentials_reach_sdk_for_both_answer_and_title(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(directory)
            app.state.runs.credentials.save(
                OpenRouterCredentials(openrouter_api_key="saved-ui-key")
            )
            with patch.object(openrouter, "get_settings", side_effect=get_settings):
                run = app.state.runs.start(None, "Question", council_enabled=False)
                await run.task
            self.assertIsNone(run.error)
            self.assertEqual(len(self.requests), 2)
            self.assertTrue(
                all(
                    request.headers["Authorization"] == "Bearer saved-ui-key"
                    for request in self.requests
                )
            )
            await app.state.runs.close()

    async def test_provider_error_cannot_echo_api_key_into_logs(self):
        self.response = httpx.Response(
            401, json={"error": {"code": 401, "message": "Rejected token: sdk-test-key"}}
        )
        with (
            self.assertLogs("backend.openrouter", level="WARNING") as logs,
            self.assertRaises(ProviderError) as error,
        ):
            await openrouter.query_model(("example/model", None), [])
        self.assertEqual(error.exception.code, "authentication")
        self.assertNotIn("sdk-test-key", str(error.exception))
        self.assertNotIn("sdk-test-key", "\n".join(logs.output))
        self.assertNotIn("Rejected token", "\n".join(logs.output))

    async def test_provider_failures_have_actionable_categories_and_safe_usage_details(self):
        for status, code in (
            (400, "request"),
            (401, "authentication"),
            (402, "credits"),
            (403, "access"),
            (404, "model"),
            (408, "timeout"),
            (422, "request"),
            (429, "rate_limit"),
            (503, "unavailable"),
            (504, "timeout"),
        ):
            with self.subTest(status=status):
                self.response = httpx.Response(
                    status, json={"error": {"code": status, "message": "Private provider body"}}
                )
                records = []
                with (
                    track_requests(records, lambda: None, "attempt-1"),
                    self.assertLogs("backend.openrouter", level="WARNING"),
                    self.assertRaises(ProviderError) as error,
                ):
                    await openrouter.query_model(("example/model", None), [])
                self.assertEqual(error.exception.code, code)
                self.assertEqual(records[0]["status"], "failed")
                self.assertEqual(records[0]["error"], error.exception.as_dict())
                self.assertNotIn("Private provider body", json.dumps(records))

    async def test_connection_failure_is_reported_as_network_error(self):
        with (
            patch.object(
                self.http_client, "send", AsyncMock(side_effect=httpx.ConnectError("offline"))
            ),
            self.assertRaises(ProviderError) as error,
        ):
            await openrouter.query_model(("example/model", None), [])
        self.assertEqual(error.exception.code, "network")

    async def test_rejected_title_reasoning_is_actionable_and_default_omits_the_parameter(self):
        self.response = httpx.Response(
            400,
            json={
                "error": {
                    "message": "Provider returned error",
                    "metadata": {"raw": "Thinking cannot be disabled. Private token: sdk-test-key"},
                }
            },
        )
        with self.assertRaises(ProviderError) as error:
            await openrouter.query_model(
                ("google/gemini-4-neon", "none"), [{"role": "user", "content": "Title"}], timeout=30
            )
        self.assertEqual(error.exception.code, "reasoning")
        self.assertTrue(error.exception.setup_required)
        self.assertIn("Choose Default", str(error.exception))
        self.assertNotIn("sdk-test-key", str(error.exception))
        self.assertEqual(json.loads(self.requests[-1].content)["reasoning"], {"effort": "none"})
        self.response = httpx.Response(200, json=completion("A generated title"))
        result = await openrouter.query_model(
            ("google/gemini-4-neon", None), [{"role": "user", "content": "Title"}], timeout=30
        )
        self.assertEqual(result["content"], "A generated title")
        self.assertNotIn("reasoning", json.loads(self.requests[-1].content))

    async def test_parallel_failure_preserves_success_and_all_failures_remain_actionable(self):
        members = [("example/one", None), ("example/two", None)]
        with patch.object(
            openrouter,
            "query_model",
            AsyncMock(side_effect=[ProviderError("model"), {"content": "Answer"}]),
        ):
            results = await openrouter.query_models_parallel(members, [])
        self.assertEqual(results, [(members[0], None), (members[1], {"content": "Answer"})])
        with (
            patch.object(
                openrouter,
                "query_model",
                AsyncMock(side_effect=[ProviderError("model"), ProviderError("authentication")]),
            ),
            self.assertRaises(ProviderError) as error,
        ):
            await openrouter.query_models_parallel(members, [])
        self.assertEqual(error.exception.code, "authentication")


if __name__ == "__main__":
    unittest.main()
