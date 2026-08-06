#!/usr/bin/env python3
"""Exercise the production Cognito verifier against a loopback JWKS endpoint."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from threading import Thread
import time

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.auth import AuthenticatedUser, CognitoTokenVerifier


ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_smoke"
APP_CLIENT_ID = "report-agent-smoke-client"
KEY_ID = "smoke-key"


def main() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = KEY_ID
    response = json.dumps({"keys": [jwk]}).encode("utf-8")

    class JwksHandler(BaseHTTPRequestHandler):
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

    server = ThreadingHTTPServer(("127.0.0.1", 0), JwksHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": ISSUER,
                "exp": now + 300,
                "iat": now - 1,
                "sub": "smoke-cognito-subject",
                "token_use": "access",
                "client_id": APP_CLIENT_ID,
            },
            private_key,
            algorithm="RS256",
            headers={"kid": KEY_ID},
        )
        verifier = CognitoTokenVerifier(
            issuer=ISSUER,
            app_client_id=APP_CLIENT_ID,
            jwks_url=f"http://127.0.0.1:{server.server_port}/jwks.json",
        )
        assert verifier.verify(f"Bearer {token}") == AuthenticatedUser(
            owner_user_id="smoke-cognito-subject",
            token_expires_at_epoch=now + 300,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    print("Cognito auth smoke passed")


if __name__ == "__main__":
    main()
