from fastapi.testclient import TestClient

from api.auth import LOCAL_REQUEST_IDENTITY, LOCAL_SESSION_ID, local_request_identity
from api.server import create_app


class _FakeRuntime:
    attachment_limits = type(
        "AttachmentLimits",
        (),
        {"max_message_bytes": 1024},
    )()

    def runtime_info(self) -> dict[str, str]:
        return {}


def test_local_request_identity_is_fixed_and_stable() -> None:
    first = local_request_identity()
    second = local_request_identity()

    assert first == second == LOCAL_REQUEST_IDENTITY
    assert first.owner_user_id == "local-user"
    assert first.user.email is None
    assert first.session_id == LOCAL_SESSION_ID


def test_local_api_needs_no_authorization_or_session_headers() -> None:
    client = TestClient(
        create_app(runtime=_FakeRuntime(), provider_api_key="test-provider-key")
    )

    response = client.get("/api/runtime")

    assert response.status_code == 200


def test_hosted_configuration_and_provider_key_routes_are_absent() -> None:
    client = TestClient(
        create_app(runtime=_FakeRuntime(), provider_api_key="test-provider-key")
    )

    assert client.get("/api/public-config").status_code == 404
    assert client.get("/api/readiness").status_code == 404
    assert client.get("/api/ops/deployment-status").status_code == 404
    assert client.get("/api/session/provider-key").status_code == 404
    assert client.put(
        "/api/session/provider-key",
        json={"api_key": "browser-secret"},
    ).status_code == 404
