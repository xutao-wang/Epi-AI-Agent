from __future__ import annotations

import json

from epi_agent.protocol import ArtifactRef, ToolResult
from epi_agent import protocol


def test_tool_result_serialization_never_slices_structured_json() -> None:
    original_message = json.dumps(
        {
            "next_offset": 25,
            "fields": [
                {"column": f"FIELD_{index}", "text": "x" * 1_000}
                for index in range(25)
            ],
        }
    )

    serialized = protocol.serialize_tool_result(
        ToolResult(
            message=original_message,
            artifacts=(
                ArtifactRef(
                    id="profile-1",
                    kind="table_profile",
                    version=1,
                ),
            ),
        )
    )
    outer = json.loads(serialized)
    inner = json.loads(outer["message"])

    assert len(serialized) <= protocol._MAX_MODEL_TOOL_MESSAGE_CHARS
    assert inner == {
        "artifact_available": True,
        "code": "MODEL_TOOL_MESSAGE_TOO_LARGE",
        "original_char_count": len(original_message),
    }
    assert outer["artifacts"] == [
        {"id": "profile-1", "kind": "table_profile", "version": 1}
    ]


def test_tool_result_serialization_keeps_plain_text_bounded() -> None:
    serialized = protocol.serialize_tool_result(
        ToolResult(message="plain text " * 2_000)
    )
    content = json.loads(serialized)

    assert len(serialized) <= protocol._MAX_MODEL_TOOL_MESSAGE_CHARS
    assert content["message"].endswith("...")
    assert content["artifacts"] == []
