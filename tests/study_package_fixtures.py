from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import tarfile
from typing import Literal

import chromadb
import duckdb

from tests.test_publication_index import minimal_publication_index


def minimal_manifest(
    *,
    study_id: str = "example-study",
    package_version: str = "1.0.0",
    label: str = "Example Study",
    source_id: str = "example-source",
    format_version: Literal[2, 3] = 2,
    study_design_format: Literal["legacy", "markdown"] | None = None,
) -> dict[str, object]:
    if format_version == 3 and study_design_format is None:
        study_design_format = "markdown"
    if study_design_format == "legacy" and format_version != 2:
        raise ValueError("Legacy study design requires format version 2")
    if study_design_format == "markdown" and format_version != 3:
        raise ValueError("Markdown study design requires format version 3")
    manifest: dict[str, object] = {
        "format_version": format_version,
        "study_id": study_id,
        "label": label,
        "package_version": package_version,
        "database": {
            "source_id": source_id,
            "duckdb": "database/study.duckdb",
            "catalog": "database/schema_catalog.json",
            "index": "database/index",
            "embedding_model": "OpenAI/text-embedding-3-large",
        },
    }
    if study_design_format == "legacy":
        manifest["study_design"] = {"document": "study-design/design.json"}
    elif study_design_format == "markdown":
        manifest["study_design"] = {
            "root": "study-design",
            "overview": "overview.md",
        }
    return manifest


def _study_design_document(study_id: str, label: str) -> dict[str, object]:
    return {
        "$schema": "report-study-design-1.0",
        "schema_version": "1.0",
        "study_id": study_id,
        "label": label,
        "study_purpose": "Fixture cohort for package staging validation.",
        "populations": {
            "index_case": {
                "canonical_label": "Index cases",
                "aliases": ["Cohort A", "index case"],
                "definition": "Participants assigned to the fixture index cohort.",
            },
            "household_contact": {
                "canonical_label": "Household contacts",
                "aliases": ["Cohort B", "household contact"],
                "definition": "Participants linked to a fixture index case.",
            },
        },
        "relationships": [
            {
                "type": "household_contact_of",
                "from": "household_contact",
                "to": "index_case",
                "description": "Each contact is linked to an index case.",
            }
        ],
        "authoritative_for": ["fixture_validation"],
    }


def _write_declared_optional_content(
    root: Path,
    manifest: dict[str, object],
    *,
    study_design_documents: dict[str, str] | None = None,
) -> tuple[Path, ...]:
    knowledge = manifest.get("knowledge")
    if isinstance(knowledge, dict) and isinstance(knowledge.get("root"), str):
        knowledge_root = root / knowledge["root"]
        knowledge_root.mkdir(parents=True)
        index = minimal_publication_index()
        index["publication_id"] = "doi:10.1000/package-fixture"
        index["citation"]["doi"] = "10.1000/package-fixture"
        index["citation"]["title"] = "Package-relative knowledge marker"
        index["review_status"] = {
            "status": "manually_verified",
            "reviewer": "fixture@example.org",
            "reviewed_at": "2026-07-26T12:00:00Z",
            "review_notes": [],
        }
        index_path = knowledge_root / "doi_10.1000_package-fixture.json"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        ingestion_manifest_path = knowledge_root / "ingestion-manifest.json"
        ingestion_manifest = {
            "schema_version": "1.0.0",
            "model": None,
            "prompt_sha256": "c" * 64,
            "documents": [
                {
                    "source_filename": "package-fixture.pdf",
                    "source_sha256": "a" * 64,
                    "extracted_text_sha256": "b" * 64,
                    "publication_id": index["publication_id"],
                    "index_filename": index_path.name,
                    "index_sha256": hashlib.sha256(
                        index_path.read_bytes()
                    ).hexdigest(),
                    "review_status": "manually_verified",
                }
            ],
        }
        ingestion_manifest_path.write_text(json.dumps(ingestion_manifest), encoding="utf-8")

    study_design = manifest.get("study_design")
    if isinstance(study_design, dict) and isinstance(study_design.get("document"), str):
        design_path = root / study_design["document"]
        design_path.parent.mkdir(parents=True, exist_ok=True)
        design_path.write_text(
            json.dumps(
                _study_design_document(
                    str(manifest["study_id"]),
                    str(manifest["label"]),
                ),
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return ()
    if isinstance(study_design, dict) and isinstance(study_design.get("root"), str):
        design_root = root / study_design["root"]
        documents = study_design_documents or {
            "overview.md": "# Fixture Study Design\n\nAuthoritative fixture overview."
        }
        paths: list[Path] = []
        for relative_path, text in sorted(documents.items()):
            path = design_root / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            paths.append(path)
        return tuple(paths)
    return ()


def create_package_root(
    tmp_path: Path,
    *,
    manifest: dict[str, object] | None = None,
    study_design_documents: dict[str, str] | None = None,
) -> Path:
    root = tmp_path / "package"
    database_root = root / "database"
    database_root.mkdir(parents=True)
    package_manifest = manifest if manifest is not None else minimal_manifest()
    (root / "study-package.json").write_text(
        json.dumps(package_manifest, sort_keys=True), encoding="utf-8"
    )
    with duckdb.connect(str(database_root / "study.duckdb")) as connection:
        connection.execute(
            'CREATE TABLE participants (SUBJID VARCHAR, AGE INTEGER)'
        )
        connection.execute("INSERT INTO participants VALUES ('P001', 41)")
    (database_root / "schema_catalog.json").write_text(
        json.dumps(
            {
                "catalog_version": 2,
                "join_keys": {"participant_key": "SUBJID"},
                "relationships": [],
                "tables": [
                    {
                        "table": "participants",
                        "text": "Participant records",
                        "has_participant_key_join": True,
                    }
                ],
                "columns": [
                    {
                        "table": "participants",
                        "column": "SUBJID",
                        "description": "Participant identifier",
                        "text": "Participant identifier",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    client = chromadb.PersistentClient(path=str(database_root / "index"))
    for name, document in (
        ("table_summaries", "Participant records"),
        ("column_chunks", "Participant identifier"),
    ):
        collection = client.create_collection(name)
        collection.add(
            ids=[name],
            embeddings=[[1.0, 0.0]],
            documents=[document],
        )
    design_paths = _write_declared_optional_content(
        root,
        package_manifest,
        study_design_documents=study_design_documents,
    )
    if design_paths:
        collection = client.create_collection("study_knowledge")
        ids: list[str] = []
        embeddings: list[list[float]] = []
        documents: list[str] = []
        metadatas: list[dict[str, str | int]] = []
        design_root = root / str(package_manifest["study_design"]["root"])
        for index, path in enumerate(design_paths):
            relative_path = path.relative_to(design_root).as_posix()
            payload = path.read_bytes()
            source_sha256 = hashlib.sha256(payload).hexdigest()
            source_id = "study-design-source." + hashlib.sha256(
                relative_path.encode("utf-8")
            ).hexdigest()[:24]
            body_text = payload.decode("utf-8").strip()
            section = next(
                (
                    line.lstrip("#").strip()
                    for line in body_text.splitlines()
                    if line.startswith("#")
                ),
                "Document",
            )
            ids.append(f"study-design.fixture-{index}")
            embeddings.append([1.0, float(index + 1)])
            documents.append(
                f"Source: {relative_path}\nSection: {section}\n\n{body_text}"
            )
            metadatas.append(
                {
                    "source_kind": "study_design",
                    "source_id": source_id,
                    "source_path": relative_path,
                    "source_sha256": source_sha256,
                    "section": section,
                    "body_text": body_text,
                    "chunk_ordinal": 0,
                }
            )
        collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )
    return root


def create_package_archive_from_root(root: Path, archive_path: Path) -> Path:
    root = Path(root)
    archive_path = Path(archive_path)
    with archive_path.open("wb") as raw_archive:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw_archive,
            mtime=0,
        ) as compressed_archive:
            with tarfile.open(
                fileobj=compressed_archive,
                mode="w",
                format=tarfile.USTAR_FORMAT,
            ) as archive:
                for path in sorted(root.rglob("*")):
                    if not path.is_file():
                        continue
                    member = tarfile.TarInfo(path.relative_to(root).as_posix())
                    member.size = path.stat().st_size
                    member.mode = 0o644
                    member.mtime = 0
                    member.uid = 0
                    member.gid = 0
                    member.uname = ""
                    member.gname = ""
                    with path.open("rb") as source:
                        archive.addfile(member, fileobj=source)
    return archive_path


def create_package_archive(
    tmp_path: Path,
    *,
    manifest: dict[str, object] | None = None,
    study_design_documents: dict[str, str] | None = None,
) -> Path:
    root = create_package_root(
        tmp_path,
        manifest=manifest,
        study_design_documents=study_design_documents,
    )
    archive_path = tmp_path / "example-study.tar.gz"
    return create_package_archive_from_root(root, archive_path)
