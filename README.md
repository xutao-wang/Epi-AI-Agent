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
  nhanes-2017-2018-0.3.1.tar.gz
python run_fastapi.py
```

The installer prompts for a study-package folder. On first startup, choose a
runtime-data folder and configure OpenAI, Anthropic, OpenRouter, or Amazon Bedrock. Startup also
shows the Guarded Local warning described below. Accepting it once records
`REPORT_AGENT_GUARDED_PYTHON_ACKNOWLEDGED=1` in the local `.env`. To change
providers later, run `python run_fastapi.py --reconfigure`.

NHANES package **0.3.1** corrects XPT numeric-zero decoding. Earlier package
0.3.0 can contain tiny positive numbers where the CDC source stores zero,
including fasting-subsample weights; use the corrected package for new analyses.
The source files remain unchanged. Package builds and validation are documented
in the [NHANES data package](../Database/nhanes-2017-2018/README.md).

For this multi-repository workspace, install the corrected release explicitly:

```bash
python study_installer.py --study-root "$PWD/study_data" \
  --study ../Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.3.1.tar.gz \
  --expected-study-id nhanes-2017-2018 --expected-package-version 0.3.1
```

Installation activates that version in `study_data/studies/registry.json` and
preserves other installed versions. Start a fresh application runtime after
installation. Rebuilding files under `Database/` alone does not update the
installed copy, and the application selects the registry's active version rather
than the newest directory by date.

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

### Amazon Bedrock

Set `BEDROCK_API_KEY` and `BEDROCK_REGION=us-east-1` in your local `.env`, or
choose **Configure Amazon Bedrock** in `python run_fastapi.py --reconfigure`.
The key must be valid in the configured region. Standard AWS aliases
`AWS_BEARER_TOKEN_BEDROCK` and `AWS_REGION` / `AWS_DEFAULT_REGION` are accepted;
the app's Bedrock variables take precedence. Keys stay on the server. No AWS
access-key pair is required when using a Bedrock API key.

Both native startup and `api.app.build_application` authenticate against the
Bedrock catalog, paginate inference profiles, and add supported candidates to
the existing **Amazon Bedrock** model group. Discovery does not prove model
subscription, entitlement, or invocation permissions. If a selected model is
denied, check its region, IAM permissions, Marketplace access, and provider
setup in the Bedrock console. Anthropic models may require the account's
use-case form before first invocation. Model selection determines billing;
Bedrock failures never fall back to a direct provider.

Optional `BEDROCK_EXCLUDED_PROVIDERS` entries remove named providers from the
Bedrock group. The shipped template leaves both catalog policies empty. The optional `BEDROCK_ALLOWED_MODELS` setting accepts comma-separated
exact app IDs and applies after discovery and final custom-profile merges.
An allowed name must resolve to the same canonical AWS model; custom names
cannot conceal another model or global route. Both policies use the supplied
environment and affect only Bedrock. Native providers and OpenRouter retain
their profiles. Without an allowlist, generic discovery still supports Nova
Micro/Lite/Pro v1 and the other supported Claude/GPT families, subject to vendor
exclusions. Empty policy settings impose no restriction.

When the configured OpenRouter registry contains `google/gemma-4-31b-it`, startup
also discovers the authenticated AWS Mantle `/v1/models` catalog. An exact
`google.gemma-4-31b` match adds `bedrock:google.gemma-4-31b` to the existing group.
Gemma uses Chat Completions at the fixed AWS endpoint
`https://bedrock-mantle.<validated-region>.api.aws/openai/v1`, with the saved
Bedrock key, native `reasoning_effort`, serial tool calling and streaming token
usage. It uses AWS billing and never falls back to OpenRouter. Mantle failure
omits the unverified counterpart while preserving usable Converse choices.
Multiple configured Gemma sources or a conflicting local counterpart ID omit
the derived counterpart with a safe warning; configure one unambiguous source.

The derived profile preserves the source's compatible output budgets, timeouts,
reasoning, sampling, vision, routing and output-approval settings; AWS output
pricing remains unknown and separate from OpenRouter pricing. Mantle rejects
`parallel_tool_calls` on requests without tools, so the serial flag is added
only when tools are bound. Gemma's tool transport also replaces the rejected
`uniqueItems: true` array keyword with explicit uniqueness guidance in the
schema description. Original registry schemas and Pydantic validation remain
unchanged, and every registered tool stays available. No source registry entry
is rewritten.

Local identities start with `bedrock:`. The app prefers an inference profile
actually returned for the configured geography, using its exact AWS target;
it never manufactures profile IDs. Japan, Australia and India prefer observed
`jp.`, `au.` and `in.` profiles, with observed `apac.` profiles as a broader
fallback where available. Regional targets are used only for models with
supported direct routes and no observed geographic target; modern Claude
models require inference profiles despite the catalog's `ON_DEMAND` flag.
Global and application targets appear as separate labeled choices requiring
explicit selection. They cannot become automatic chat or title defaults merely
because discovery listed them first. If only these choices are available,
startup requires an explicit operator registration of the intended target
below, or another regional/geographic model. An explicit operator registration
allows that target to be chosen automatically. Routing scope is shown in each
model's label.

Operators can register a new compatible model or explicit inference target in
`config/custom_models.json`, or the file specified by
`REPORT_AGENT_CUSTOM_MODELS_PATH`. For example:

```json
[
  {
    "id": "bedrock:my-compatible-model",
    "provider": "bedrock",
    "model": "us.vendor.exact-target-returned-by-aws",
    "bedrock_region": "us-east-1",
    "supports_tool_calling": true,
    "supports_streaming": true,
    "supports_sampling_controls": false,
    "initial_output_tokens": 1024,
    "automatic_output_token_ceiling": 2048,
    "user_output_token_increment": 1024,
    "absolute_output_token_ceiling": 4096
  }
]
```

Use a real AWS target, assert only capabilities verified for that model, and
set its output budgets, vision, sampling and timeouts explicitly as needed.
For an application-profile ARN, also set `bedrock_base_model_id` to its exact
foundation-model ID. Opaque application-profile IDs inherit their exact base
identity from authenticated discovery when observed. If an opaque target cannot
be resolved, set a valid `bedrock_base_model_id`; with active vendor exclusions,
the app omits unresolved targets and provides safe configuration guidance.
Application profiles returned by discovery appear as
separate choices labeled **Application**. Do not set `base_url`; credentials
are sent only to native AWS endpoints. Bedrock uses one shared credential for
catalog discovery and all selected models. Omit `api_key_env` or set it to
`BEDROCK_API_KEY`; other per-model credential variables are rejected.
`AWS_BEARER_TOKEN_BEDROCK` remains supported as the shared settings fallback,
not as a separate account credential on a model entry.
The region defaults to the Bedrock settings when omitted. Overrides are made
available after authenticated catalog discovery in that region, and preserve
their saved local identity. Converse OpenAI GPT and Claude Sonnet 5 reasoning overrides may set
`reasoning_effort`; other reasoning overrides are rejected rather than sent as
unsupported parameters. Embeddings and semantic judges remain separately configured.

References: [Gemma 4 31B Mantle capabilities](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-google-gemma-4-31b.html),
[AWS model discovery](https://docs.aws.amazon.com/bedrock/latest/userguide/models-get-info.html),
[AWS API keys](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html),
[Nova Micro capabilities](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-micro.html),
[GPT-5.6 Luna APIs](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-56-luna.html),
[Claude request parameters](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages-request-response.html),
and the [LangChain Converse adapter](https://reference.langchain.com/python/langchain-aws/chat_models/bedrock_converse/ChatBedrockConverse).

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

Generated analyses always use **Guarded Local**. A fresh worker may run
standalone in-memory Python or receive one selected table as the in-memory
`dataset`. It also receives a clean allowlisted environment, private temporary
directories, ordinary operating-system resource limits, a wall timeout,
bounded output, and process-group cleanup. Static checks block common
filesystem, network, environment, process-control, dynamic-execution,
interactive-input, and package-installation operations. Results are staged for
human review.

When selected, `dataset` is the only preloaded analysis value. Standalone code
starts without it and can perform calculations, dependency probes,
demonstrations, or explicitly requested in-memory simulations. Generated code
imports each library it uses explicitly, so optional scientific libraries are
loaded only when needed. Standalone execution must never substitute synthetic
data for a request that depends on real participant or study data.

Guarded Local runs with the same host permissions as the application. These
controls reduce accidental misuse and runaway execution, but they are not an
operating-system sandbox and cannot safely contain deliberately adversarial
Python. Use the native application only with requests and dependencies you
trust.

Docker adds a boundary between the whole application and the host; it does not
create a separate boundary between generated Python and the rest of the
application container. Application networking remains enabled for model
providers and trusted tools. Generated Python must not use it; static checks
reject common direct network clients but do not provide network isolation.

The installed scientific package set is fixed by `requirements.txt`. Generated
code cannot install packages with pip, Conda, uv, system package managers, or
source builds. Missing imports return `DEPENDENCY_NOT_AVAILABLE`. See the
[generated Python execution policy](tools/python_execution_policy.md) for the
complete contract and stable policy rule IDs.

### Adding an approved Python package

If an analysis imports an unavailable module, the run stops with
`Package “<module>” isn’t installed.` The agent may make one changed-code retry
with installed packages, but only when the alternative is scientifically
equivalent. It does not force a workaround, change the requested method, or
invent a result. If no valid alternative works, Python is disabled only for
the rest of that user turn and the agent returns a nonfatal package warning;
the application, other tools, and later requests remain available.

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

Production development uses `master`. The `master-benchmark` branch preserves
evaluation functionality. Port shared fixes selectively instead of merging the
whole benchmark branch into production.
Use a separate runtime-data directory for `master-benchmark` via
`REPORT_AGENT_RUNTIME_ROOT` to keep evaluation artifacts out of normal conversation storage.
