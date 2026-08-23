# Introduction

This is a lightweight local demo of AI Agent focusing on Epidemiological Research. It runs on macOS and Linux
with Python 3.12 and an OpenAI or Anthropic API key (or a self-hosted
OpenAI-compatible endpoint such as vLLM).
[Watch the demo video](https://drive.google.com/file/d/1A-N8pTOn6tKcZ_D6DLPc62EWvb2e_68E/view?usp=sharing)
## Start the demo

```bash
# Install uv if needed (macOS/Linux)
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/xutao-wang/Epi-AI-Agent.git
cd Epi-AI-Agent

uv python install 3.12
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt

cp .env.example .env
```

The repository root includes the RePORT India and NHANES study-package
archives used by this demo. Install both packages before starting the server:

```bash
python study_installer.py --study \
  report-india-synthetic-0.4.0.tar.gz \
  nhanes-2017-2018-0.3.0.tar.gz
python run_fastapi.py
```

The real compiled-browser smoke tests also require Playwright's pinned Python
package from `requirements.txt` and its Chromium binary:

```bash
python -m playwright install chromium
```

If Chromium is not installed, browser smokes are unavailable and must not be
reported as passing.

On the first native start, choose where to keep local runtime data, then
configure OpenAI, Anthropic Claude, or a compatible endpoint. Re-open the
provider menu any time with
`python run_fastapi.py --reconfigure`. Set `REPORT_AGENT_STUDY_ROOT` and
`REPORT_AGENT_RUNTIME_ROOT` to preselect the local folders. They hold
conversations, uploads, generated datasets, and results and are intentionally
not committed.

The launcher verifies every configured provider on every start. A failed
OpenAI or Anthropic key is reported by provider name and you are prompted for
a replacement; the failed value is never saved. A failed compatible endpoint
is omitted from the available model list for that run. Once at least one
provider verifies, the app prints the following local address:

<http://127.0.0.1:8000/>

This is a native Python startup. The application uses a fixed local identity so
conversations created by prior local versions remain available. The verified
keys stay in the local `.env` file and are never returned by an API response.

While the agent is working, **Cancel** stops the active request and returns the
conversation to its latest completed or approved save point. The cancelled
request and its original attachments remain visible and can be retried later;
unfinished model, tool, and Python output is discarded. **New conversation**
continues to open a separate blank conversation and does not cancel work.

## Model providers

The available model list is derived from verified providers:

- A verified `OPENAI_API_KEY` shows every registered GPT model.
- A verified `ANTHROPIC_API_KEY` shows every registered Claude model.
- Configure providers independently through `--reconfigure`; when both keys
  verify, both model families appear.
- `REPORT_AGENT_MODEL` and `REPORT_AGENT_ALLOWED_MODELS` are obsolete and are
  removed from `.env` during startup. The default is GPT-5.6 Terra when OpenAI
  is available, otherwise Claude Opus 5.
- Custom endpoints are registered in `config/custom_models.json` (see
  `config/custom_models.example.json`): each entry names the endpoint's
  `base_url`, the served model name, optional token limits, and an optional
  `api_key_env` variable for its key. This option connects to an externally
  managed compatible service; it does not install or start vLLM or Ray.

Saved conversations remain readable if their original provider is unavailable.
To send another message in such a conversation, select and confirm one of the
currently available models; the conversation then continues with that model.

Evidence retrieval routes embeddings independently from the selected chat
model. Non-secret embedding cards live in `config/embedding_models.json`; the
registry default is used unless an operator sets `DB_RAG_EMBEDDING_PROFILE`.
The built-in `openai-text-embedding-3-large` profile reads `OPENAI_API_KEY`,
even when the chat model is Claude or a custom model. The legacy
`DB_RAG_EMBEDDING_MODEL` setting is still mapped to a card by its exact index
compatibility identity during migration.

At application startup, the selected profile makes one bounded real connection
probe. A successful probe is silent and enables combined vector-plus-lexical
catalog, reviewed-publication, and study-design ranking for study indexes that
match the card. Invalid cards, missing credentials, unsupported transports,
provider failures, invalid vectors, and incompatible indexes never stop the
application or agent: searches continue with lexical matching and the UI shows
one sanitized fallback notice. Searches do not repeat the startup health probe;
later query failures fall back only for that request.

Adding a future embedding model such as Qwen requires an administrator to add a
registry card, register its code-owned transport adapter, and build matching
study indexes. The browser intentionally has no embedding-profile selector,
because users cannot rebuild packaged indexes from the application.

To make one real chat-provider smoke request with OpenAI and OpenRouter keys
removed for the duration of the request, run:

```bash
.venv/bin/python scripts/smoke_anthropic_only.py
```

The smoke prints only the selected model and response length, never credential
values or response content.

## Included demo data

The study uses a synthetic RePORT India database, its schema
catalog, a matching OpenAI embedding index, verified publication summaries, and
study-design metadata. No raw source data or original papers are included.

The `data/` folder also includes small synthetic CSV files and a matching
column dictionary for attachment and analysis demonstrations.

## Safety note

Local Python analysis is bounded for accidental or model-generated mistakes. It
is not a security sandbox for hostile code; use the demo only with files and
requests you trust.
