from __future__ import annotations

import json

import pytest

from api.deployment import required_secret_names
from api.public_config import (
    ApplicationConfigurationError,
    application_auth_config,
    public_app_config,
)


def test_cognito_public_config_contains_only_browser_safe_values() -> None:
    environ = {
        "REPORT_AGENT_AUTH_MODE": "cognito",
        "REPORT_AGENT_AWS_REGION": "us-east-1",
        "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
        "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "client-123",
        "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://auth.example/logout",
        "REPORT_AGENT_AUTH_REDIRECT_URI": "https://demo.example/callback",
        "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://demo.example/",
        "OPENAI_API_KEY": "must-not-be-public",
    }
    result = public_app_config(application_auth_config(environ)).model_dump()

    assert result == {
        "auth_mode": "cognito",
        "provider_key_required": True,
        "cognito": {
            "authority": (
                "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
            ),
            "client_id": "client-123",
            "logout_endpoint": "https://auth.example/logout",
            "redirect_uri": "https://demo.example/callback",
            "post_logout_redirect_uri": "https://demo.example/",
        },
    }
    assert "must-not-be-public" not in json.dumps(result)


@pytest.mark.parametrize(
    ("environment_name", "value"),
    [
        ("REPORT_AGENT_AUTH_MODE", ""),
        ("REPORT_AGENT_AUTH_MODE", "unsupported"),
    ],
)
def test_application_auth_config_rejects_unsupported_auth_modes(
    environment_name: str,
    value: str,
) -> None:
    with pytest.raises(ApplicationConfigurationError, match="REPORT_AGENT_AUTH_MODE"):
        application_auth_config({environment_name: value})


@pytest.mark.parametrize(
    "missing_name",
    [
        "REPORT_AGENT_AWS_REGION",
        "REPORT_AGENT_COGNITO_USER_POOL_ID",
        "REPORT_AGENT_COGNITO_APP_CLIENT_ID",
        "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT",
        "REPORT_AGENT_AUTH_REDIRECT_URI",
        "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI",
    ],
)
def test_cognito_auth_config_requires_each_configured_value(
    missing_name: str,
) -> None:
    environ = {
        "REPORT_AGENT_AUTH_MODE": "cognito",
        "REPORT_AGENT_AWS_REGION": "us-east-1",
        "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
        "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "client-123",
        "REPORT_AGENT_COGNITO_LOGOUT_ENDPOINT": "https://auth.example/logout",
        "REPORT_AGENT_AUTH_REDIRECT_URI": "https://demo.example/callback",
        "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://demo.example/",
    }
    environ.pop(missing_name)

    with pytest.raises(ApplicationConfigurationError, match=missing_name):
        application_auth_config(environ)


def test_local_config_uses_no_cognito_values_or_provider_key_requirement() -> None:
    result = public_app_config(application_auth_config({})).model_dump()

    assert result == {
        "auth_mode": "local",
        "provider_key_required": False,
        "cognito": None,
    }


def test_secret_requirements_are_mode_aware() -> None:
    assert required_secret_names("local") == ("OPENAI_API_KEY",)
    assert required_secret_names("cognito") == ()


def test_secret_requirements_reject_unknown_auth_mode() -> None:
    with pytest.raises(ValueError, match="auth_mode"):
        required_secret_names("unsupported")
