"""Narrow public SDK adaptations for verified native Bedrock models."""
from __future__ import annotations

from langchain_aws import ChatBedrockConverse


class ChatBedrockDeepSeekV32(ChatBedrockConverse):
    """Keep normal optional tool use for DeepSeek V3.2.

    langchain-aws 1.4.6 turns an omitted choice into forced ``any`` for every
    DeepSeek V3 model. On V3.2 that triggers grammar validation that rejects
    otherwise accepted app schemas. Explicit ``auto`` supports both ordinary
    replies and tool use while preserving the original schemas.
    """

    def bind_tools(self, tools, *, tool_choice=None, strict=None, **kwargs):
        return super().bind_tools(
            tools, tool_choice="auto" if tool_choice is None else tool_choice,
            strict=strict, **kwargs,
        )
