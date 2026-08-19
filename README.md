# Introduction

This is a lightweight local demo of AI Agent focusing on Epidemiological Research. It runs on macOS and Linux
with Python 3.12 and an OpenAI API key.
[Watch the demo video](https://drive.google.com/file/d/1cHvKXqePVmtHrQcJatWAn8k-g_BR4HHr/view?usp=sharing)
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
python study_installer.py --study report-india-synthetic-0.2.0.tar.gz
python run_fastapi.py
```

The real compiled-browser smoke tests also require Playwright's pinned Python
package from `requirements.txt` and its Chromium binary:

```bash
python -m playwright install chromium
```

If Chromium is not installed, browser smokes are unavailable and must not be
reported as passing.

On the first native start, the study installer separately asks where to keep
study packages, and the launcher asks where to keep local runtime data. Set
`REPORT_AGENT_STUDY_ROOT` and `REPORT_AGENT_RUNTIME_ROOT` to preselect those
locations. The runtime folder holds conversations, uploads, generated datasets,
and results; it is intentionally not committed.

You will then be asked to enter your OpenAI API key, so have it ready. Once it
is verified, the app prints the following local address:

<http://127.0.0.1:8000/>

This remains a native Python startup: Docker is not required. Local mode uses
the fixed local browser identity and session so conversations created by prior
local versions remain available. The verified key stays in the local `.env`
file and is never returned by an API response.

## Invitation-only hosted mode

The application also has a Cognito/BYOK mode for a future hosted working demo.
Set the following values in the server environment, leave `OPENAI_API_KEY`
unset on the server, and keep exactly one Uvicorn worker:

```bash
REPORT_AGENT_AUTH_MODE=cognito
REPORT_AGENT_AWS_REGION=us-east-1
REPORT_AGENT_COGNITO_USER_POOL_ID=us-east-1_example
REPORT_AGENT_COGNITO_APP_CLIENT_ID=client-id
REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT=https://example.auth.us-east-1.amazoncognito.com/logout
REPORT_AGENT_AUTH_REDIRECT_URI=https://demo.example/auth/callback
REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI=https://demo.example/
REPORT_AGENT_WEB_CONCURRENCY=1
REPORT_AGENT_RUNTIME_ROOT=/path/to/private/runtime
REPORT_AGENT_CHECKPOINT_DB_PATH=/path/to/private/runtime/agent_memory_fastapi.db
REPORT_AGENT_STUDY_ROOT=/path/to/private/study-data
python run_fastapi.py --host 127.0.0.1 --port 8000
```

Each invited user signs in and enters their own OpenAI key in the browser. The
key is validated against OpenAI and held only in process memory for that user
and browser session. It disappears on logout, server restart, token expiry, or
after 12 hours without use, and must then be entered again. Conversations,
checkpoints, uploads, and generated artifacts persist across restarts in
owner-specific storage; provider keys do not.

The hosted working demo is invitation-only and accepts synthetic or fully
de-identified data only. Phase 2A repository operations are documented in the
[AWS runbook](docs/aws/phase2a-runbook.md); repository support does not mean a
live stack exists until Task 11. See [docs/working-demo.md](docs/working-demo.md)
for the operating boundary and acceptance smoke.

While the agent is working, **Cancel** stops the active request and returns the
conversation to its latest completed or approved save point. The cancelled
request and its original attachments remain visible and can be retried later;
unfinished model, tool, and Python output is discarded. **New conversation**
continues to open a separate blank conversation and does not cancel work.

## Included demo data

The study uses a synthetic RePORT India database, its schema
catalog, a matching OpenAI embedding index, verified publication summaries, and
study-design metadata. No raw source data or original papers are included.

The `data/` folder also includes small synthetic CSV files and a matching
column dictionary for attachment and analysis demonstrations.

## Multi-study semantic catalog smoke

The internal backend smoke below installs the real RePORT India and NHANES
2017–2018 packages into a temporary directory, binds each package to its own
Chroma index, performs semantic schema retrieval, and opens each DuckDB
read-only. It requires `OPENAI_API_KEY` and does not modify the configured
study installation.

```bash
PYTHONPATH=. .venv/bin/python scripts/smoke_multi_study_semantic_catalog.py \
  --report-archive ../Database/report-india-synthetic/delivery/report-india-synthetic-0.3.1.tar.gz \
  --nhanes-archive ../Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.2.0.tar.gz
```

## Safety note

Local Python analysis is bounded for accidental or model-generated mistakes. It
is not a security sandbox for hostile code; use the demo only with files and
requests you trust.
