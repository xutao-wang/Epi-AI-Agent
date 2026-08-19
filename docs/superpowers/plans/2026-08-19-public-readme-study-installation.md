# Public README and Fresh Study Installation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make public setup self-contained with two root-level study archives, then replace the configured installed-study state with a fresh installation of those packages.

**Architecture:** Treat the two `.tar.gz` files as versioned repository inputs and keep private `Database` paths out of public documentation. Update only README setup content, then delete only the installer-managed `study_data` root and rebuild its registry through the public CLI; preserve the separate `runtime` root.

**Tech Stack:** Git, Markdown, Python 3.12, `study_installer.py`, pytest.

## Global Constraints

- Work on `local-multi-study` and preserve unrelated changes.
- Preserve and commit the requested deletions of RePORT archives `0.2.0` and `0.3.0`.
- Track RePORT `0.3.1` and NHANES `0.2.0` at the repository root.
- Public commands must not reference the sibling `Database` directory.
- Delete only `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/study_data`.
- Never delete or modify `/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/runtime`.
- Run Python through `.venv`.

---

## File Map

- Add: `nhanes-2017-2018-0.2.0.tar.gz` — public NHANES package.
- Preserve: `report-india-synthetic-0.3.1.tar.gz` — public RePORT package.
- Delete: `report-india-synthetic-0.2.0.tar.gz` and `report-india-synthetic-0.3.0.tar.gz` — obsolete packages already deleted in the working tree.
- Modify: `README.md` — local two-study setup only.
- Recreate: `study_data/` — ignored installed packages and registry.
- Do not modify: `runtime/` — conversations, uploads, datasets, and results.

### Task 1: Make the root archive layout complete

**Files:**
- Create: `nhanes-2017-2018-0.2.0.tar.gz`
- Delete: `report-india-synthetic-0.2.0.tar.gz`
- Delete: `report-india-synthetic-0.3.0.tar.gz`

**Interfaces:**
- Consumes: `../Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.2.0.tar.gz`.
- Produces: both archives used by the path-free installer command.

- [ ] **Step 1: Prove the root layout is incomplete**

Run:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
required = (
    "report-india-synthetic-0.3.1.tar.gz",
    "nhanes-2017-2018-0.2.0.tar.gz",
)
missing = [name for name in required if not Path(name).is_file()]
assert not missing, f"missing root archives: {missing}"
PY
```

Expected: FAIL listing `nhanes-2017-2018-0.2.0.tar.gz`.

- [ ] **Step 2: Copy the exact NHANES archive**

```bash
cp ../Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.2.0.tar.gz nhanes-2017-2018-0.2.0.tar.gz
```

- [ ] **Step 3: Verify byte identity and required files**

```bash
.venv/bin/python - <<'PY'
from hashlib import sha256
from pathlib import Path
source = Path("../Database/nhanes-2017-2018/delivery/nhanes-2017-2018-0.2.0.tar.gz")
copied = Path("nhanes-2017-2018-0.2.0.tar.gz")
assert Path("report-india-synthetic-0.3.1.tar.gz").is_file()
assert copied.is_file()
assert sha256(source.read_bytes()).digest() == sha256(copied.read_bytes()).digest()
PY
```

Expected: PASS with no output.

- [ ] **Step 4: Commit the archive set**

```bash
git add nhanes-2017-2018-0.2.0.tar.gz report-india-synthetic-0.2.0.tar.gz report-india-synthetic-0.3.0.tar.gz
git commit -m "build: update bundled study packages"
```

### Task 2: Simplify the public README

**Files:**
- Modify: `README.md:1-130`

**Interfaces:**
- Consumes: both root archives from Task 1.
- Produces: one public, path-free multi-study setup flow.

- [ ] **Step 1: Prove the README contract currently fails**

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
text = Path("README.md").read_text(encoding="utf-8")
assert "## Invitation-only hosted mode" not in text
assert "## Multi-study semantic catalog-binding smoke" not in text
assert "scripts/smoke_multi_study_semantic_catalog.py" not in text
assert "scripts/smoke_multi_study_review_failure_recovery_real.py" not in text
assert "report-india-synthetic-0.3.1.tar.gz" in text
assert "nhanes-2017-2018-0.2.0.tar.gz" in text
assert "../Database/" not in text
PY
```

Expected: FAIL because hosted and internal-smoke sections remain.

- [ ] **Step 2: Apply the approved README content**

Replace the single-study setup lines with:

````markdown
The repository root includes the RePORT India and NHANES study-package
archives used by this demo. Install both packages before starting the server:

```bash
python study_installer.py --study \
  report-india-synthetic-0.3.1.tar.gz \
  nhanes-2017-2018-0.2.0.tar.gz
python run_fastapi.py
```
````

Delete `## Invitation-only hosted mode` through the paragraph ending in
`acceptance smoke.`, while retaining the following cancellation paragraph.
Delete `## Multi-study semantic catalog-binding smoke` through the line before
`## Safety note`.

- [ ] **Step 3: Verify README and installer behavior**

Run the Step 1 assertion again. Expected: PASS.

```bash
.venv/bin/python -m pytest tests/test_study_installer_cli.py tests/test_study_package_installer.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit the README**

```bash
git add README.md
git commit -m "docs: simplify public multi-study setup"
```

### Task 3: Perform the destructive fresh installation

**Files:**
- Delete and recreate: `study_data/`
- Do not modify: `runtime/`

**Interfaces:**
- Consumes: the two root archives.
- Produces: active versions `report-india-synthetic@0.3.1` and `nhanes-2017-2018@0.2.0`, with no obsolete installed versions.

- [ ] **Step 1: Guard the cleanup boundary**

```bash
.venv/bin/python - <<'PY'
import os
from pathlib import Path
from utils.env_loader import load_app_environment
root = Path.cwd().resolve()
load_app_environment(root)
study_root = Path(os.environ["REPORT_AGENT_STUDY_ROOT"]).resolve()
runtime_root = Path(os.environ["REPORT_AGENT_RUNTIME_ROOT"]).resolve()
assert study_root == root / "study_data", study_root
assert runtime_root == root / "runtime", runtime_root
assert study_root != runtime_root
assert (root / "report-india-synthetic-0.3.1.tar.gz").is_file()
assert (root / "nhanes-2017-2018-0.2.0.tar.gz").is_file()
print(f"cleanup_target={study_root}")
print(f"protected_runtime={runtime_root}")
PY
```

Expected: print the exact cleanup and protected paths from the global constraints.

- [ ] **Step 2: Delete only installed study state**

```bash
rm -rf "/Users/xutaowang/Desktop/RA work/Epi-Agent/Epi-AI-Agent/study_data"
```

Expected: `study_data` is absent and `runtime` still exists.

- [ ] **Step 3: Run the exact public installation command**

```bash
source .venv/bin/activate
python study_installer.py --study \
  report-india-synthetic-0.3.1.tar.gz \
  nhanes-2017-2018-0.2.0.tar.gz
```

Expected:

```text
Installed: report-india-synthetic@0.3.1
Installed: nhanes-2017-2018@0.2.0
```

- [ ] **Step 4: Verify the fresh package set and protected runtime**

```bash
.venv/bin/python - <<'PY'
import json
from pathlib import Path
root = Path("study_data/studies")
registry = json.loads((root / "registry.json").read_text(encoding="utf-8"))
expected = {
    "nhanes-2017-2018": "0.2.0",
    "report-india-synthetic": "0.3.1",
}
assert registry == {"active": expected, "format_version": 1}, registry
installed = {
    (study.name, version.name)
    for study in (root / "packages").iterdir()
    for version in study.iterdir()
    if version.is_dir()
}
assert installed == {
    ("nhanes-2017-2018", "0.2.0"),
    ("report-india-synthetic", "0.3.1"),
}, installed
assert Path("runtime").is_dir()
PY
```

Expected: PASS with no output.

- [ ] **Step 5: Run final verification**

```bash
.venv/bin/python -m pytest tests/test_study_installer_cli.py tests/test_study_package_installer.py tests/test_installed_study_bundle.py -q
git diff --check
git status --short
```

Expected: tests pass, diff check is clean, and no unintended changes remain.
