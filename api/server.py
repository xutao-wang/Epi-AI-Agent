from __future__ import annotations

import json
import inspect
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.deployment import cors_allow_origin_regex
from api.auth import RequestIdentity, local_request_identity
from api.runtime import (
    CancellationRestoreError,
    ReportAgentApiRuntime,
    StaleInterruptError,
    ThreadAlreadyRunningError,
    ThreadAwaitingReviewError,
)
from api.schemas import (
    ApiThreadState,
    AttachmentUploadResult,
    CompletedAnalysisResult,
    ConversationHistoryResponse,
    CreateThreadRequest,
    CreateThreadResponse,
    DatasetPreview,
    DatasetProvenance,
    DatasetSchemaResponse,
    ResetThreadResponse,
    RenameConversationRequest,
    ResumeInterruptRequest,
    RuntimeInfo,
    RuntimeOptions,
    SubmitMessageRequest,
    TablePreview,
)
from utils.attachment_artifacts import AttachmentError, AttachmentLimits
from utils.review_interrupts import InvalidInterruptDecisionError


_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
def _content_disposition(disposition: str, filename: str) -> str:
    clean = "".join(
        character
        for character in filename
        if character not in {"\x00", "\r", "\n"}
    )
    ascii_fallback = "".join(
        character if 32 <= ord(character) < 127 and character not in {'"', "\\"}
        else "_"
        for character in clean
    ) or "attachment"
    return (
        f'{disposition}; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(clean, safe='')}"
    )


async def _read_bounded_attachment_uploads(
    files: list[UploadFile],
    limits: AttachmentLimits,
) -> list[tuple[str, str, bytes]]:
    if len(files) > limits.max_files_per_message:
        raise AttachmentError(
            "TOO_MANY_FILES",
            "upload exceeds the configured attachment count limit",
        )
    uploads: list[tuple[str, str, bytes]] = []
    aggregate_bytes = 0
    try:
        for upload in files:
            content = bytearray()
            while True:
                chunk = await upload.read(_UPLOAD_READ_CHUNK_BYTES)
                if not chunk:
                    break
                content.extend(chunk)
                aggregate_bytes += len(chunk)
                if len(content) > limits.max_bytes:
                    raise AttachmentError(
                        "FILE_TOO_LARGE",
                        (
                            f"{upload.filename or 'attachment'} exceeds the "
                            "configured attachment size limit"
                        ),
                    )
                if aggregate_bytes > limits.max_message_bytes:
                    raise AttachmentError(
                        "MESSAGE_ATTACHMENTS_TOO_LARGE",
                        "upload exceeds the configured total attachment size limit",
                    )
            uploads.append(
                (
                    upload.filename or "attachment",
                    upload.content_type or "",
                    bytes(content),
                )
            )
        return uploads
    finally:
        for upload in files:
            await upload.close()


def create_app(
    runtime: ReportAgentApiRuntime,
    *,
    provider_api_key: str,
    static_dir: Path | None = None,
    cors_origin_regex: str | None = None,
) -> FastAPI:
    app = FastAPI(title="RePORT Agent API")
    attachment_limits = runtime.attachment_limits

    def provider_key_for_work(
        identity: RequestIdentity,
        thread_id: str,
    ) -> str:
        try:
            runtime.authorize_thread(identity, thread_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail="Conversation not found",
            ) from exc
        return provider_api_key

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=cors_origin_regex or cors_allow_origin_regex(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    api = APIRouter(dependencies=[Depends(local_request_identity)])

    @api.get("/api/conversations", response_model=ConversationHistoryResponse)
    def list_conversations(
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> ConversationHistoryResponse:
        return ConversationHistoryResponse(
            items=[item.__dict__ for item in runtime.list_conversations(identity)]
        )

    @api.patch("/api/conversations/{thread_id}")
    def rename_conversation(
        thread_id: str,
        request: RenameConversationRequest,
        identity: RequestIdentity = Depends(local_request_identity),
    ):
        record = runtime.rename_conversation(identity, thread_id, request.title)
        if record is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return record

    @api.post("/api/conversations/{thread_id}/open")
    def open_conversation(
        thread_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ):
        record = runtime.open_conversation(identity, thread_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return record

    @api.post("/api/conversations/{thread_id}/archive")
    def archive_conversation(
        thread_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ):
        try:
            record = runtime.archive_conversation(identity, thread_id)
        except (ThreadAlreadyRunningError, ThreadAwaitingReviewError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return record

    @api.post("/api/conversations/{thread_id}/restore")
    def restore_conversation(
        thread_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ):
        try:
            record = runtime.restore_conversation(identity, thread_id)
        except (ThreadAlreadyRunningError, ThreadAwaitingReviewError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return record

    @api.delete("/api/conversations/{thread_id}", status_code=204)
    def delete_conversation(
        thread_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> Response:
        try:
            deleted = runtime.delete_conversation(identity, thread_id)
        except (ThreadAlreadyRunningError, ThreadAwaitingReviewError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return Response(status_code=204)

    @api.post("/api/threads")
    def create_thread(
        request: CreateThreadRequest | None = None,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> CreateThreadResponse:
        settings = {"model_name": request.model_name} if request and request.model_name else None
        try:
            return CreateThreadResponse(thread_id=runtime.create_thread(identity, settings))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/api/runtime")
    def runtime_info(
        _identity: RequestIdentity = Depends(local_request_identity),
    ) -> RuntimeInfo:
        return runtime.runtime_info()

    @api.get("/api/runtime/options")
    def runtime_options(
        _identity: RequestIdentity = Depends(local_request_identity),
    ) -> RuntimeOptions:
        return runtime.runtime_options()

    @api.get("/api/threads/{thread_id}/state")
    def get_thread_state(
        thread_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> ApiThreadState:
        try:
            return runtime.state(
                identity,
                thread_id,
                provider_api_key=provider_api_key,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc

    @api.post(
        "/api/threads/{thread_id}/attachments",
        response_model=AttachmentUploadResult,
    )
    async def stage_attachments(
        thread_id: str,
        http_request: Request,
        files: list[UploadFile] = File(...),
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> AttachmentUploadResult:
        try:
            runtime.authorize_thread(identity, thread_id)
            declared_length = http_request.headers.get("content-length")
            try:
                content_length = (
                    int(declared_length)
                    if declared_length is not None
                    else None
                )
            except ValueError:
                content_length = None
            if (
                content_length is not None
                and content_length
                > attachment_limits.max_message_bytes + _MULTIPART_OVERHEAD_BYTES
            ):
                raise HTTPException(
                    status_code=413,
                    detail="Attachment request body is too large",
                )
            uploads = await _read_bounded_attachment_uploads(
                files,
                runtime.attachment_limits,
            )
            return runtime.stage_attachments(identity, thread_id, uploads)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        except AttachmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.delete(
        "/api/threads/{thread_id}/attachments/{attachment_id}",
        status_code=204,
    )
    def discard_staged_attachment(
        thread_id: str,
        attachment_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> Response:
        try:
            runtime.discard_staged_attachment(identity, thread_id, attachment_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        except AttachmentError as exc:
            status_code = 404 if exc.code == "ATTACHMENT_NOT_FOUND" else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return Response(status_code=204)

    @api.get("/api/threads/{thread_id}/attachments/{attachment_id}")
    def get_conversation_attachment(
        thread_id: str,
        attachment_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> Response:
        try:
            artifact = runtime.conversation_attachment_bytes(
                identity,
                thread_id,
                attachment_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Attachment not found") from exc
        return Response(
            content=artifact.content,
            media_type=artifact.mime,
            headers={
                "Content-Disposition": _content_disposition(
                    "inline",
                    artifact.filename,
                ),
            },
        )

    @api.get(
        "/api/threads/{thread_id}/datasets/{dataset_id}/preview",
        response_model=DatasetPreview,
    )
    def dataset_preview(
        thread_id: str,
        dataset_id: str,
        limit: int = 100,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> DatasetPreview:
        try:
            return runtime.dataset_preview(identity, thread_id, dataset_id, limit=limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="Dataset file not found",
            ) from exc

    @api.get(
        "/api/threads/{thread_id}/datasets/{dataset_id}/schema",
        response_model=DatasetSchemaResponse,
    )
    def dataset_schema(
        thread_id: str,
        dataset_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> DatasetSchemaResponse:
        try:
            return runtime.dataset_schema(identity, thread_id, dataset_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="Dataset file not found",
            ) from exc

    @api.get(
        "/api/threads/{thread_id}/datasets/{dataset_id}/provenance",
        response_model=DatasetProvenance,
    )
    def dataset_provenance(
        thread_id: str,
        dataset_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> DatasetProvenance:
        try:
            return runtime.dataset_provenance(identity, thread_id, dataset_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get(
        "/api/threads/{thread_id}/analysis-runs/{analysis_id}",
        response_model=CompletedAnalysisResult,
    )
    def analysis_result(
        thread_id: str,
        analysis_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> CompletedAnalysisResult:
        try:
            return runtime.analysis_result(identity, thread_id, analysis_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Analysis result not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/threads/{thread_id}/datasets/{dataset_id}/download")
    def dataset_download(
        thread_id: str,
        dataset_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> Response:
        try:
            content = runtime.dataset_csv_bytes(identity, thread_id, dataset_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="Dataset file not found",
            ) from exc
        return Response(
            content=content,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{dataset_id}.csv"',
            },
        )

    @api.get("/api/threads/{thread_id}/artifacts/{artifact_id}")
    def file_artifact(
        thread_id: str,
        artifact_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> Response:
        try:
            artifact = runtime.file_artifact_bytes(identity, thread_id, artifact_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Artifact not found") from exc
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="Artifact file not found",
            ) from exc
        return Response(
            content=artifact.content,
            media_type=artifact.mime,
            headers={
                "Content-Disposition": _content_disposition(
                    "inline",
                    artifact.filename,
                ),
            },
        )

    @api.get(
        "/api/threads/{thread_id}/artifacts/{artifact_id}/table-preview",
        response_model=TablePreview,
    )
    def table_preview(
        thread_id: str,
        artifact_id: str,
        limit: int = 100,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> TablePreview:
        try:
            return runtime.table_preview(identity, thread_id, artifact_id, limit=limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Table artifact not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @api.post("/api/threads/{thread_id}/messages")
    def submit_message(
        thread_id: str,
        request: SubmitMessageRequest,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> ApiThreadState:
        provider_key = provider_key_for_work(identity, thread_id)
        try:
            runtime.submit_message(
                identity,
                thread_id,
                request.text,
                request.attachment_ids,
                request.model_name,
                provider_api_key=provider_key,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        except (ThreadAlreadyRunningError, ThreadAwaitingReviewError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AttachmentError as exc:
            status_code = 404 if exc.code == "ATTACHMENT_NOT_FOUND" else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return runtime.state(identity, thread_id)

    @api.post("/api/threads/{thread_id}/cancel")
    def cancel_run(
        thread_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> ApiThreadState:
        try:
            cancel_run_parameters = inspect.signature(runtime.cancel_run).parameters
            if len(cancel_run_parameters) == 1:
                return runtime.cancel_run(thread_id)
            return runtime.cancel_run(identity, thread_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        except CancellationRestoreError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "CANCELLATION_RESTORE_FAILED",
                    "message": str(exc),
                },
            ) from exc

    @api.post("/api/threads/{thread_id}/interrupts/{interrupt_id}/resume")
    def resume_interrupt(
        thread_id: str,
        interrupt_id: str,
        request: ResumeInterruptRequest,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> ApiThreadState:
        provider_key = provider_key_for_work(identity, thread_id)
        try:
            runtime.resume_interrupt(
                identity,
                thread_id,
                interrupt_id,
                request.model_dump(exclude_defaults=True),
                provider_api_key=provider_key,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        except ThreadAlreadyRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidInterruptDecisionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except StaleInterruptError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return runtime.state(identity, thread_id)

    @api.post("/api/threads/{thread_id}/reset")
    def reset_thread(
        thread_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> ResetThreadResponse:
        try:
            return ResetThreadResponse(thread_id=runtime.reset(identity, thread_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc

    @api.get("/api/threads/{thread_id}/export")
    def export_thread(
        thread_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> Response:
        try:
            content = json.dumps(runtime.export_thread(identity, thread_id), indent=2).encode("utf-8")
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{thread_id}-thread.json"',
            },
        )

    @api.get("/api/threads/{thread_id}/export.zip")
    def export_thread_archive(
        thread_id: str,
        identity: RequestIdentity = Depends(local_request_identity),
    ) -> Response:
        try:
            content = runtime.export_thread_archive(identity, thread_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        return Response(
            content=content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{thread_id}-thread.zip"',
            },
        )

    app.include_router(api)
    _mount_static_frontend(app, static_dir)

    return app


def _mount_static_frontend(app: FastAPI, static_dir: str | Path | None) -> None:
    if static_dir is None:
        return

    root = Path(static_dir)
    index_path = root / "index.html"
    if not index_path.exists():
        return

    assets_path = root / "assets"
    if assets_path.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=assets_path),
            name="frontend-assets",
        )

    @app.get("/", include_in_schema=False)
    def frontend_index() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/{frontend_path:path}", include_in_schema=False)
    def frontend_route(frontend_path: str) -> FileResponse:
        if frontend_path == "api" or frontend_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(index_path)
