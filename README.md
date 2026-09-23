# Introduction

This is a lightweight local AI agent demo for epidemiological research. It runs
on macOS and Linux with Python 3.12 and an OpenAI or Anthropic API key.

[Watch the demo video](https://drive.google.com/file/d/1A-N8pTOn6tKcZ_D6DLPc62EWvb2e_68E/view?usp=sharing)

## Native setup: macOS and Linux/HPC

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
runtime-data folder and configure OpenAI, Anthropic, or OpenRouter. Startup also
shows the Guarded Local warning described below. Accepting it once records
`REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1` in the local `.env`. To change
providers later, run `python run_fastapi.py --reconfigure`.

For a non-interactive native launch, read the warning under
[Generated Python](#generated-python-guarded-local), then set the acknowledgement
explicitly:

```bash
export REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1
python run_fastapi.py
```

By default, the application is available at:

<http://127.0.0.1:8080/>

## Optional Docker deployment

Docker is optional. Use it when you want the whole Epi-AI-Agent application
separated from the host outside explicitly mounted storage. Docker Desktop on
macOS or a Docker Engine/Compose installation on Linux can run the same image.

After reading the Guarded Local warning below, supply the acknowledgement and
at least one provider credential at container runtime:

```bash
export REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1
export OPENAI_API_KEY="replace-with-provider-key"
docker compose up --build
```

Open <http://127.0.0.1:8080/>. Compose publishes no non-loopback host port. A
named volume persists the SQLite checkpoint, conversations, uploads, and
generated artifacts at `/app/runtime` across container replacement. Provider
keys and the acknowledgement are runtime environment values and are not copied
into the image.

To expose one existing study-data directory, mount only that directory as
read-only with the supplied override:

```bash
export REPORT_AGENT_STUDY_DATA="/absolute/path/to/study_data"
docker compose -f compose.yaml -f compose.study-data.yaml up --build
```

The default configuration does not mount the source checkout, host home,
credential directories, root filesystem, or container-engine socket.

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

## Embeddings for semantic search
Semantic search uses the built-in OpenAI `text-embedding-3-large` model and
requires `OPENAI_API_KEY`, even when the chat model is Claude. If OpenAI
embeddings are unavailable, search falls back to lexical matching.

## Generated Python: Guarded Local

Generated analyses always use **Guarded Local**. A fresh worker receives one
approved table as the in-memory `dataset`, a clean allowlisted environment,
private temporary directories, ordinary operating-system resource limits, a
wall timeout, bounded output, and process-group cleanup. Static checks block
common filesystem, network, environment, process-control, dynamic-execution,
interactive-input, and package-installation operations. Results are staged for
human review.

Only `dataset` is preloaded for generated analysis code. Generated code imports
each library it uses explicitly, so optional scientific libraries are loaded
only when the analysis needs them.

Guarded Local runs with the same host permissions as the application. These
controls reduce accidental misuse and runaway execution, but they are not an
operating-system sandbox and cannot safely contain deliberately adversarial
Python. Use the native application only with requests and dependencies you
trust.

Docker adds a boundary between the whole application and the host; it does not
create a separate boundary between generated Python and the rest of the
application container. Application networking remains enabled for model
providers and trusted tools, while generated Python remains offline by static
policy.

The installed scientific package set is fixed by `requirements.txt`. Generated
code cannot install packages with pip, Conda, uv, system package managers, or
source builds. Missing imports return `DEPENDENCY_NOT_AVAILABLE`. See the
[generated Python execution policy](tools/python_execution_policy.md) for the
complete contract and stable policy rule IDs.

### Adding an approved Python package

If an analysis imports an unavailable module, the run stops with
`Package “<module>” isn’t installed.` The application remains available. Retry
with an installed library, or update the environment before sending a new
request.

Package changes are operator-managed. Do not copy the import name directly
into an installer command: imports and distributions can differ, such as
`sklearn` and `scikit-learn`. Review the package, select an exact version, and
add `distribution==version` to `requirements.txt`.

For a native installation, stop Epi-AI-Agent and run:

```bash
source .venv/bin/activate
uv pip install -r requirements.txt
python -c "import xxx"
python run_fastapi.py
```

Replace `xxx` with the verified import name. Installing while the service is
running is unsupported.

For Docker, stop the service and rebuild from the updated requirements:

```bash
docker compose down
docker compose build
docker compose up -d
docker compose exec epi-agent python -c "import xxx"
```

Do not install into a running container; that change disappears when the
container is replaced. After verification, send a new request to retry the
analysis. Failed generated code is never replayed automatically.
