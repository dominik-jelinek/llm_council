# LLM Council

![LLM Council](header.jpg)

Ask several language models a question, inspect their individual answers and
anonymous peer evaluations, and read a final answer synthesized by a chairman.
The Council switch chooses between the full council and the chairman alone.
Follow-up questions include previous user messages and final answers as context.

The interface uses **server-rendered HTML, HTMX, and a small JavaScript file**.
FastAPI serves both the UI and API. There is no Node dependency or frontend build.

## Setup

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Create a `.env` file in the project root:

```dotenv
OPENROUTER_API_KEY=your-openrouter-key
```

Pydantic Settings loads the key from this file or the environment; environment
variables take precedence. The key uses `SecretStr` so settings output masks it.
Without a key, saved conversations remain accessible, but model requests fail
locally without contacting OpenRouter.

Use **Setup Council** above **New Conversation** in the sidebar to add or remove council members and
set each model's reasoning effort, the chairman, and the title model. **Save Council**
persists these settings in `data/conversations/settings/council.json`, shared across
all conversations and preserved after restarts. New requests (including retries)
use the saved configuration; active runs finish with the configuration they started with.

Before the first save, defaults come from `backend/config.py`. Each entry is a
`(model_id, reasoning_effort)` pair, for example:

```python
COUNCIL_MODELS = [
    ("openai/gpt-6.2-sol", None),
    ("anthropic/claude-fable-5.2", "high"),
]
```

`None` leaves the model's reasoning setting at its provider default; this is the
default for all configured models. An explicit string such as `"high"` or `"none"`
is sent as `reasoning.effort`. Supported levels depend on the model. The same
model at different efforts remains distinct in responses, evaluations, and rankings.
Identical model/effort pairs are also allowed: each row makes its own call and
keeps a separate answer and ranking entry, distinguished by its member number.

All model requests use the official [OpenRouter Python SDK](https://github.com/OpenRouterTeam/python-sdk)
with async chat calls. API credentials remain in Pydantic Settings and are never
included in the council preferences file.

## Run

From the project root:

```bash
./start.sh
```

Open **http://localhost:8001**. The UI and API share this address.

Alternatively:

```bash
uv run python -m backend.main
```

For development with automatic Python reloads:

```bash
uv run uvicorn backend.main:app --reload --host 127.0.0.1 --port 8001
```

Templates and static files are read directly; refresh the browser after editing
them. HTMX and its SSE extension are pinned and served from `frontend/static/vendor`.

## Behavior

- Opening the app starts a blank conversation. New conversations are saved only
  when their first message is sent; leaving an unsent draft adds nothing to history.
- Unfinished questions are kept separately for each conversation and the new-conversation
  composer in the current browser tab. Switching conversations or reloading restores
  the text. Sending clears that draft once the server accepts the message.
- The trash button beside a conversation deletes its history and local draft and
  stops any response still running for it. Deleting the open conversation returns
  to the new-conversation composer.
- Projects group conversations in a collapsible section above the rest of the
  history. Each project starts collapsed. Use **+** beside Projects to create a
  project, and **+** beside its name to start a conversation inside it. Project
  drafts stay separate from each other and from the general new-conversation draft.
- Drag conversations onto a project or use their **…** button to choose a project.
  Choose **No project**, or drag back to Conversations, to remove the grouping.
  Groups persist across restarts and do not share prompts, context, or settings.
- Council is on by default. Each turn collects model responses in parallel,
  gathers anonymous peer rankings, and asks the chairman to synthesize an answer.
- Each stage appears as it completes. This is stage-level streaming, not token
  streaming. Stages 1 and 2 start collapsed; only the final answer starts expanded.
  Click a stage heading to expand it. Streaming preserves your open/closed choice.
- Response and evaluation tabs switch locally. Rankings show extracted votes,
  model names, and average positions.
- Turn Council off below the message field to use the chairman only. The choice
  is remembered after sending a message in that conversation, including retries.
- Copy buttons copy the original Markdown for user messages, model responses,
  evaluations, and final answers.
- **Edit** on a user message opens an inline editor. **Save & send** creates a new
  branch using the context before that message and its original Council setting.
  The original message, answers, and follow-ups remain saved. Left/right buttons
  above the message switch versions without calling models. Nested edits keep
  their own branches. Opening a conversation defaults to the branch with the newest
  message, including follow-ups on older branches. Switching a branch opens its
  newest nested continuation. Explicit branch links preserve access to older versions.
- Branches can be switched while a response runs; its answer stays attached to
  the question that started it. Sending or saving another edit waits for that run.
- Enter sends a message; Shift+Enter inserts a newline.
- Runs continue when you switch conversations or reload the browser. Reopening an
  active conversation reconnects to its existing run.
- A failed turn leaves its question available for **Retry**, without duplicating
  the user message. Concurrent submissions to the same conversation are rejected.
- Markdown is rendered on the server with embedded HTML disabled.
- Each model response and interaction has a cost footer and **Details** button.
  Costs use OpenRouter's reported USD charge; no price estimates or extra API
  requests are made. Details list each request's input/output tokens, cached reads,
  cache writes, reasoning tokens, and other output tokens. Reasoning is already
  included in output totals. Cached percentages use cached reads divided by input
  tokens, weighted across requests for the interaction total.
- Interaction costs include council responses, evaluations, the chairman, title
  generation, and recorded requests from failed attempts when retried. Usage is
  saved as requests finish, separately for each edited question. Missing provider
  fields and older conversations show unavailable values; incomplete costs are
  labelled as reported subtotals, never assumed to be free.

Usage fields follow OpenRouter's [usage accounting documentation](https://openrouter.ai/docs/cookbook/administration/usage-accounting).

Conversations remain JSON files in `data/conversations/`. Existing linear files
are read directly and converted to message trees on their next save. Each message
is stored once, with a parent and a saved branch selection; the API's `messages`
list contains the selected path. New answers save ranking metadata; old answers
reconstruct it from their saved responses and rankings.

## Structure

| Location | Responsibility |
| --- | --- |
| `backend/main.py` | App setup, lifecycle, static files, error responses |
| `backend/council.py` | Prompts, stage execution, rankings, chairman context |
| `backend/openrouter.py` | Async OpenRouter SDK adapter and parallel model calls |
| `backend/usage.py` | Request accounting scopes, token normalization, totals and display formatting |
| `backend/council_settings.py` | Validated persistent council preferences |
| `backend/setup_views.py` | Council configuration editor and form validation |
| `backend/persistence.py` | Atomic JSON writes |
| `backend/storage.py` | JSON persistence and atomic file replacement |
| `backend/projects.py` | Persistent project names and validation |
| `backend/project_views.py` | Project creation and conversation assignment forms |
| `backend/branches.py` | Message trees, legacy histories, branch selection and navigation |
| `backend/branch_views.py` | Inline message editor and branch navigation routes |
| `backend/runs.py` | Run ownership, duplicate prevention, event replay, saving results |
| `backend/api.py` | JSON API and JSON event streams |
| `backend/views.py` | Page/form routes and HTML event streams |
| `backend/rendering.py` | Templates and safe Markdown presentation |
| `backend/events.py` | SSE wire formatting |
| `frontend/templates/` | Page, sidebar, composer, message, and stage fragments |
| `frontend/static/` | Styles, local interaction handlers, vendored HTMX |

A form POST starts a run and returns the new message and stage placeholders.
A separate GET subscribes to that run through HTMX's SSE extension. Reconnecting
only observes the existing work. Stage fragments update independently, keeping
other panels and their selected tabs intact.

This remains a **local, single-process app**. Use one Uvicorn worker: active runs
and event replay live in memory, with the latest 100 completed runs retained.
Saved conversations survive restarts; unfinished work must be retried after a
restart. Multiple workers or durable background jobs would need shared run
coordination and a different storage strategy.

## API

The existing `/api/conversations` routes remain available:

- `GET /api/conversations`, `POST /api/conversations`
- `GET /api/conversations/{id}`
- `POST /api/conversations/{id}/message`
- `POST /api/conversations/{id}/message/stream`
- `POST /api/conversations/{id}/retry`
- `POST /api/conversations/{id}/retry/stream`

Message endpoints accept `{"content": "your question", "council_enabled": true}`;
`council_enabled` defaults to `true`. Retries reuse the saved choice. Streaming API endpoints
retain JSON SSE events; the HTMX UI has separate HTML endpoints. Failed runs
produce an error event or a non-success HTTP response and can be retried.
Completed results include a `requests` ledger; `usage_update` events carry
snapshots as requests finish. Saved conversation usage is available under
`request_usage`, keyed by the user message ID, including inactive branches.
Health checks are at `GET /health`; `/` now serves the UI.

## Checks

Ruff is installed with the development dependencies by `uv sync`. Check Python
lint and formatting with:

```bash
uv run ruff check .
uv run ruff format --check .
```

Backend, API, storage, and rendering tests use fake model responses and temporary
conversation files:

```bash
uv run python -m unittest discover -s tests -v
node --check frontend/static/app.js  # Optional JavaScript syntax check
```

The browser smoke test also uses fake models and an isolated local server. It
checks stage updates and collapsing, cost/token details, tabs, copying, the Council switch, persistent Setup Council
preferences, editing, nested branches, follow-ups, retries, reloads, navigation,
browser history, and mobile layout:

```bash
uv run --with playwright playwright install chromium
uv run --with playwright python -m tests.browser_smoke
```

Set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to use an existing Chromium installation.
Screenshots are written to `/tmp/llm-council-htmx-desktop.png` and
`/tmp/llm-council-htmx-mobile.png`. Browser tooling is optional and is not an
application dependency.
