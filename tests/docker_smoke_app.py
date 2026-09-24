from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

from fastapi import FastAPI, HTTPException
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from epi_agent.runtimes.python import (
    LocalPythonRuntime,
    PythonExecutionRequest,
    PythonRuntimeFailure,
)


RUNTIME_ROOT = Path(
    os.getenv("REPORT_AGENT_RUNTIME_ROOT", "/app/runtime")
).resolve()
DATABASE_PATH = RUNTIME_ROOT / "smoke.sqlite"
app = FastAPI()


class SmokeAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    code: str = Field(min_length=1, max_length=100_000)


def _store_review_in_sqlite(
    path: Path,
    review: dict[str, object],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        review,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS smoke_review "
            "(id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO smoke_review (id, payload) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload",
            (encoded,),
        )


def _load_review_from_sqlite(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise KeyError("No smoke analysis has been stored.")
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT payload FROM smoke_review WHERE id = 1"
        ).fetchone()
    if row is None:
        raise KeyError("No smoke analysis has been stored.")
    payload = json.loads(str(row[0]))
    if not isinstance(payload, dict):
        raise ValueError("Stored smoke review must be an object.")
    return payload


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analysis")
def analysis(request: SmokeAnalysisRequest) -> dict[str, object]:
    try:
        result = LocalPythonRuntime(
            runtime_root=RUNTIME_ROOT / "execution",
            timeout_seconds=60,
        ).execute(
            PythonExecutionRequest(
                code=request.code,
                selected_dataset_id="smoke",
            ),
            {"smoke": pd.DataFrame({"cases": [2, 3, 5]})},
        )
    except PythonRuntimeFailure as error:
        raise HTTPException(
            status_code=422,
            detail={"code": error.code, "message": str(error)},
        ) from error
    review: dict[str, object] = {
        "output_text": result.output_text,
        "runtime": result.runtime.model_dump(mode="json"),
        "review_status": "pending_review",
    }
    _store_review_in_sqlite(DATABASE_PATH, review)
    return review


@app.get("/analysis/latest")
def latest_analysis() -> dict[str, object]:
    try:
        return _load_review_from_sqlite(DATABASE_PATH)
    except (KeyError, ValueError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
