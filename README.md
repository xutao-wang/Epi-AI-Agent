# Epi-AI-Agent

A local AI assistant for epidemiological research, with a browser interface
for exploring study data, reviewing evidence, and running analyses.
Supports macOS and Linux with Python 3.12.

[Watch the demo video](https://drive.google.com/file/d/1A-N8pTOn6tKcZ_D6DLPc62EWvb2e_68E/view?usp=sharing)

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```bash
git clone https://github.com/xutao-wang/Epi-AI-Agent.git
cd Epi-AI-Agent

uv python install 3.12
uv venv --python 3.12
source .venv/bin/activate
uv pip install -r requirements.txt

cp .env.example .env
```

Add a provider API key to `.env`, or configure one when you first start the app.

## Start the app

```bash
python run_fastapi.py
```

Open <http://127.0.0.1:8080/>.

First startup asks you to choose a data folder, configure a model provider,
and acknowledge the generated Python execution warning.
To change providers later, run `python run_fastapi.py --reconfigure`.

## Study data

Install a study package to ask questions about its data. For example, use the
included synthetic RePORT India package:

```bash
python study_installer.py --study report-india-synthetic-0.4.0.tar.gz
```

The installer asks where to store study packages. Install other studies with
the same command and their archive paths, then restart the app.
For NHANES analyses, use the corrected package version 0.3.1 or later.

## Model providers

Configure the providers you want in `.env`:

- **OpenAI:** `OPENAI_API_KEY`.
- **Anthropic:** `ANTHROPIC_API_KEY`.
- **OpenRouter:** `OPENROUTER_API_KEY` and models registered in
  `config/custom_models.json`; start from
  [the example configuration](config/custom_models.example.json).
- **Amazon Bedrock (optional):** `BEDROCK_API_KEY` and `BEDROCK_REGION`.


For a local vLLM server, run `python run_fastapi.py --reconfigure` and choose
**Connect to a compatible endpoint**. For HPC setup, see
[the Amarel instructions](config/vllm_amarel/load_llm_amarel_instruction.md).

Default semantic search uses OpenAI embeddings. When they are unavailable,
search falls back to lexical matching.

You need at least on API key or local vLLM server configure to start using the application.
## Python analyses

Generated Python runs with your account's permissions and is not an
operating-system sandbox. Read the startup warning before enabling it.
Analysis results are staged for your review.

To add a Python package, update `requirements.txt`, stop the app, and run
`uv pip install -r requirements.txt` before restarting. See
[the execution policy](tools/python_execution_policy.md) for details.
