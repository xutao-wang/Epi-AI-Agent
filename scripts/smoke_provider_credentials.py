#!/usr/bin/env python3
"""Exercise the production in-memory provider credential store."""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.auth import AuthenticatedUser, RequestIdentity
from api.provider_credentials import ProviderCredentialStore


def _identity(
    owner_user_id: str,
    session_id: str,
    *,
    token_expires_at_epoch: int | None = None,
) -> RequestIdentity:
    return RequestIdentity(
        user=AuthenticatedUser(
            owner_user_id=owner_user_id,
            token_expires_at_epoch=token_expires_at_epoch,
        ),
        session_id=session_id,
    )


def main() -> None:
    monotonic_now = [100.0]
    wall_now = [1_000.0]
    store = ProviderCredentialStore(
        idle_ttl_seconds=5,
        monotonic_clock=lambda: monotonic_now[0],
        wall_clock=lambda: wall_now[0],
    )
    first_session = _identity(
        "smoke-user",
        "11111111-1111-4111-8111-111111111111",
    )
    second_session = _identity(
        "smoke-user",
        "22222222-2222-4222-8222-222222222222",
        token_expires_at_epoch=1_001,
    )
    store.put(first_session, "smoke-key-one")
    store.put(second_session, "smoke-key-two")
    assert store.get(first_session) == "smoke-key-one"
    assert store.get(second_session) == "smoke-key-two"

    store.delete(first_session)
    assert store.get(first_session) is None
    assert store.get(second_session) == "smoke-key-two"

    wall_now[0] = 1_001
    assert store.get(second_session) is None
    assert repr(store) == "ProviderCredentialStore(entries=0)"
    print("Provider credential store smoke passed")


if __name__ == "__main__":
    main()
