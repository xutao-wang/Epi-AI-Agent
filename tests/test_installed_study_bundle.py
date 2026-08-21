from __future__ import annotations

import json

import db_rag.study as study_module
from db_rag.study import build_study_bundle
from study_package.installer import install_study_archives
from tests.study_package_fixtures import (
    create_package_archive,
    create_package_archive_from_root,
    create_package_root,
    minimal_manifest,
)


def _manifest_with_optional_capabilities(
    *,
    knowledge: bool = False,
    study_design: bool = False,
) -> dict[str, object]:
    manifest = minimal_manifest(source_id="nondefault-source")
    if knowledge:
        manifest["knowledge"] = {"root": "package-content/knowledge"}
    if study_design:
        manifest["study_design"] = {
            "document": "package-content/study-design.json"
        }
    return manifest


def test_study_module_exposes_only_package_driven_bundle_factory() -> None:
    assert hasattr(study_module, "build_study_bundle")
    assert not hasattr(study_module, "build_report_study_bundle")


def test_database_only_package_builds_minimal_study_bundle(tmp_path) -> None:
    studies_root = tmp_path / "runtime" / "studies"
    archive = create_package_archive(
        tmp_path / "source",
        manifest=minimal_manifest(source_id="nondefault-source"),
    )
    installed = install_study_archives([archive], studies_root)[0]

    bundle = build_study_bundle(installed)

    assert bundle.study_id == "example-study"
    assert bundle.label == "Example Study"
    assert bundle.package_version == "1.0.0"
    assert bundle.source_id == "nondefault-source"
    assert bundle.knowledge is None
    assert bundle.study_design is None
    assert bundle.catalog is not None
    assert bundle.db_rag_paths.duckdb_path == (
        installed.package_root / "database" / "study.duckdb"
    )
    assert (
        bundle.db_rag_paths.embedding_model
        == "OpenAI/text-embedding-3-large"
    )
    assert set(bundle.data_sources) == {"nondefault-source"}
    assert bundle.catalog.inspect_table("nondefault-source", "participants")


def test_database_package_binds_catalog_relationship_specification(tmp_path) -> None:
    package_root = create_package_root(tmp_path / "source")
    archive = create_package_archive_from_root(
        package_root,
        tmp_path / "example-study.tar.gz",
    )
    installed = install_study_archives(
        [archive],
        tmp_path / "runtime" / "studies",
    )[0]

    bundle = build_study_bundle(installed)
    source = bundle.data_sources["example-source"]

    assert source.relationship_spec.table_keys == {
        "participants": {"participant_key": "SUBJID"}
    }
    assert source.relationship_spec.relationships == ()


def test_knowledge_only_package_builds_knowledge_capability(tmp_path) -> None:
    studies_root = tmp_path / "runtime" / "studies"
    archive = create_package_archive(
        tmp_path / "source",
        manifest=_manifest_with_optional_capabilities(knowledge=True),
    )
    installed = install_study_archives([archive], studies_root)[0]

    bundle = build_study_bundle(installed)

    assert bundle.knowledge is not None
    assert bundle.study_design is None
    assert not callable(getattr(bundle.knowledge, "search", None))
    assert bundle.knowledge.search_lexical(
        "package-relative knowledge marker"
    )[0].title == "Package-relative knowledge marker"


def test_design_only_package_builds_study_design_capability(tmp_path) -> None:
    studies_root = tmp_path / "runtime" / "studies"
    archive = create_package_archive(
        tmp_path / "source",
        manifest=_manifest_with_optional_capabilities(study_design=True),
    )
    installed = install_study_archives([archive], studies_root)[0]

    bundle = build_study_bundle(installed)

    assert bundle.knowledge is None
    assert bundle.study_design is not None
    assert bundle.study_overview is None
    assert bundle.study_design.study_id == "example-study"
    assert bundle.study_design.source_path == (
        installed.package_root / "package-content" / "study-design.json"
    ).resolve()


def test_complete_package_builds_all_optional_capabilities(tmp_path) -> None:
    studies_root = tmp_path / "runtime" / "studies"
    archive = create_package_archive(
        tmp_path / "source",
        manifest=_manifest_with_optional_capabilities(
            knowledge=True,
            study_design=True,
        ),
    )
    installed = install_study_archives([archive], studies_root)[0]

    bundle = build_study_bundle(installed)

    assert bundle.knowledge is not None
    assert bundle.study_design is not None
    assert not callable(getattr(bundle.knowledge, "search", None))
    assert bundle.knowledge.search_lexical(
        "package-relative knowledge marker"
    )[0].title == "Package-relative knowledge marker"
    assert bundle.study_design.source_path == (
        installed.package_root / "package-content" / "study-design.json"
    ).resolve()


def test_v3_markdown_package_builds_document_design_capability(tmp_path) -> None:
    studies_root = tmp_path / "runtime" / "studies"
    archive = create_package_archive(
        tmp_path / "source",
        manifest=minimal_manifest(
            source_id="nondefault-source",
            format_version=3,
            study_design_format="markdown",
        ),
        study_design_documents={
            "overview.md": "# Overview\n\nAuthoritative Markdown.",
            "reference/visits.md": "# Visits\n\nRetrieval-only schedule.",
        },
    )
    installed = install_study_archives([archive], studies_root)[0]

    bundle = build_study_bundle(installed)

    assert bundle.study_design is not None
    assert bundle.study_overview is bundle.study_design
    assert bundle.study_design.render_context() == (
        "# Overview\n\nAuthoritative Markdown."
    )
    assert bundle.study_design.study_id == "example-study"
    assert bundle.study_design.package_version == "1.0.0"
