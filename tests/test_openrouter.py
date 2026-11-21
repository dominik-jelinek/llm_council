"""Exercise the actual SDK against a mock HTTP transport, never a paid provider."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openrouter import OpenRouter

from backend import openrouter
from backend.config import Settings
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
                    self.assertLogs("backend.openrouter", level="ERROR") as logs,
                ):
                    response = await openrouter.query_model(("example/model", None), [])
                self.assertIsNone(response)
                client.assert_not_called()
                self.assertIn("OPENROUTER_API_KEY is not configured", logs.output[0])

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
                    result = await openrouter.query_model(("example/model", "high"), [])
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
                    self.assertIsNone(result)
                    self.assertEqual(record["status"], "failed")

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
                with self.assertLogs("backend.openrouter", level="WARNING"):
                    self.assertIsNone(await openrouter.query_model(("example/model", None), []))
                self.assertEqual(len(self.requests), before + 1)

    async def test_transport_timeout_fails_but_task_cancellation_propagates(self):
        with patch.object(
            self.http_client, "send", AsyncMock(side_effect=httpx.ReadTimeout("timeout"))
        ):
            with self.assertLogs("backend.openrouter", level="WARNING"):
                self.assertIsNone(await openrouter.query_model(("example/model", None), []))
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


if __name__ == "__main__":
    unittest.main()
