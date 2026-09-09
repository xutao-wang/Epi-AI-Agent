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
runtime-data folder and configure OpenAI, Anthropic, or OpenRouter. To change
providers later, run `python run_fastapi.py --reconfigure`.

By default, the application is available at:

<http://127.0.0.1:8080/>

## Model providers

- `OPENAI_API_KEY` enables OpenAI models.
- `ANTHROPIC_API_KEY` enables Anthropic models.
- `OPENROUTER_API_KEY` enables registered OpenRouter models. Add them in
  `config/custom_models.json` (copy `config/custom_models.example.json` first).

### Local vLLM models

Run vLLM on its OpenAI-compatible endpoint (default:
`http://127.0.0.1:8000/v1`), then start the app with
`python run_fastapi.py --reconfigure` and choose **Connect to a compatible
endpoint**. The app discovers models from `/models`.

### vLLM on Amarel (optional)

See [the Amarel vLLM instructions](config/vllm_amarel/load_llm_amarel_instruction.md)
for single- and multi-node launch guidance.

Semantic search uses the built-in OpenAI `text-embedding-3-large` model and
requires `OPENAI_API_KEY`, even when the chat model is Claude. If OpenAI
embeddings are unavailable, search falls back to lexical matching.

## Safety note

Local Python analysis is not a security sandbox. Use the demo only with files
and requests you trust.
