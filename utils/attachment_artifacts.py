from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException
from utils.user_storage import ThreadStorageScope, UserStorageLayout


_SUPPORTED_FORMATS: dict[str, tuple[str, str]] = {
    ".csv": ("tabular", "text/csv"),
    ".tsv": ("tabular", "text/tab-separated-values"),
    ".xls": ("tabular", "application/vnd.ms-excel"),
    ".xlsx": (
        "tabular",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    ".json": ("structured", "application/json"),
    ".xml": ("structured", "application/xml"),
    ".pdf": ("document", "application/pdf"),
    ".docx": (
        "document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".txt": ("document", "text/plain"),
    ".md": ("document", "text/markdown"),
    ".markdown": ("document", "text/markdown"),
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
}
_FORMAT_BY_EXTENSION = {
    ".markdown": "md",
    ".jpg": "jpeg",
    ".jpeg": "jpeg",
}
_OPAQUE_ID = re.compile(r"^attachment-[0-9a-f]{32}$")
_OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_GENERIC_MIME_TYPES = {"", "application/octet-stream"}
_MIME_ALIASES: dict[str, set[str]] = {
    ".csv": {"text/csv", "application/csv", "application/vnd.ms-excel"},
    ".tsv": {"text/tab-separated-values", "text/tsv"},
    ".xls": {"application/vnd.ms-excel"},
    ".xlsx": {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
    ".json": {"application/json", "text/json"},
    ".xml": {"application/xml", "text/xml"},
    ".pdf": {"application/pdf"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    },
    ".txt": {"text/plain"},
    ".md": {"text/markdown", "text/x-markdown"},
    ".markdown": {"text/markdown", "text/x-markdown"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
}
_MAX_OOXML_ENTRIES = 10_000
_MAX_OOXML_MEMBER_BYTES = 50 * 1024 * 1024
_MAX_OOXML_TOTAL_BYTES = 200 * 1024 * 1024
_MAX_OOXML_COMPRESSION_RATIO = 1_000


@dataclass(frozen=True)
class AttachmentLimits:
    max_bytes: int = 52_428_800
    max_files_per_message: int = 10
    max_message_bytes: int = 209_715_200
    max_document_pages: int = 200
    max_extracted_chars: int = 200_000
    max_image_pixels: int = 50_000_000
    staged_ttl_seconds: int = 86_400
    max_table_rows: int = 100_000
    max_table_columns: int = 1_000
    max_table_cells: int = 1_000_000

    @classmethod
    def from_env(cls) -> "AttachmentLimits":
        return cls(
            max_bytes=int(
                os.getenv("REPORT_AGENT_ATTACHMENT_MAX_BYTES", "52428800")
            ),
            max_files_per_message=int(
                os.getenv("REPORT_AGENT_ATTACHMENT_MAX_FILES_PER_MESSAGE", "10")
            ),
            max_message_bytes=int(
                os.getenv(
                    "REPORT_AGENT_ATTACHMENT_MAX_MESSAGE_BYTES",
                    "209715200",
                )
            ),
            max_document_pages=int(
                os.getenv("REPORT_AGENT_ATTACHMENT_MAX_DOCUMENT_PAGES", "200")
            ),
            max_extracted_chars=int(
                os.getenv("REPORT_AGENT_ATTACHMENT_MAX_EXTRACTED_CHARS", "200000")
            ),
            max_image_pixels=int(
                os.getenv("REPORT_AGENT_ATTACHMENT_MAX_IMAGE_PIXELS", "50000000")
            ),
            staged_ttl_seconds=int(
                os.getenv(
                    "REPORT_AGENT_ATTACHMENT_STAGED_TTL_SECONDS",
                    "86400",
                )
            ),
            max_table_rows=int(
                os.getenv("REPORT_AGENT_ATTACHMENT_MAX_TABLE_ROWS", "100000")
            ),
            max_table_columns=int(
                os.getenv("REPORT_AGENT_ATTACHMENT_MAX_TABLE_COLUMNS", "1000")
            ),
            max_table_cells=int(
                os.getenv("REPORT_AGENT_ATTACHMENT_MAX_TABLE_CELLS", "1000000")
            ),
        )


class AttachmentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def validate_message_attachment_limits(
    manifests: list[dict[str, Any]],
    limits: AttachmentLimits,
) -> None:
    if len(manifests) > limits.max_files_per_message:
        raise AttachmentError(
            "TOO_MANY_FILES",
            "message exceeds the configured attachment count limit",
        )
    total_bytes = sum(
        size
        for manifest in manifests
        if isinstance((size := manifest.get("byte_size")), int)
        and not isinstance(size, bool)
    )
    if total_bytes > limits.max_message_bytes:
        raise AttachmentError(
            "MESSAGE_ATTACHMENTS_TOO_LARGE",
            "message attachments exceed the configured total size limit",
        )


def _format_for_extension(extension: str) -> str:
    return _FORMAT_BY_EXTENSION.get(extension, extension.removeprefix("."))


def _decode_utf8(filename: str, content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AttachmentError(
            "CONTENT_TYPE_MISMATCH",
            f"{filename} is not valid UTF-8 text",
        ) from exc


def _validate_ooxml(filename: str, content: bytes, required_member: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > _MAX_OOXML_ENTRIES:
                raise AttachmentError(
                    "UNSAFE_ARCHIVE",
                    f"{filename} contains too many archive members",
                )
            total_uncompressed = 0
            for entry in entries:
                if entry.flag_bits & 0x1:
                    raise AttachmentError(
                        "UNSAFE_ARCHIVE",
                        f"{filename} contains an encrypted archive member",
                    )
                if entry.file_size > _MAX_OOXML_MEMBER_BYTES:
                    raise AttachmentError(
                        "UNSAFE_ARCHIVE",
                        f"{filename} contains an oversized archive member",
                    )
                total_uncompressed += entry.file_size
                if total_uncompressed > _MAX_OOXML_TOTAL_BYTES:
                    raise AttachmentError(
                        "UNSAFE_ARCHIVE",
                        f"{filename} expands beyond the archive safety limit",
                    )
                if (
                    entry.file_size > 0
                    and entry.compress_size > 0
                    and entry.file_size / entry.compress_size
                    > _MAX_OOXML_COMPRESSION_RATIO
                ):
                    raise AttachmentError(
                        "UNSAFE_ARCHIVE",
                        f"{filename} has an unsafe archive compression ratio",
                    )
            members = {entry.filename for entry in entries}
    except (OSError, zipfile.BadZipFile) as exc:
        raise AttachmentError(
            "CONTENT_TYPE_MISMATCH",
            f"{filename} is not a valid Office Open XML file",
        ) from exc
    if "[Content_Types].xml" not in members or required_member not in members:
        raise AttachmentError(
            "CONTENT_TYPE_MISMATCH",
            f"{filename} does not match its declared Office document type",
        )


def detect_supported_attachment(
    filename: str,
    declared_mime: str,
    content: bytes,
) -> dict[str, str]:
    clean_filename = Path(filename).name
    if (
        not filename
        or clean_filename != filename
        or clean_filename in {".", ".."}
        or any(character in filename for character in ("\x00", "\r", "\n"))
    ):
        raise AttachmentError("INVALID_FILENAME", "attachment filename is invalid")
    extension = Path(clean_filename).suffix.lower()
    supported = _SUPPORTED_FORMATS.get(extension)
    if supported is None:
        raise AttachmentError(
            "UNSUPPORTED_FORMAT",
            f"{clean_filename} has an unsupported file format",
        )
    if not content:
        raise AttachmentError("EMPTY_FILE", f"{clean_filename} is empty")

    kind, canonical_mime = supported
    file_format = _format_for_extension(extension)
    normalized_mime = declared_mime.split(";", 1)[0].strip().lower()
    if (
        normalized_mime not in _GENERIC_MIME_TYPES
        and normalized_mime not in _MIME_ALIASES[extension]
    ):
        raise AttachmentError(
            "CONTENT_TYPE_MISMATCH",
            f"{clean_filename} does not match its declared media type",
        )

    if file_format in {"csv", "tsv", "txt", "md"}:
        _decode_utf8(clean_filename, content)
    elif file_format == "json":
        text = _decode_utf8(clean_filename, content)
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise AttachmentError(
                "CONTENT_TYPE_MISMATCH",
                f"{clean_filename} is not valid JSON",
            ) from exc
    elif file_format == "xml":
        try:
            SafeElementTree.fromstring(content)
        except DefusedXmlException as exc:
            raise AttachmentError(
                "UNSAFE_XML",
                f"{clean_filename} contains a prohibited XML construct",
            ) from exc
        except SafeElementTree.ParseError as exc:
            raise AttachmentError(
                "CONTENT_TYPE_MISMATCH",
                f"{clean_filename} is not valid XML",
            ) from exc
    elif file_format == "pdf" and not content.startswith(b"%PDF-"):
        raise AttachmentError(
            "CONTENT_TYPE_MISMATCH",
            f"{clean_filename} is not a PDF file",
        )
    elif file_format == "png" and not content.startswith(_PNG_SIGNATURE):
        raise AttachmentError(
            "CONTENT_TYPE_MISMATCH",
            f"{clean_filename} is not a PNG file",
        )
    elif file_format == "jpeg" and not content.startswith(b"\xff\xd8\xff"):
        raise AttachmentError(
            "CONTENT_TYPE_MISMATCH",
            f"{clean_filename} is not a JPEG file",
        )
    elif file_format == "xls" and not content.startswith(_OLE_SIGNATURE):
        raise AttachmentError(
            "CONTENT_TYPE_MISMATCH",
            f"{clean_filename} is not an XLS file",
        )
    elif file_format == "xlsx":
        _validate_ooxml(clean_filename, content, "xl/workbook.xml")
    elif file_format == "docx":
        _validate_ooxml(clean_filename, content, "word/document.xml")

    return {
        "kind": kind,
        "format": file_format,
        "mime": canonical_mime,
        "declared_mime": declared_mime or "application/octet-stream",
    }


class LocalAttachmentStore:
    def __init__(
        self,
        runtime_root: str | Path,
        limits: AttachmentLimits | None = None,
    ) -> None:
        self.runtime_root = Path(runtime_root).expanduser().resolve()
        self.root = self.runtime_root / "attachments"
        self.root.mkdir(parents=True, exist_ok=True)
        self.limits = limits or AttachmentLimits.from_env()
        self.cleanup_expired_staged(
            older_than_seconds=self.limits.staged_ttl_seconds,
        )

    @staticmethod
    def owner_thread_key(owner_user_id: str, thread_id: str) -> str:
        if not isinstance(owner_user_id, str) or not owner_user_id.strip():
            raise AttachmentError("INVALID_OWNER", "owner_user_id is required")
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise AttachmentError("INVALID_THREAD_ID", "thread_id is required")
        return (
            f"{len(owner_user_id)}:{owner_user_id}"
            f"{len(thread_id)}:{thread_id}"
        )

    @staticmethod
    def _thread_component(thread_id: str) -> str:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise AttachmentError("INVALID_THREAD_ID", "thread_id is required")
        return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_attachment_id(attachment_id: str) -> None:
        if not isinstance(attachment_id, str) or not _OPAQUE_ID.fullmatch(
            attachment_id
        ):
            raise AttachmentError(
                "ATTACHMENT_NOT_FOUND",
                "attachment was not found for this thread",
            )

    def _scope_thread_id(self, scope: ThreadStorageScope | str) -> str:
        if isinstance(scope, ThreadStorageScope):
            return scope.thread_id
        return scope

    def _attachment_dir(
        self,
        scope: ThreadStorageScope | str,
        attachment_id: str,
    ) -> Path:
        self._validate_attachment_id(attachment_id)
        if isinstance(scope, ThreadStorageScope):
            return scope.attachments / attachment_id
        # A raw thread is accepted only for local compatibility. Prefer the
        # formal local-user scope for new writes, but retain reads of files
        # created by the pre-owner-layout attachment store.
        legacy = self.root / self._thread_component(scope) / attachment_id
        if legacy.exists():
            return legacy
        return UserStorageLayout(self.runtime_root).thread(
            "local-user", scope
        ).attachments / attachment_id

    def _manifest_path(
        self,
        scope: ThreadStorageScope | str,
        attachment_id: str,
    ) -> Path:
        return self._attachment_dir(scope, attachment_id) / "manifest.json"

    def _content_path(
        self,
        scope: ThreadStorageScope | str,
        attachment_id: str,
    ) -> Path:
        return self._attachment_dir(scope, attachment_id) / "content"

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent,
                prefix=f".{path.name}.",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @classmethod
    def _atomic_write_json(cls, path: Path, value: dict[str, Any]) -> None:
        cls._atomic_write(
            path,
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

    def stage(
        self,
        scope: ThreadStorageScope | str,
        filename: str,
        declared_mime: str,
        content: bytes,
    ) -> dict[str, Any]:
        if len(content) > self.limits.max_bytes:
            raise AttachmentError(
                "FILE_TOO_LARGE",
                f"{filename} exceeds the configured attachment size limit",
            )
        detected = detect_supported_attachment(filename, declared_mime, content)
        attachment_id = f"attachment-{uuid4().hex}"
        thread_id = self._scope_thread_id(scope)
        content_path = self._content_path(scope, attachment_id)
        storage_key = content_path.relative_to(self.runtime_root).as_posix()
        manifest: dict[str, Any] = {
            "id": attachment_id,
            "thread_id": thread_id,
            "origin": "uploaded",
            "kind": detected["kind"],
            "format": detected["format"],
            "filename": filename,
            "mime": detected["mime"],
            "declared_mime": detected["declared_mime"],
            "byte_size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "storage_key": storage_key,
            "status": "staged",
            "created_at": datetime.now(UTC).isoformat(),
        }
        if isinstance(scope, ThreadStorageScope):
            manifest["owner_user_id"] = scope.owner_user_id
        self._atomic_write(content_path, content)
        self._atomic_write_json(
            self._manifest_path(scope, attachment_id),
            manifest,
        )
        return dict(manifest)

    def require(
        self,
        scope: ThreadStorageScope | str,
        attachment_id: str,
    ) -> dict[str, Any]:
        thread_id = self._scope_thread_id(scope)
        manifest_path = self._manifest_path(scope, attachment_id)
        if (
            isinstance(scope, ThreadStorageScope)
            and scope.owner_user_id == "local-user"
            and not manifest_path.exists()
        ):
            manifest_path = self._manifest_path(scope.thread_id, attachment_id)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise AttachmentError(
                "ATTACHMENT_NOT_FOUND",
                "attachment was not found for this thread",
            ) from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("id") != attachment_id
            or manifest.get("thread_id") != thread_id
        ):
            raise AttachmentError(
                "ATTACHMENT_NOT_FOUND",
                "attachment was not found for this thread",
            )
        return dict(manifest)

    def read_bytes(self, scope: ThreadStorageScope | str, attachment_id: str) -> bytes:
        manifest = self.require(scope, attachment_id)
        try:
            content_path = self._content_path(scope, attachment_id)
            if (
                isinstance(scope, ThreadStorageScope)
                and scope.owner_user_id == "local-user"
                and not content_path.exists()
            ):
                content_path = self._content_path(scope.thread_id, attachment_id)
            content = content_path.read_bytes()
        except OSError as exc:
            raise AttachmentError(
                "ATTACHMENT_NOT_FOUND",
                "attachment content was not found for this thread",
            ) from exc
        if (
            len(content) != manifest.get("byte_size")
            or hashlib.sha256(content).hexdigest() != manifest.get("sha256")
        ):
            raise AttachmentError(
                "ATTACHMENT_CORRUPTED",
                "attachment content failed its integrity check",
            )
        return content

    def mark_available(
        self,
        scope: ThreadStorageScope | str,
        attachment_id: str,
    ) -> dict[str, Any]:
        manifest = self.require(scope, attachment_id)
        if manifest.get("status") != "staged":
            raise AttachmentError(
                "INVALID_ATTACHMENT_STATE",
                "attachment expected staged status before becoming available",
            )
        manifest["status"] = "available"
        manifest["available_at"] = datetime.now(UTC).isoformat()
        self._atomic_write_json(
            self._manifest_path(scope, attachment_id),
            manifest,
        )
        return dict(manifest)

    def begin_binding(
        self,
        scope: ThreadStorageScope | str,
        attachment_id: str,
    ) -> dict[str, Any]:
        manifest = self.require(scope, attachment_id)
        if manifest.get("status") != "staged":
            raise AttachmentError(
                "INVALID_ATTACHMENT_STATE",
                "attachment expected staged status before binding",
            )
        manifest["status"] = "binding"
        manifest["binding_started_at"] = datetime.now(UTC).isoformat()
        self._atomic_write_json(
            self._manifest_path(scope, attachment_id),
            manifest,
        )
        return dict(manifest)

    def commit_binding(
        self,
        scope: ThreadStorageScope | str,
        attachment_id: str,
    ) -> dict[str, Any]:
        manifest = self.require(scope, attachment_id)
        if manifest.get("status") != "binding":
            raise AttachmentError(
                "INVALID_ATTACHMENT_STATE",
                "only binding attachments can become available",
            )
        manifest["status"] = "available"
        manifest.pop("binding_started_at", None)
        manifest["available_at"] = datetime.now(UTC).isoformat()
        self._atomic_write_json(
            self._manifest_path(scope, attachment_id),
            manifest,
        )
        return dict(manifest)

    def record_inspection(
        self,
        scope: ThreadStorageScope | str,
        attachment_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        manifest = self.require(scope, attachment_id)
        if manifest.get("status") not in {"binding", "available"}:
            raise AttachmentError(
                "INVALID_ATTACHMENT_STATE",
                "only bound attachments can record inspection metadata",
            )
        manifest["inspection"] = json.loads(json.dumps(profile))
        self._atomic_write_json(
            self._manifest_path(scope, attachment_id),
            manifest,
        )
        return dict(manifest)

    def rollback_binding(
        self,
        scope: ThreadStorageScope | str,
        attachment_id: str,
    ) -> dict[str, Any]:
        manifest = self.require(scope, attachment_id)
        if manifest.get("status") != "binding":
            raise AttachmentError(
                "INVALID_ATTACHMENT_STATE",
                "only binding attachments can be rolled back",
            )
        manifest["status"] = "staged"
        manifest.pop("binding_started_at", None)
        self._atomic_write_json(
            self._manifest_path(scope, attachment_id),
            manifest,
        )
        return dict(manifest)

    def rollback_available(
        self,
        thread_id: str,
        attachment_id: str,
    ) -> dict[str, Any]:
        """Compensate a failed message-binding transaction."""

        manifest = self.require(thread_id, attachment_id)
        if manifest.get("status") != "available":
            raise AttachmentError(
                "INVALID_ATTACHMENT_STATE",
                "only available attachments can be rolled back",
            )
        manifest["status"] = "staged"
        manifest.pop("available_at", None)
        self._atomic_write_json(
            self._manifest_path(thread_id, attachment_id),
            manifest,
        )
        return dict(manifest)

    def discard_staged(self, scope: ThreadStorageScope | str, attachment_id: str) -> None:
        manifest = self.require(scope, attachment_id)
        if manifest.get("status") != "staged":
            raise AttachmentError(
                "INVALID_ATTACHMENT_STATE",
                "only staged attachments can be discarded",
            )
        directory = self._attachment_dir(scope, attachment_id)
        self._content_path(scope, attachment_id).unlink(missing_ok=True)
        self._manifest_path(scope, attachment_id).unlink(missing_ok=True)
        try:
            directory.rmdir()
        except OSError:
            pass

    def delete_thread(self, scope: ThreadStorageScope | str) -> None:
        if isinstance(scope, ThreadStorageScope):
            shutil.rmtree(scope.root, ignore_errors=True)
            if scope.owner_user_id == "local-user":
                shutil.rmtree(
                    self.root / self._thread_component(scope.thread_id),
                    ignore_errors=True,
                )
            return
        shutil.rmtree(
            self.root / self._thread_component(scope),
            ignore_errors=True,
        )
        shutil.rmtree(
            UserStorageLayout(self.runtime_root).thread(
                "local-user", scope
            ).root,
            ignore_errors=True,
        )

    def cleanup_expired_staged(self, *, older_than_seconds: int) -> int:
        if older_than_seconds < 0:
            raise ValueError("older_than_seconds must be non-negative")
        cutoff = datetime.now(UTC).timestamp() - older_than_seconds
        removed = 0
        manifest_paths = list(self.root.glob("*/attachment-*/manifest.json"))
        manifest_paths.extend(
            self.runtime_root.glob(
                "users/*/threads/*/attachments/attachment-*/manifest.json"
            )
        )
        for manifest_path in manifest_paths:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                status = manifest.get("status")
                lifecycle_timestamp = (
                    manifest.get("binding_started_at")
                    if status == "binding"
                    else manifest.get("created_at")
                )
                created_at = datetime.fromisoformat(
                    str(lifecycle_timestamp or "")
                )
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                attachment_id = str(manifest.get("id") or "")
                thread_id = str(manifest.get("thread_id") or "")
                owner_user_id = str(manifest.get("owner_user_id") or "").strip()
                storage_scope: ThreadStorageScope | str = (
                    UserStorageLayout(self.runtime_root).thread(
                        owner_user_id,
                        thread_id,
                    )
                    if owner_user_id
                    else thread_id
                )
                expected_path = self._manifest_path(storage_scope, attachment_id)
            except (
                AttachmentError,
                FileNotFoundError,
                OSError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                continue
            if (
                status not in {"staged", "binding"}
                or created_at.timestamp() >= cutoff
                or expected_path != manifest_path
            ):
                continue
            if status == "binding":
                self.rollback_binding(storage_scope, attachment_id)
                removed += 1
                continue
            self.discard_staged(storage_scope, attachment_id)
            removed += 1
        return removed
