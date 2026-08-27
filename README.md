# Introduction

This is a lightweight local AI agent demo for epidemiological research. It runs
on macOS and Linux with Python 3.12 and an OpenAI or Anthropic API key.

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

<http://127.0.0.1:8080/>

## Model providers

- `OPENAI_API_KEY` enables the registered OpenAI models.
- `ANTHROPIC_API_KEY` enables the registered Anthropic models.

### Local vLLM models

Epi-AI-Agent can run beside vLLM on the same machine. Keep vLLM on its
default OpenAI-compatible endpoint (`http://127.0.0.1:8000/v1`) and run this
application on its default port `8080`:

```bash
cp config/custom_models.example.json config/custom_models.json
python run_fastapi.py --reconfigure
```

Use `python run_fastapi.py` as the supported launcher. The `api.app` module
exports the application factory without constructing a provider catalog at
import time, so `uvicorn api.app:app` is not a supported launch command.

Choose **Connect to a compatible endpoint**. Only this explicit selection
enables the configured endpoint. Epi-AI-Agent then calls its `/models` route
once during startup and exposes every returned model in the model picker. The
selection is saved locally, so subsequent launches refresh the selected
endpoint without reopening the provider menu. A registry entry that has not
been selected is never contacted.

The discovered application IDs use the endpoint namespace and exact served
model ID, for example `vllm:Qwen/Qwen3-72B-Instruct`. If the vLLM server uses
an API key, set `api_key_env` in `custom_models.json` to the name of an
environment variable containing that key; do not put the key itself in JSON.
The previous fixed-model entry format remains supported when a static model
mapping is needed.

Semantic search uses the built-in OpenAI `text-embedding-3-large` model and
requires `OPENAI_API_KEY`, even when the chat model is Claude. If OpenAI
embeddings are unavailable, search falls back to lexical matching.

## Safety note

Local Python analysis is not a security sandbox. Use the demo only with files
and requests you trust.
