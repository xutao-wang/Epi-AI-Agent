# Working demo operation boundary

## Local native operation

The supported researcher workflow is:

```bash
source .venv/bin/activate
python run_fastapi.py
```

Python 3.12 and `OPENAI_API_KEY` are required. The launcher verifies the key
from `.env` (or prompts for and saves a verified key), prepares the selected
runtime directory, and serves the committed browser build.

Requests use the fixed `local-user` identity internally. Legacy unowned local
conversation rows are claimed for that identity so prior local work remains
available. Conversations, checkpoints, attachments, datasets, and exports stay
under the configured local runtime root.

## Local paths

The defaults are project-local and can be overridden in `.env`:

- `REPORT_AGENT_RUNTIME_ROOT` stores conversations, uploads, generated data,
  and execution artifacts.
- `REPORT_AGENT_CHECKPOINT_DB_PATH` selects the SQLite checkpoint/history
  database.
- `REPORT_AGENT_STUDY_ROOT` contains installed study packages.
- `REPORT_AGENT_STATIC_DIR` selects the compiled frontend bundle.

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
