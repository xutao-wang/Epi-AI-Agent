from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

import pandas as pd
import pyarrow.parquet as parquet
from utils.user_storage import ThreadStorageScope


_PERSISTENCE_JOURNAL_DIRECTORY = ".persistence_attempts"
_PERSISTENCE_STATES = {"begun", "staged", "promoted", "committed"}
_PERSISTENCE_PATH_KEYS = {"path", "schema_path", "metadata_path"}
_PERSISTENCE_LINEAGE_KEYS = {
    "study_id",
    "approved_selected_columns",
    "approved_selected_tables",
    "expected_output_aliases",
    "plan_content_sha256",
    "thread_id",
    "plan_id",
    "plan_version",
    "sql_content_sha256",
    "sql_id",
    "sql_version",
    "predecessor_dataset_id",
    "predecessor_dataset_version",
}
_PERSISTENCE_REPLACEMENT_KEYS = {
    "predecessor_id",
    "predecessor_kind",
    "predecessor_version",
    "feedback_id",
    "feedback_kind",
    "feedback_version",
}
_PERSISTENCE_DATASET_KEYS = {
    "id",
    "kind",
    "path",
    "schema_path",
    "metadata_path",
    "row_count",
    "column_count",
    "columns",
    "created_at",
    "provenance",
    "version",
    "status",
}
_DATASET_PROVENANCE_KEYS = {
    "description",
    "feedback_history",
    "goal_text",
    "grain_columns",
    "join_expansion",
    "name",
    "output_column_sources",
    "post_sql_warnings",
    "plan_id",
    "plan_version",
    "population_assumption",
    "population_scope",
    "predecessor_dataset_id",
    "predecessor_dataset_version",
    "relationship_metrics",
    "selected_columns",
    "selected_tables",
    "selection_artifact_id",
    "selection_id",
    "source",
    "source_attachment_ids",
    "source_message_event_id",
    "source_question",
    "source_tables",
    "study_id",
    "sql",
    "sql_candidate_artifact_id",
    "sql_id",
    "sql_version",
    "thread_id",
}
_DATASET_SCHEMA_COLUMN_KEYS = {
    "condition",
    "dataType",
    "depends_on",
    "description",
    "section_context",
    "values",
}


def _storage_root(value: str | Path | ThreadStorageScope) -> Path:
    if isinstance(value, ThreadStorageScope):
        return value.datasets
    return Path(value).expanduser().resolve()


def _safe_path_component(value: str, *, label: str) -> str:
    component = str(value or "").strip()
    if (
        not component
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
        or "\x00" in component
    ):
        raise ValueError(f"{label} must be one safe path component")
    return component


def _validate_attachment_provenance(
    provenance: dict[str, Any] | None,
) -> None:
    item = dict(provenance or {})
    source_attachment_ids = item.get("source_attachment_ids")
    if source_attachment_ids is not None:
        if (
            not isinstance(source_attachment_ids, list)
            or len(source_attachment_ids) > 11
            or not all(
                isinstance(attachment_id, str)
                and 0 < len(attachment_id) <= 512
                and not any(
                    character in attachment_id
                    for character in ("\x00", "\r", "\n")
                )
                for attachment_id in source_attachment_ids
            )
            or len(source_attachment_ids) != len(set(source_attachment_ids))
        ):
            raise ValueError(
                "provenance.source_attachment_ids must contain unique "
                "bounded string identifiers"
            )
    source_message_event_id = item.get("source_message_event_id")
    if source_message_event_id is not None and (
        not isinstance(source_message_event_id, str)
        or not source_message_event_id
        or len(source_message_event_id) > 512
        or any(
            character in source_message_event_id
            for character in ("\x00", "\r", "\n")
        )
    ):
        raise ValueError(
            "provenance.source_message_event_id must be a bounded string identifier"
        )


def generated_dataset_artifact_paths(
    *,
    dataset_root: str | Path | None = None,
    runtime_root: str | Path | ThreadStorageScope | None = None,
    thread_id: str | None = None,
    dataset_id: str,
) -> dict[str, Path]:
    if dataset_root is not None:
        thread_root = Path(dataset_root).expanduser().resolve()
    else:
        if runtime_root is None:
            raise ValueError("runtime_root is required when dataset_root is omitted")
        if isinstance(runtime_root, ThreadStorageScope):
            thread_root = runtime_root.datasets
        else:
            configured_root = _storage_root(runtime_root)
            safe_thread_id = _safe_path_component(thread_id or "", label="thread_id")
            thread_root = (configured_root / safe_thread_id).resolve()
            if thread_root.parent != configured_root:
                raise ValueError("Dataset thread root must be under the configured dataset root")
    safe_dataset_id = _safe_path_component(dataset_id, label="dataset_id")
    return {
        "root": thread_root,
        "path": thread_root / f"{safe_dataset_id}.parquet",
        "schema_path": thread_root / f"{safe_dataset_id}.schema.json",
        "metadata_path": thread_root / f"{safe_dataset_id}.metadata.json",
    }


def generated_dataset_staging_paths(
    *,
    dataset_root: str | Path | None = None,
    runtime_root: str | Path | ThreadStorageScope | None = None,
    thread_id: str | None = None,
    dataset_id: str,
) -> dict[str, Path]:
    final_paths = generated_dataset_artifact_paths(
        dataset_root=dataset_root,
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )
    staging_root = final_paths["root"] / ".staging" / dataset_id
    return {
        "root": staging_root,
        "path": staging_root / final_paths["path"].name,
        "schema_path": staging_root / final_paths["schema_path"].name,
        "metadata_path": staging_root / final_paths["metadata_path"].name,
    }


def generated_dataset_persistence_journal_path(
    *,
    dataset_root: str | Path | None = None,
    runtime_root: str | Path | ThreadStorageScope | None = None,
    dataset_id: str,
) -> Path:
    if dataset_root is None and runtime_root is None:
        raise ValueError("runtime_root is required for dataset persistence")
    configured_root = _storage_root(dataset_root or runtime_root)
    safe_dataset_id = _safe_path_component(dataset_id, label="dataset_id")
    journal_root = (configured_root / _PERSISTENCE_JOURNAL_DIRECTORY).resolve()
    if journal_root.parent != configured_root:
        raise ValueError("Persistence journal must be under the configured dataset root")
    journal_path = journal_root / f"{safe_dataset_id}.json"
    if journal_path.resolve().parent != journal_root:
        raise ValueError("Persistence journal must be under the configured dataset root")
    return journal_path


def _normalized_path_map(paths: dict[str, Any]) -> dict[str, Path]:
    return {
        key: Path(str(paths.get(key) or ""))
        for key in ("path", "schema_path", "metadata_path")
    }


def _validate_dataset_persistence_journal(
    attempt: dict[str, Any],
    *,
    dataset_root: str | Path | None = None,
    runtime_root: str | Path | ThreadStorageScope | None = None,
    thread_id: str,
    dataset_id: str,
) -> dict[str, Any]:
    record = dict(attempt)
    state = str(record.get("state") or "")
    lineage = dict(record.get("lineage") or {})
    replacement = record.get("replacement")
    expected_final = generated_dataset_artifact_paths(
        dataset_root=dataset_root,
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )
    expected_staging = generated_dataset_staging_paths(
        dataset_root=dataset_root,
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )
    base_keys = {
        "dataset_id",
        "state",
        "lineage",
        "expected_final_paths",
        "expected_staging_paths",
    }
    if replacement is not None:
        base_keys.add("replacement")
    if state != "begun":
        base_keys.update({"manifest", "dataset"})
    if (
        set(record) != base_keys
        or record.get("dataset_id") != dataset_id
        or state not in _PERSISTENCE_STATES
        or set(lineage) != _PERSISTENCE_LINEAGE_KEYS
        or not str(lineage.get("study_id") or "").strip()
        or lineage.get("thread_id") != thread_id
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(lineage.get("plan_content_sha256") or ""),
        )
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(lineage.get("sql_content_sha256") or ""),
        )
        or not isinstance(lineage.get("expected_output_aliases"), list)
        or not all(
            isinstance(value, str) and value
            for value in lineage.get("expected_output_aliases") or []
        )
        or not isinstance(lineage.get("approved_selected_tables"), list)
        or not all(
            isinstance(value, str) and value
            for value in lineage.get("approved_selected_tables") or []
        )
        or not isinstance(lineage.get("approved_selected_columns"), list)
        or not all(
            isinstance(value, dict)
            for value in lineage.get("approved_selected_columns") or []
        )
        or _normalized_path_map(
            dict(record.get("expected_final_paths") or {})
        )
        != _normalized_path_map(expected_final)
        or _normalized_path_map(
            dict(record.get("expected_staging_paths") or {})
        )
        != _normalized_path_map(expected_staging)
    ):
        raise ValueError(
            "Persistence journal lineage or paths do not match the configured dataset root"
        )
    if replacement is not None and (
        not isinstance(replacement, dict)
        or set(replacement) != _PERSISTENCE_REPLACEMENT_KEYS
    ):
        raise ValueError("Persistence journal replacement control is invalid")
    if state != "begun":
        manifest = dict(record.get("manifest") or {})
        dataset = dict(record.get("dataset") or {})
        if (
            set(manifest) != _PERSISTENCE_PATH_KEYS
            or set(dataset) != _PERSISTENCE_DATASET_KEYS
            or dataset.get("id") != dataset_id
            or {
                key: dataset.get(key)
                for key in _PERSISTENCE_PATH_KEYS
            }
            != dict(record["expected_final_paths"])
        ):
            raise ValueError(
                "Persistence journal manifest or dataset metadata is invalid"
            )
        for item in manifest.values():
            file_record = dict(item or {})
            if (
                set(file_record) != {"sha256", "size"}
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(file_record.get("sha256") or ""),
                )
                or not isinstance(file_record.get("size"), int)
                or file_record["size"] < 0
            ):
                raise ValueError("Persistence journal manifest is invalid")
    json.dumps(record, allow_nan=False)
    return record


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir_durable(path: Path) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    path.mkdir(parents=True, exist_ok=True)
    for created in reversed(missing):
        _fsync_directory(created.parent)


def write_dataset_persistence_journal(
    *,
    runtime_root: str | Path | ThreadStorageScope | None,
    thread_id: str,
    dataset_id: str,
    attempt: dict[str, Any],
) -> Path:
    """Atomically retain storage state until graph-checkpoint durability is known."""

    record = _validate_dataset_persistence_journal(
        attempt,
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )
    journal_path = generated_dataset_persistence_journal_path(
        runtime_root=runtime_root,
        dataset_id=dataset_id,
    )
    _mkdir_durable(journal_path.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{dataset_id}.",
        suffix=".tmp",
        dir=journal_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(journal_path)
        _fsync_directory(journal_path.parent)
    except Exception:
        if temporary_path.parent == journal_path.parent:
            temporary_path.unlink(missing_ok=True)
        raise
    return journal_path


def load_dataset_persistence_journal(
    *,
    dataset_root: str | Path | None = None,
    runtime_root: str | Path | ThreadStorageScope | None = None,
    thread_id: str,
    dataset_id: str,
) -> dict[str, Any] | None:
    journal_path = generated_dataset_persistence_journal_path(
        dataset_root=dataset_root,
        runtime_root=runtime_root,
        dataset_id=dataset_id,
    )
    if not journal_path.exists():
        return None
    if (
        not journal_path.is_file()
        or journal_path.resolve().parent != journal_path.parent.resolve()
    ):
        raise ValueError(
            "Persistence journal must be a file under the configured dataset root"
        )
    try:
        content = json.loads(journal_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Persistence journal is malformed") from error
    if not isinstance(content, dict):
        raise ValueError("Persistence journal is malformed")
    return _validate_dataset_persistence_journal(
        content,
        dataset_root=dataset_root,
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )


def cleanup_dataset_staging(
    *,
    runtime_root: str | Path | ThreadStorageScope | None,
    thread_id: str,
    dataset_id: str,
    expected_staging_paths: dict[str, Any],
) -> None:
    expected = generated_dataset_staging_paths(
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )
    if _normalized_path_map(expected_staging_paths) != _normalized_path_map(expected):
        raise ValueError("Staging cleanup target is not the expected generated path")
    staging_root = Path(str(expected_staging_paths.get("root") or ""))
    if staging_root != expected["root"]:
        raise ValueError("Staging cleanup root is not the expected generated path")
    if staging_root.exists():
        shutil.rmtree(staging_root)
        try:
            staging_root.parent.rmdir()
        except OSError:
            pass


@dataclass(frozen=True)
class StagedDatasetArtifact:
    artifact: dict[str, Any]
    manifest: dict[str, dict[str, Any]]
    expected_final_paths: dict[str, str]
    expected_staging_paths: dict[str, str]


def _file_manifest(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "sha256": digest.hexdigest(),
        "size": path.stat().st_size,
    }


def _verify_file_manifest(path: Path, expected: dict[str, Any]) -> None:
    if not path.is_file() or _file_manifest(path) != dict(expected or {}):
        raise ValueError(f"Dataset durable manifest mismatch: {path.name}")


def verify_dataset_artifact_manifest(
    *,
    paths: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    normalized = _normalized_path_map(paths)
    for key, path in normalized.items():
        _verify_file_manifest(path, dict(manifest.get(key) or {}))


def _strict_json_object(path: Path, *, label: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs):
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    try:
        content = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError(f"{label} contains a non-JSON number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is malformed") from error
    if not isinstance(content, dict):
        raise ValueError(f"{label} must be a JSON object")
    return content


def _parquet_type_matches(declared: str, actual: str) -> bool:
    normalized_declared = declared.strip().casefold().replace(" ", "")
    normalized_actual = actual.strip().casefold().replace(" ", "")
    aliases = {
        "bool": "bool",
        "boolean": "bool",
        "datetime64[ns]": "timestamp[ns]",
        "datetime64[us]": "timestamp[us]",
        "double": "double",
        "float32": "float",
        "float64": "double",
        "object": "string",
        "str": "string",
        "string": "string",
    }
    declared_type = aliases.get(normalized_declared, normalized_declared)
    actual_type = aliases.get(normalized_actual, normalized_actual)
    if declared_type == "string" and actual_type in {"large_string", "null"}:
        return True
    return declared_type == actual_type


def load_verified_dataset_artifact(
    *,
    runtime_root: str | Path | ThreadStorageScope | None,
    thread_id: str,
    dataset_id: str,
    paths: dict[str, Any],
    manifest: dict[str, Any],
    expected_kind: str,
    expected_version: int,
    expected_status: str,
) -> dict[str, Any]:
    expected = generated_dataset_artifact_paths(
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )
    if _normalized_path_map(paths) != _normalized_path_map(expected):
        raise ValueError(
            "Dataset durable paths do not match the configured dataset root"
        )
    verify_dataset_artifact_manifest(paths=paths, manifest=manifest)
    normalized = _normalized_path_map(paths)
    artifact = _strict_json_object(
        normalized["metadata_path"],
        label="Dataset metadata",
    )
    if set(artifact) != _PERSISTENCE_DATASET_KEYS:
        raise ValueError("Dataset metadata has unsupported or missing fields")
    provenance = artifact.get("provenance")
    columns = artifact.get("columns")
    if (
        artifact.get("id") != dataset_id
        or artifact.get("kind") != expected_kind
        or {
            key: artifact.get(key)
            for key in _PERSISTENCE_PATH_KEYS
        }
        != {key: str(expected[key]) for key in _PERSISTENCE_PATH_KEYS}
        or not isinstance(artifact.get("row_count"), int)
        or isinstance(artifact.get("row_count"), bool)
        or artifact["row_count"] < 0
        or not isinstance(artifact.get("column_count"), int)
        or isinstance(artifact.get("column_count"), bool)
        or artifact["column_count"] < 0
        or not isinstance(columns, list)
        or not all(isinstance(column, str) and column for column in columns)
        or len(columns) != len(set(columns))
        or artifact["column_count"] != len(columns)
        or not isinstance(artifact.get("created_at"), str)
        or not isinstance(provenance, dict)
        or not set(provenance).issubset(_DATASET_PROVENANCE_KEYS)
        or not isinstance(artifact.get("version"), int)
        or isinstance(artifact.get("version"), bool)
        or artifact["version"] != expected_version
        or artifact.get("status") != expected_status
    ):
        raise ValueError("Dataset metadata is invalid")
    try:
        created_at = datetime.fromisoformat(artifact["created_at"])
    except ValueError as error:
        raise ValueError("Dataset metadata created_at is invalid") from error
    if created_at.tzinfo is None:
        raise ValueError("Dataset metadata created_at must include a timezone")

    schema = _strict_json_object(
        normalized["schema_path"],
        label="Dataset schema",
    )
    if set(schema) != set(columns):
        raise ValueError("Dataset schema columns do not match dataset metadata")
    for column, column_schema in schema.items():
        if (
            not isinstance(column_schema, dict)
            or not set(column_schema).issubset(_DATASET_SCHEMA_COLUMN_KEYS)
            or not isinstance(column_schema.get("dataType"), str)
            or not column_schema["dataType"].strip()
        ):
            raise ValueError(f"Dataset schema metadata is invalid for {column}")

    try:
        parquet_file = parquet.ParquetFile(normalized["path"])
        parquet_metadata = parquet_file.metadata
        parquet_schema = parquet_file.schema_arrow
    except Exception as error:
        raise ValueError("Dataset Parquet metadata is invalid") from error
    parquet_columns = list(parquet_schema.names)
    if (
        parquet_metadata.num_rows != artifact["row_count"]
        or parquet_metadata.num_columns != artifact["column_count"]
        or parquet_columns != columns
    ):
        raise ValueError(
            "Dataset metadata row or column shape does not match Parquet"
        )
    for field in parquet_schema:
        if not _parquet_type_matches(
            str(schema[field.name]["dataType"]),
            str(field.type),
        ):
            raise ValueError(
                f"Dataset schema type does not match Parquet for {field.name}"
            )
    return artifact


def stage_dataset_artifact(
    *,
    runtime_root: str | Path | ThreadStorageScope | None,
    thread_id: str,
    dataset_id: str,
    kind: str,
    dataframe: pd.DataFrame,
    schema: dict[str, Any] | None,
    provenance: dict[str, Any] | None,
    artifact_version: int | None = None,
    artifact_status: str | None = None,
) -> StagedDatasetArtifact:
    _validate_attachment_provenance(provenance)
    final = generated_dataset_artifact_paths(
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )
    staging = generated_dataset_staging_paths(
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )
    final["root"].mkdir(parents=True, exist_ok=True)
    if any(final[key].exists() for key in ("path", "schema_path", "metadata_path")):
        raise FileExistsError(
            f"Dataset final path already exists before staging: {dataset_id}"
        )
    cleanup_dataset_staging(
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
        expected_staging_paths={
            key: str(value)
            for key, value in staging.items()
        },
    )
    staging["root"].mkdir(parents=True, exist_ok=False)

    artifact = {
        "id": dataset_id,
        "kind": kind,
        "path": str(final["path"]),
        "schema_path": str(final["schema_path"]),
        "metadata_path": str(final["metadata_path"]),
        "row_count": int(len(dataframe)),
        "column_count": int(len(dataframe.columns)),
        "columns": list(dataframe.columns),
        "created_at": datetime.now(UTC).isoformat(),
        "provenance": dict(provenance or {}),
    }
    if artifact_version is not None:
        artifact["version"] = int(artifact_version)
    if artifact_status is not None:
        artifact["status"] = str(artifact_status)
    try:
        dataframe.to_parquet(staging["path"], index=False)
        staging["schema_path"].write_text(
            json.dumps(schema or {}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        staging["metadata_path"].write_text(
            json.dumps(artifact, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest = {
            key: _file_manifest(staging[key])
            for key in ("path", "schema_path", "metadata_path")
        }
    except Exception:
        cleanup_dataset_staging(
            runtime_root=runtime_root,
            thread_id=thread_id,
            dataset_id=dataset_id,
            expected_staging_paths={
                key: str(value)
                for key, value in staging.items()
            },
        )
        raise
    return StagedDatasetArtifact(
        artifact=artifact,
        manifest=manifest,
        expected_final_paths={
            key: str(final[key])
            for key in ("path", "schema_path", "metadata_path")
        },
        expected_staging_paths={
            key: str(staging[key])
            for key in ("path", "schema_path", "metadata_path")
        },
    )


def promote_staged_dataset_artifact(
    *,
    runtime_root: str | Path | ThreadStorageScope | None,
    thread_id: str,
    dataset_id: str,
    expected_final_paths: dict[str, Any],
    expected_staging_paths: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    expected_final = generated_dataset_artifact_paths(
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )
    expected_staging = generated_dataset_staging_paths(
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
    )
    if (
        _normalized_path_map(expected_final_paths)
        != _normalized_path_map(expected_final)
        or _normalized_path_map(expected_staging_paths)
        != _normalized_path_map(expected_staging)
    ):
        raise ValueError("Dataset promotion paths do not match the configured root")

    for key in ("path", "schema_path", "metadata_path"):
        final_path = expected_final[key]
        staging_path = expected_staging[key]
        expected_hash = dict(manifest.get(key) or {})
        if final_path.exists():
            _verify_file_manifest(final_path, expected_hash)
            if staging_path.exists():
                _verify_file_manifest(staging_path, expected_hash)
                staging_path.unlink()
            continue
        _verify_file_manifest(staging_path, expected_hash)
        staging_path.replace(final_path)

    verify_dataset_artifact_manifest(
        paths=expected_final_paths,
        manifest=manifest,
    )
    cleanup_dataset_staging(
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
        expected_staging_paths={
            key: str(value)
            for key, value in expected_staging.items()
        },
    )


def dataset_artifact_description(artifact: dict[str, Any] | None) -> str:
    item = dict(artifact or {})
    provenance = dict(item.get("provenance") or {})
    for value in (
        provenance.get("name"),
        item.get("name"),
        provenance.get("description"),
        provenance.get("goal_text"),
        provenance.get("source_question"),
        item.get("summary"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def persist_dataset_artifact(
    *,
    runtime_root: str | Path | ThreadStorageScope | None,
    thread_id: str,
    dataset_id: str,
    kind: str,
    dataframe: pd.DataFrame,
    schema: dict[str, Any] | None,
    provenance: dict[str, Any] | None,
    artifact_version: int | None = None,
    artifact_status: str | None = None,
) -> dict[str, Any]:
    staged = stage_dataset_artifact(
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
        kind=kind,
        dataframe=dataframe,
        schema=schema,
        provenance=provenance,
        artifact_version=artifact_version,
        artifact_status=artifact_status,
    )
    promote_staged_dataset_artifact(
        runtime_root=runtime_root,
        thread_id=thread_id,
        dataset_id=dataset_id,
        expected_final_paths=staged.expected_final_paths,
        expected_staging_paths=staged.expected_staging_paths,
        manifest=staged.manifest,
    )
    return staged.artifact


def register_dataset_artifact(
    state: dict[str, Any],
    artifact: dict[str, Any],
    *,
    make_active: bool,
) -> dict[str, Any]:
    artifacts = dict(state.get("artifacts") or {})
    datasets = dict(artifacts.get("datasets") or {})
    datasets[artifact["id"]] = artifact
    artifacts["datasets"] = datasets
    if make_active:
        artifacts["active_dataset_id"] = artifact["id"]
    return {
        **state,
        "artifacts": artifacts,
    }


def portable_dataset_artifact(
    artifact: dict[str, Any],
    *,
    runtime_root: str | Path | ThreadStorageScope,
) -> dict[str, Any]:
    configured_root = _storage_root(runtime_root)
    portable = dict(artifact)
    for path_key, storage_key in (
        ("path", "storage_key"),
        ("schema_path", "schema_storage_key"),
        ("metadata_path", "metadata_storage_key"),
    ):
        source_path = Path(str(portable.pop(path_key))).expanduser().resolve()
        try:
            relative_path = source_path.relative_to(configured_root)
        except ValueError as exc:
            raise ValueError(
                f"{path_key} must be under the configured dataset runtime root"
            ) from exc
        portable[storage_key] = relative_path.as_posix()
    return portable


def _dataset_storage_path(
    artifact: dict[str, Any],
    *,
    path_key: str,
    storage_key: str,
    runtime_root: str | Path | ThreadStorageScope | None,
) -> Path | None:
    legacy_path = artifact.get(path_key)
    if isinstance(legacy_path, str) and legacy_path:
        return Path(legacy_path)
    key = artifact.get(storage_key)
    if not isinstance(key, str) or not key:
        return None
    if runtime_root is None:
        raise ValueError(
            f"runtime_root is required to resolve dataset {storage_key}"
        )
    configured_root = _storage_root(runtime_root)
    resolved = (configured_root / key).resolve()
    if resolved == configured_root or configured_root not in resolved.parents:
        raise ValueError(f"dataset {storage_key} escapes the configured runtime root")
    return resolved


def load_dataset_artifact(
    artifact: dict[str, Any],
    *,
    runtime_root: str | Path | ThreadStorageScope | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    data_path = _dataset_storage_path(
        artifact,
        path_key="path",
        storage_key="storage_key",
        runtime_root=runtime_root,
    )
    if data_path is None:
        raise KeyError("path")
    df = pd.read_parquet(data_path)
    schema_path = _dataset_storage_path(
        artifact,
        path_key="schema_path",
        storage_key="schema_storage_key",
        runtime_root=runtime_root,
    )
    schema = {}
    if schema_path and schema_path.exists():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return df, schema


NON_SELECTABLE_DATASET_STATUSES = {"pending_review", "cancelled", "superseded", "rejected"}


def _dataset_artifact_status(artifact: dict[str, Any] | None) -> str:
    item = dict(artifact or {})
    return str(item.get("status") or "").strip()


def is_pending_review_dataset_artifact(artifact: dict[str, Any] | None) -> bool:
    return _dataset_artifact_status(artifact) == "pending_review"


def is_selectable_dataset_artifact(artifact: dict[str, Any] | None) -> bool:
    return _dataset_artifact_status(artifact) not in NON_SELECTABLE_DATASET_STATUSES
