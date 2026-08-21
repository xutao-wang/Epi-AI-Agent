from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tarfile
import uuid

import chromadb
import duckdb

from db_rag.catalog_relationships import (
    CATALOG_VERSION,
    parse_catalog_relationships,
)
from db_rag.local_knowledge import LocalPublicationKnowledge
from db_rag.relationships import build_relationship_inventory
from db_rag.study_design import LocalStudyDesign

from .manifest import (
    LegacyStudyDesignManifest,
    MarkdownStudyDesignManifest,
    StudyPackageManifest,
    load_installed_manifest,
    resolve_package_path,
)
from .registry import StudyRegistryFile, load_registry, package_root, write_registry


@dataclass(frozen=True)
class PackageWarning:
    path: str
    message: str


@dataclass(frozen=True)
class StagedStudy:
    archive_path: Path
    extracted_root: Path
    archive_sha256: str
    manifest: StudyPackageManifest
    stage_root: Path
    warnings: tuple[PackageWarning, ...] = ()


@dataclass(frozen=True)
class InstalledStudy:
    study_id: str
    package_version: str
    package_root: Path
    archive_sha256: str
    manifest: StudyPackageManifest
    warnings: tuple[PackageWarning, ...] = ()


MAX_OVERVIEW_BYTES = 32 * 1024


def _copy_archive(source: Path, destination: Path) -> str:
    digest = sha256()
    with source.open("rb") as source_file, destination.open("xb") as destination_file:
        while chunk := source_file.read(1024 * 1024):
            digest.update(chunk)
            destination_file.write(chunk)
    return digest.hexdigest()


def _validate_member(member: tarfile.TarInfo, seen: set[str]) -> None:
    name = member.name
    parts = name.split("/")
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or any(part in {"", ".", ".."} for part in parts)
        or name in seen
        or member.issym()
        or member.islnk()
        or not (member.isfile() or member.isdir())
    ):
        raise ValueError(f"unsafe archive member: {name!r}")
    seen.add(name)


def _extract_archive(archive_path: Path, extracted_root: Path) -> None:
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            seen: set[str] = set()
            for member in members:
                _validate_member(member, seen)
            archive.extractall(extracted_root, members=members, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise ValueError(f"Cannot read study archive: {archive_path}") from error


def _validate_duckdb(path: Path) -> None:
    try:
        with duckdb.connect(str(path), read_only=True) as connection:
            tables = connection.execute("SHOW TABLES").fetchall()
    except duckdb.Error as error:
        raise ValueError(f"database.duckdb is not a readable DuckDB database: {path}") from error
    if not tables:
        raise ValueError("database.duckdb contains no tables")


def _validate_catalog(path: Path) -> dict[str, object]:
    try:
        catalog = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"database.catalog is not valid JSON: {path}") from error
    if (
        not isinstance(catalog, dict)
        or catalog.get("catalog_version") != CATALOG_VERSION
    ):
        raise ValueError(
            f"database.catalog must use catalog_version {CATALOG_VERSION}"
        )
    if not isinstance(catalog.get("tables"), list) or not catalog["tables"]:
        raise ValueError("database.catalog contains no tables")
    if not isinstance(catalog.get("columns"), list) or not catalog["columns"]:
        raise ValueError("database.catalog contains no columns")
    return catalog


def _validate_index(path: Path) -> None:
    try:
        client = chromadb.PersistentClient(path=str(path))
        for collection_name in ("table_summaries", "column_chunks"):
            collection = client.get_collection(collection_name)
            if collection.count() < 1:
                raise ValueError(f"database.index collection is empty: {collection_name}")
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"database.index is not a readable Chroma store: {path}") from error


def _validate_knowledge(path: Path) -> None:
    try:
        LocalPublicationKnowledge.from_root(path)
    except ValueError as error:
        raise ValueError(f"knowledge.root is not valid publication knowledge: {path}") from error


def _validate_legacy_study_design(
    path: Path,
    manifest: StudyPackageManifest,
) -> None:
    try:
        design = LocalStudyDesign.from_path(path)
    except ValueError as error:
        raise ValueError(f"study_design.document is not a valid study design: {path}") from error
    if (
        design.study_id != manifest.study_id
        or design.label != manifest.label
    ):
        raise ValueError(
            "study_design.document identity does not match the study package manifest"
        )


def _overview_error(code: str, path: Path, reason: str) -> ValueError:
    return ValueError(
        f'{code}: Package declares study_design, but "{path}" {reason}.\n\n'
        "Fix one of:\n"
        "1. Remove the study_design declaration and study-design folder to install "
        "without study-design capability.\n"
        "2. Add or fix a nonempty UTF-8 study-design/overview.md no larger than "
        "32 KiB; shorten it or move details to optional Markdown documents."
    )


def _decode_design_markdown(
    path: Path,
    relative_path: str,
    *,
    overview: bool,
) -> bytes:
    payload = path.read_bytes()
    if overview and len(payload) > MAX_OVERVIEW_BYTES:
        raise _overview_error(
            "STUDY_DESIGN_OVERVIEW_TOO_LARGE",
            path,
            f"is {len(payload)} bytes; the limit is {MAX_OVERVIEW_BYTES} bytes",
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        if overview:
            raise _overview_error(
                "STUDY_DESIGN_OVERVIEW_INVALID_UTF8",
                path,
                "is not valid UTF-8",
            ) from error
        raise ValueError(
            "STUDY_DESIGN_MARKDOWN_INVALID_UTF8: "
            f'"{relative_path}" is not valid UTF-8.'
        ) from error
    if not text.strip():
        if overview:
            raise _overview_error(
                "STUDY_DESIGN_OVERVIEW_EMPTY",
                path,
                "is empty",
            )
        raise ValueError(
            f'STUDY_DESIGN_MARKDOWN_EMPTY: "{relative_path}" is empty.'
        )
    return payload


def _indexed_design_sources(index_path: Path) -> set[tuple[str, str]]:
    client = chromadb.PersistentClient(path=str(index_path))
    try:
        collection = client.get_collection("study_knowledge")
    except Exception:
        return set()
    rows = collection.get(
        where={"source_kind": "study_design"},
        include=["metadatas"],
    )
    sources: set[tuple[str, str]] = set()
    for metadata in rows.get("metadatas") or []:
        source_path = str(metadata.get("source_path") or "").strip()
        source_sha256 = str(metadata.get("source_sha256") or "").strip()
        if not source_path or len(source_sha256) != 64:
            raise ValueError(
                "database.index study-design source provenance is incomplete"
            )
        sources.add((source_path, source_sha256))
    return sources


def _validate_markdown_study_design(
    root: Path,
    index_path: Path,
    declaration: MarkdownStudyDesignManifest,
) -> tuple[PackageWarning, ...]:
    overview_path = root / declaration.overview
    if (
        not overview_path.exists()
        or overview_path.is_symlink()
        or not overview_path.is_file()
    ):
        raise _overview_error(
            "STUDY_DESIGN_OVERVIEW_MISSING",
            overview_path,
            "is missing or is not a regular file",
        )

    packaged_sources: set[tuple[str, str]] = set()
    warnings: list[PackageWarning] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative_path = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ValueError(
                f'STUDY_DESIGN_UNSAFE_ENTRY: "{relative_path}" may not be a symlink.'
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(
                f'STUDY_DESIGN_UNSAFE_ENTRY: "{relative_path}" is not a regular file.'
            )
        if path.suffix != ".md":
            warnings.append(
                PackageWarning(
                    path=relative_path,
                    message=(
                        f"{relative_path} is not consumed by study-design indexing"
                    ),
                )
            )
            continue
        payload = _decode_design_markdown(
            path,
            relative_path,
            overview=relative_path == declaration.overview,
        )
        packaged_sources.add((relative_path, sha256(payload).hexdigest()))

    indexed_sources = _indexed_design_sources(index_path)
    if packaged_sources != indexed_sources:
        raise ValueError(
            "Packaged Markdown and indexed study-design path/hash sets differ: "
            f"missing from index={sorted(packaged_sources - indexed_sources)}; "
            f"missing from package={sorted(indexed_sources - packaged_sources)}."
        )
    return tuple(warnings)


def validate_staged_package(
    package_root: Path,
    manifest: StudyPackageManifest,
) -> tuple[PackageWarning, ...]:
    declared = manifest.declared_paths()
    resolved: dict[str, Path] = {}
    for field, (relative, kind) in declared.items():
        path = resolve_package_path(package_root, relative, field)
        if not path.exists() or path.is_symlink():
            raise ValueError(f"{field} is missing from study package")
        if kind == "file" and not path.is_file():
            raise ValueError(f"{field} must be a file")
        if kind == "directory" and not path.is_dir():
            raise ValueError(f"{field} must be a directory")
        resolved[field] = path

    _validate_duckdb(resolved["database.duckdb"])
    catalog = _validate_catalog(resolved["database.catalog"])
    relationship_spec = parse_catalog_relationships(catalog)
    inventory = build_relationship_inventory(
        resolved["database.duckdb"],
        relationship_spec=relationship_spec,
    )
    inventory.validate_declared_relationships()
    _validate_index(resolved["database.index"])
    if manifest.knowledge is not None:
        _validate_knowledge(resolved["knowledge.root"])
    if manifest.study_design is not None:
        if isinstance(manifest.study_design, LegacyStudyDesignManifest):
            _validate_legacy_study_design(
                resolved["study_design.document"],
                manifest,
            )
        else:
            return _validate_markdown_study_design(
                resolved["study_design.root"],
                resolved["database.index"],
                manifest.study_design,
            )
    return ()


def stage_study_archive(archive: Path, studies_root: Path) -> StagedStudy:
    source = Path(archive)
    if (
        source.is_symlink()
        or not source.is_file()
        or source.suffixes[-2:] != [".tar", ".gz"]
    ):
        raise ValueError(f"Study archive must be a local .tar.gz file: {source}")

    staging_root = Path(studies_root) / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    stage_root = staging_root / uuid.uuid4().hex
    stage_root.mkdir()
    copied_archive = stage_root / "study.tar.gz"
    extracted_root = stage_root / "package"
    extracted_root.mkdir()
    try:
        archive_sha256 = _copy_archive(source, copied_archive)
        _extract_archive(copied_archive, extracted_root)
        manifest = load_installed_manifest(extracted_root)
        package_warnings = validate_staged_package(extracted_root, manifest)
        return StagedStudy(
            archive_path=copied_archive,
            extracted_root=extracted_root,
            archive_sha256=archive_sha256,
            manifest=manifest,
            stage_root=stage_root,
            warnings=package_warnings,
        )
    except Exception:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def _installed_record_path(package_root: Path) -> Path:
    return Path(package_root) / ".installed.json"


def _write_installed_record(staged: StagedStudy) -> None:
    _installed_record_path(staged.extracted_root).write_text(
        json.dumps(
            {
                "archive_sha256": staged.archive_sha256,
                "study_id": staged.manifest.study_id,
                "package_version": staged.manifest.package_version,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def load_installed_study(package_root_path: Path) -> InstalledStudy:
    package_root_path = Path(package_root_path)
    manifest = load_installed_manifest(package_root_path)
    package_warnings = validate_staged_package(package_root_path, manifest)
    try:
        record = json.loads(_installed_record_path(package_root_path).read_text(encoding="utf-8"))
        archive_sha256 = str(record["archive_sha256"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid installed study record: {package_root_path}") from error
    if (
        record.get("study_id") != manifest.study_id
        or record.get("package_version") != manifest.package_version
        or len(archive_sha256) != 64
    ):
        raise ValueError(f"Installed study record does not match manifest: {package_root_path}")
    return InstalledStudy(
        study_id=manifest.study_id,
        package_version=manifest.package_version,
        package_root=package_root_path,
        archive_sha256=archive_sha256,
        manifest=manifest,
        warnings=package_warnings,
    )


def _cleanup_staged(staged: StagedStudy) -> None:
    shutil.rmtree(staged.stage_root, ignore_errors=True)


def install_study_archives(
    archives: Sequence[Path],
    studies_root: Path,
    *,
    expected_study_id: str | None = None,
    expected_package_version: str | None = None,
) -> tuple[InstalledStudy, ...]:
    if not archives:
        raise ValueError("At least one study archive is required")
    if (expected_study_id is None) != (expected_package_version is None):
        raise ValueError(
            "Expected study ID and package version must be provided together"
        )

    staged_studies: list[StagedStudy] = []
    try:
        for archive in archives:
            staged_studies.append(stage_study_archive(archive, studies_root))

        if expected_study_id is not None and expected_package_version is not None:
            if len(staged_studies) != 1:
                raise ValueError(
                    "Expected study identity can only guard one archive at a time"
                )
            actual_manifest = staged_studies[0].manifest
            if (
                actual_manifest.study_id != expected_study_id
                or actual_manifest.package_version != expected_package_version
            ):
                raise ValueError(
                    "Study archive identity "
                    f"{actual_manifest.study_id}@{actual_manifest.package_version} "
                    "does not match expected identity "
                    f"{expected_study_id}@{expected_package_version}"
                )

        seen_study_ids: set[str] = set()
        for staged in staged_studies:
            study_id = staged.manifest.study_id
            if study_id in seen_study_ids:
                raise ValueError(f"Batch contains duplicate study_id: {study_id}")
            seen_study_ids.add(study_id)

        current_registry = load_registry(studies_root)
        active = dict(current_registry.active)
        installed: list[InstalledStudy | None] = [None] * len(staged_studies)
        promotions: list[tuple[int, StagedStudy, Path]] = []
        for index, staged in enumerate(staged_studies):
            manifest = staged.manifest
            destination = package_root(
                studies_root,
                manifest.study_id,
                manifest.package_version,
            )
            if destination.exists():
                existing = load_installed_study(destination)
                if existing.archive_sha256 != staged.archive_sha256:
                    raise ValueError(
                        "Published study package versions are immutable: "
                        f"{manifest.study_id}@{manifest.package_version}"
                    )
                installed[index] = existing
            else:
                promotions.append((index, staged, destination))
            active[manifest.study_id] = manifest.package_version

        for _index, staged, _destination in promotions:
            _write_installed_record(staged)
        promoted_destinations: list[Path] = []
        try:
            for index, staged, destination in promotions:
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError(f"Study package destination already exists: {destination}")
                promoted_destinations.append(destination)
                shutil.move(str(staged.extracted_root), str(destination))
                installed[index] = InstalledStudy(
                    study_id=staged.manifest.study_id,
                    package_version=staged.manifest.package_version,
                    package_root=destination,
                    archive_sha256=staged.archive_sha256,
                    manifest=staged.manifest,
                    warnings=staged.warnings,
                )

            write_registry(
                studies_root,
                StudyRegistryFile(active=active),
            )
        except Exception:
            for destination in reversed(promoted_destinations):
                shutil.rmtree(destination, ignore_errors=True)
            raise
        return tuple(item for item in installed if item is not None)
    finally:
        for staged in staged_studies:
            _cleanup_staged(staged)


def activate_study_version(
    study_id: str,
    package_version: str,
    studies_root: Path,
) -> InstalledStudy:
    installed = load_installed_study(package_root(studies_root, study_id, package_version))
    validate_staged_package(installed.package_root, installed.manifest)
    registry = load_registry(studies_root)
    active = dict(registry.active)
    active[study_id] = package_version
    write_registry(studies_root, StudyRegistryFile(active=active))
    return installed
