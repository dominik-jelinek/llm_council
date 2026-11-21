"""3-stage LLM Council orchestration."""

from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

from backend.config import CouncilConfig, ModelSpec, model_label
from backend.openrouter import query_model, query_models_parallel
from backend.usage import request_phase


class CouncilError(Exception):
    """A failed turn that can be retried without saving a fake assistant answer."""


def member_labels(models: tuple[ModelSpec, ...]) -> list[str]:
    """Duplicate entries get their seat number so answers and votes stay separate."""
    counts = Counter(models)
    return [
        model_label(*model) + (f" #{index}" if counts[model] > 1 else "")
        for index, model in enumerate(models, 1)
    ]


def result_model_label(result: dict[str, Any]) -> str:
    return result.get("member_label") or model_label(
        result["model"], result.get("reasoning_effort")
    )


def response_labels(stage1_results: list[dict[str, Any]]) -> dict[str, str]:
    return {
        f"Response {chr(65 + index)}": result_model_label(result)
        for index, result in enumerate(stage1_results)
    }


def ranking_metadata(
    stage1_results: list[dict[str, Any]], stage2_results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Reconstruct display metadata for conversations saved by older versions."""
    labels = response_labels(stage1_results)
    return {
        "label_to_model": labels,
        "aggregate_rankings": calculate_aggregate_rankings(stage2_results, labels),
    }


async def stage1_collect_responses(
    user_query: str, history: list[dict[str, Any]], models: tuple[ModelSpec, ...]
) -> list[dict[str, Any]]:
    """
    Stage 1: Collect individual responses from all council models.

    Args:
        user_query: The user's question
        history: Prior conversation messages

    Returns:
        List of dicts with 'model' and 'response' keys
    """
    messages = build_conversation_messages(history, user_query)

    # Query all models in parallel
    with request_phase("stage1"):
        responses = await query_models_parallel(models, messages)

    # Format results
    stage1_results = []
    for (model, response), label in zip(responses, member_labels(models)):
        if response is not None:  # Only include successful responses
            stage1_results.append(
                {
                    "model": model[0],
                    "reasoning_effort": model[1],
                    "member_label": label,
                    "response": response.get("content", ""),
                    "request": response.get("request"),
                }
            )

    return stage1_results


async def stage2_collect_rankings(
    user_query: str,
    stage1_results: list[dict[str, Any]],
    history: list[dict[str, Any]],
    models: tuple[ModelSpec, ...],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """
    Stage 2: Each model ranks the anonymized responses.

    Args:
        user_query: The original user query
        stage1_results: Results from Stage 1
        history: Prior conversation messages

    Returns:
        Tuple of (rankings list, label_to_model mapping)
    """
    # Create anonymized labels for responses (Response A, Response B, etc.)
    label_to_model = response_labels(stage1_results)

    # Build the ranking prompt
    responses_text = "\n\n".join(
        [f"{label}:\n{result['response']}" for label, result in zip(label_to_model, stage1_results)]
    )

    ranking_prompt = f"""You are evaluating different responses to the following question:

Question: {user_query}

Here are the responses from different models (anonymized):

{responses_text}

Your task:
1. First, evaluate each response individually. For each response, explain what it does well and what it does poorly.
2. Then, at the very end of your response, provide a final ranking.

IMPORTANT: Your final ranking MUST be formatted EXACTLY as follows:
- Start with the line "FINAL RANKING:" (all caps, with colon)
- Then list the responses from best to worst as a numbered list
- Each line should be: number, period, space, then ONLY the response label (e.g., "1. Response A")
- Do not add any other text or explanations in the ranking section

Example of the correct format for your ENTIRE response:

Response A provides good detail on X but misses Y...
Response B is accurate but lacks depth on Z...
Response C offers the most comprehensive answer...

FINAL RANKING:
1. Response C
2. Response A
3. Response B

Now provide your evaluation and ranking:"""

    messages = build_conversation_messages(history, ranking_prompt)

    # Get rankings from all council models in parallel
    with request_phase("stage2"):
        responses = await query_models_parallel(models, messages)

    # Format results
    stage2_results = []
    for (model, response), label in zip(responses, member_labels(models)):
        if response is not None:
            full_text = response.get("content", "")
            parsed = parse_ranking_from_text(full_text)
            stage2_results.append(
                {
                    "model": model[0],
                    "reasoning_effort": model[1],
                    "member_label": label,
                    "ranking": full_text,
                    "parsed_ranking": parsed,
                    "request": response.get("request"),
                }
            )

    return stage2_results, label_to_model


async def stage3_synthesize_final(
    user_query: str,
    stage1_results: list[dict[str, Any]],
    stage2_results: list[dict[str, Any]],
    history: list[dict[str, Any]],
    chairman: ModelSpec,
) -> dict[str, Any]:
    """
    Stage 3: Chairman synthesizes final response.

    Args:
        user_query: The original user query
        stage1_results: Individual model responses from Stage 1
        stage2_results: Rankings from Stage 2
        history: Prior conversation messages

    Returns:
        Dict with 'model' and 'response' keys
    """
    # Build comprehensive context for chairman
    stage1_text = "\n\n".join(
        [
            f"Model: {result_model_label(result)}\nResponse: {result['response']}"
            for result in stage1_results
        ]
    )

    stage2_text = "\n\n".join(
        [
            f"Model: {result_model_label(result)}\nRanking: {result['ranking']}"
            for result in stage2_results
        ]
    )

    chairman_prompt = f"""You are the Chairman of an LLM Council. Multiple AI models have provided responses to a user's question, and then ranked each other's responses.

Original Question: {user_query}

STAGE 1 - Individual Responses:
{stage1_text}

STAGE 2 - Peer Rankings:
{stage2_text}

Your task as Chairman is to synthesize all of this information into a single, comprehensive, accurate answer to the user's original question. Consider:
- The individual responses and their insights
- The peer rankings and what they reveal about response quality
- Any patterns of agreement or disagreement

Provide a clear, well-reasoned final answer that represents the council's collective wisdom:"""

    messages = build_conversation_messages(history, chairman_prompt)

    # Query the chairman model
    with request_phase("stage3"):
        response = await query_model(chairman, messages)

    if response is None:
        raise CouncilError("The chairman could not generate a final answer. Please retry.")

    return {
        "model": chairman[0],
        "reasoning_effort": chairman[1],
        "response": response.get("content", ""),
        "request": response.get("request"),
    }


def build_conversation_messages(
    conversation_messages: list[dict[str, Any]], user_query: str
) -> list[dict[str, str]]:
    """
    Build chat context using prior user messages and final chairman answers only.

    Args:
        conversation_messages: Stored conversation messages before the latest user turn
        user_query: The latest user message

    Returns:
        Chat-completions message list for the chairman model
    """
    messages = []

    for message in conversation_messages:
        if message.get("role") == "user" and message.get("content"):
            messages.append({"role": "user", "content": message["content"]})
            continue

        if message.get("role") == "assistant":
            stage3 = message.get("stage3") or {}
            final_response = stage3.get("response")
            if final_response:
                messages.append({"role": "assistant", "content": final_response})

    messages.append({"role": "user", "content": user_query})

    return messages


async def answer_with_chairman(
    conversation_messages: list[dict[str, Any]], user_query: str, chairman: ModelSpec
) -> dict[str, Any]:
    """
    Answer a question using the chairman only, with any previous conversation context.

    Args:
        conversation_messages: Stored conversation messages before the latest user turn
        user_query: The latest user message

    Returns:
        Dict with 'model' and 'response' keys
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are the Chairman of an LLM Council. "
                "Previous assistant messages are prior final answers synthesized by the chairman. "
                "Use the conversation history to answer the latest user message consistently and clearly."
            ),
        },
        *build_conversation_messages(conversation_messages, user_query),
    ]
    with request_phase("stage3"):
        response = await query_model(chairman, messages)

    if response is None:
        raise CouncilError("The chairman could not answer the question. Please retry.")

    return {
        "model": chairman[0],
        "reasoning_effort": chairman[1],
        "response": response.get("content", ""),
        "request": response.get("request"),
    }


def parse_ranking_from_text(ranking_text: str) -> list[str]:
    """
    Parse the FINAL RANKING section from the model's response.

    Args:
        ranking_text: The full text response from the model

    Returns:
        List of response labels in ranked order
    """
    import re

    # Look for "FINAL RANKING:" section
    if "FINAL RANKING:" in ranking_text:
        # Extract everything after "FINAL RANKING:"
        parts = ranking_text.split("FINAL RANKING:")
        if len(parts) >= 2:
            ranking_section = parts[1]
            # Try to extract numbered list format (e.g., "1. Response A")
            # This pattern looks for: number, period, optional space, "Response X"
            numbered_matches = re.findall(r"\d+\.\s*Response [A-Z]", ranking_section)
            if numbered_matches:
                # Extract just the "Response X" part
                return [re.search(r"Response [A-Z]", m).group() for m in numbered_matches]

            # Fallback: Extract all "Response X" patterns in order
            matches = re.findall(r"Response [A-Z]", ranking_section)
            return matches

    # Fallback: try to find any "Response X" patterns in order
    matches = re.findall(r"Response [A-Z]", ranking_text)
    return matches


def calculate_aggregate_rankings(
    stage2_results: list[dict[str, Any]], label_to_model: dict[str, str]
) -> list[dict[str, Any]]:
    """
    Calculate aggregate rankings across all models.

    Args:
        stage2_results: Rankings from each model
        label_to_model: Mapping from anonymous labels to model names

    Returns:
        List of dicts with model name and average rank, sorted best to worst
    """
    from collections import defaultdict

    # Track positions for each model
    model_positions = defaultdict(list)

    for ranking in stage2_results:
        ranking_text = ranking["ranking"]

        # Parse the ranking from the structured format
        parsed_ranking = parse_ranking_from_text(ranking_text)

        for position, label in enumerate(parsed_ranking, start=1):
            if label in label_to_model:
                model_name = label_to_model[label]
                model_positions[model_name].append(position)

    # Calculate average position for each model
    aggregate = []
    for model, positions in model_positions.items():
        if positions:
            avg_rank = sum(positions) / len(positions)
            aggregate.append(
                {
                    "model": model,
                    "average_rank": round(avg_rank, 2),
                    "rankings_count": len(positions),
                }
            )

    # Sort by average rank (lower is better)
    aggregate.sort(key=lambda x: x["average_rank"])

    return aggregate


async def generate_conversation_title(user_query: str, model: ModelSpec) -> str:
    """
    Generate a short title for a conversation based on the first user message.

    Args:
        user_query: The first user message

    Returns:
        A short title (3-5 words)
    """
    title_prompt = f"""Generate a very short title (3-5 words maximum) that summarizes the following question.
The title should be concise and descriptive. Do not use quotes or punctuation in the title.

Question: {user_query}

Title:"""

    messages = [{"role": "user", "content": title_prompt}]

    with request_phase("title"):
        response = await query_model(model, messages, timeout=30.0)

    if response is None:
        # Fallback to a generic title
        return "New Conversation"

    title = response.get("content", "New Conversation").strip()

    # Clean up the title - remove quotes, limit length
    title = title.strip("\"'")

    # Truncate if too long
    if len(title) > 50:
        title = title[:47] + "..."

    return title


async def stream_turn(
    user_query: str,
    history: list[dict[str, Any]],
    *,
    configuration: CouncilConfig,
    council_enabled: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """The single execution pipeline, independent of HTTP and persistence."""
    if not council_enabled:
        yield {"type": "stage3_start"}
        result = await answer_with_chairman(history, user_query, configuration.chairman)
        yield {"type": "stage3_complete", "data": result}
        return

    yield {"type": "stage1_start"}
    responses = await stage1_collect_responses(user_query, history, configuration.members)
    if not responses:
        raise CouncilError("None of the council models responded. Please retry.")
    yield {"type": "stage1_complete", "data": responses}

    yield {"type": "stage2_start"}
    rankings, labels = await stage2_collect_rankings(
        user_query, responses, history, configuration.members
    )
    metadata = {
        "label_to_model": labels,
        "aggregate_rankings": calculate_aggregate_rankings(rankings, labels),
    }
    yield {"type": "stage2_complete", "data": rankings, "metadata": metadata}

    yield {"type": "stage3_start"}
    result = await stage3_synthesize_final(
        user_query, responses, rankings, history, configuration.chairman
    )
    yield {"type": "stage3_complete", "data": result}
