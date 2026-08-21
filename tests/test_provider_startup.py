from __future__ import annotations

from utils.provider_startup import verify_provider_credential


def test_verify_provider_credential_dispatches_to_anthropic() -> None:
    seen: list[str] = []

    verify_provider_credential(
        "anthropic",
        "anthropic-key",
        anthropic_checker=seen.append,
    )

    assert seen == ["anthropic-key"]


def test_verify_provider_credential_supports_keyless_compatible_endpoint() -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    verify_provider_credential(
        "openai_compatible",
        "",
        base_url="http://127.0.0.1:8001/v1",
        openai_checker=lambda key, **kwargs: seen.append((key, kwargs)),
    )

    assert seen == [
        (
            "",
            {
                "base_url": "http://127.0.0.1:8001/v1",
                "provider_label": "The custom endpoint",
                "allow_empty_key": True,
            },
        )
    ]
