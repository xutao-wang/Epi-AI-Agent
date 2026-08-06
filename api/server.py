from __future__ import annotations

import json
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
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from api.deployment import cors_allow_origin_regex
from api.auth import (
    LocalTokenVerifier,
    RequestIdentity,
    TokenVerifier,
    request_identity_dependency,
)
from api.provider_credentials import (
    OpenAIProviderKeyValidator,
    ProviderCredentialStore,
    ProviderKeyValidator,
)
from api.runtime import (
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
    PublicAppConfig,
    ProviderKeyRequest,
    ProviderKeyStatus,
    ResetThreadResponse,
    RenameConversationRequest,
    ResumeInterruptRequest,
    RuntimeInfo,
    RuntimeOptions,
    SubmitMessageRequest,
    TablePreview,
)
from utils.attachment_artifacts import AttachmentError, AttachmentLimits
from utils.provider_startup import ProviderCredentialError
from utils.review_interrupts import InvalidInterruptDecisionError


_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_PROVIDER_KEY_INVALID_KIND = "PROVIDER_KEY_INVALID"


class _RequestBodyTooLarge(Exception):
    pass


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


class AttachmentRequestBodyLimitMiddleware:
    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        is_attachment_upload = (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and str(scope.get("path") or "").endswith("/attachments")
        )
        if not is_attachment_upload:
            await self.app(scope, receive, send)
            return

        headers = {
            key.lower(): value
            for key, value in list(scope.get("headers") or [])
        }
        declared_length = headers.get(b"content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > self.max_bytes:
                    response = JSONResponse(
                        status_code=413,
                        content={"detail": "Attachment request body is too large"},
                    )
                    await response(scope, receive, send)
                    return
            except ValueError:
                pass

        received = 0
        response_started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body") or b"")
            if received > self.max_bytes:
                raise _RequestBodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            response = JSONResponse(
                status_code=413,
                content={"detail": "Attachment request body is too large"},
            )
            await response(scope, receive, send)


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
    static_dir: Path | None = None,
    cors_origin_regex: str | None = None,
    public_config: PublicAppConfig | None = None,
    token_verifier: TokenVerifier | None = None,
    credential_store: ProviderCredentialStore | None = None,
    provider_key_validator: ProviderKeyValidator | None = None,
) -> FastAPI:
    app = FastAPI(title="RePORT Agent API")

    @app.exception_handler(RequestValidationError)
    async def redact_provider_key_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        if request.url.path != "/api/session/provider-key":
            return await request_validation_exception_handler(request, exc)
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {
                        key: value
                        for key, value in error.items()
                        if key != "input"
                    }
                    for error in exc.errors()
                ]
            },
        )

    public_config = public_config or PublicAppConfig(
        auth_mode="local",
        provider_key_required=False,
    )
    require_identity = request_identity_dependency(
        token_verifier or LocalTokenVerifier()
    )
    credential_store = credential_store or ProviderCredentialStore()
    provider_key_validator = provider_key_validator or OpenAIProviderKeyValidator()
    attachment_limits = runtime.attachment_limits
    app.add_middleware(
        AttachmentRequestBodyLimitMiddleware,
        max_bytes=(
            attachment_limits.max_message_bytes
            + _MULTIPART_OVERHEAD_BYTES
        ),
    )
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

    @app.get("/api/public-config", response_model=PublicAppConfig)
    def get_public_config() -> PublicAppConfig:
        return public_config

    api = APIRouter(dependencies=[Depends(require_identity)])

    @api.get(
        "/api/session/provider-key",
        response_model=ProviderKeyStatus,
    )
    def provider_key_status(
        identity: RequestIdentity = Depends(require_identity),
    ) -> ProviderKeyStatus:
        return ProviderKeyStatus(configured=credential_store.has(identity))

    @api.put(
        "/api/session/provider-key",
        response_model=ProviderKeyStatus,
    )
    def configure_provider_key(
        request: ProviderKeyRequest,
        identity: RequestIdentity = Depends(require_identity),
    ) -> ProviderKeyStatus:
        api_key = request.api_key.strip()
        try:
            provider_key_validator.validate("openai", api_key)
            credential_store.put(identity, api_key)
        except ProviderCredentialError as exc:
            message = str(exc)
            if api_key:
                message = message.replace(api_key, "<redacted>")
            raise HTTPException(
                status_code=400,
                detail={
                    "kind": _PROVIDER_KEY_INVALID_KIND,
                    "message": message,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ProviderKeyStatus(configured=True)

    @api.delete("/api/session/provider-key", status_code=204)
    def clear_provider_key(
        identity: RequestIdentity = Depends(require_identity),
    ) -> Response:
        credential_store.delete(identity)
        runtime.release_session(identity.owner_user_id, identity.session_id)
        return Response(status_code=204)

    @api.get("/api/conversations", response_model=ConversationHistoryResponse)
    def list_conversations() -> ConversationHistoryResponse:
        return ConversationHistoryResponse(
            items=[item.__dict__ for item in runtime.list_conversations()]
        )

    @api.patch("/api/conversations/{thread_id}")
    def rename_conversation(
        thread_id: str,
        request: RenameConversationRequest,
    ):
        record = runtime.rename_conversation(thread_id, request.title)
        if record is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return record

    @api.post("/api/conversations/{thread_id}/open")
    def open_conversation(thread_id: str):
        record = runtime.open_conversation(thread_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return record

    @api.post("/api/conversations/{thread_id}/archive")
    def archive_conversation(thread_id: str):
        try:
            record = runtime.archive_conversation(thread_id)
        except (ThreadAlreadyRunningError, ThreadAwaitingReviewError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return record

    @api.post("/api/conversations/{thread_id}/restore")
    def restore_conversation(thread_id: str):
        try:
            record = runtime.restore_conversation(thread_id)
        except (ThreadAlreadyRunningError, ThreadAwaitingReviewError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return record

    @api.delete("/api/conversations/{thread_id}", status_code=204)
    def delete_conversation(thread_id: str) -> Response:
        try:
            deleted = runtime.delete_conversation(thread_id)
        except (ThreadAlreadyRunningError, ThreadAwaitingReviewError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return Response(status_code=204)

    @api.post("/api/threads")
    def create_thread(
        request: CreateThreadRequest | None = None,
    ) -> CreateThreadResponse:
        settings = {"model_name": request.model_name} if request and request.model_name else None
        try:
            return CreateThreadResponse(thread_id=runtime.create_thread(settings))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/api/runtime")
    def runtime_info() -> RuntimeInfo:
        return runtime.runtime_info()

    @api.get("/api/runtime/options")
    def runtime_options() -> RuntimeOptions:
        return runtime.runtime_options()

    @api.get("/api/threads/{thread_id}/state")
    def get_thread_state(thread_id: str) -> ApiThreadState:
        return runtime.state(thread_id)

    @api.post(
        "/api/threads/{thread_id}/attachments",
        response_model=AttachmentUploadResult,
    )
    async def stage_attachments(
        thread_id: str,
        files: list[UploadFile] = File(...),
    ) -> AttachmentUploadResult:
        try:
            uploads = await _read_bounded_attachment_uploads(
                files,
                runtime.attachment_limits,
            )
            return runtime.stage_attachments(thread_id, uploads)
        except AttachmentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.delete(
        "/api/threads/{thread_id}/attachments/{attachment_id}",
        status_code=204,
    )
    def discard_staged_attachment(
        thread_id: str,
        attachment_id: str,
    ) -> Response:
        try:
            runtime.discard_staged_attachment(thread_id, attachment_id)
        except AttachmentError as exc:
            status_code = 404 if exc.code == "ATTACHMENT_NOT_FOUND" else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return Response(status_code=204)

    @api.get("/api/threads/{thread_id}/attachments/{attachment_id}")
    def get_conversation_attachment(
        thread_id: str,
        attachment_id: str,
    ) -> Response:
        try:
            artifact = runtime.conversation_attachment_bytes(
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
    ) -> DatasetPreview:
        try:
            return runtime.dataset_preview(thread_id, dataset_id, limit=limit)
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
    def dataset_schema(thread_id: str, dataset_id: str) -> DatasetSchemaResponse:
        try:
            return runtime.dataset_schema(thread_id, dataset_id)
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
    def dataset_provenance(thread_id: str, dataset_id: str) -> DatasetProvenance:
        try:
            return runtime.dataset_provenance(thread_id, dataset_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Dataset not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get(
        "/api/threads/{thread_id}/analysis-runs/{analysis_id}",
        response_model=CompletedAnalysisResult,
    )
    def analysis_result(thread_id: str, analysis_id: str) -> CompletedAnalysisResult:
        try:
            return runtime.analysis_result(thread_id, analysis_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Analysis result not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @api.get("/api/threads/{thread_id}/datasets/{dataset_id}/download")
    def dataset_download(thread_id: str, dataset_id: str) -> Response:
        try:
            content = runtime.dataset_csv_bytes(thread_id, dataset_id)
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
    def file_artifact(thread_id: str, artifact_id: str) -> Response:
        try:
            artifact = runtime.file_artifact_bytes(thread_id, artifact_id)
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
    ) -> TablePreview:
        try:
            return runtime.table_preview(thread_id, artifact_id, limit=limit)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Table artifact not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @api.post("/api/threads/{thread_id}/messages")
    def submit_message(thread_id: str, request: SubmitMessageRequest) -> ApiThreadState:
        try:
            runtime.submit_message(
                thread_id,
                request.text,
                request.attachment_ids,
                request.model_name,
                request.active_study_id,
            )
        except (ThreadAlreadyRunningError, ThreadAwaitingReviewError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AttachmentError as exc:
            status_code = 404 if exc.code == "ATTACHMENT_NOT_FOUND" else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return runtime.state(thread_id)

    @api.post("/api/threads/{thread_id}/interrupts/{interrupt_id}/resume")
    def resume_interrupt(
        thread_id: str,
        interrupt_id: str,
        request: ResumeInterruptRequest,
    ) -> ApiThreadState:
        try:
            runtime.resume_interrupt(
                thread_id,
                interrupt_id,
                request.model_dump(exclude_defaults=True),
            )
        except ThreadAlreadyRunningError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except InvalidInterruptDecisionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except StaleInterruptError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return runtime.state(thread_id)

    @api.post("/api/threads/{thread_id}/reset")
    def reset_thread(thread_id: str) -> ResetThreadResponse:
        return ResetThreadResponse(thread_id=runtime.reset(thread_id))

    @api.get("/api/threads/{thread_id}/export")
    def export_thread(thread_id: str) -> Response:
        return Response(
            content=json.dumps(runtime.export_thread(thread_id), indent=2).encode("utf-8"),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{thread_id}-thread.json"',
            },
        )

    @api.get("/api/threads/{thread_id}/export.zip")
    def export_thread_archive(thread_id: str) -> Response:
        return Response(
            content=runtime.export_thread_archive(thread_id),
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
