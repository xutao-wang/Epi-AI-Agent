from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from api.schemas import CognitoPublicConfig, PublicAppConfig


class ApplicationConfigurationError(ValueError):
    """Raised when the application deployment configuration is incomplete."""


@dataclass(frozen=True)
class ApplicationAuthConfig:
    mode: Literal["local", "cognito"]
    aws_region: str | None
    cognito_user_pool_id: str | None
    cognito_app_client_id: str | None
    redirect_uri: str | None
    post_logout_redirect_uri: str | None

    @property
    def cognito_issuer(self) -> str | None:
        if self.mode == "local":
            return None
        assert self.aws_region is not None
        assert self.cognito_user_pool_id is not None
        return (
            f"https://cognito-idp.{self.aws_region}.amazonaws.com/"
            f"{self.cognito_user_pool_id}"
        )


_COGNITO_ENVIRONMENT_NAMES = (
    "REPORT_AGENT_AWS_REGION",
    "REPORT_AGENT_COGNITO_USER_POOL_ID",
    "REPORT_AGENT_COGNITO_APP_CLIENT_ID",
    "REPORT_AGENT_AUTH_REDIRECT_URI",
    "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI",
)


def application_auth_config(environ: Mapping[str, str]) -> ApplicationAuthConfig:
    mode = environ.get("REPORT_AGENT_AUTH_MODE", "local").strip().lower()
    if mode not in {"local", "cognito"}:
        raise ApplicationConfigurationError(
            "REPORT_AGENT_AUTH_MODE must be 'local' or 'cognito'"
        )

    values = {
        name: environ.get(name, "").strip() or None
        for name in _COGNITO_ENVIRONMENT_NAMES
    }
    if mode == "cognito":
        missing = [name for name in _COGNITO_ENVIRONMENT_NAMES if values[name] is None]
        if missing:
            raise ApplicationConfigurationError(
                "Cognito mode requires: " + ", ".join(missing)
            )

    return ApplicationAuthConfig(
        mode=mode,
        aws_region=values["REPORT_AGENT_AWS_REGION"],
        cognito_user_pool_id=values["REPORT_AGENT_COGNITO_USER_POOL_ID"],
        cognito_app_client_id=values["REPORT_AGENT_COGNITO_APP_CLIENT_ID"],
        redirect_uri=values["REPORT_AGENT_AUTH_REDIRECT_URI"],
        post_logout_redirect_uri=values["REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI"],
    )


def public_app_config(config: ApplicationAuthConfig) -> PublicAppConfig:
    if config.mode == "local":
        return PublicAppConfig(auth_mode="local", provider_key_required=False)

    assert config.cognito_issuer is not None
    assert config.cognito_app_client_id is not None
    assert config.redirect_uri is not None
    assert config.post_logout_redirect_uri is not None
    return PublicAppConfig(
        auth_mode="cognito",
        provider_key_required=True,
        cognito=CognitoPublicConfig(
            authority=config.cognito_issuer,
            client_id=config.cognito_app_client_id,
            redirect_uri=config.redirect_uri,
            post_logout_redirect_uri=config.post_logout_redirect_uri,
        ),
    )
