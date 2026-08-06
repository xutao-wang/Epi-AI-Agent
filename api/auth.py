from __future__ import annotations

from dataclasses import dataclass
import hmac
from typing import Annotated, Protocol
from uuid import UUID

from fastapi import Header, HTTPException
import jwt
from jwt.exceptions import InvalidTokenError, PyJWTError


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


class TokenVerifier(Protocol):
    def verify(self, authorization: str | None) -> AuthenticatedUser:
        raise NotImplementedError


class JwksClient(Protocol):
    def get_signing_key_from_jwt(self, token: str):
        raise NotImplementedError


class LocalTokenVerifier:
    def verify(self, authorization: str | None) -> AuthenticatedUser:
        return AuthenticatedUser(owner_user_id="local-user")


class CognitoTokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        app_client_id: str,
        jwks_url: str | None = None,
        jwks_client: JwksClient | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.app_client_id = app_client_id
        self.jwks_client = jwks_client or jwt.PyJWKClient(
            jwks_url or f"{self.issuer}/.well-known/jwks.json",
        )

    def verify(self, authorization: str | None) -> AuthenticatedUser:
        if authorization is None or not authorization.startswith("Bearer "):
            raise InvalidTokenError("invalid access token")
        token = authorization.removeprefix("Bearer ")
        if not token:
            raise InvalidTokenError("invalid access token")

        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                issuer=self.issuer,
                options={"verify_aud": False, "require": ["exp", "iat", "sub"]},
            )
            if claims.get("token_use") != "access":
                raise InvalidTokenError("token_use must be access")
            if not hmac.compare_digest(
                str(claims.get("client_id", "")),
                self.app_client_id,
            ):
                raise InvalidTokenError("client_id mismatch")
            sub = claims["sub"]
            if not isinstance(sub, str) or not sub.strip():
                raise InvalidTokenError("invalid access token")
            email = claims.get("email")
            return AuthenticatedUser(
                owner_user_id=sub,
                email=email if isinstance(email, str) else None,
                token_expires_at_epoch=int(claims["exp"]),
            )
        except PyJWTError as exc:
            raise InvalidTokenError("invalid access token") from exc


def request_identity_dependency(verifier: TokenVerifier):
    def require_identity(
        authorization: Annotated[str | None, Header()] = None,
        x_epi_session_id: Annotated[str | None, Header()] = None,
    ) -> RequestIdentity:
        try:
            user = verifier.verify(authorization)
        except InvalidTokenError as exc:
            raise HTTPException(status_code=401, detail="Unauthorized") from exc

        if x_epi_session_id is None:
            raise HTTPException(status_code=400, detail="Invalid session ID")
        try:
            session_id = UUID(x_epi_session_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid session ID") from exc
        if str(session_id) != x_epi_session_id:
            raise HTTPException(status_code=400, detail="Invalid session ID")
        return RequestIdentity(user=user, session_id=x_epi_session_id)

    return require_identity
