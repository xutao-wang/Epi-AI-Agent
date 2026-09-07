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

`custom_models.json` contains only endpoint connection settings. Model-specific
fallbacks live in `config/amarel_model_profiles.json`, keyed by the exact model ID
returned by `/v1/models`. At startup, live deployment metadata takes priority
(currently including vLLM's `max_model_len`), then the matching predefined
profile is applied. Only profiles for model IDs advertised by the selected
endpoint are validated; an unused profile cannot block startup. Missing
application profile values receive conservative defaults:
unknown capabilities are disabled, local cost remains unknown, timeouts are
180/600 seconds, and the output-token ladder is 1024/2048/1024/4096. When a
live context window is smaller, those token limits are reduced automatically.
The profile `summary` is optional; the application supplies generic display
text when it is absent.

The optional nested `vllm` object is read by
`config/vllm_amarel/load_llm_with_vllm.sh` before the endpoint exists. It
contains optional command-line overrides such as `max_model_len`, tensor
parallelism, GPU-memory utilization, and tool-call parsing. The launcher passes
only values explicitly present for the selected model, leaving every omitted
value to the installed vLLM version. A missing model entry or an empty `vllm`
object therefore uses native vLLM defaults; explicitly supplied values and
parameter names are still validated. Launch a model from that directory with
only the image name and exact model ID:

```bash
bash load_llm_with_vllm.sh \
  vllm-openai_v0.19.1.sif \
  google/gemma-4-31B-it
```

After startup, `/v1/models` remains authoritative for the context length that
vLLM actually accepted and exposed to the application.

For an existing multi-node Slurm allocation, pass the allocated node count to
the companion launcher:

```bash
bash config/vllm_amarel/load_llm_with_vllm_multi_node.sh \
  vllm-openai_v0.19.1.sif \
  Qwen/Qwen3-Next-80B-A3B-Instruct-FP8 \
  2
```

Single- and multi-node launches share the same optional model overrides. The
multi-node launcher defaults tensor parallelism to one when it is omitted,
because Slurm needs a GPU count before vLLM starts, and derives pipeline
parallelism from the node-count argument. See
`config/vllm_amarel/load_llm_amarel_instruction.md` for the matching `salloc`
request and networking overrides.

Semantic search uses the built-in OpenAI `text-embedding-3-large` model and
requires `OPENAI_API_KEY`, even when the chat model is Claude. If OpenAI
embeddings are unavailable, search falls back to lexical matching.

## Safety note

Local Python analysis is not a security sandbox. Use the demo only with files
and requests you trust.
