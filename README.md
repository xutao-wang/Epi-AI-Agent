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

On the first native start, the study installer separately asks where to keep
study packages, and the launcher asks where to keep local runtime data. Set
`REPORT_AGENT_STUDY_ROOT` and `REPORT_AGENT_RUNTIME_ROOT` to preselect those
locations. The runtime folder holds conversations, uploads, generated datasets,
and results; it is intentionally not committed.

You will then be asked to enter your OpenAI API key, so have it ready. Once it
is verified, the app prints the following local address:

<http://127.0.0.1:8000/>

This is a native Python startup. The application uses a fixed local identity so
conversations created by prior local versions remain available. The verified
key stays in the local `.env` file and is never returned by an API response.

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

## Safety note

Local Python analysis is bounded for accidental or model-generated mistakes. It
is not a security sandbox for hostile code; use the demo only with files and
requests you trust.
