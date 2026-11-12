"""Optional upload plug: a generic, opt-in hook on the paperclip upload.

When ``REPORT_AGENT_UPLOAD_PLUG_URL`` is empty (the default) nothing changes.
When it is set, the browser hands a chosen ZIP to the plug's page (shown in a
modal), the plug prepares the files, and the browser gets back only an opaque
handle. This module then pulls the prepared files from the plug, server to
server, with a shared secret, and returns them in the same shape the plain
upload route stages.

The app knows nothing about what the plug does to the files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import httpx

CONTRACT_VERSION = "1"

PLUG_URL_ENV = "REPORT_AGENT_UPLOAD_PLUG_URL"
PLUG_SECRET_ENV = "REPORT_AGENT_UPLOAD_PLUG_SECRET"
PLUG_TIMEOUT_ENV = "REPORT_AGENT_UPLOAD_PLUG_TIMEOUT_SECONDS"

_HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_MIN_SECRET_CHARS = 32
_MAX_MANIFEST_BYTES = 64 * 1024
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
    secret: str = ""
    timeout_seconds: float = 30.0
    transport: httpx.BaseTransport | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    @property
    def origin(self) -> str:
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}"

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
        if len(self.secret) < _MIN_SECRET_CHARS:
            raise ValueError(
                f"{PLUG_SECRET_ENV} must be set (at least {_MIN_SECRET_CHARS} characters) "
                f"when {PLUG_URL_ENV} is set"
            )
        if self.timeout_seconds <= 0:
            raise ValueError(f"{PLUG_TIMEOUT_ENV} must be positive")


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


def fetch_delivery(
    settings: UploadPlugSettings,
    handle: str,
    thread_id: str,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> list[tuple[str, str, bytes]]:
    """Pull the prepared files for ``handle`` and verify them.

    Returns ``(filename, mime, content)`` tuples, the same shape the plain
    upload route passes to staging. Raises ``UploadPlugError`` on any problem;
    nothing is returned unless every file passed every check.
    """
    if not settings.enabled:
        raise UploadPlugError("PLUG_UNREACHABLE", "no upload plug is configured")
    validate_handle(handle)
    base = f"{settings.url}/v1/deliveries/{quote(handle, safe='')}"
    headers = {
        "Authorization": f"Bearer {settings.secret}",
        "X-Gate-Contract": CONTRACT_VERSION,
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
            files = _check_manifest(
                manifest,
                max_files=max_files,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            )
            return [
                (name, mime, _get_file(client, f"{base}/files/{index}", headers, params, size, digest))
                for index, (name, mime, size, digest) in enumerate(files)
            ]
    except httpx.HTTPError as exc:
        raise UploadPlugError("PLUG_UNREACHABLE", "the upload plug could not be reached") from exc


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code == 409:
        raise UploadPlugError("CONTRACT_MISMATCH", "the upload plug uses a different contract version")
    if response.status_code in {401, 403}:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug refused this app's credentials")
    if response.status_code != 200:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug did not release these files")


def _get_manifest(client: httpx.Client, url: str, headers: dict, params: dict) -> dict:
    with client.stream("GET", url, headers=headers, params=params) as response:
        _raise_for_status(response)
        body = _read_capped(response, _MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(body)
    except ValueError as exc:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an unreadable manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("contract") != CONTRACT_VERSION:
        raise UploadPlugError("CONTRACT_MISMATCH", "the upload plug uses a different contract version")
    return manifest


def _check_manifest(
    manifest: dict,
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> list[tuple[str, str, int, str]]:
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise UploadPlugError("PLUG_REJECTED", "the upload plug returned no files")
    if len(entries) > max_files:
        raise UploadPlugError("TOO_MANY_FILES", "the prepared upload exceeds the attachment count limit")
    checked: list[tuple[str, str, int, str]] = []
    seen: set[str] = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid manifest")
        name = _safe_member_name(entry.get("name"))
        if name.lower() in seen:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned duplicate file names")
        seen.add(name.lower())
        size = entry.get("size")
        digest = entry.get("sha256")
        mime = entry.get("mime", "")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid file size")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid checksum")
        if not isinstance(mime, str) or len(mime) > 255:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug returned an invalid media type")
        if size > max_file_bytes:
            raise UploadPlugError("FILE_TOO_LARGE", "a prepared file exceeds the attachment size limit")
        total += size
        if total > max_total_bytes:
            raise UploadPlugError(
                "MESSAGE_ATTACHMENTS_TOO_LARGE",
                "the prepared upload exceeds the total attachment size limit",
            )
        checked.append((name, mime, size, digest))
    return checked


def _get_file(
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
        raise UploadPlugError("PLUG_REJECTED", "a prepared file did not match its checksum")
    return content


def _read_capped(response: httpx.Response, cap: int) -> bytes:
    content = bytearray()
    for chunk in response.iter_bytes(_READ_CHUNK_BYTES):
        content.extend(chunk)
        if len(content) > cap:
            raise UploadPlugError("PLUG_REJECTED", "the upload plug sent more data than declared")
    return bytes(content)
