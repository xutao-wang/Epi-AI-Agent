from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import duckdb

from epi_agent.studies import (
    StudyBundle,
    StudySourceUnavailableError,
)

from study_package.installer import InstalledStudy
from study_package.manifest import LegacyStudyDesignManifest

from .config import resolve_db_rag_runtime_paths
from .catalog import SchemaCatalog, load_full_schema_catalog
from .local_knowledge import LocalPublicationKnowledge
from .study_design import LocalStudyDesign
from .study_design_documents import MarkdownStudyDesign
from .relationships import RelationshipInventory, build_relationship_inventory


@dataclass(frozen=True)
class DuckDbStudyDataSource:
    path: Path

    @cached_property
    def _relationship_inventory(self) -> RelationshipInventory:
        if not self.path.is_file():
            raise StudySourceUnavailableError(
                "The configured DuckDB study source is unavailable."
            )
        try:
            return build_relationship_inventory(self.path)
        except duckdb.Error as error:
            raise StudySourceUnavailableError(
                "The configured DuckDB study source could not be opened."
            ) from error

    def relationship_inventory(self) -> RelationshipInventory:
        return self._relationship_inventory


def build_study_bundle(package: InstalledStudy) -> StudyBundle:
    manifest = package.manifest
    paths = resolve_db_rag_runtime_paths(package.package_root, manifest)
    knowledge = (
        LocalPublicationKnowledge.from_root(
            package.package_root / manifest.knowledge.root
        )
        if manifest.knowledge is not None
        else None
    )
    if isinstance(manifest.study_design, LegacyStudyDesignManifest):
        study_design = LocalStudyDesign.from_path(
            package.package_root / manifest.study_design.document
        )
    elif manifest.study_design is not None:
        study_design = MarkdownStudyDesign.from_package(
            package.package_root,
            manifest,
        )
    else:
        study_design = None
    return StudyBundle(
        study_id=manifest.study_id,
        label=manifest.label,
        knowledge=knowledge,
        catalog=SchemaCatalog(
            load_full_schema_catalog(paths.catalog_path),
            default_source_id=manifest.database.source_id,
        ),
        data_sources={
            manifest.database.source_id: DuckDbStudyDataSource(paths.duckdb_path)
        },
        study_design=study_design,
        package_version=manifest.package_version,
        description=manifest.description,
        source_id=manifest.database.source_id,
        db_rag_paths=paths,
    )
