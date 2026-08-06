from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from epi_agent.protocol import (
    ArtifactRef,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)
from epi_agent.registry import ToolRegistry
from utils.attachment_artifacts import AttachmentError
from utils.attachment_readers import AttachmentReaderService


_MAX_AUTHORIZED_ATTACHMENT_REPAIR_IDS = 20


class AttachmentIdsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment_ids: list[str] = Field(min_length=1, max_length=10)


class AttachmentIdArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attachment_id: str = Field(min_length=1, max_length=512)


class LoadTableArgs(AttachmentIdArgs):
    sheet_name: str | None = Field(default=None, max_length=200)
    annotation_ids: list[str] = Field(default_factory=list, max_length=10)


class InspectImageArgs(AttachmentIdArgs):
    question: str = Field(min_length=1, max_length=1000)


def _json_message(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


class _AttachmentTool:
    spec: ToolSpec

    def __init__(self, service: AttachmentReaderService) -> None:
        self.service = service

    def _require_context(self, context: ToolContext) -> None:
        if context.attachment_store is None:
            raise ToolExecutionError(
                "ATTACHMENT_STORE_UNAVAILABLE",
                "Attachment storage is unavailable.",
                recoverable=False,
            )
        if context.attachment_store is not self.service.store:
            raise ToolExecutionError(
                "ATTACHMENT_STORE_UNAVAILABLE",
                "Attachment storage does not match the active tool service.",
                recoverable=False,
            )

    def _invoke_service(
        self,
        operation,
        context: ToolContext,
        *,
        attachment_ids: list[str],
    ) -> Any:
        self._require_context(context)
        allowed = set(context.authorized_attachment_ids)
        if any(attachment_id not in allowed for attachment_id in attachment_ids):
            authorized_ids = sorted(allowed)
            raise ToolExecutionError(
                "ATTACHMENT_NOT_AUTHORIZED",
                "Attachment is not authorized for the current conversation. "
                "Retry with an exact authorized attachment ID.",
                recoverable=True,
                details={
                    "authorized_attachment_ids": authorized_ids[
                        :_MAX_AUTHORIZED_ATTACHMENT_REPAIR_IDS
                    ],
                    "authorized_attachment_ids_truncated": len(authorized_ids)
                    > _MAX_AUTHORIZED_ATTACHMENT_REPAIR_IDS,
                },
            )
        try:
            return operation()
        except AttachmentError as exc:
            raise ToolExecutionError(
                exc.code,
                str(exc),
                recoverable=exc.code
                not in {
                    "ATTACHMENT_STORE_UNAVAILABLE",
                    "ATTACHMENT_CORRUPTED",
                },
            ) from exc

    @staticmethod
    def _storage_scope(context: ToolContext):
        # Unit-only contexts intentionally omit thread_storage. Production
        # graph contexts always provide the owner-authorized scope.
        return context.thread_storage or context.thread_id


class InspectAttachmentsTool(_AttachmentTool):
    spec = ToolSpec(
        name="attachments-inspect",
        description=(
            "Inspect bounded metadata for one or more attachments available "
            "in this conversation thread."
        ),
        args_model=AttachmentIdsArgs,
        read_only=True,
        interrupting=False,
    )

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        attachment_ids = list(arguments["attachment_ids"])
        result = self._invoke_service(
            lambda: self.service.inspect(
                self._storage_scope(context),
                attachment_ids,
            ),
            context,
            attachment_ids=attachment_ids,
        )
        return ToolResult(message=_json_message({"attachments": result}))


class ReadDocumentTool(_AttachmentTool):
    spec = ToolSpec(
        name="attachments-read_document",
        description="Extract bounded text from an attached PDF, DOCX, TXT, or Markdown file.",
        args_model=AttachmentIdArgs,
        read_only=True,
        interrupting=False,
    )

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        attachment_id = str(arguments["attachment_id"])
        result = self._invoke_service(
            lambda: self.service.read_document(
                self._storage_scope(context),
                attachment_id,
            ),
            context,
            attachment_ids=[attachment_id],
        )
        return ToolResult(message=_json_message(result))


class ParseStructuredAttachmentTool(_AttachmentTool):
    spec = ToolSpec(
        name="attachments-parse_structured",
        description="Parse a bounded structural summary from an attached JSON or XML file.",
        args_model=AttachmentIdArgs,
        read_only=True,
        interrupting=False,
    )

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        attachment_id = str(arguments["attachment_id"])
        result = self._invoke_service(
            lambda: self.service.parse_structured(
                self._storage_scope(context),
                attachment_id,
            ),
            context,
            attachment_ids=[attachment_id],
        )
        return ToolResult(message=_json_message(result))


class InspectImageTool(_AttachmentTool):
    spec = ToolSpec(
        name="attachments-inspect_image",
        description="Inspect an attached PNG or JPEG with the configured vision model.",
        args_model=InspectImageArgs,
        read_only=True,
        interrupting=False,
    )

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        attachment_id = str(arguments["attachment_id"])
        result = self._invoke_service(
            lambda: self.service.inspect_image(
                self._storage_scope(context),
                attachment_id,
                question=str(arguments["question"]),
            ),
            context,
            attachment_ids=[attachment_id],
        )
        return ToolResult(message=_json_message(result))


class LoadTableTool(_AttachmentTool):
    spec = ToolSpec(
        name="attachments-load_table",
        description=(
            "Materialize an attached CSV, TSV, XLS, XLSX, record JSON, or "
            "record XML file as a derived analysis dataset without activating it."
        ),
        args_model=LoadTableArgs,
        read_only=True,
        interrupting=False,
    )

    def invoke(
        self,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> ToolResult:
        attachment_id = str(arguments["attachment_id"])
        annotation_ids = list(arguments.get("annotation_ids") or [])
        dataset = self._invoke_service(
            lambda: self.service.load_table(
                self._storage_scope(context),
                attachment_id,
                sheet_name=arguments.get("sheet_name"),
                annotation_ids=annotation_ids,
            ),
            context,
            attachment_ids=[attachment_id, *annotation_ids],
        )
        reference = context.artifact_store.save_dataset(
            dataset,
            make_active=False,
        )
        message = {
            "id": reference.id,
            "kind": reference.kind,
            "version": reference.version,
            "row_count": int(dataset.get("row_count") or 0),
            "column_count": int(dataset.get("column_count") or 0),
            "columns": list(dataset.get("columns") or []),
            "provenance": dict(dataset.get("provenance") or {}),
        }
        return ToolResult(
            message=_json_message(message),
            artifacts=(reference,),
        )


def build_attachment_tool_registry(
    service: AttachmentReaderService,
) -> ToolRegistry:
    return ToolRegistry(
        [
            InspectAttachmentsTool(service),
            ReadDocumentTool(service),
            ParseStructuredAttachmentTool(service),
            InspectImageTool(service),
            LoadTableTool(service),
        ]
    )
