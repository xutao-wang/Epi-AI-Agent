from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
from jwt.exceptions import InvalidTokenError
import pytest

from api.auth import (
    AuthenticatedUser,
    CognitoTokenVerifier,
    LOCAL_SESSION_ID,
    LocalTokenVerifier,
)
from api.server import create_app


_ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
_APP_CLIENT_ID = "report-agent-client"
_KEY_ID = "test-key"


@pytest.fixture
def rsa_private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def jwks_url(rsa_private_key: rsa.RSAPrivateKey):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_private_key.public_key()))
    jwk["kid"] = _KEY_ID
    response = json.dumps({"keys": [jwk]}).encode("utf-8")

    class _JwksHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/jwks.json":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _JwksHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/jwks.json"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _access_token(
    private_key: rsa.RSAPrivateKey,
    **overrides: Any,
) -> str:
    now = int(time.time())
    claims = {
        "iss": _ISSUER,
        "exp": now + 300,
        "iat": now - 1,
        "sub": "cognito-subject-123",
        "token_use": "access",
        "client_id": _APP_CLIENT_ID,
    }
    claims.update(overrides)
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": _KEY_ID},
    )


def _verifier(jwks_url: str) -> CognitoTokenVerifier:
    return CognitoTokenVerifier(
        issuer=_ISSUER,
        app_client_id=_APP_CLIENT_ID,
        jwks_url=jwks_url,
    )


def test_local_verifier_uses_fixed_non_email_owner() -> None:
    user = LocalTokenVerifier().verify(None)

    assert user == AuthenticatedUser(owner_user_id="local-user")
    assert user.email is None
    assert user.token_expires_at_epoch is None


def test_cognito_verifier_accepts_rs256_access_token(
    rsa_private_key: rsa.RSAPrivateKey,
    jwks_url: str,
) -> None:
    token = _access_token(rsa_private_key, email="owner@example.com")

    user = _verifier(jwks_url).verify(f"Bearer {token}")

    assert user == AuthenticatedUser(
        owner_user_id="cognito-subject-123",
        email="owner@example.com",
        token_expires_at_epoch=jwt.decode(
            token,
            options={"verify_signature": False},
        )["exp"],
    )


def test_cognito_verifier_accepts_an_injected_jwks_client(
    rsa_private_key: rsa.RSAPrivateKey,
) -> None:
    class StaticJwksClient:
        def get_signing_key_from_jwt(self, token: str):
            return type(
                "SigningKey",
                (),
                {"key": rsa_private_key.public_key()},
            )()

    verifier = CognitoTokenVerifier(
        issuer=_ISSUER,
        app_client_id=_APP_CLIENT_ID,
        jwks_client=StaticJwksClient(),
    )

    user = verifier.verify(f"Bearer {_access_token(rsa_private_key)}")

    assert user.owner_user_id == "cognito-subject-123"


def test_cognito_verifier_does_not_substitute_email_for_subject(
    rsa_private_key: rsa.RSAPrivateKey,
    jwks_url: str,
) -> None:
    user = _verifier(jwks_url).verify(
        f"Bearer {_access_token(rsa_private_key, email='owner@example.com')}"
    )

    assert user.owner_user_id == "cognito-subject-123"


def test_cognito_verifier_allows_an_absent_email_claim(
    rsa_private_key: rsa.RSAPrivateKey,
    jwks_url: str,
) -> None:
    user = _verifier(jwks_url).verify(f"Bearer {_access_token(rsa_private_key)}")

    assert user.email is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://unexpected.example.com"},
        {"exp": int(time.time()) - 1},
        {"token_use": "id"},
        {"client_id": "another-client"},
        {"sub": "   "},
    ],
    ids=["issuer", "expiration", "token-use", "client-id", "blank-subject"],
)
def test_cognito_verifier_rejects_invalid_claims(
    rsa_private_key: rsa.RSAPrivateKey,
    jwks_url: str,
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(InvalidTokenError):
        _verifier(jwks_url).verify(f"Bearer {_access_token(rsa_private_key, **overrides)}")


def test_cognito_verifier_rejects_a_bad_rsa_signature(
    jwks_url: str,
) -> None:
    untrusted_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(InvalidTokenError):
        _verifier(jwks_url).verify(f"Bearer {_access_token(untrusted_key)}")


def test_cognito_verifier_rejects_non_rs256_tokens(
    jwks_url: str,
) -> None:
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": _ISSUER,
            "exp": now + 300,
            "iat": now - 1,
            "sub": "cognito-subject-123",
            "token_use": "access",
            "client_id": _APP_CLIENT_ID,
        },
        "not-an-rsa-key-with-at-least-thirty-two-bytes",
        algorithm="HS256",
        headers={"kid": _KEY_ID},
    )

    with pytest.raises(InvalidTokenError):
        _verifier(jwks_url).verify(f"Bearer {token}")


class _RejectingTokenVerifier:
    def verify(self, authorization: str | None) -> AuthenticatedUser:
        if authorization != "Bearer accepted-token":
            raise InvalidTokenError("invalid token")
        return AuthenticatedUser(owner_user_id="test-owner")


def test_protected_routes_reject_missing_or_malformed_bearer_tokens() -> None:
    client = TestClient(
        create_app(runtime=_FakeRuntime(), token_verifier=_RejectingTokenVerifier())
    )
    headers = {"X-Epi-Session-ID": LOCAL_SESSION_ID}

    assert client.get("/api/runtime", headers=headers).status_code == 401
    assert client.get(
        "/api/runtime",
        headers={**headers, "Authorization": "accepted-token"},
    ).status_code == 401
    assert client.get(
        "/api/runtime",
        headers={**headers, "Authorization": "Bearer accepted-token"},
    ).status_code == 200


@pytest.mark.parametrize(
    "session_id",
    [None, "not-a-uuid", "00000000-0000-4000-8000-000000000001 "],
    ids=["missing", "malformed", "noncanonical"],
)
def test_protected_routes_require_canonical_session_ids(session_id: str | None) -> None:
    client = TestClient(
        create_app(runtime=_FakeRuntime(), token_verifier=_RejectingTokenVerifier())
    )
    headers = {"Authorization": "Bearer accepted-token"}
    if session_id is not None:
        headers["X-Epi-Session-ID"] = session_id

    assert client.get("/api/runtime", headers=headers).status_code == 400


def test_health_and_public_config_routes_remain_public() -> None:
    client = TestClient(
        create_app(runtime=_FakeRuntime(), token_verifier=_RejectingTokenVerifier())
    )

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/public-config").json() == {
        "auth_mode": "local",
        "provider_key_required": False,
        "cognito": None,
    }


class _FakeRuntime:
    attachment_limits = type(
        "AttachmentLimits",
        (),
        {"max_message_bytes": 1024},
    )()

    def runtime_info(self) -> dict[str, str]:
        return {}
