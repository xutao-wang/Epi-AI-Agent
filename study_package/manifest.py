from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EMBEDDING_MODEL = "OpenAI/text-embedding-3-large"
_SAFE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _require_safe_segment(value: str, *, field: str) -> str:
    if not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{field} must be a safe identifier segment")
    return value


def _require_relative_path(value: str, *, field: str) -> str:
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError(f"{field} must be a safe relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field} must be a safe relative path")
    return value


def _require_nonblank_text(value: str | None, *, field: str) -> str | None:
    if value is not None and not value.strip():
        raise ValueError(f"{field} must not be blank")
    return value


class DatabaseManifest(_ManifestModel):
    source_id: str
    duckdb: str
    catalog: str
    index: str
    embedding_model: Literal[EMBEDDING_MODEL]

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        return _require_safe_segment(value, field="database.source_id")

    @field_validator("duckdb", "catalog", "index")
    @classmethod
    def validate_path(cls, value: str, info) -> str:
        return _require_relative_path(value, field=f"database.{info.field_name}")


class KnowledgeManifest(_ManifestModel):
    root: str

    @field_validator("root")
    @classmethod
    def validate_root(cls, value: str) -> str:
        return _require_relative_path(value, field="knowledge.root")


class LegacyStudyDesignManifest(_ManifestModel):
    document: str

    @field_validator("document")
    @classmethod
    def validate_document(cls, value: str) -> str:
        return _require_relative_path(value, field="study_design.document")


class MarkdownStudyDesignManifest(_ManifestModel):
    root: str
    overview: Literal["overview.md"]

    @field_validator("root", "overview")
    @classmethod
    def validate_path(cls, value: str, info) -> str:
        return _require_relative_path(value, field=f"study_design.{info.field_name}")


StudyDesignManifest = LegacyStudyDesignManifest | MarkdownStudyDesignManifest


class StudyPackageManifest(_ManifestModel):
    format_version: Literal[2, 3]
    study_id: str
    label: str = Field(min_length=1, max_length=200)
    package_version: str
    database: DatabaseManifest
    description: str | None = Field(default=None, min_length=1, max_length=1000)
    knowledge: KnowledgeManifest | None = None
    study_design: StudyDesignManifest | None = None

    @model_validator(mode="before")
    @classmethod
    def validate_versioned_study_design(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        version = value.get("format_version")
        design = value.get("study_design")
        if version == 3:
            if not isinstance(design, dict) or set(design) != {"root", "overview"}:
                raise ValueError("format_version 3 requires a study_design declaration")
            if (
                design.get("root") != "study-design"
                or design.get("overview") != "overview.md"
            ):
                raise ValueError(
                    "format_version 3 requires study-design/overview.md"
                )
        elif isinstance(design, dict) and version == 2 and set(design) != {"document"}:
            raise ValueError(
                "study_design declaration does not match format_version 2"
            )
        return value

    @field_validator("study_id", "package_version")
    @classmethod
    def validate_identifier(cls, value: str, info) -> str:
        return _require_safe_segment(value, field=info.field_name)

    @field_validator("label", "description")
    @classmethod
    def validate_text(cls, value: str | None, info) -> str | None:
        return _require_nonblank_text(value, field=info.field_name)

    def declared_paths(self) -> dict[str, tuple[str, Literal["file", "directory"]]]:
        paths: dict[str, tuple[str, Literal["file", "directory"]]] = {
            "database.duckdb": (self.database.duckdb, "file"),
            "database.catalog": (self.database.catalog, "file"),
            "database.index": (self.database.index, "directory"),
        }
        if self.knowledge is not None:
            paths["knowledge.root"] = (self.knowledge.root, "directory")
        if self.study_design is not None:
            if isinstance(self.study_design, LegacyStudyDesignManifest):
                paths["study_design.document"] = (
                    self.study_design.document,
                    "file",
                )
            else:
                paths["study_design.root"] = (
                    self.study_design.root,
                    "directory",
                )
        return paths


def parse_study_package_manifest(data: object) -> StudyPackageManifest:
    return StudyPackageManifest.model_validate(data)


def load_installed_manifest(package_root: Path) -> StudyPackageManifest:
    root = Path(package_root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("package root is not a real directory")

    path = root / "study-package.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"Cannot read study-package.json: {path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid study-package.json: {path}") from error
    manifest = parse_study_package_manifest(data)
    for field, (relative, kind) in manifest.declared_paths().items():
        declared_path = resolve_package_path(root, relative, field)
        if not declared_path.exists() or declared_path.is_symlink():
            raise ValueError(f"{field} is missing from study package")
        if kind == "file" and not declared_path.is_file():
            raise ValueError(f"{field} must be a file")
        if kind == "directory" and not declared_path.is_dir():
            raise ValueError(f"{field} must be a directory")
    return manifest


def resolve_package_path(
    package_root: Path,
    relative: str,
    field: str,
) -> Path:
    root = Path(package_root)
    _require_relative_path(relative, field=field)
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"{field} package root is not a real directory")

    current = root
    for part in relative.split("/"):
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{field} may not traverse a symlink")

    return current
