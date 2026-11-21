"""Deterministic model responses; tests never call OpenRouter."""

from backend.config import CouncilConfig
from backend.usage import begin_request, finish_request, normalize_usage

MODELS = ["example/alpha", "example/beta"]
COUNCIL_MEMBERS = [(model, None) for model in MODELS]
CONFIGURATION = CouncilConfig(
    members=COUNCIL_MEMBERS,
    chairman=("example/chairman", None),
    title_model=("example/title", None),
)
RESPONSES = {
    MODELS[0]: {
        "content": "## Alpha answer\n\nA useful explanation.\n\n```python\nprint('hello')\n```"
    },
    MODELS[1]: {
        "content": "## Beta answer\n\nA second perspective with a [reference](https://example.com)."
    },
}
RANKINGS = {
    model: {
        "content": "Response A is clearer than Response B.\n\nFINAL RANKING:\n1. Response A\n2. Response B"
    }
    for model in MODELS
}


async def parallel_reply(models, messages):
    replies = RANKINGS if "FINAL RANKING:" in messages[-1]["content"] else RESPONSES
    return [(model, accounted_reply(model, replies[model[0]])) for model in models]


def accounted_reply(model, response):
    request = begin_request(model)
    cost = {"stage1": 0.001, "stage2": 0.002, "stage3": 0.003, "title": 0.0001}.get(
        request["phase"], 0.001
    )
    request["usage"] = normalize_usage(
        {
            "cost": cost,
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "total_tokens": 1200,
            "prompt_tokens_details": {"cached_tokens": 750, "cache_write_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 50},
        }
    )
    request["status"] = "complete"
    finish_request()
    return {**response, "request": request}


async def single_reply(model, messages, timeout=120.0):
    if timeout == 30.0:
        return accounted_reply(model, {"content": "A useful conversation"})
    if messages[0]["role"] == "system":
        return accounted_reply(model, {"content": "A contextual follow-up answer."})
    return accounted_reply(
        model, {"content": "## Final answer\n\nThe council agrees on a useful answer."}
    )
