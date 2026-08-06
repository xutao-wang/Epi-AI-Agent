#!/usr/bin/env python3
"""Exercise owner authorization and provider-key gating through FastAPI."""

from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from api.auth import CognitoTokenVerifier
from api.conversation_history import ConversationHistoryStore
from api.provider_credentials import ProviderCredentialStore
from api.runtime import ReportAgentApiRuntime
from api.server import create_app


ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_smoke"
APP_CLIENT_ID = "report-agent-authorization-smoke"
KEY_ID = "authorization-smoke-key"
SESSIONS = {
    "smoke-user-a": "11111111-1111-4111-8111-111111111111",
    "smoke-user-b": "22222222-2222-4222-8222-222222222222",
}
DEFAULT_SETTINGS = {
    "model_name": "gpt-5.4",
    "temperature": 0.1,
    "top_p": 0.9,
    "max_steps": 4,
    "timeout_seconds": 300,
    "db_rag_embedding_model": "OpenAI/text-embedding-3-large",
    "db_rag_reranker_model": "disabled",
}


def _access_token(private_key: rsa.RSAPrivateKey, owner: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": ISSUER,
            "exp": now + 300,
            "iat": now - 1,
            "sub": owner,
            "token_use": "access",
            "client_id": APP_CLIENT_ID,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )


def main() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class StaticJwksClient:
        def get_signing_key_from_jwt(self, token: str):
            return type(
                "SigningKey",
                (),
                {"key": private_key.public_key()},
            )()

    runtime: ReportAgentApiRuntime | None = None
    checkpoint_connection: sqlite3.Connection | None = None
    try:
        with TemporaryDirectory(prefix="api-owner-authorization-smoke-") as root:
            runtime_root = Path(root)
            database_path = runtime_root / "runtime.db"
            checkpoint_connection = sqlite3.connect(
                database_path,
                check_same_thread=False,
            )
            checkpointer = SqliteSaver(checkpoint_connection)
            builder = StateGraph(dict)
            builder.add_node("finish", lambda state: state)
            builder.add_edge(START, "finish")
            builder.add_edge("finish", END)
            runtime = ReportAgentApiRuntime(
                graph_factory=lambda _settings, _context: builder.compile(
                    checkpointer=checkpointer
                ),
                default_runtime_settings=DEFAULT_SETTINGS,
                models=["gpt-5.4"],
                runtime_root=runtime_root,
                history_store=ConversationHistoryStore(database_path),
            )
            verifier = CognitoTokenVerifier(
                issuer=ISSUER,
                app_client_id=APP_CLIENT_ID,
                jwks_client=StaticJwksClient(),
            )
            app = create_app(
                runtime,
                token_verifier=verifier,
                credential_store=ProviderCredentialStore(),
            )

            def headers(owner: str) -> dict[str, str]:
                return {
                    "Authorization": f"Bearer {_access_token(private_key, owner)}",
                    "X-Epi-Session-ID": SESSIONS[owner],
                }

            with TestClient(app) as client:
                created = client.post(
                    "/api/threads",
                    headers=headers("smoke-user-a"),
                    json={},
                )
                assert created.status_code == 200
                thread_id = created.json()["thread_id"]

                owner_read = client.get(
                    f"/api/threads/{thread_id}/state",
                    headers=headers("smoke-user-a"),
                )
                owner_work = client.post(
                    f"/api/threads/{thread_id}/messages",
                    headers=headers("smoke-user-a"),
                    json={"text": "smoke"},
                )
                guessed_read = client.get(
                    f"/api/threads/{thread_id}/state",
                    headers=headers("smoke-user-b"),
                )
                guessed_work = client.post(
                    f"/api/threads/{thread_id}/messages",
                    headers=headers("smoke-user-b"),
                    json={"text": "smoke"},
                )
                anonymous = client.get("/api/runtime")

                assert owner_read.status_code == 200
                assert owner_work.status_code == 428
                assert owner_work.json() == {
                    "detail": {"code": "PROVIDER_KEY_REQUIRED"}
                }
                assert guessed_read.status_code == 404
                assert guessed_work.status_code == 404
                assert anonymous.status_code == 401
    finally:
        if runtime is not None:
            runtime._title_executor.shutdown(wait=True)
        if checkpoint_connection is not None:
            checkpoint_connection.close()

    print("API owner authorization smoke: PASS")


if __name__ == "__main__":
    main()
