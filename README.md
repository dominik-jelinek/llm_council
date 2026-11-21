# LLM Council

Ask several AI models a question, compare their answers and anonymous peer
evaluations, and get a final answer synthesized by a chairman model.
Choose your models in the UI, explore alternative questions through conversation
branches, and track costs and token usage.

## Run

Requires Python 3.10+, [uv](https://docs.astral.sh/uv/), and an OpenRouter API key.

```bash
git clone https://github.com/dominik-jelinek/llm_council.git
cd llm_council
uv sync
```

Start the app:

```bash
uv run python -m backend.main
```

Open **http://localhost:8001**. In **Setup**, enter your OpenRouter API key,
choose your models, and save. Then send your first question.

To change storage or the listening address, set `DATA_DIR`, `HOST`, or `PORT` in
`.env` or the environment, then restart. See `.env.example`. `DATA_DIR` defaults to `data/conversations`;
changing it selects a different directory without moving existing data.

API keys saved in Setup stay in `DATA_DIR/settings/credentials.json`, a Git-ignored,
unencrypted file with owner-only access on Unix. The saved key is never returned
to the UI or API. Alternatively, set `OPENROUTER_API_KEY` in the environment or `.env`.
