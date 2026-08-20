from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import chromadb
import pytest

from study_package.manifest import (
    load_installed_manifest,
    parse_study_package_manifest,
    resolve_package_path,
)


MINIMAL_MANIFEST = {
    "format_version": 2,
    "study_id": "example-study",
    "label": "Example Study",
    "package_version": "1.0.0",
    "database": {
        "source_id": "example-source",
        "duckdb": "database/study.duckdb",
        "catalog": "database/schema_catalog.json",
        "index": "database/index",
        "embedding_model": "OpenAI/text-embedding-3-large",
    },
}


def _write_installed_package(
    tmp_path: Path,
    manifest: dict[str, object] | None = None,
) -> Path:
    package_root = tmp_path / "package"
    package_root.mkdir()
    package_manifest = copy.deepcopy(manifest or MINIMAL_MANIFEST)
    (package_root / "study-package.json").write_text(
        json.dumps(package_manifest), encoding="utf-8"
    )
    database = package_root / "database"
    database.mkdir()
    (database / "study.duckdb").touch()
    (database / "schema_catalog.json").touch()
    (database / "index").mkdir()
    knowledge = package_manifest.get("knowledge")
    if isinstance(knowledge, dict):
        (package_root / str(knowledge["root"])).mkdir()
    study_design = package_manifest.get("study_design")
    if isinstance(study_design, dict):
        if "document" in study_design:
            document = package_root / str(study_design["document"])
            document.parent.mkdir(parents=True, exist_ok=True)
            document.touch()
        else:
            root = package_root / str(study_design["root"])
            root.mkdir(parents=True, exist_ok=True)
            (root / "overview.md").write_text("# Overview", encoding="utf-8")
    return package_root


def test_minimal_manifest_omits_optional_capabilities() -> None:
    manifest = parse_study_package_manifest(MINIMAL_MANIFEST)

    assert manifest.study_id == "example-study"
    assert manifest.description is None
    assert manifest.knowledge is None
    assert manifest.study_design is None


def test_manifest_accepts_bounded_optional_description() -> None:
    manifest = parse_study_package_manifest(
        {**MINIMAL_MANIFEST, "description": "A neutral participant database."}
    )

    assert manifest.description == "A neutral participant database."


@pytest.mark.parametrize(
    ("field", "value"),
    [("label", "x" * 201), ("description", "x" * 1001)],
)
def test_manifest_rejects_text_over_its_bound(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        parse_study_package_manifest({**MINIMAL_MANIFEST, field: value})


def test_manifest_rejects_legacy_format() -> None:
    with pytest.raises(ValueError, match="format_version"):
        parse_study_package_manifest({**MINIMAL_MANIFEST, "format_version": 1})


def test_manifest_accepts_v3_markdown_design() -> None:
    manifest = parse_study_package_manifest(
        {
            **MINIMAL_MANIFEST,
            "format_version": 3,
            "study_design": {
                "root": "study-design",
                "overview": "overview.md",
            },
        }
    )

    assert manifest.format_version == 3
    assert manifest.study_design is not None
    assert manifest.study_design.root == "study-design"
    assert manifest.study_design.overview == "overview.md"
    assert manifest.declared_paths()["study_design.root"] == (
        "study-design",
        "directory",
    )


def test_manifest_rejects_v3_without_study_design() -> None:
    with pytest.raises(ValueError, match="format_version 3 requires"):
        parse_study_package_manifest({**MINIMAL_MANIFEST, "format_version": 3})


@pytest.mark.parametrize(
    ("format_version", "study_design"),
    [
        (2, {"root": "study-design", "overview": "overview.md"}),
        (3, {"document": "study-design/design.json"}),
    ],
)
def test_manifest_rejects_study_design_shape_from_other_version(
    format_version: int,
    study_design: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="format_version"):
        parse_study_package_manifest(
            {
                **MINIMAL_MANIFEST,
                "format_version": format_version,
                "study_design": study_design,
            }
        )


@pytest.mark.parametrize("overview", ["summary.md", "nested/overview.md", "../overview.md"])
def test_v3_manifest_requires_exact_overview_name(overview: str) -> None:
    with pytest.raises(ValueError, match="overview"):
        parse_study_package_manifest(
            {
                **MINIMAL_MANIFEST,
                "format_version": 3,
                "study_design": {
                    "root": "study-design",
                    "overview": overview,
                },
            }
        )


def test_v3_manifest_requires_study_design_root() -> None:
    with pytest.raises(ValueError, match="study-design/overview.md"):
        parse_study_package_manifest(
            {
                **MINIMAL_MANIFEST,
                "format_version": 3,
                "study_design": {
                    "root": "study_design",
                    "overview": "overview.md",
                },
            }
        )


@pytest.mark.parametrize("value", ["../escape", "/absolute", "a\\b", "a//b"])
def test_manifest_rejects_unsafe_paths(value: str) -> None:
    data = copy.deepcopy(MINIMAL_MANIFEST)
    data["database"]["duckdb"] = value

    with pytest.raises(ValueError, match="relative path"):
        parse_study_package_manifest(data)


def test_manifest_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra"):
        parse_study_package_manifest({**MINIMAL_MANIFEST, "package_id": "unused"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("study_id", "Example Study"),
        ("package_version", "version/one"),
        ("label", "   "),
        ("description", "   "),
    ],
)
def test_manifest_rejects_invalid_identity_text(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        parse_study_package_manifest({**MINIMAL_MANIFEST, field: value})


def test_resolve_package_path_rejects_symlinked_component(tmp_path) -> None:
    package_root = tmp_path / "package"
    outside_root = tmp_path / "outside"
    package_root.mkdir()
    outside_root.mkdir()
    (package_root / "database").symlink_to(outside_root, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        resolve_package_path(
            package_root,
            "database/study.duckdb",
            "database.duckdb",
        )


@pytest.mark.parametrize(
    ("field", "relative"),
    [
        ("database.duckdb", "database/study.duckdb"),
        ("database.catalog", "database/schema_catalog.json"),
        ("database.index", "database/index"),
    ],
)
def test_load_installed_manifest_rejects_missing_required_asset(
    tmp_path: Path, field: str, relative: str
) -> None:
    package_root = _write_installed_package(tmp_path)
    path = package_root / relative
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()

    with pytest.raises(ValueError, match=field):
        load_installed_manifest(package_root)


def test_load_installed_manifest_rejects_symlinked_package_root(tmp_path: Path) -> None:
    package_root = _write_installed_package(tmp_path)
    linked_root = tmp_path / "linked-package"
    linked_root.symlink_to(package_root, target_is_directory=True)

    with pytest.raises(ValueError, match="package root"):
        load_installed_manifest(linked_root)


def test_load_installed_manifest_rejects_symlinked_declared_target(tmp_path: Path) -> None:
    package_root = _write_installed_package(tmp_path)
    outside = tmp_path / "outside.duckdb"
    outside.touch()
    target = package_root / "database" / "study.duckdb"
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(ValueError, match="symlink"):
        load_installed_manifest(package_root)


@pytest.mark.parametrize(
    ("field", "relative", "replacement_is_directory"),
    [
        ("database.duckdb", "database/study.duckdb", True),
        ("database.index", "database/index", False),
    ],
)
def test_load_installed_manifest_rejects_declared_asset_with_wrong_kind(
    tmp_path: Path, field: str, relative: str, replacement_is_directory: bool
) -> None:
    package_root = _write_installed_package(tmp_path)
    path = package_root / relative
    if path.is_dir():
        path.rmdir()
    else:
        path.unlink()
    if replacement_is_directory:
        path.mkdir()
    else:
        path.touch()

    with pytest.raises(ValueError, match=field):
        load_installed_manifest(package_root)


def test_load_installed_manifest_validates_optional_declared_assets(tmp_path: Path) -> None:
    manifest = {
        **MINIMAL_MANIFEST,
        "knowledge": {"root": "knowledge"},
        "study_design": {"document": "design/protocol.md"},
    }
    package_root = _write_installed_package(tmp_path, manifest)
    (package_root / "design" / "protocol.md").unlink()

    with pytest.raises(ValueError, match="study_design.document"):
        load_installed_manifest(package_root)


def test_load_installed_manifest_accepts_knowledge_only_package(tmp_path: Path) -> None:
    package_root = _write_installed_package(
        tmp_path,
        {**MINIMAL_MANIFEST, "knowledge": {"root": "knowledge"}},
    )

    manifest = load_installed_manifest(package_root)

    assert manifest.knowledge is not None
    assert manifest.knowledge.root == "knowledge"
    assert manifest.study_design is None


def test_load_installed_manifest_accepts_study_design_only_package(tmp_path: Path) -> None:
    package_root = _write_installed_package(
        tmp_path,
        {**MINIMAL_MANIFEST, "study_design": {"document": "design/protocol.md"}},
    )

    manifest = load_installed_manifest(package_root)

    assert manifest.knowledge is None
    assert manifest.study_design is not None
    assert manifest.study_design.document == "design/protocol.md"


def test_load_installed_manifest_accepts_v3_study_design_directory(
    tmp_path: Path,
) -> None:
    package_root = _write_installed_package(
        tmp_path,
        {
            **MINIMAL_MANIFEST,
                "format_version": 3,
                "study_design": {
                    "root": "study-design",
                    "overview": "overview.md",
                },
        },
    )

    manifest = load_installed_manifest(package_root)

    assert manifest.study_design is not None
    assert manifest.study_design.root == "study-design"


def test_v3_package_fixture_writes_markdown_and_matching_index(tmp_path: Path) -> None:
    from tests.study_package_fixtures import create_package_root, minimal_manifest

    root = create_package_root(
        tmp_path,
        manifest=minimal_manifest(
            format_version=3,
            study_design_format="markdown",
        ),
        study_design_documents={
            "overview.md": "# Overview\n\nAuthoritative.",
            "reference/visits.md": "# Visits\n\nRetrieval detail.",
        },
    )

    overview = root / "study-design" / "overview.md"
    assert overview.is_file()
    collection = chromadb.PersistentClient(
        path=str(root / "database" / "index")
    ).get_collection("study_knowledge")
    rows = collection.get(include=["metadatas"])
    metadatas = list(rows["metadatas"] or [])
    assert {metadata["source_path"] for metadata in metadatas} == {
        "overview.md",
        "reference/visits.md",
    }
    assert next(
        metadata["source_sha256"]
        for metadata in metadatas
        if metadata["source_path"] == "overview.md"
    ) == hashlib.sha256(overview.read_bytes()).hexdigest()


@pytest.mark.parametrize("wrong_kind", [False, True], ids=["missing", "file"])
def test_load_installed_manifest_rejects_missing_or_wrong_kind_knowledge_root(
    tmp_path: Path, wrong_kind: bool
) -> None:
    package_root = _write_installed_package(
        tmp_path,
        {**MINIMAL_MANIFEST, "knowledge": {"root": "knowledge"}},
    )
    knowledge_root = package_root / "knowledge"
    knowledge_root.rmdir()
    if wrong_kind:
        knowledge_root.touch()

    with pytest.raises(ValueError, match="knowledge.root"):
        load_installed_manifest(package_root)
