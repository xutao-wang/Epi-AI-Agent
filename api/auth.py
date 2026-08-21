from __future__ import annotations

from dataclasses import dataclass


LOCAL_SESSION_ID = "00000000-0000-4000-8000-000000000001"


@dataclass(frozen=True)
class AuthenticatedUser:
    owner_user_id: str
    email: str | None = None
    token_expires_at_epoch: int | None = None


@dataclass(frozen=True)
class RequestIdentity:
    user: AuthenticatedUser
    session_id: str

    @property
    def owner_user_id(self) -> str:
        return self.user.owner_user_id


LOCAL_REQUEST_IDENTITY = RequestIdentity(
    user=AuthenticatedUser(owner_user_id="local-user"),
    session_id=LOCAL_SESSION_ID,
)


def local_request_identity() -> RequestIdentity:
    """Return the fixed identity used by the native local application."""
    return LOCAL_REQUEST_IDENTITY
