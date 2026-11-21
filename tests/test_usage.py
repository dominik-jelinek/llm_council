"""Usage totals, scope isolation, persistence, retries, and late title accounting."""

import asyncio
import tempfile
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx

from backend import council
from backend.main import create_app
from backend.usage import format_cost, normalize_usage, summarize_usage
from tests.fixtures import (
    accounted_reply,
    configure_test_app,
    parallel_reply,
    single_reply,
)


class UsageTotalsTests(unittest.TestCase):
    def test_totals_weight_cache_ratio_and_do_not_double_count_reasoning(self):
        records = [
            {
                "usage": normalize_usage(
                    {
                        "cost": cost,
                        "prompt_tokens": inputs,
                        "completion_tokens": 300,
                        "total_tokens": inputs + 300,
                        "prompt_tokens_details": {
                            "cached_tokens": cached,
                            "cache_write_tokens": 25,
                        },
                        "completion_tokens_details": {"reasoning_tokens": 200},
                    }
                )
            }
            for cost, inputs, cached in ((0.1, 1000, 500), (0.2, 100, 0))
        ]
        result = summarize_usage(records)
        self.assertEqual(result["cost_usd"], Decimal("0.3"))
        self.assertAlmostEqual(result["cache_percent"], 500 / 1100 * 100)
        self.assertEqual(result["output_tokens"], 600)
        self.assertEqual(result["reasoning_tokens"], 400)
        self.assertEqual(result["other_output_tokens"], 200)
        self.assertEqual(result["total_tokens"], 1700)
        self.assertEqual(result["cache_write_tokens"], 50)

    def test_missing_usage_is_distinct_from_free_and_uncached_usage(self):
        unknown = summarize_usage([{"usage": normalize_usage(None)}])
        self.assertIsNone(unknown["cost_usd"])
        self.assertIsNone(unknown["cache_percent"])
        self.assertIsNone(unknown["reasoning_tokens"])
        free = {
            "usage": normalize_usage(
                {
                    "cost": 0,
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    "prompt_tokens_details": {"cached_tokens": 0},
                }
            )
        }
        result = summarize_usage([free])
        self.assertEqual(result["cost_usd"], 0)
        self.assertEqual(result["cache_percent"], 0)
        self.assertIsNone(result["other_output_tokens"])
        mixed = summarize_usage([free, {"usage": {}}])
        self.assertTrue(mixed["partial"])
        self.assertIsNone(mixed["cache_percent"])
        self.assertEqual(format_cost(None), "Unavailable")
        self.assertEqual(format_cost(0), "$0")
        self.assertEqual(format_cost(0.0000002), "< $0.000001")

    def test_invalid_counts_and_costs_are_not_presented_as_real_usage(self):
        result = normalize_usage(
            {
                "cost": float("nan"),
                "prompt_tokens": 10,
                "completion_tokens": -1,
                "total_tokens": True,
                "prompt_tokens_details": {"cached_tokens": 11},
                "completion_tokens_details": {"reasoning_tokens": 1.5},
            }
        )
        for field in (
            "cost_usd",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "reasoning_tokens",
        ):
            self.assertIsNone(result[field])


class UsageRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = create_app(self.directory.name)
        configure_test_app(self.app)
        self.runs = self.app.state.runs
        self.store = self.app.state.store
        self.conversation_id = self.store.create()["id"]
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

    async def test_usage_is_persisted_and_rendered_after_restart(self):
        run = self.runs.start(self.conversation_id, "Question")
        await run.task
        stored = self.store.get(self.conversation_id)
        requests = stored["request_usage"][run.message_id]
        self.assertEqual(len(requests), 6)
        self.assertEqual(summarize_usage(requests)["cost_usd"], Decimal("0.0091"))
        self.assertEqual(
            sorted(row["phase"] for row in requests),
            ["stage1", "stage1", "stage2", "stage2", "stage3", "title"],
        )
        self.assertEqual(len([event for event in run.events if event["type"] == "usage_update"]), 6)
        restored = create_app(self.directory.name)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restored), base_url="http://test"
        ) as client:
            page = await client.get(f"/conversations/{self.conversation_id}")
        self.assertIn("$0.0091 USD", page.text)
        self.assertIn("Cached input: 75.0%", page.text)
        self.assertIn("Reasoning", page.text)
        self.assertIn("Title", page.text)

    async def test_title_usage_is_saved_even_when_the_answer_finishes_first(self):
        title_entered, release = asyncio.Event(), asyncio.Event()

        async def slow_title(model, messages, timeout=120):
            if timeout == 30:
                title_entered.set()
                await release.wait()
            return await single_reply(model, messages, timeout)

        with patch.object(council, "query_model", side_effect=slow_title):
            run = self.runs.start(self.conversation_id, "Question")
            await title_entered.wait()
            self.assertTrue(run.saved)
            self.assertFalse(run.done)
            release.set()
            await run.task
        saved = self.store.get(self.conversation_id)
        self.assertEqual(len(saved["request_usage"][run.message_id]), 6)
        self.assertEqual(
            summarize_usage(saved["request_usage"][run.message_id])["cost_usd"], Decimal("0.0091")
        )

    async def test_retry_preserves_paid_requests_from_the_failed_attempt(self):
        async def failed(models, messages):
            responses = []
            for model in models:
                response = accounted_reply(model, {"content": ""})
                response["request"]["status"] = "failed"
                responses.append((model, None))
            await asyncio.sleep(0)
            return responses

        with patch.object(council, "query_models_parallel", side_effect=failed):
            with self.assertLogs("backend.runs", level="ERROR"):
                first = self.runs.start(self.conversation_id, "Question")
                await first.task
        initial = self.store.get(self.conversation_id)["request_usage"][first.message_id]
        restored = create_app(self.directory.name).state.runs
        retry = restored.start(self.conversation_id)
        await retry.task
        saved = self.store.get(self.conversation_id)["request_usage"][first.message_id]
        self.assertEqual(saved[: len(initial)], initial)
        self.assertEqual(len(saved), len(initial) + 6)
        self.assertEqual({row["attempt"] for row in saved}, {first.id, retry.id})
        self.assertEqual(
            summarize_usage(saved)["cost_usd"],
            summarize_usage(initial)["cost_usd"] + Decimal("0.0091"),
        )

    async def test_parallel_conversations_and_edits_keep_separate_ledgers(self):
        other_id = self.store.create()["id"]
        first = self.runs.start(self.conversation_id, "First")
        other = self.runs.start(other_id, "Other", council_enabled=False)
        await asyncio.gather(first.task, other.task)
        self.assertEqual(len(first.result["requests"]), 6)
        self.assertEqual(len(other.result["requests"]), 2)
        self.assertEqual({row["attempt"] for row in first.result["requests"]}, {first.id})
        self.assertEqual({row["attempt"] for row in other.result["requests"]}, {other.id})
        edited = self.runs.start(self.conversation_id, "Edited", edit_message_id=first.message_id)
        await edited.task
        usage = self.store.get(self.conversation_id)["request_usage"]
        self.assertEqual(len(usage), 2)
        self.assertEqual(usage[first.message_id], first.result["requests"])
        self.assertEqual(usage[edited.message_id], edited.result["requests"])
