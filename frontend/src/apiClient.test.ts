import { describe, expect, it, vi } from "vitest";
import {
  ApiError,
  createApiClient,
  createThread,
  deleteConversation,
  discardStagedAttachment,
  getArtifactText,
  getThreadState,
  listConversations,
  markConversationOpened,
  archiveConversation,
  cancelRun,
  renameConversation,
  restoreConversation,
  getRuntimeInfo,
  getRuntimeOptions,
  resumeInterrupt,
  resetThread,
  submitMessage,
  uploadAttachments,
} from "./apiClient";
import type {
  ApiThreadState,
  ResumeInterruptPayload,
  RuntimeSettings,
} from "./types";

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status: 200,
    ...init,
  });
}

const threadState: ApiThreadState = {
  thread_id: "thread-1",
  run: {
    state: "idle",
    steps: 0,
    error: null,
    started_at: null,
    updated_at: null,
  },
  conversation: [],
  activity_runs: [
    {
      id: "run-1",
      thread_id: "thread-1",
      user_message_id: "user-1",
      state: "running",
      activities: [
        {
          id: "activity-1",
          sequence: 1,
          label: "Understanding your request",
          status: "running",
          tool_name: null,
          tool_call_id: null,
          created_at: "2026-08-11T00:00:00+00:00",
          updated_at: "2026-08-11T00:00:00+00:00",
        },
      ],
      created_at: "2026-08-11T00:00:00+00:00",
      updated_at: "2026-08-11T00:00:00+00:00",
    },
  ],
  active_interrupt: null,
  runtime_settings: null,
  runtime_settings_locked: false,
  datasets: [],
  file_artifacts: [],
  output: {},
  diagnostics: {},
  embedding_startup_status: {
    profile_id: "configured",
    profile_label: "Configured embedding profile",
    provider: "unknown",
    index_compatibility: "",
    available: true,
    retrieval_mode: "hybrid_vector_lexical",
    reason_code: null,
    message: "",
    compatible_study_ids: [],
    incompatible_study_ids: [],
  },
};

const runtimeInfo = {
  model_name: "gpt-5.4",
  temperature: 0.1,
  top_p: 0.9,
  max_steps: 4,
  timeout_seconds: 300,
  db_rag_embedding_model: "OpenAI/text-embedding-3-large",
  db_rag_reranker_model: "disabled",
};

const runtimeSettings: RuntimeSettings = {
  model_name: "gpt-5.4-mini",
  temperature: 0.2,
  top_p: null,
  max_steps: 8,
  timeout_seconds: 600,
  db_rag_embedding_model: "OpenAI/text-embedding-3-large",
  db_rag_reranker_model: "disabled",
};

const runtimeOptions = {
  defaults: runtimeInfo,
  capabilities: {
    publication_knowledge: {
      status: "available",
      message: "Publication knowledge is available.",
    },
    db_rag_dataset: {
      status: "not_configured",
      message: "DB-RAG dataset is not configured.",
    },
  },
  models: [
    {
      id: "gpt-5.4",
      label: "gpt-5.4 (Standard)",
      provider: "openai",
      provider_label: "OpenAI",
      summary: "Reliable general-purpose default.",
      initial_output_tokens: 8_192,
      automatic_output_token_ceiling: 16_384,
      user_output_token_increment: 8_192,
      absolute_output_token_ceiling: 24_576,
      request_timeout_seconds: 120,
      workflow_timeout_seconds: 300,
      automatic_output_cost: "$0.25",
      incremental_output_cost: "$0.13",
    },
  ],
};

const approvePayload: ResumeInterruptPayload = {
  action: "approve",
  selected_column_keys: ["projects.project_id"],
};

describe("apiClient", () => {
  it("sends local requests without authentication or session headers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [] }));
    const client = createApiClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
    });

    await client.listConversations();

    const request = new Request(fetchMock.mock.calls[0][0], fetchMock.mock.calls[0][1]);
    expect(request.headers.get("Authorization")).toBeNull();
    expect(request.headers.get("X-Epi-Session-ID")).toBeNull();
  });

  it("fetches protected attachment blobs through the bound client", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("image", { headers: { "Content-Type": "image/png" } }),
    );
    const client = createApiClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
    });

    await expect(client.fetchAttachmentBlob("thread-1", "attachment-1")).resolves.toMatchObject({
      size: 5,
      type: "image/png",
    });

    const request = new Request(fetchMock.mock.calls[0][0], fetchMock.mock.calls[0][1]);
    expect(request.url).toBe("http://api.test/api/threads/thread-1/attachments/attachment-1");
  });

  it("fetches artifact, dataset, and thread-export blobs through the bound client", async () => {
    const fetchMock = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) =>
      Promise.resolve(new Response("file", { headers: { "Content-Type": "text/csv" } })),
    );
    const client = createApiClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
    });

    await Promise.all([
      client.fetchArtifactBlob("thread-1", "artifact-1"),
      client.fetchDatasetBlob("thread-1", "dataset-1"),
      client.fetchThreadExportBlob("thread-1"),
    ]);

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://api.test/api/threads/thread-1/artifacts/artifact-1",
      "http://api.test/api/threads/thread-1/datasets/dataset-1/download",
      "http://api.test/api/threads/thread-1/export.zip",
    ]);
  });

  it("lists saved conversations", async () => {
    const response = {
      items: [
        {
          thread_id: "thread-1",
          title: "TB cohort survival",
          title_source: "automatic",
          model_name: "gpt-5.6-terra",
          created_at: "2026-07-30T00:00:00+00:00",
          updated_at: "2026-07-30T00:00:00+00:00",
        },
      ],
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(response));

    await expect(listConversations(fetchMock)).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith("/api/conversations");
  });

  it("renames a saved conversation", async () => {
    const response = {
      thread_id: "thread-1",
      title: "Renamed conversation",
      title_source: "manual",
      model_name: "gpt-5.6-terra",
      created_at: "2026-07-30T00:00:00+00:00",
      updated_at: "2026-07-30T00:01:00+00:00",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(response));

    await expect(
      renameConversation(fetchMock, "", "thread-1", "Renamed conversation"),
    ).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith("/api/conversations/thread-1", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "Renamed conversation" }),
    });
  });

  it("records when a saved conversation is opened", async () => {
    const response = {
      thread_id: "thread-1",
      title: "TB cohort survival",
      title_source: "automatic",
      model_name: "gpt-5.6-terra",
      created_at: "2026-07-30T00:00:00+00:00",
      updated_at: "2026-07-30T00:01:00+00:00",
      last_opened_at: "2026-07-30T18:24:00+00:00",
      archived_at: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(response));

    await expect(markConversationOpened(fetchMock, "", "thread-1")).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith("/api/conversations/thread-1/open", {
      method: "POST",
    });
  });

  it("archives, restores, and deletes a saved conversation", async () => {
    const response = {
      thread_id: "thread-1",
      title: "TB cohort survival",
      title_source: "automatic",
      model_name: "gpt-5.6-terra",
      created_at: "2026-07-30T00:00:00+00:00",
      updated_at: "2026-07-30T00:01:00+00:00",
      archived_at: null,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ ...response, archived_at: "2026-07-30T00:02:00+00:00" }))
      .mockResolvedValueOnce(jsonResponse(response))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await expect(archiveConversation(fetchMock, "", "thread-1")).resolves.toEqual({
      ...response,
      archived_at: "2026-07-30T00:02:00+00:00",
    });
    await expect(restoreConversation(fetchMock, "", "thread-1")).resolves.toEqual(response);
    await expect(deleteConversation(fetchMock, "", "thread-1")).resolves.toBeUndefined();

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/conversations/thread-1/archive",
      { method: "POST" },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/conversations/thread-1/restore",
      { method: "POST" },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/conversations/thread-1",
      { method: "DELETE" },
    );
  });

  it("creates a thread without a JSON body", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ thread_id: "thread-1" }));

    await expect(createThread(fetchMock)).resolves.toEqual({
      thread_id: "thread-1",
    });

    expect(fetchMock).toHaveBeenCalledWith("/api/threads", {
      method: "POST",
    });
  });

  it("creates a thread with a model choice", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ thread_id: "thread-1" }));

    await expect(createThread(fetchMock, "", runtimeSettings.model_name)).resolves.toEqual({
      thread_id: "thread-1",
    });

    expect(fetchMock).toHaveBeenCalledWith("/api/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model_name: runtimeSettings.model_name }),
    });
  });

  it("gets thread state", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(threadState));

    await expect(
      getThreadState(fetchMock, "", "thread-1"),
    ).resolves.toEqual(threadState);

    expect(fetchMock).toHaveBeenCalledWith("/api/threads/thread-1/state");
  });

  it("cancels the active run and returns the restored thread state", async () => {
    const cancelledState = {
      ...threadState,
      run: { ...threadState.run, state: "cancelled" as const },
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(cancelledState));

    await expect(
      cancelRun(fetchMock, "http://api.test", "thread with/slash"),
    ).resolves.toEqual(cancelledState);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/threads/thread%20with%2Fslash/cancel",
      { method: "POST" },
    );
  });

  it("gets runtime info", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(runtimeInfo));

    await expect(getRuntimeInfo(fetchMock, "http://api.test")).resolves.toEqual(
      runtimeInfo,
    );

    expect(fetchMock).toHaveBeenCalledWith("http://api.test/api/runtime");
  });

  it("gets runtime options", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(runtimeOptions));
    const client = createApiClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
    });

    await expect(client.getRuntimeOptions()).resolves.toEqual(runtimeOptions);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/runtime/options",
      { headers: expect.any(Headers) },
    );
  });

  it("gets runtime options through the standalone helper", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(runtimeOptions));

    await expect(getRuntimeOptions(fetchMock, "")).resolves.toEqual(
      runtimeOptions,
    );

    expect(fetchMock).toHaveBeenCalledWith("/api/runtime/options");
  });

  it("submits a message", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(threadState));

    await expect(
      submitMessage(fetchMock, "", "thread-1", "Find NIH grants"),
    ).resolves.toEqual(threadState);

    expect(fetchMock).toHaveBeenCalledWith("/api/threads/thread-1/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: "Find NIH grants" }),
    });
  });

  it("submits exact attachment ids with a message", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(threadState));

    await submitMessage(
      fetchMock,
      "",
      "thread-1",
      "Analyze these files",
      ["attachment-a", "attachment-b"],
    );

    expect(fetchMock).toHaveBeenCalledWith("/api/threads/thread-1/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: "Analyze these files",
        attachment_ids: ["attachment-a", "attachment-b"],
      }),
    });
  });

  it("resumes an interrupt with a backend-shaped payload", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(threadState));

    await expect(
      resumeInterrupt(fetchMock, "", "thread-1", "interrupt-1", approvePayload),
    ).resolves.toEqual(threadState);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/threads/thread-1/interrupts/interrupt-1/resume",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(approvePayload),
      },
    );
  });

  it("posts a typed revision payload unchanged", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(threadState));
    const revisePayload: ResumeInterruptPayload = {
      action: "revise",
      feedback: "Use robust standard errors.",
    };

    await resumeInterrupt(
      fetchMock,
      "",
      "thread-1",
      "interrupt-1",
      revisePayload,
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/threads/thread-1/interrupts/interrupt-1/resume",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(revisePayload),
      },
    );
  });

  it("normalizes trailing slash apiBase values", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(threadState));

    await getThreadState(fetchMock, "http://127.0.0.1:8000/", "thread-1");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/threads/thread-1/state",
    );
  });

  it("encodes thread and interrupt path params", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(threadState));

    await resumeInterrupt(
      fetchMock,
      "",
      "thread with/slash",
      "interrupt with/slash",
      approvePayload,
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/threads/thread%20with%2Fslash/interrupts/interrupt%20with%2Fslash/resume",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(approvePayload),
      },
    );
  });

  it("throws ApiError with status and FastAPI detail", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        { detail: "duplicate running" },
        { status: 409, statusText: "Conflict" },
      ),
    );

    await expect(
      submitMessage(fetchMock, "", "thread-1", "Find NIH grants"),
    ).rejects.toMatchObject({
      name: "ApiError",
      message: "API request failed with status 409",
      status: 409,
      detail: "duplicate running",
    });
  });

  it("throws ApiError for duplicate running interrupt resumes", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ detail: "thread is already running" }, { status: 409 }),
    );

    const request = resumeInterrupt(fetchMock, "", "thread-1", "interrupt-1", {
      action: "revise",
      feedback: "Try broader columns.",
    });

    await expect(request).rejects.toBeInstanceOf(ApiError);
    await expect(request).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      detail: "thread is already running",
    });
  });

  it("throws ApiError for get-state failures", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: "not found" }, { status: 404 }));

    await expect(
      getThreadState(fetchMock, "", "missing-thread"),
    ).rejects.toMatchObject({
      status: 404,
      detail: "not found",
    });
  });

  it("supports a bound factory client", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(runtimeInfo))
      .mockResolvedValueOnce(jsonResponse(threadState))
      .mockResolvedValueOnce(jsonResponse(threadState));
    const client = createApiClient({
      apiBase: "http://127.0.0.1:8000/",
      fetchImpl: fetchMock,
    });

    await client.getRuntimeInfo();
    await client.submitMessage("thread-1", "Find NIH grants");
    await client.resumeInterrupt("thread-1", "interrupt-1", {
      action: "cancel",
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8000/api/runtime",
      { headers: expect.any(Headers) },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://127.0.0.1:8000/api/threads/thread-1/messages",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({ text: "Find NIH grants" }),
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8000/api/threads/thread-1/interrupts/interrupt-1/resume",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({ action: "cancel" }),
      },
    );
  });

  it("supports factory createThread and getThreadState methods", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ thread_id: "thread-1" }))
      .mockResolvedValueOnce(jsonResponse(threadState));
    const client = createApiClient({ fetchImpl: fetchMock });

    await expect(client.createThread()).resolves.toEqual({
      thread_id: "thread-1",
    });
    await expect(client.getThreadState("thread-1")).resolves.toEqual(threadState);

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/threads", {
      method: "POST",
      headers: expect.any(Headers),
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/threads/thread-1/state",
      { headers: expect.any(Headers) },
    );
  });

  it("cancels a run without authentication headers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(threadState));
    const client = createApiClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
    });

    await client.cancelRun("thread-1");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.test/api/threads/thread-1/cancel");
    expect(init).toEqual({ method: "POST", headers: expect.any(Headers) });
    const request = new Request(url, init);
    expect(request.headers.get("Authorization")).toBeNull();
    expect(request.headers.get("X-Epi-Session-ID")).toBeNull();
  });

  it("resets a thread", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ thread_id: "thread-2" }));

    await expect(resetThread(fetchMock, "", "thread-1")).resolves.toEqual({
      thread_id: "thread-2",
    });

    expect(fetchMock).toHaveBeenCalledWith("/api/threads/thread-1/reset", {
      method: "POST",
    });
  });

  it("uploads all selected message attachments under the files key", async () => {
    const result = {
      attachments: [],
      errors: [],
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(result));
    const csv = new File(["id\n1\n"], "cohort.csv", { type: "text/csv" });
    const xml = new File(["<variables/>"], "annotations.xml", {
      type: "application/xml",
    });

    await expect(
      uploadAttachments(
        fetchMock,
        "http://api.test",
        "thread-1",
        [csv, xml],
      ),
    ).resolves.toEqual(result);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api.test/api/threads/thread-1/attachments");
    expect(init.method).toBe("POST");
    expect(init.body.getAll("files")).toEqual([csv, xml]);
  });

  it("discards a staged attachment", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(null, { status: 204 }),
    );

    await discardStagedAttachment(
      fetchMock,
      "http://api.test",
      "thread with/slash",
      "attachment with/slash",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/threads/thread%20with%2Fslash/attachments/attachment%20with%2Fslash",
      { method: "DELETE" },
    );
  });

  it("fetches dataset preview rows", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        dataset_id: "subset-1",
        columns: ["subject_id"],
        rows: [{ subject_id: "SUB-1" }],
        row_count: 1,
      }),
    );
    const client = createApiClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
    });

    const preview = await client.getDatasetPreview("thread-1", "subset-1", 25);

    expect(preview.rows).toEqual([{ subject_id: "SUB-1" }]);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/threads/thread-1/datasets/subset-1/preview?limit=25",
      { headers: expect.any(Headers) },
    );
  });

  it("fetches dataset schema", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        dataset_id: "subset-1",
        schema: { subject_id: { dataType: "string" } },
      }),
    );
    const client = createApiClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
    });

    const schema = await client.getDatasetSchema("thread-1", "subset-1");

    expect(schema.schema).toEqual({ subject_id: { dataType: "string" } });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/threads/thread-1/datasets/subset-1/schema",
      { headers: expect.any(Headers) },
    );
  });

  it("fetches exact dataset SQL provenance", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        dataset_id: "subset-1",
        dataset_version: 1,
        sql: 'SELECT "AGE" FROM "Index Baseline"',
        sql_artifact: { id: "sql-1", kind: "validated_sql", version: 1 },
        sql_sha256: "hash",
      }),
    );
    const client = createApiClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
    });

    await expect(client.getDatasetProvenance("thread-1", "subset-1"))
      .resolves.toMatchObject({ dataset_id: "subset-1", sql: 'SELECT "AGE" FROM "Index Baseline"' });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/threads/thread-1/datasets/subset-1/provenance",
      { headers: expect.any(Headers) },
    );
  });

  it("fetches an exact completed analysis result", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        analysis_run_id: "analysis-1",
        analysis_run_version: 1,
        method: "custom_python",
        python_code: "print('exact')",
        output_text: "exact output",
        dataset: { id: "subset-1", kind: "analysis_dataset", version: 1 },
        tables: [],
        figures: [],
      }),
    );
    const client = createApiClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
    });

    await expect(client.getAnalysisResult("thread-1", "analysis-1"))
      .resolves.toMatchObject({ analysis_run_id: "analysis-1", python_code: "print('exact')" });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/threads/thread-1/analysis-runs/analysis-1",
      { headers: expect.any(Headers) },
    );
  });

  it("gets a bounded analysis table preview", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        columns: ["group", "n"],
        rows: [{ group: "Good", n: "10" }],
        row_count: 1,
      }),
    );

    const client = createApiClient({ apiBase: "http://api.test", fetchImpl: fetchMock });
    expect(client).toHaveProperty("getTablePreview");
    await expect(client.getTablePreview("thread-1", "table-1", 100)).resolves.toEqual({
      columns: ["group", "n"],
      rows: [{ group: "Good", n: "10" }],
      row_count: 1,
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/threads/thread-1/artifacts/table-1/table-preview?limit=100",
      { headers: expect.any(Headers) },
    );
  });

  it("fetches text artifacts", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("summary,value\nn,42\n", {
        headers: { "Content-Type": "text/csv" },
        status: 200,
      }),
    );

    await expect(
      getArtifactText(
        fetchMock,
        "http://api.test/",
        "thread with/slash",
        "artifact with/slash",
      ),
    ).resolves.toBe("summary,value\nn,42\n");

    expect(fetchMock).toHaveBeenCalledWith(
      "http://api.test/api/threads/thread%20with%2Fslash/artifacts/artifact%20with%2Fslash",
    );
  });
});
