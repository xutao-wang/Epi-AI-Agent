from __future__ import annotations

from dataclasses import fields
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

import study_package.installer as installer
from study_package.installer import (
    InstalledStudy,
    activate_study_version,
    install_study_archives,
    stage_study_archive,
)
from tests.study_package_fixtures import (
    create_package_archive,
    create_package_archive_from_root,
    create_package_root,
    minimal_manifest,
)


def _optional_manifest() -> dict[str, object]:
    manifest = minimal_manifest()
    manifest["knowledge"] = {"root": "knowledge"}
    manifest["study_design"] = {"document": "study-design.json"}
    return manifest


def _markdown_manifest() -> dict[str, object]:
    return minimal_manifest(format_version=3, study_design_format="markdown")


def _markdown_package_root(tmp_path: Path) -> Path:
    return create_package_root(
        tmp_path,
        manifest=_markdown_manifest(),
        study_design_documents={
            "overview.md": "# Overview\n\nAuthoritative context.",
            "reference/visits.md": "# Visits\n\nRetrieval-only schedule.",
        },
    )


def _write_archive_from_members(
    archive_path: Path,
    members: list[tarfile.TarInfo],
) -> None:
    with tarfile.open(archive_path, "w:gz") as archive:
        for member in members:
            content = b"unsafe archive payload" if member.isfile() else None
            archive.addfile(
                member,
                fileobj=io.BytesIO(content) if content is not None else None,
            )


def _file_member(name: str) -> tarfile.TarInfo:
    member = tarfile.TarInfo(name)
    member.size = len(b"unsafe archive payload")
    return member


def test_stage_study_archive_copies_and_validates_real_package(tmp_path) -> None:
    archive = create_package_archive(tmp_path)
    expected_sha256 = sha256(archive.read_bytes()).hexdigest()

    staged = stage_study_archive(archive, tmp_path / "runtime" / "studies")

    assert staged.archive_path != archive
    assert staged.archive_path.read_bytes() == archive.read_bytes()
    assert staged.manifest.study_id == "example-study"
    assert (staged.extracted_root / "database" / "study.duckdb").is_file()
    assert staged.archive_sha256 == expected_sha256


def test_v3_fixture_builder_creates_required_study_design_overview(
    tmp_path: Path,
) -> None:
    package_root = create_package_root(
        tmp_path,
        manifest=minimal_manifest(format_version=3),
    )

    manifest = json.loads((package_root / "study-package.json").read_text())

    assert manifest["study_design"] == {
        "root": "study-design",
        "overview": "overview.md",
    }
    assert (package_root / "study-design" / "overview.md").is_file()

    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )
    staged = stage_study_archive(archive, tmp_path / "runtime" / "studies")

    assert staged.manifest.format_version == 3


def test_stage_rejects_declared_relationship_column_missing_from_duckdb(
    tmp_path: Path,
) -> None:
    package_root = create_package_root(tmp_path / "source")
    catalog_path = package_root / "database" / "schema_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["join_keys"] = {"participant_key": "MISSING_SUBJID"}
    catalog["columns"].append(
        {
            "table": "participants",
            "column": "MISSING_SUBJID",
            "text": "Broken declared key.",
        }
    )
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    with pytest.raises(ValueError, match="missing DuckDB column"):
        stage_study_archive(archive, tmp_path / "runtime" / "studies")


def test_stage_rejects_catalog_v1(tmp_path: Path) -> None:
    package_root = create_package_root(tmp_path / "source")
    catalog_path = package_root / "database" / "schema_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["catalog_version"] = 1
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    with pytest.raises(ValueError, match="catalog_version 2"):
        stage_study_archive(archive, tmp_path / "runtime" / "studies")


def test_activate_rejects_installed_catalog_v1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    studies_root = tmp_path / "runtime" / "studies"
    installed = install_study_archives(
        [create_package_archive(tmp_path / "source")],
        studies_root,
    )[0]
    catalog_path = installed.package_root / "database" / "schema_catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["catalog_version"] = 1
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    registry_writes: list[object] = []
    monkeypatch.setattr(
        installer,
        "write_registry",
        lambda *_args, **_kwargs: registry_writes.append(object()),
    )

    with pytest.raises(ValueError, match="catalog_version 2"):
        activate_study_version("example-study", "1.0.0", studies_root)

    assert registry_writes == []


def test_installed_study_exposes_task_two_installation_identity() -> None:
    assert tuple(field.name for field in fields(InstalledStudy)) == (
        "study_id",
        "package_version",
        "package_root",
        "archive_sha256",
        "manifest",
        "warnings",
    )


def test_stage_v3_markdown_design_is_valid(tmp_path: Path) -> None:
    package_root = _markdown_package_root(tmp_path / "source")
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    staged = stage_study_archive(archive, tmp_path / "runtime" / "studies")

    assert staged.manifest.format_version == 3
    assert staged.warnings == ()


def test_stage_v3_non_markdown_file_returns_warning_not_failure(tmp_path: Path) -> None:
    package_root = _markdown_package_root(tmp_path / "source")
    asset = package_root / "study-design" / "reference" / "diagram.png"
    asset.write_bytes(b"asset")
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    staged = stage_study_archive(archive, tmp_path / "runtime" / "studies")

    assert [(warning.path, warning.message) for warning in staged.warnings] == [
        (
            "reference/diagram.png",
            "reference/diagram.png is not consumed by study-design indexing",
        )
    ]


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "STUDY_DESIGN_OVERVIEW_EMPTY"),
        (b"\xff", "STUDY_DESIGN_OVERVIEW_INVALID_UTF8"),
        (b"x" * (32 * 1024 + 1), "STUDY_DESIGN_OVERVIEW_TOO_LARGE"),
    ],
)
def test_stage_v3_rejects_invalid_overview_with_both_fixes(
    tmp_path: Path,
    payload: bytes,
    code: str,
) -> None:
    package_root = _markdown_package_root(tmp_path / "source")
    (package_root / "study-design" / "overview.md").write_bytes(payload)
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    with pytest.raises(ValueError) as raised:
        stage_study_archive(archive, tmp_path / "runtime" / "studies")

    message = str(raised.value)
    assert code in message
    assert "1. Remove" in message
    assert "2. Add or fix" in message


def test_stage_v3_rejects_missing_overview_with_both_fixes(tmp_path: Path) -> None:
    package_root = _markdown_package_root(tmp_path / "source")
    (package_root / "study-design" / "overview.md").unlink()
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    with pytest.raises(ValueError) as raised:
        stage_study_archive(archive, tmp_path / "runtime" / "studies")

    message = str(raised.value)
    assert "STUDY_DESIGN_OVERVIEW_MISSING" in message
    assert "1. Remove" in message
    assert "2. Add or fix" in message


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"", "STUDY_DESIGN_MARKDOWN_EMPTY"),
        (b"\xff", "STUDY_DESIGN_MARKDOWN_INVALID_UTF8"),
    ],
)
def test_stage_v3_rejects_invalid_optional_markdown_with_exact_path(
    tmp_path: Path,
    payload: bytes,
    code: str,
) -> None:
    package_root = _markdown_package_root(tmp_path / "source")
    (package_root / "study-design" / "reference" / "visits.md").write_bytes(
        payload
    )
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    with pytest.raises(ValueError) as raised:
        stage_study_archive(archive, tmp_path / "runtime" / "studies")

    assert code in str(raised.value)
    assert "reference/visits.md" in str(raised.value)


def test_stage_v3_rejects_markdown_hash_mismatch(tmp_path: Path) -> None:
    package_root = _markdown_package_root(tmp_path / "source")
    (package_root / "study-design" / "reference" / "visits.md").write_text(
        "# Visits\n\nChanged after indexing.",
        encoding="utf-8",
    )
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    with pytest.raises(ValueError, match="path/hash sets differ"):
        stage_study_archive(archive, tmp_path / "runtime" / "studies")


def test_stage_v3_rejects_missing_design_index_source(tmp_path: Path) -> None:
    package_root = _markdown_package_root(tmp_path / "source")
    client = installer.chromadb.PersistentClient(
        path=str(package_root / "database" / "index")
    )
    client.delete_collection("study_knowledge")
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    with pytest.raises(ValueError, match="path/hash sets differ"):
        stage_study_archive(archive, tmp_path / "runtime" / "studies")


def test_stage_v3_rejects_extra_design_index_source(tmp_path: Path) -> None:
    package_root = _markdown_package_root(tmp_path / "source")
    collection = installer.chromadb.PersistentClient(
        path=str(package_root / "database" / "index")
    ).get_collection("study_knowledge")
    collection.add(
        ids=["extra-design-source"],
        embeddings=[[0.0, 1.0]],
        documents=["Unpackaged design source"],
        metadatas=[
            {
                "source_kind": "study_design",
                "source_id": "extra",
                "source_path": "extra.md",
                "source_sha256": "a" * 64,
                "section": "Extra",
                "body_text": "Unpackaged design source",
                "chunk_ordinal": 0,
            }
        ],
    )
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "study.tar.gz",
    )

    with pytest.raises(ValueError, match="path/hash sets differ"):
        stage_study_archive(archive, tmp_path / "runtime" / "studies")


def test_fixture_archive_is_deterministic_despite_source_timestamps(tmp_path) -> None:
    package_root = create_package_root(tmp_path / "package-root")
    catalog_path = package_root / "database" / "schema_catalog.json"
    first_archive = tmp_path / "first.tar.gz"
    second_archive = tmp_path / "second.tar.gz"

    os.utime(catalog_path, (1_000_000_000, 1_000_000_000))
    create_package_archive_from_root(package_root, first_archive)
    os.utime(catalog_path, (1_100_000_000, 1_100_000_000))
    create_package_archive_from_root(package_root, second_archive)

    assert first_archive.read_bytes() == second_archive.read_bytes()


def test_stage_study_archive_uses_private_copy_after_source_replacement(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = create_package_archive(tmp_path)
    original_bytes = archive.read_bytes()
    expected_sha256 = sha256(original_bytes).hexdigest()
    copy_archive = installer._copy_archive

    def copy_then_replace(source: Path, destination: Path) -> str:
        digest = copy_archive(source, destination)
        source.write_bytes(b"source replaced after private copy")
        return digest

    monkeypatch.setattr(installer, "_copy_archive", copy_then_replace)

    staged = stage_study_archive(archive, tmp_path / "runtime" / "studies")

    assert archive.read_bytes() == b"source replaced after private copy"
    assert staged.archive_path.read_bytes() == original_bytes
    assert staged.archive_sha256 == expected_sha256


def test_stage_study_archive_rejects_a_symlink_source(tmp_path) -> None:
    archive = create_package_archive(tmp_path / "target")
    symlink = tmp_path / "archive-link.tar.gz"
    symlink.symlink_to(archive)

    with pytest.raises(ValueError, match="local .tar.gz"):
        stage_study_archive(symlink, tmp_path / "runtime" / "studies")


def test_stage_study_archive_rejects_invalid_declared_publication_knowledge(
    tmp_path,
) -> None:
    package_root = create_package_root(tmp_path / "package-root", manifest=_optional_manifest())
    (package_root / "knowledge" / "ingestion-manifest.json").write_text(
        "{}",
        encoding="utf-8",
    )
    archive = tmp_path / "invalid-knowledge.tar.gz"
    create_package_archive_from_root(package_root, archive)

    with pytest.raises(ValueError, match="knowledge.root"):
        stage_study_archive(archive, tmp_path / "runtime" / "studies")


@pytest.mark.parametrize(
    ("field", "value"),
    [("study_id", "other-study"), ("label", "Other Study")],
)
def test_stage_study_archive_rejects_declared_design_with_mismatched_identity(
    tmp_path,
    field: str,
    value: str,
) -> None:
    package_root = create_package_root(tmp_path / "package-root", manifest=_optional_manifest())
    design_path = package_root / "study-design.json"
    design = json.loads(design_path.read_text(encoding="utf-8"))
    design[field] = value
    design_path.write_text(json.dumps(design), encoding="utf-8")
    archive = tmp_path / "mismatched-design.tar.gz"
    create_package_archive_from_root(package_root, archive)

    with pytest.raises(ValueError, match="study_design.document"):
        stage_study_archive(archive, tmp_path / "runtime" / "studies")


@pytest.mark.parametrize(
    "members",
    [
        [_file_member("/absolute.txt")],
        [_file_member("../outside.txt")],
        [_file_member("database\\study.duckdb")],
        [_file_member("duplicate.txt"), _file_member("duplicate.txt")],
        [tarfile.TarInfo("symbolic-link")],
        [tarfile.TarInfo("hard-link")],
        [tarfile.TarInfo("named-pipe")],
    ],
    ids=(
        "absolute-path",
        "traversal",
        "backslash",
        "duplicate-member",
        "symlink-member",
        "hardlink-member",
        "special-member",
    ),
)
def test_stage_study_archive_rejects_unsafe_members(
    tmp_path,
    members: list[tarfile.TarInfo],
) -> None:
    if members[0].name == "symbolic-link":
        members[0].type = tarfile.SYMTYPE
        members[0].linkname = "target"
    elif members[0].name == "hard-link":
        members[0].type = tarfile.LNKTYPE
        members[0].linkname = "target"
    elif members[0].name == "named-pipe":
        members[0].type = tarfile.FIFOTYPE

    archive_path = tmp_path / "unsafe.tar.gz"
    _write_archive_from_members(archive_path, members)

    with pytest.raises(ValueError, match="unsafe archive member"):
        stage_study_archive(archive_path, tmp_path / "runtime" / "studies")
