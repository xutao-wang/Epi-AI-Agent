# Introduction

This is a lightweight local AI agent demo for epidemiological research. It runs
on macOS and Linux with Python 3.12 and an OpenAI or Anthropic API key.

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

Install the included synthetic RePORT India and NHANES study packages, then start the
server:

```bash
python study_installer.py --study \
  report-india-synthetic-0.4.0.tar.gz \
  nhanes-2017-2018-0.3.0.tar.gz
python run_fastapi.py
```

The installer prompts for a study-package folder. On first startup, choose a
runtime-data folder and configure OpenAI or Anthropic. To change providers
later, run `python run_fastapi.py --reconfigure`.

By default, the application is available at:

<http://127.0.0.1:8000/>

## Model providers

- `OPENAI_API_KEY` enables the registered OpenAI models.
- `ANTHROPIC_API_KEY` enables the registered Anthropic models.

Semantic search uses the built-in OpenAI `text-embedding-3-large` model and
requires `OPENAI_API_KEY`, even when the chat model is Claude. If OpenAI
embeddings are unavailable, search falls back to lexical matching.

## Safety note

Local Python analysis is not a security sandbox. Use the demo only with files
and requests you trust.
