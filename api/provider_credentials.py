from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import threading
import time
from typing import Protocol

from api.auth import RequestIdentity
from utils.provider_startup import verify_active_provider


@dataclass
class _CredentialRecord:
    api_key: str
    last_accessed_monotonic: float
    token_expires_at_epoch: int | None

    def __repr__(self) -> str:
        return "_CredentialRecord(api_key=<redacted>)"


class ProviderKeyValidator(Protocol):
    def validate(self, provider: str, api_key: str) -> None:
        raise NotImplementedError


class OpenAIProviderKeyValidator:
    def __init__(
        self,
        *,
        verify_provider: Callable[[str, str], None] = verify_active_provider,
    ) -> None:
        self._verify_provider = verify_provider

    def validate(self, provider: str, api_key: str) -> None:
        self._verify_provider(provider, api_key)


class ProviderCredentialStore:
    def __init__(
        self,
        *,
        idle_ttl_seconds: float = 43_200,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        on_expire: Callable[[str, str], None] | None = None,
    ) -> None:
        self._idle_ttl_seconds = idle_ttl_seconds
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._on_expire = on_expire
        self._entries: dict[tuple[str, str], _CredentialRecord] = {}
        self._lock = threading.RLock()

    def __repr__(self) -> str:
        with self._lock:
            return f"ProviderCredentialStore(entries={len(self._entries)})"

    @staticmethod
    def _key(identity: RequestIdentity) -> tuple[str, str]:
        return (identity.owner_user_id, identity.session_id)

    def put(self, identity: RequestIdentity, api_key: str) -> None:
        normalized = api_key.strip()
        if not normalized:
            raise ValueError("api_key is required")
        with self._lock:
            self._entries[self._key(identity)] = _CredentialRecord(
                api_key=normalized,
                last_accessed_monotonic=self._monotonic_clock(),
                token_expires_at_epoch=identity.user.token_expires_at_epoch,
            )

    def get(self, identity: RequestIdentity) -> str | None:
        expired_key: tuple[str, str] | None = None
        with self._lock:
            key = self._key(identity)
            record = self._entries.get(key)
            if record is None:
                return None
            monotonic_now = self._monotonic_clock()
            if self._is_expired(
                record,
                monotonic_now=monotonic_now,
                wall_now=self._wall_clock(),
            ):
                self._entries.pop(key, None)
                expired_key = key
                api_key = None
            else:
                record.last_accessed_monotonic = monotonic_now
                api_key = record.api_key
        if expired_key is not None:
            self._notify_expired((expired_key,))
        return api_key

    def has(self, identity: RequestIdentity) -> bool:
        return self.get(identity) is not None

    def delete(self, identity: RequestIdentity) -> None:
        with self._lock:
            self._entries.pop(self._key(identity), None)

    def prune_expired(self) -> int:
        with self._lock:
            monotonic_now = self._monotonic_clock()
            wall_now = self._wall_clock()
            expired_keys = [
                key
                for key, record in self._entries.items()
                if self._is_expired(
                    record,
                    monotonic_now=monotonic_now,
                    wall_now=wall_now,
                )
            ]
            for key in expired_keys:
                self._entries.pop(key, None)
        self._notify_expired(expired_keys)
        return len(expired_keys)

    def _notify_expired(
        self,
        expired_keys: Sequence[tuple[str, str]],
    ) -> None:
        if self._on_expire is None:
            return
        for owner_user_id, session_id in expired_keys:
            self._on_expire(owner_user_id, session_id)

    def _is_expired(
        self,
        record: _CredentialRecord,
        *,
        monotonic_now: float,
        wall_now: float,
    ) -> bool:
        return (
            monotonic_now - record.last_accessed_monotonic >= self._idle_ttl_seconds
            or (
                record.token_expires_at_epoch is not None
                and wall_now >= record.token_expires_at_epoch
            )
        )


__all__ = [
    "OpenAIProviderKeyValidator",
    "ProviderCredentialStore",
    "ProviderKeyValidator",
]
