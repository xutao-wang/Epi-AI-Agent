"""Optional upload plug: a generic, opt-in hook on the paperclip upload.

When ``REPORT_AGENT_UPLOAD_PLUG_URL`` is empty (the default) nothing changes.
When it is set, the browser hands a chosen ZIP to the plug's page (shown in a
modal), the plug prepares the files, and the browser gets back only an opaque
handle. This module then pulls the prepared study ZIP from the plug, server to
server, with a shared secret, unzips it in memory and returns its files in the
same shape the plain upload route stages.

The app does not look inside the prepared files beyond checking the archive,
their size, hash and type; what the plug does to them is the plug's business.
Nothing pulled from the plug is written to disk or logged here.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import zipfile
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

import httpx

CONTRACT_VERSION = "3"

PLUG_URL_ENV = "REPORT_AGENT_UPLOAD_PLUG_URL"
PLUG_SECRET_ENV = "REPORT_AGENT_UPLOAD_PLUG_SECRET"
PLUG_TIMEOUT_ENV = "REPORT_AGENT_UPLOAD_PLUG_TIMEOUT_SECONDS"

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MEMBER_PATH_RE = re.compile(
    r"^(dataset|dictionary|forms|metadata)/(dataset|dictionary|form|metadata)_[1-9][0-9]{0,3}"
    r"\.(csv|tsv|xlsx|txt|md|json|xml|docx)$"
)
# Each folder holds one role only: dataset/dataset_1.csv, forms/form_1.json.
_FOLDER_ROLES = {
    "dataset": "dataset",
    "dictionary": "dictionary",
    "forms": "form",
    "metadata": "metadata",
}
# Upper bound on the plug's ZIP (each entry stored or deflated no larger than its file;
# no extra fields, comments, data descriptors or ZIP64 records): the files, the end
# record, and per entry a local header (30) and a central entry (46) that each hold the path.
# The media type the checker writes for each member extension; anything else is refused.
_MEMBER_MIME = {
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain",
    "md": "text/markdown",
    "json": "application/json",
    "xml": "application/xml",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_ZIP_END_RECORD_BYTES = 22
_ZIP_ENTRY_BYTES = 30 + 46
_ZIP_METHODS = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_ZIP_ENCRYPTED_FLAG = 0x1
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_MIN_SECRET_CHARS = 32
_MAX_MANIFEST_BYTES = 64 * 1024
# Contract 3: the plug's per-upload report, the archive's last entry. Codes, counts, neutral
# paths and hashes only. It is checked here and never staged as a chat attachment.
_REPORT_PATH = "report/report.json"
_REPORT_MIME = "application/json"
_MAX_REPORT_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 1024 * 1024


class UploadPlugError(Exception):
    """A plug failure. ``code`` is a fixed enum; the message holds no file data."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class UploadPlugSettings:
    url: str = ""
    secret: str = field(default="", repr=False)
    timeout_seconds: float = 30.0
    transport: httpx.BaseTransport | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def origin(self) -> str:
        """The origin a browser reports for the plug's page: lowercase host, default port dropped."""
        parts = urlsplit(self.url)
        host = (parts.hostname or "").lower()
        if ":" in host:
            host = f"[{host}]"
        default = {"http": 80, "https": 443}.get(parts.scheme)
        port = f":{parts.port}" if parts.port and parts.port != default else ""
        return f"{parts.scheme}://{host}{port}"

    @classmethod
    def from_env(cls) -> "UploadPlugSettings":
        settings = cls(
            url=(os.getenv(PLUG_URL_ENV) or "").strip().rstrip("/"),
            secret=(os.getenv(PLUG_SECRET_ENV) or "").strip(),
            timeout_seconds=float(os.getenv(PLUG_TIMEOUT_ENV) or "30"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        """Refuse a half-configured plug at startup (fail closed)."""
        if not self.url:
            return
        parts = urlsplit(self.url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError(f"{PLUG_URL_ENV} must be an http(s) URL")
        if parts.scheme == "http" and parts.hostname not in _LOCAL_HOSTS:
            raise ValueError(f"{PLUG_URL_ENV} must use https unless it is on this computer")
        if parts.query or parts.fragment or parts.username or parts.password:
            raise ValueError(f"{PLUG_URL_ENV} must not carry a query, fragment or credentials")
        if parts.path not in ("", "/"):
            raise ValueError(f"{PLUG_URL_ENV} must be the plug's origin only, with no path")
        try:
            parts.port
        except ValueError as exc:
            raise ValueError(f"{PLUG_URL_ENV} has an invalid port") from exc
        if len(self.secret) < _MIN_SECRET_CHARS:
            raise ValueError(
                f"{PLUG_SECRET_ENV} must be set (at least {_MIN_SECRET_CHARS} characters) "
                f"when {PLUG_URL_ENV} is set"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(f"{PLUG_TIMEOUT_ENV} must be positive")


def quiet_http_logs() -> None:
    """httpx logs every request URL at INFO, and delivery URLs carry the handle and thread id.
    Called only when a plug is configured, so a plain app keeps its logging as before."""
    logging.getLogger("httpx").setLevel(logging.WARNING)


def validate_handle(handle: str) -> str:
    if not isinstance(handle, str) or not _HANDLE_RE.fullmatch(handle):
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid handle")
    return handle


def _safe_member_name(name: object) -> str:
    if not isinstance(name, str) or not name or len(name) > 255:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid file name")
    if (
        "/" in name
        or "\\" in name
        or name in {".", ".."}
        or name.startswith(".")
        or any(ord(ch) < 32 for ch in name)
    ):
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid file name")
    return name


def _member_basename(path: object) -> str:
    """Check a manifest path and return the file name the app stages it under."""
    if not isinstance(path, str) or not _MEMBER_PATH_RE.fullmatch(path):
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid file path")
    folder, name = path.split("/", 1)
    if name.rsplit("_", 1)[0] != _FOLDER_ROLES[folder]:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid file path")
    return _safe_member_name(name)


def _unique_names(names: list[str]) -> None:
    seen: set[str] = set()
    for name in names:
        if name.lower() in seen:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned duplicate file names")
        seen.add(name.lower())


def fetch_delivery(
    settings: UploadPlugSettings,
    handle: str,
    thread_id: str,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> list[tuple[str, str, bytes]]:
    """Pull the prepared study ZIP for ``handle``, verify it and unzip it in memory.

    Returns ``(filename, mime, content)`` tuples, the same shape the plain
    upload route passes to staging; ``filename`` is the member's base name.
    Raises ``UploadPlugError`` on any problem; nothing is returned unless the
    archive and every file in it passed every check.
    """
    if not settings.enabled:
        raise UploadPlugError("PLUG_UNREACHABLE", "no upload plug is configured")
    validate_handle(handle)
    base = f"{settings.url.rstrip('/')}/v1/deliveries/{quote(handle, safe='')}"
    headers = {
        "Authorization": f"Bearer {settings.secret}",
        "X-Gate-Contract": CONTRACT_VERSION,
        # No compressed transfer: the read caps below count the bytes as sent.
        "Accept-Encoding": "identity",
    }
    params = {"thread_id": thread_id}
    try:
        with httpx.Client(
            timeout=settings.timeout_seconds,
            follow_redirects=False,
            transport=settings.transport,
            trust_env=False,
        ) as client:
            manifest = _get_manifest(client, base, headers, params)
            # Every check on the manifest runs before the archive is fetched, so an
            # over-cap or malformed delivery is refused without spending it.
            archive_size, archive_digest, members, report = _check_manifest(
                manifest,
                max_files=max_files,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            )
            archive = _get_archive(
                client, f"{base}/archive", headers, params, archive_size, archive_digest
            )
    except httpx.HTTPError as exc:
        raise UploadPlugError("PLUG_UNREACHABLE", "the upload plug could not be reached") from None
    return _unzip_members(archive, members, report)


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 409:
        raise UploadPlugError("CONTRACT_MISMATCH", "the upload plug uses a different contract version")
    if response.status_code in {401, 403}:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug refused this app's credentials")
    if response.status_code in {502, 503, 504}:
        raise UploadPlugError("PLUG_UNREACHABLE", "the upload plug could not be reached")
    if response.status_code != 200:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug did not release these files")


def _get_manifest(client: httpx.Client, url: str, headers: dict, params: dict) -> dict:
    with client.stream("GET", url, headers=headers, params=params) as response:
        _raise_for_status(response)
        body = _read_capped(response, _MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(body)
    except ValueError as exc:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an unreadable manifest") from None
    if not isinstance(manifest, dict) or manifest.get("contract") != CONTRACT_VERSION:
        raise UploadPlugError("CONTRACT_MISMATCH", "the upload plug uses a different contract version")
    if set(manifest) != {"contract", "archive", "members", "report"}:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid manifest")
    return manifest


def _check_manifest(
    manifest: dict,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> tuple[int, str, list[tuple[str, str, str, int, str]], tuple[int, str]]:
    """Return the archive size and hash, ``(path, name, mime, size, sha256)`` per member and the
    report's ``(size, sha256)``."""
    entries = manifest.get("members")
    if not isinstance(entries, list) or not entries:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned no files")
    if len(entries) > max_files:
        raise UploadPlugError("TOO_MANY_FILES", "the prepared upload exceeds the attachment count limit")
    checked: list[tuple[str, str, str, int, str]] = []
    paths: set[str] = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "mime", "size", "sha256"}:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid manifest")
        path = entry.get("path")
        name = _member_basename(path)
        if path in paths:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned duplicate file paths")
        paths.add(path)
        size = entry.get("size")
        digest = entry.get("sha256")
        mime = entry.get("mime", "")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid file size")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid checksum")
        if mime != _MEMBER_MIME.get(name.rsplit(".", 1)[-1]):
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid media type")
        if size > max_file_bytes:
            raise UploadPlugError("FILE_TOO_LARGE", "a prepared file exceeds the attachment size limit")
        total += size
        if total > max_total_bytes:
            raise UploadPlugError(
                "MESSAGE_ATTACHMENTS_TOO_LARGE",
                "the prepared upload exceeds the total attachment size limit",
            )
        checked.append((path, name, mime, size, digest))
    _unique_names([name for _path, name, _mime, _size, _digest in checked])

    report = manifest.get("report")
    if not isinstance(report, dict) or set(report) != {"path", "mime", "size", "sha256"}:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid manifest")
    report_size = report.get("size")
    report_digest = report.get("sha256")
    if (
        report.get("path") != _REPORT_PATH
        or report.get("mime") != _REPORT_MIME
        or isinstance(report_size, bool)
        or not isinstance(report_size, int)
        or not 0 < report_size <= _MAX_REPORT_BYTES
        or not isinstance(report_digest, str)
        or not _SHA256_RE.fullmatch(report_digest)
    ):
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid report entry")

    archive = manifest.get("archive")
    if not isinstance(archive, dict) or set(archive) != {"size", "sha256"}:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid manifest")
    archive_size = archive.get("size")
    archive_digest = archive.get("sha256")
    if isinstance(archive_size, bool) or not isinstance(archive_size, int) or archive_size < 0:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid archive size")
    if not isinstance(archive_digest, str) or not _SHA256_RE.fullmatch(archive_digest):
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid checksum")
    bound = total + _ZIP_END_RECORD_BYTES + sum(
        _ZIP_ENTRY_BYTES + 2 * len(path) for path, _name, _mime, _size, _digest in checked
    ) + report_size + _ZIP_ENTRY_BYTES + 2 * len(_REPORT_PATH)
    if archive_size > bound:
        raise UploadPlugError("PLUG_REJECTED", "the prepared archive is larger than its files allow")
    return archive_size, archive_digest, checked, (report_size, report_digest)


def _get_archive(
    client: httpx.Client,
    url: str,
    headers: dict,
    params: dict,
    size: int,
    digest: str,
) -> bytes:
    with client.stream("GET", url, headers=headers, params=params) as response:
        _raise_for_status(response)
        content = _read_capped(response, size)
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise UploadPlugError("PLUG_REJECTED", "the prepared archive did not match its checksum")
    return content


def _unzip_members(
    archive: bytes,
    members: list[tuple[str, str, str, int, str]],
    report: tuple[int, str],
) -> list[tuple[str, str, bytes]]:
    """Unzip in memory, all or nothing: the entries must be exactly the manifest's files and
    the report. The report is checked and dropped; only the members are returned."""
    try:
        return _read_members(archive, members, report)
    except UploadPlugError:
        raise
    except Exception:
        # zipfile, zlib and struct messages carry entry names: never pass them on.
        raise UploadPlugError(
            "PLUG_REJECTED", "the prepared archive could not be read safely"
        ) from None


def _read_members(
    archive: bytes,
    members: list[tuple[str, str, str, int, str]],
    report: tuple[int, str],
) -> list[tuple[str, str, bytes]]:
    mismatch = UploadPlugError("PLUG_REJECTED", "the prepared archive does not match its manifest")
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        # Every entry is checked before any is read. infolist() keeps duplicates, and
        # orig_filename is the name as stored (filename is cut at a NUL byte).
        entries = bundle.infolist()
        for entry in entries:
            name = entry.orig_filename
            if (
                entry.filename != name
                or entry.is_dir()
                or name.startswith("/")
                or "\\" in name
                or ".." in name.split("/")
            ):
                raise mismatch
            if entry.flag_bits & _ZIP_ENCRYPTED_FLAG:
                raise UploadPlugError("PLUG_REJECTED", "the prepared archive holds an encrypted file")
            if entry.compress_type not in _ZIP_METHODS:
                raise UploadPlugError(
                    "PLUG_REJECTED", "the prepared archive uses an unsupported compression method"
                )
        names = [entry.orig_filename for entry in entries]
        expected = [path for path, *_rest in members] + [_REPORT_PATH]
        # the report is the last entry; the members come in any order before it
        if len(names) != len(expected) or set(names) != set(expected) or names[-1] != _REPORT_PATH:
            raise mismatch
        by_path = {entry.orig_filename: entry for entry in entries}
        for path, _name, _mime, size, _digest in members:
            if by_path[path].file_size != size:
                raise mismatch
        report_size, report_digest = report
        if by_path[_REPORT_PATH].file_size != report_size:
            raise mismatch
        files: list[tuple[str, str, bytes]] = []
        for path, name, mime, size, digest in members:
            with bundle.open(by_path[path]) as handle:
                content = handle.read(size + 1)
            if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                raise UploadPlugError("PLUG_REJECTED", "a prepared file did not match its checksum")
            files.append((name, mime, content))
        with bundle.open(by_path[_REPORT_PATH]) as handle:
            body = handle.read(report_size + 1)
    if len(body) != report_size or hashlib.sha256(body).hexdigest() != report_digest:
        raise UploadPlugError("PLUG_REJECTED", "the plug's report did not match its checksum")
    _check_report(body, members)
    return files


def _check_report(body: bytes, members: list[tuple[str, str, str, int, str]]) -> None:
    """The report must list exactly the delivered files with their sizes and hashes and say
    that the plug's second scan passed. Nothing else in it is read or kept."""
    invalid = UploadPlugError("PLUG_REJECTED", "the plug's report does not match the delivered files")
    try:
        report = json.loads(body.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise invalid from None
    if not isinstance(report, dict) or report.get("contract") != CONTRACT_VERSION:
        raise invalid
    if report.get("report_version") != 1:
        raise invalid
    listed = report.get("files")
    want = [{"path": path, "size": size, "sha256": digest} for path, _n, _m, size, digest in members]
    if not isinstance(listed, list) or listed != want:
        raise invalid
    scan = report.get("second_scan")
    if scan != {"result": "PASS", "files": len(members), "open_items": 0}:
        raise invalid


def _read_capped(response: httpx.Response, cap: int) -> bytes:
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise UploadPlugError("PLUG_REJECTED", "the upload plug sent an encoded response")
    content = bytearray()
    for chunk in response.iter_bytes(_READ_CHUNK_BYTES):
        content.extend(chunk)
        if len(content) > cap:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug sent more data than declared")
    return bytes(content)
