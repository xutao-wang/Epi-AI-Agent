import type {
  ApiThreadState,
  AttachmentUploadResult,
  CompletedAnalysisResult,
  DatasetPreview,
  DatasetProvenance,
  DatasetSchemaResponse,
  ResumeInterruptPayload,
  RuntimeInfo,
  RuntimeOptions,
  RuntimeSettings,
  ConversationSummary,
  TablePreview,
} from "./types";

type FetchImpl = typeof fetch;

interface CreateThreadResponse {
  thread_id: string;
}

export interface CreateApiClientOptions {
  apiBase?: string;
  fetchImpl?: FetchImpl;
}

export class ApiError extends Error {
  status: number;
  detail?: unknown;

  constructor(status: number, detail?: unknown) {
    super(`API request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function apiUrl(apiBase: string, path: string) {
  return `${apiBase.replace(/\/+$/, "")}${path}`;
}

function pathParam(value: string) {
  return encodeURIComponent(value);
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new ApiError(response.status, await responseDetail(response));
  }

  return (await response.json()) as T;
}

async function responseDetail(response: Response): Promise<unknown> {
  try {
    const body = (await response.json()) as unknown;
    if (body && typeof body === "object" && "detail" in body) {
      return (body as { detail: unknown }).detail;
    }
  } catch {
    return undefined;
  }

  return undefined;
}

function jsonPostInit(body: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export async function createThread(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  modelName?: string,
): Promise<CreateThreadResponse> {
  const init = modelName
    ? jsonPostInit({ model_name: modelName })
    : { method: "POST" };
  const response = await fetchImpl(
    apiUrl(apiBase, "/api/threads"),
    init,
  );
  return parseJsonResponse<CreateThreadResponse>(response);
}

export async function listConversations(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
): Promise<{ items: ConversationSummary[] }> {
  return parseJsonResponse(await fetchImpl(apiUrl(apiBase, "/api/conversations")));
}

export async function renameConversation(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  title: string,
): Promise<ConversationSummary> {
  return parseJsonResponse(await fetchImpl(
    apiUrl(apiBase, `/api/conversations/${pathParam(threadId)}`),
    { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ title }) },
  ));
}

export async function markConversationOpened(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
): Promise<ConversationSummary> {
  return parseJsonResponse(await fetchImpl(
    apiUrl(apiBase, `/api/conversations/${pathParam(threadId)}/open`),
    { method: "POST" },
  ));
}

export async function archiveConversation(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
): Promise<ConversationSummary> {
  return parseJsonResponse(await fetchImpl(
    apiUrl(apiBase, `/api/conversations/${pathParam(threadId)}/archive`),
    { method: "POST" },
  ));
}

export async function restoreConversation(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
): Promise<ConversationSummary> {
  return parseJsonResponse(await fetchImpl(
    apiUrl(apiBase, `/api/conversations/${pathParam(threadId)}/restore`),
    { method: "POST" },
  ));
}

export async function deleteConversation(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
): Promise<void> {
  const response = await fetchImpl(
    apiUrl(apiBase, `/api/conversations/${pathParam(threadId)}`),
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new ApiError(response.status, await responseDetail(response));
  }
}

export async function getRuntimeInfo(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
): Promise<RuntimeInfo> {
  const response = await fetchImpl(apiUrl(apiBase, "/api/runtime"));
  return parseJsonResponse<RuntimeInfo>(response);
}

export async function getRuntimeOptions(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
): Promise<RuntimeOptions> {
  const response = await fetchImpl(apiUrl(apiBase, "/api/runtime/options"));
  return parseJsonResponse<RuntimeOptions>(response);
}

export async function resetThread(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
): Promise<CreateThreadResponse> {
  const response = await fetchImpl(
    apiUrl(apiBase, `/api/threads/${pathParam(threadId)}/reset`),
    { method: "POST" },
  );
  return parseJsonResponse<CreateThreadResponse>(response);
}

export async function getThreadState(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
): Promise<ApiThreadState> {
  const response = await fetchImpl(
    apiUrl(apiBase, `/api/threads/${pathParam(threadId)}/state`),
  );
  return parseJsonResponse<ApiThreadState>(response);
}

export async function cancelRun(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
): Promise<ApiThreadState> {
  const response = await fetchImpl(
    apiUrl(apiBase, `/api/threads/${pathParam(threadId)}/cancel`),
    { method: "POST" },
  );
  return parseJsonResponse<ApiThreadState>(response);
}

export async function submitMessage(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  text: string,
  attachmentIds: string[] = [],
  modelName?: string,
): Promise<ApiThreadState> {
  const body = attachmentIds.length
    ? { text, attachment_ids: attachmentIds }
    : { text };
  if (modelName) {
    Object.assign(body, { model_name: modelName });
  }
  const response = await fetchImpl(
    apiUrl(apiBase, `/api/threads/${pathParam(threadId)}/messages`),
    jsonPostInit(body),
  );
  return parseJsonResponse<ApiThreadState>(response);
}

export async function uploadAttachments(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  files: File[],
): Promise<AttachmentUploadResult> {
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  const response = await fetchImpl(
    apiUrl(apiBase, `/api/threads/${pathParam(threadId)}/attachments`),
    { method: "POST", body },
  );
  return parseJsonResponse<AttachmentUploadResult>(response);
}

export async function discardStagedAttachment(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  attachmentId: string,
): Promise<void> {
  const response = await fetchImpl(
    apiUrl(
      apiBase,
      `/api/threads/${pathParam(threadId)}/attachments/${pathParam(attachmentId)}`,
    ),
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new ApiError(response.status, await responseDetail(response));
  }
}

function attachmentUrl(
  apiBase = "",
  threadId: string,
  attachmentId: string,
) {
  return apiUrl(
    apiBase,
    `/api/threads/${pathParam(threadId)}/attachments/${pathParam(attachmentId)}`,
  );
}

export async function resumeInterrupt(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  interruptId: string,
  payload: ResumeInterruptPayload,
): Promise<ApiThreadState> {
  const response = await fetchImpl(
    apiUrl(
      apiBase,
      `/api/threads/${pathParam(threadId)}/interrupts/${pathParam(interruptId)}/resume`,
    ),
    jsonPostInit(payload),
  );
  return parseJsonResponse<ApiThreadState>(response);
}

export async function getDatasetPreview(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  datasetId: string,
  limit = 100,
): Promise<DatasetPreview> {
  const response = await fetchImpl(
    apiUrl(
      apiBase,
      `/api/threads/${pathParam(threadId)}/datasets/${pathParam(datasetId)}/preview?limit=${encodeURIComponent(String(limit))}`,
    ),
  );
  return parseJsonResponse<DatasetPreview>(response);
}

export async function getDatasetSchema(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  datasetId: string,
): Promise<DatasetSchemaResponse> {
  const response = await fetchImpl(
    apiUrl(
      apiBase,
      `/api/threads/${pathParam(threadId)}/datasets/${pathParam(datasetId)}/schema`,
    ),
  );
  return parseJsonResponse<DatasetSchemaResponse>(response);
}

export async function getDatasetProvenance(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  datasetId: string,
): Promise<DatasetProvenance> {
  const response = await fetchImpl(
    apiUrl(
      apiBase,
      `/api/threads/${pathParam(threadId)}/datasets/${pathParam(datasetId)}/provenance`,
    ),
  );
  return parseJsonResponse<DatasetProvenance>(response);
}

export async function getAnalysisResult(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  analysisId: string,
): Promise<CompletedAnalysisResult> {
  const response = await fetchImpl(
    apiUrl(
      apiBase,
      `/api/threads/${pathParam(threadId)}/analysis-runs/${pathParam(analysisId)}`,
    ),
  );
  return parseJsonResponse<CompletedAnalysisResult>(response);
}

function datasetDownloadUrl(
  apiBase = "",
  threadId: string,
  datasetId: string,
) {
  return apiUrl(
    apiBase,
    `/api/threads/${pathParam(threadId)}/datasets/${pathParam(datasetId)}/download`,
  );
}

function artifactUrl(apiBase = "", threadId: string, artifactId: string) {
  return apiUrl(
    apiBase,
    `/api/threads/${pathParam(threadId)}/artifacts/${pathParam(artifactId)}`,
  );
}

export async function getTablePreview(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  artifactId: string,
  limit = 100,
): Promise<TablePreview> {
  const response = await fetchImpl(
    apiUrl(
      apiBase,
      `/api/threads/${pathParam(threadId)}/artifacts/${pathParam(artifactId)}/table-preview?limit=${encodeURIComponent(String(limit))}`,
    ),
  );
  return parseJsonResponse<TablePreview>(response);
}

export async function getArtifactText(
  fetchImpl: FetchImpl = fetch,
  apiBase = "",
  threadId: string,
  artifactId: string,
): Promise<string> {
  const response = await fetchImpl(artifactUrl(apiBase, threadId, artifactId));
  if (!response.ok) {
    throw new ApiError(response.status, await responseDetail(response));
  }
  return response.text();
}

function threadExportUrl(apiBase = "", threadId: string) {
  return apiUrl(apiBase, `/api/threads/${pathParam(threadId)}/export.zip`);
}

async function fetchBlob(fetchImpl: FetchImpl, url: string): Promise<Blob> {
  const response = await fetchImpl(url);
  if (!response.ok) {
    throw new ApiError(response.status, await responseDetail(response));
  }
  return response.blob();
}

async function fetchAttachmentBlob(
  fetchImpl: FetchImpl,
  apiBase: string,
  threadId: string,
  attachmentId: string,
): Promise<Blob> {
  return fetchBlob(fetchImpl, attachmentUrl(apiBase, threadId, attachmentId));
}

async function fetchArtifactBlob(
  fetchImpl: FetchImpl,
  apiBase: string,
  threadId: string,
  artifactId: string,
): Promise<Blob> {
  return fetchBlob(fetchImpl, artifactUrl(apiBase, threadId, artifactId));
}

async function fetchDatasetBlob(
  fetchImpl: FetchImpl,
  apiBase: string,
  threadId: string,
  datasetId: string,
): Promise<Blob> {
  return fetchBlob(fetchImpl, datasetDownloadUrl(apiBase, threadId, datasetId));
}

async function fetchThreadExportBlob(
  fetchImpl: FetchImpl,
  apiBase: string,
  threadId: string,
): Promise<Blob> {
  return fetchBlob(fetchImpl, threadExportUrl(apiBase, threadId));
}

export function createApiClient({
  apiBase = "",
  fetchImpl = fetch,
}: CreateApiClientOptions = {}) {
  const localFetch: FetchImpl = (input, init = {}) => {
    const headers = new Headers(init.headers);
    return fetchImpl(input, { ...init, headers });
  };

  return {
    createThread(modelName?: string) {
      return createThread(localFetch, apiBase, modelName);
    },
    getRuntimeInfo() {
      return getRuntimeInfo(localFetch, apiBase);
    },
    listConversations() {
      return listConversations(localFetch, apiBase);
    },
    renameConversation(threadId: string, title: string) {
      return renameConversation(localFetch, apiBase, threadId, title);
    },
    markConversationOpened(threadId: string) {
      return markConversationOpened(localFetch, apiBase, threadId);
    },
    archiveConversation(threadId: string) {
      return archiveConversation(localFetch, apiBase, threadId);
    },
    restoreConversation(threadId: string) {
      return restoreConversation(localFetch, apiBase, threadId);
    },
    deleteConversation(threadId: string) {
      return deleteConversation(localFetch, apiBase, threadId);
    },
    getRuntimeOptions() {
      return getRuntimeOptions(localFetch, apiBase);
    },
    resetThread(threadId: string) {
      return resetThread(localFetch, apiBase, threadId);
    },
    getThreadState(threadId: string) {
      return getThreadState(localFetch, apiBase, threadId);
    },
    cancelRun(threadId: string) {
      return cancelRun(localFetch, apiBase, threadId);
    },
    submitMessage(
      threadId: string,
      text: string,
      attachmentIds: string[] = [],
      modelName?: string,
    ) {
      return submitMessage(
        localFetch,
        apiBase,
        threadId,
        text,
        attachmentIds,
        modelName,
      );
    },
    uploadAttachments(threadId: string, files: File[]) {
      return uploadAttachments(localFetch, apiBase, threadId, files);
    },
    discardStagedAttachment(threadId: string, attachmentId: string) {
      return discardStagedAttachment(
        localFetch,
        apiBase,
        threadId,
        attachmentId,
      );
    },
    fetchAttachmentBlob(threadId: string, attachmentId: string) {
      return fetchAttachmentBlob(
        localFetch,
        apiBase,
        threadId,
        attachmentId,
      );
    },
    resumeInterrupt(
      threadId: string,
      interruptId: string,
      payload: ResumeInterruptPayload,
    ) {
      return resumeInterrupt(localFetch, apiBase, threadId, interruptId, payload);
    },
    getDatasetPreview(threadId: string, datasetId: string, limit = 100) {
      return getDatasetPreview(localFetch, apiBase, threadId, datasetId, limit);
    },
    getDatasetSchema(threadId: string, datasetId: string) {
      return getDatasetSchema(localFetch, apiBase, threadId, datasetId);
    },
    getDatasetProvenance(threadId: string, datasetId: string) {
      return getDatasetProvenance(localFetch, apiBase, threadId, datasetId);
    },
    getAnalysisResult(threadId: string, analysisId: string) {
      return getAnalysisResult(localFetch, apiBase, threadId, analysisId);
    },
    fetchArtifactBlob(threadId: string, artifactId: string) {
      return fetchArtifactBlob(localFetch, apiBase, threadId, artifactId);
    },
    fetchDatasetBlob(threadId: string, datasetId: string) {
      return fetchDatasetBlob(localFetch, apiBase, threadId, datasetId);
    },
    getTablePreview(threadId: string, artifactId: string, limit = 100) {
      return getTablePreview(localFetch, apiBase, threadId, artifactId, limit);
    },
    getArtifactText(threadId: string, artifactId: string) {
      return getArtifactText(localFetch, apiBase, threadId, artifactId);
    },
    fetchThreadExportBlob(threadId: string) {
      return fetchThreadExportBlob(localFetch, apiBase, threadId);
    },
  };
}

export type ApiClient = ReturnType<typeof createApiClient>;
