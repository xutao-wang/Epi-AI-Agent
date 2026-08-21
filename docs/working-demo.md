# Working demo operation boundary

## Local native operation

The supported researcher workflow is:

```bash
source .venv/bin/activate
python run_fastapi.py
```

Python 3.12 and at least one usable model provider are required. On first run,
the launcher offers OpenAI, Anthropic, or a registered compatible endpoint.
Providers are configured independently; use `python run_fastapi.py
--reconfigure` to add a second provider or to replace or remove one. The
launcher securely prompts for missing built-in provider keys, verifies all
configured providers on every startup, saves only verified keys to `.env`,
prepares the selected runtime directory, and serves the committed browser
build.

The model selector is derived from providers that passed startup verification:
OpenAI enables registered GPT models, Anthropic enables registered Claude
models, and a reachable registered compatible endpoint enables its declared
models. `REPORT_AGENT_MODEL` and `REPORT_AGENT_ALLOWED_MODELS` are not setup
inputs and are removed from `.env` during startup. Compatible endpoints must
already be running; this repository does not install or launch vLLM or Ray.

If a saved conversation names a model that is not currently available, its
history still loads. Continuing it requires selecting and confirming an
available replacement model.

Requests use the fixed `local-user` identity internally. Legacy unowned local
conversation rows are claimed for that identity so prior local work remains
available. Conversations, checkpoints, attachments, datasets, and exports stay
under the configured local runtime root.

## Local paths

The defaults are project-local and can be overridden in `.env`:

- `REPORT_AGENT_RUNTIME_ROOT` stores conversations, uploads, generated data,
  and execution artifacts.
- `REPORT_AGENT_STUDY_ROOT` contains installed study packages.
- `REPORT_AGENT_STATIC_DIR` selects the compiled frontend bundle.

The checkpoint/history database is derived from `REPORT_AGENT_RUNTIME_ROOT`;
it is not configured separately by the normal setup flow.

The study installer and application launcher prompt for local directories when
needed. Persistent data formats and ownership layout remain stable across
restarts.

## Data policy

Use synthetic data or data that has been fully de-identified before upload. Do
not upload direct identifiers, protected health information, confidential
source records, or provider credentials as data. The included RePORT India
study assets and example CSVs are synthetic.

## Verification

Run the retained Python and frontend suites locally:

```bash
python -m pytest -q
npm --prefix frontend test -- --run
npm --prefix frontend run build
```

Local Python execution runs in a bounded subprocess and strips provider and
cloud credentials from the child environment. It protects against accidental
or model-generated mistakes, but it is not a security boundary for hostile
code.
