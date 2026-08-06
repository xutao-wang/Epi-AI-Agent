from __future__ import annotations

import os
from pathlib import Path

import pytest

from api.auth import AuthenticatedUser, RequestIdentity
from api.provider_credentials import ProviderCredentialStore


def identity(
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


def test_provider_credentials_are_isolated_by_owner_and_session() -> None:
    store = ProviderCredentialStore()
    identity_a = identity("user-a", "11111111-1111-4111-8111-111111111111")
    identity_b = identity("user-b", "22222222-2222-4222-8222-222222222222")
    store.put(identity_a, "key-a")
    store.put(identity_b, "key-b")

    assert store.get(identity_a) == "key-a"
    assert store.get(identity_b) == "key-b"
    store.delete(identity_a)
    assert store.get(identity_a) is None
    assert store.get(identity_b) == "key-b"


def test_provider_credential_key_is_exact_owner_and_session_tuple() -> None:
    store = ProviderCredentialStore()
    first_session = identity("shared-owner", "11111111-1111-4111-8111-111111111111")
    second_session = identity("shared-owner", "22222222-2222-4222-8222-222222222222")
    different_owner = identity("different-owner", first_session.session_id)
    store.put(first_session, "first-key")

    assert set(store._entries) == {("shared-owner", first_session.session_id)}
    assert store.get(second_session) is None
    assert store.get(different_owner) is None


def test_deleting_a_session_leaves_the_same_users_other_session_intact() -> None:
    store = ProviderCredentialStore()
    first_session = identity("user-a", "11111111-1111-4111-8111-111111111111")
    second_session = identity("user-a", "22222222-2222-4222-8222-222222222222")
    store.put(first_session, "first-key")
    store.put(second_session, "second-key")

    store.delete(first_session)

    assert store.get(first_session) is None
    assert store.get(second_session) == "second-key"


def test_provider_credentials_expire_after_idle_ttl() -> None:
    monotonic_now = [100.0]
    expired: list[tuple[str, str]] = []
    store = ProviderCredentialStore(
        idle_ttl_seconds=5,
        monotonic_clock=lambda: monotonic_now[0],
        on_expire=lambda owner, session: expired.append((owner, session)),
    )
    request_identity = identity("user-a", "11111111-1111-4111-8111-111111111111")
    store.put(request_identity, "idle-key")

    monotonic_now[0] += 5

    assert store.get(request_identity) is None
    assert store.has(request_identity) is False
    assert expired == [("user-a", request_identity.session_id)]


def test_cognito_bound_credentials_expire_with_token_before_idle_ttl() -> None:
    monotonic_now = [100.0]
    wall_now = [1_000.0]
    expired: list[tuple[str, str]] = []
    store = ProviderCredentialStore(
        idle_ttl_seconds=60,
        monotonic_clock=lambda: monotonic_now[0],
        wall_clock=lambda: wall_now[0],
        on_expire=lambda owner, session: expired.append((owner, session)),
    )
    request_identity = identity(
        "user-a",
        "11111111-1111-4111-8111-111111111111",
        token_expires_at_epoch=1_001,
    )
    store.put(request_identity, "token-key")

    wall_now[0] = 1_001

    assert store.get(request_identity) is None
    assert expired == [("user-a", request_identity.session_id)]


def test_prune_expired_redacts_keys_and_returns_deletion_count() -> None:
    monotonic_now = [100.0]
    wall_now = [1_000.0]
    expired: list[tuple[str, str]] = []
    store = ProviderCredentialStore(
        idle_ttl_seconds=5,
        monotonic_clock=lambda: monotonic_now[0],
        wall_clock=lambda: wall_now[0],
        on_expire=lambda owner, session: expired.append((owner, session)),
    )
    idle_identity = identity("idle", "11111111-1111-4111-8111-111111111111")
    token_identity = identity(
        "token",
        "22222222-2222-4222-8222-222222222222",
        token_expires_at_epoch=1_001,
    )
    active_identity = identity("active", "33333333-3333-4333-8333-333333333333")
    store.put(idle_identity, "idle-secret")
    store.put(token_identity, "token-secret")
    monotonic_now[0] += 5
    wall_now[0] = 1_001
    store.put(active_identity, "active-secret")

    assert store.prune_expired() == 2
    assert store.get(active_identity) == "active-secret"
    assert "secret" not in repr(store)
    assert "secret" not in repr(store._entries[store._key(active_identity)])
    assert repr(store) == "ProviderCredentialStore(entries=1)"
    assert expired == [
        ("idle", idle_identity.session_id),
        ("token", token_identity.session_id),
    ]


def test_store_only_keeps_normalized_nonempty_key_in_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = ProviderCredentialStore()
    request_identity = identity("user-a", "11111111-1111-4111-8111-111111111111")
    before_environment = dict(os.environ)
    before_files = sorted(tmp_path.iterdir())
    monkeypatch.chdir(tmp_path)

    store.put(request_identity, "  normalized-key  ")

    assert store.get(request_identity) == "normalized-key"
    assert dict(os.environ) == before_environment
    assert sorted(tmp_path.iterdir()) == before_files
    with pytest.raises(ValueError, match="api_key is required"):
        store.put(request_identity, " \t\n")
