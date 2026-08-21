from anthropic import BadRequestError
from httpx import Request, Response

from utils.provider_errors import classify_llm_error


def test_anthropic_low_credit_bad_request_is_actionable() -> None:
    error = BadRequestError(
        "Your credit balance is too low to access the Anthropic API.",
        response=Response(
            400,
            request=Request("POST", "https://api.anthropic.com/v1/messages"),
        ),
        body={
            "type": "error",
            "error": {
                "type": "invalid_request_error",
                "message": "Your credit balance is too low to access the Anthropic API.",
            },
        },
    )

    code, message = classify_llm_error(error)

    assert code == "PROVIDER_CREDITS_EXHAUSTED"
    assert "Anthropic account has no remaining API credits" in message
