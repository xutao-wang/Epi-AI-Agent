import "@testing-library/jest-dom/vitest";
import {
  act,
  createEvent,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AppForTesting as App,
  isPendingMessageAcknowledged,
  normalizeNewConversationRuntimeSettings,
} from "./App";
import { createApiClient } from "./apiClient";
import type {
  ActivityRun,
  ApiThreadState,
  ModelOption,
  RuntimeOptions,
  RuntimeSettings,
  EmbeddingStartupStatus,
} from "./types";

const hybridEmbeddingStatus: EmbeddingStartupStatus = {
  profile_id: "openai-text-embedding-3-large",
  profile_label: "OpenAI text-embedding-3-large",
  provider: "openai",
  index_compatibility: "OpenAI/text-embedding-3-large",
  available: true,
  retrieval_mode: "hybrid_vector_lexical",
  reason_code: null,
  message: "",
  compatible_study_ids: [],
  incompatible_study_ids: [],
};

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status: 200,
    ...init,
  });
}

function threadState(
  overrides: Partial<ApiThreadState> = {},
): ApiThreadState {
  return {
    thread_id: "thread-1",
    run: {
      state: "idle",
      steps: 0,
      error: null,
      error_code: null,
      user_message: null,
      started_at: null,
      updated_at: null,
    },
    conversation: [],
    activity_runs: [],
    active_interrupt: null,
    runtime_settings: null,
    runtime_settings_locked: false,
    datasets: [],
    file_artifacts: [],
    output: {},
    diagnostics: {},
    embedding_startup_status: hybridEmbeddingStatus,
    ...overrides,
  };
}

function activityRun(
  state: ActivityRun["state"] = "running",
  overrides: Partial<ActivityRun> = {},
): ActivityRun {
  return {
    id: "activity-run-1",
    thread_id: "thread-1",
    user_message_id: "user-1",
    state,
    activities: [
      {
        id: "activity-1",
        sequence: 1,
        label: "Searching the data catalog",
        status: state === "waiting" ? "waiting" : "running",
        tool_name: "dbrag-search_catalog",
        tool_call_id: "call-1",
        created_at: "2026-08-11T00:00:00+00:00",
        updated_at: "2026-08-11T00:00:00+00:00",
      },
    ],
    created_at: "2026-08-11T00:00:00+00:00",
    updated_at: "2026-08-11T00:00:00+00:00",
    ...overrides,
  };
}

function reviewState() {
  return threadState({
    run: {
      state: "interrupted",
      steps: 2,
      error: null,
      error_code: null,
      user_message: null,
      started_at: null,
      updated_at: null,
    },
    conversation: [
      {
        id: "message-1",
        role: "user",
        text: "Find projects about diabetes",
      },
    ],
    active_interrupt: {
      id: "interrupt-1",
      type: "dataset_plan_review",
      artifact: {
        id: "plan-1",
        kind: "dataset_plan",
        version: 1,
        expected_status: "draft",
      },
      view: {
        goal: "Find projects about diabetes.",
        dataset_title: "Diabetes Projects",
        concept_groups: [
          {
            concept_id: "diabetes",
            concept_label: "diabetes",
            columns: [
              {
                key: "projects.project_id",
                table: "Projects",
                column: "PROJECT_ID",
                description: "Project identifier.",
                roles: ["requested"],
              },
              {
                key: "projects.title",
                table: "Projects",
                column: "TITLE",
                description: "Project title.",
                selected: false,
                roles: ["requested"],
              },
            ],
          },
        ],
        selected_fields: ["projects.project_id"],
        filters: [],
        joins: [],
        unresolved_scientific_choices: [],
      },
    },
  });
}

function datasetReviewInterrupt(
  datasetId = "subset-1",
  interruptId = `interrupt-${datasetId}`,
) {
  return {
    id: interruptId,
    type: "dataset_review" as const,
    artifact: {
      id: datasetId,
      kind: "db_rag_result",
      version: 1,
      expected_status: "pending_review",
    },
    view: {
      goal: "Create a diabetes subset.",
      dimensions: { rows: 2, columns: 1 },
      columns: [{ table: "subject", column: "gender", description: "Gender" }],
      filters: [],
      quality: {},
      warnings: [],
      provenance: {
        plan: { id: "plan-1", version: 1 },
        sql: { id: "sql-1", version: 1 },
        quality_report: { id: "quality-1", version: 1 },
      },
      feedback_history: [],
    },
  };
}

function analysisReviewInterrupt() {
  return {
    id: "interrupt-analysis",
    type: "analysis_result_review" as const,
    artifact: {
      id: "analysis-1",
      kind: "analysis_run",
      version: 1,
      expected_status: "pending_review",
    },
    view: {
      method: "custom_python",
      dataset: { id: "dataset-1", kind: "analysis_dataset", version: 1 },
      specification: {
        analysis_goal: "Estimate relapse-free survival.",
        code: "print('Kaplan-Meier estimates')",
      },
      output_text:
        "Kaplan-Meier estimates\nLog-rank p-value=0.031\nCox HR=1.41",
      warnings: [],
      warnings_truncated: false,
      runtime: { language: "Python", version: "3.12.10" },
      tables: [],
      figures: [],
      feedback_history: [],
    },
  };
}

const defaultRuntimeSettings: RuntimeSettings = {
  model_name: "gpt-5.4",
  temperature: 0.1,
  top_p: 0.9,
  max_steps: 4,
  timeout_seconds: 300,
  db_rag_embedding_model: "OpenAI/text-embedding-3-large",
  db_rag_reranker_model: "disabled",
};

function modelOption(
  id: string,
  label: string,
  overrides: Partial<ModelOption> = {},
): ModelOption {
  return {
    id,
    label,
    provider: "openai",
    provider_label: "OpenAI",
    supports_sampling_controls: true,
    summary: "Reliable general-purpose default.",
    initial_output_tokens: 8_192,
    automatic_output_token_ceiling: 16_384,
    user_output_token_increment: 8_192,
    absolute_output_token_ceiling: 24_576,
    request_timeout_seconds: 120,
    workflow_timeout_seconds: 300,
    automatic_output_cost: "$0.25",
    incremental_output_cost: "$0.13",
    ...overrides,
  };
}

const runtimeOptions: RuntimeOptions = {
  defaults: defaultRuntimeSettings,
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
    modelOption("gpt-5.4", "gpt-5.4 (Standard)"),
    modelOption("gpt-5.4-mini", "gpt-5.4-mini"),
    modelOption("gpt-5.6-sol", "gpt-5.6-sol (Medium)", {
      supports_sampling_controls: false,
      summary: "Deepest and highest-cost tier for complex analysis.",
      initial_output_tokens: 25_000,
      automatic_output_token_ceiling: 50_000,
      user_output_token_increment: 25_000,
      absolute_output_token_ceiling: 75_000,
      request_timeout_seconds: 240,
      workflow_timeout_seconds: 600,
      automatic_output_cost: "$1.50",
      incremental_output_cost: "$0.75",
    }),
  ],
  embedding_startup_status: hybridEmbeddingStatus,
};

function createThreadResponse(threadId = "thread-1") {
  return jsonResponse({ thread_id: threadId });
}

function runtimeOptionsResponse(options: RuntimeOptions = runtimeOptions) {
  return jsonResponse(options);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("normalizeNewConversationRuntimeSettings", () => {
  it("normalizes runtime settings against provider-neutral model options", () => {
    const validSettings: RuntimeSettings = {
      ...defaultRuntimeSettings,
      model_name: "custom/qwen3",
      temperature: 0.7,
    };
    const staleSettings: RuntimeSettings = {
      ...defaultRuntimeSettings,
      model_name: "removed/historical-model",
    };
    const options: RuntimeOptions = {
      ...runtimeOptions,
      defaults: {
        ...defaultRuntimeSettings,
        model_name: "custom/qwen3",
      },
      models: [
        modelOption("custom/qwen3", "Qwen 3 via custom endpoint", {
          provider: "openai_compatible",
          provider_label: "Custom endpoint",
        }),
      ],
    };

    expect(
      normalizeNewConversationRuntimeSettings(validSettings, options),
    ).toBe(validSettings);
    expect(
      normalizeNewConversationRuntimeSettings(staleSettings, options),
    ).toBe(options.defaults);
  });
});

describe("App", () => {
  it("shows one startup fallback notice before and after thread creation", async () => {
    const message =
      "Semantic embedding search is unavailable. " +
      "(OpenAI text-embedding-3-large is not configured.) Catalog, publication, " +
      "and study-design searches will use lexical matching only.";
    const fallbackStatus: EmbeddingStartupStatus = {
      ...hybridEmbeddingStatus,
      available: false,
      retrieval_mode: "lexical_fallback",
      reason_code: "EMBEDDING_CREDENTIALS_MISSING",
      message,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        runtimeOptionsResponse({
          ...runtimeOptions,
          embedding_startup_status: fallbackStatus,
        }),
      )
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            conversation: [{ id: "user-1", role: "user", text: "Hello" }],
            embedding_startup_status: fallbackStatus,
          }),
        ),
      );
    render(
      <App
        apiBase="http://api.test"
        fetchImpl={fetchMock}
        loadConversationHistory={false}
      />,
    );

    expect(await screen.findByText(message)).toBeInTheDocument();
    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      { target: { value: "Hello" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(screen.getAllByText(message)).toHaveLength(1);
  });

  it("renders the local application without hosted account controls", async () => {
    render(<App apiBase="http://api.test" loadConversationHistory={false} />);

    expect(
      await screen.findByText("Ask questions about your data or query from existing database"),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign out" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Signed-in user")).not.toBeInTheDocument();
  });

  it("shows the updated initial prompt and example question without a ready status", async () => {
    render(<App apiBase="http://api.test" loadConversationHistory={false} />);

    expect(
      await screen.findByText("Ask questions about your data or query from existing database"),
    ).toBeInTheDocument();
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toHaveAttribute(
      "placeholder",
      "e.g. Create a baseline index-case dataset with age, sex, marital status, smoking, alcohol use, and diabetes medication use. Give me a simple summary table.",
    );
  });

  it("shows a safe terminal failure and keeps the composer usable", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "error",
              steps: 1,
              error: "RateLimitError: quota exhausted",
              error_code: "PROVIDER_CREDITS_EXHAUSTED",
              user_message:
                "The provider account has no remaining API credits. Add credits or use a funded API key, then retry.",
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Analyze relapse" },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    const composer = await screen.findByLabelText(
      "Ask a question about your dataset!",
    );
    fireEvent.change(composer, { target: { value: "Analyze relapse" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "no remaining API credits",
    );
    expect(composer).not.toBeDisabled();
    fireEvent.change(composer, { target: { value: "Try again" } });
    expect(screen.getByRole("button", { name: "Send" })).not.toBeDisabled();
  });

  it("blocks the composer for an unprojectable interrupt", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "error",
              steps: 2,
              error: "A pending workflow interrupt could not be projected for display.",
              error_code: "INTERRUPT_PROJECTION_FAILED",
              user_message:
                "A pending review could not be displayed. This workflow is paused; do not submit another request.",
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Create a cohort" },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    const composer = await screen.findByLabelText(
      "Ask a question about your dataset!",
    );
    fireEvent.change(composer, { target: { value: "Create a cohort" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A pending review could not be displayed",
    );
    expect(composer).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("reconciles optimistic messages using both text and attachment ids", () => {
    const pending = {
      id: "pending",
      role: "user" as const,
      text: "Analyze this",
      created_at: null,
      attachments: [
        {
          id: "attachment-new",
          kind: "tabular",
          label: "new.csv",
          filename: "new.csv",
          mime: "text/csv",
          byte_size: 10,
          relationship: "input" as const,
          origin_message_id: "pending",
        },
      ],
    };
    const prior = {
      ...pending,
      id: "prior",
      attachments: [
        {
          ...pending.attachments[0],
          id: "attachment-prior",
          filename: "prior.csv",
        },
      ],
    };

    expect(isPendingMessageAcknowledged(prior, pending)).toBe(false);
    expect(
      isPendingMessageAcknowledged(
        {...pending, id: "server-message"},
        pending,
      ),
    ).toBe(true);
  });

  it("stages message attachments and submits their exact ids", async () => {
    const attachment = {
      id: "attachment-csv",
      filename: "cohort.csv",
      kind: "tabular",
      format: "csv",
      mime: "text/csv",
      byte_size: 12,
      status: "staged" as const,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse({ attachments: [attachment], errors: [] }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            conversation: [
              {
                id: "user-attachment",
                role: "user",
                text: "Analyze this cohort",
                attachments: [
                  {
                    id: attachment.id,
                    kind: attachment.kind,
                    label: attachment.filename,
                    filename: attachment.filename,
                    mime: attachment.mime,
                    byte_size: attachment.byte_size,
                    relationship: "input",
                    origin_message_id: "user-attachment",
                  },
                ],
              },
            ],
          }),
        ),
      );
    const file = new File(["id\n1\n"], "cohort.csv", { type: "text/csv" });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(await screen.findByTestId("attachment-file-input"), {
      target: { files: [file] },
    });
    expect(await screen.findByText("cohort.csv")).toBeInTheDocument();

    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      { target: { value: "Analyze this cohort" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    const [, uploadInit] = fetchMock.mock.calls[2];
    expect(fetchMock.mock.calls[2][0]).toBe(
      "http://api.test/api/threads/thread-1/attachments",
    );
    expect(uploadInit.body.getAll("files")).toEqual([file]);
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "http://api.test/api/threads/thread-1/messages",
        {
          method: "POST",
          headers: expect.any(Headers),
          body: JSON.stringify({
            text: "Analyze this cohort",
            attachment_ids: ["attachment-csv"],
          }),
        },
      );
    });
  });

  it("renders a submitted user message without the initial suggestion before the backend responds", async () => {
    const submitResponse = deferred<Response>();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockReturnValueOnce(submitResponse.promise);

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Find NIH grants" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    const messageList = await screen.findByRole("list", {
      name: "Conversation messages",
    });
    expect(within(messageList).getByText("Find NIH grants")).toBeInTheDocument();
    expect(within(messageList).getByLabelText("Agent activity")).toHaveTextContent(
      "Submitting your message",
    );
    expect(messageList.children[0]).toHaveTextContent("user");
    expect(messageList.children[0]).toHaveTextContent("Find NIH grants");
    expect(messageList.children[1]).toHaveTextContent("Submitting your message");
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toHaveValue("");
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).not.toHaveAttribute("placeholder");

    submitResponse.resolve(
      jsonResponse(
        threadState({
          run: {
            state: "running",
            steps: 1,
            error: null,
            started_at: null,
            updated_at: null,
          },
          conversation: [
            { id: "user-1", role: "user", text: "Find NIH grants" },
          ],
          diagnostics: { workflow_milestone: "needs_code" },
        }),
      ),
    );
    await screen.findByText("Working on your request.");
    expect(within(messageList).getAllByText("Find NIH grants")).toHaveLength(1);
    expect(within(messageList).getByLabelText("Agent activity")).toHaveTextContent(
      "Agent is working",
    );
    expect(within(messageList).getByLabelText("Agent activity")).not.toHaveTextContent(
      "needs_code",
    );
    expect(messageList.children[1]).toHaveTextContent("Agent is working");
  });

  it("places a running timeline directly after its matching user message", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 1,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Create a cohort" },
            ],
            activity_runs: [activityRun()],
          }),
        ),
      );
    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      { target: { value: "Create a cohort" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    const messageList = await screen.findByRole("list", {
      name: "Conversation messages",
    });
    const timeline = await within(messageList).findByLabelText(
      "Agent activity timeline",
    );
    expect(messageList.children[0]).toHaveTextContent("Create a cohort");
    expect(messageList.children[1]).toBe(timeline);
    expect(within(messageList).queryByLabelText("Agent activity")).not.toBeInTheDocument();
  });

  it("updates a polled activity in place without duplicating it", async () => {
    vi.useFakeTimers();
    const updatedRun = activityRun("running", {
      activities: [
        {
          ...activityRun().activities[0],
          status: "completed",
          updated_at: "2026-08-11T00:00:01+00:00",
        },
        {
          id: "activity-2",
          sequence: 2,
          label: "Choosing the next step",
          status: "running",
          tool_name: null,
          tool_call_id: null,
          created_at: "2026-08-11T00:00:01+00:00",
          updated_at: "2026-08-11T00:00:01+00:00",
        },
      ],
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 1,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Create a cohort" },
            ],
            activity_runs: [activityRun()],
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 2,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Create a cohort" },
            ],
            activity_runs: [updatedRun],
          }),
        ),
      );
    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "Create a cohort" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getAllByText("Searching the data catalog")).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(screen.getAllByText("Searching the data catalog")).toHaveLength(1);
    expect(screen.getByText("Choosing the next step")).toBeInTheDocument();
    expect(screen.getAllByLabelText("Agent activity timeline")).toHaveLength(1);
  });

  it("renders repeated successful calls as separate plain-language rows", async () => {
    const first = {
      ...activityRun().activities[0],
      status: "completed" as const,
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 2,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Create a cohort" },
            ],
            activity_runs: [
              activityRun("running", {
                activities: [
                  first,
                  {
                    ...first,
                    id: "activity-2",
                    sequence: 2,
                    tool_call_id: "call-2",
                  },
                ],
              }),
            ],
          }),
        ),
      );
    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      { target: { value: "Create a cohort" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findAllByText("Searching the data catalog")).toHaveLength(2);
    expect(screen.queryByText("dbrag-search_catalog")).not.toBeInTheDocument();
    expect(screen.queryByText(/Call 1|Call 2/)).not.toBeInTheDocument();
  });

  it("keeps a waiting timeline last in the message list before its review card", async () => {
    const waiting = activityRun("waiting", {
      user_message_id: "message-1",
      activities: [
        {
          ...activityRun().activities[0],
          label: "Waiting for dataset plan review",
          status: "waiting",
          tool_name: "dbrag-request_dataset_plan_review",
        },
      ],
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse({ ...reviewState(), activity_runs: [waiting] }),
      );
    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      { target: { value: "Find projects about diabetes" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    const messageList = await screen.findByRole("list", {
      name: "Conversation messages",
    });
    const timeline = await within(messageList).findByLabelText(
      "Agent activity timeline",
    );
    const approve = screen.getByRole("button", { name: "Approve plan and extract" });
    expect(messageList.lastElementChild).toBe(timeline);
    expect(messageList.contains(approve)).toBe(false);
    expect(
      messageList.compareDocumentPosition(approve) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("shows a completed historical timeline collapsed", async () => {
    const completed = activityRun("completed", {
      activities: [
        {
          ...activityRun().activities[0],
          status: "completed",
        },
      ],
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 2,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Create a cohort" },
            ],
            activity_runs: [completed],
          }),
        ),
      );
    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      { target: { value: "Create a cohort" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("button", { name: "Show activity history · 1 step" }),
    ).toHaveAttribute("aria-expanded", "false");
  });

  it.each(["awaiting_clarification", "retrying_after_error", "needs_code"])(
    "hides confusing internal activity milestone %s while the agent is running",
    async (workflowMilestone) => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 1,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Use cohort 1" },
            ],
            diagnostics: { workflow_milestone: workflowMilestone },
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Use cohort 1" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    const activity = await screen.findByLabelText("Agent activity");
    expect(activity).toHaveTextContent("Agent is working");
    expect(activity).toHaveTextContent("Working on your request.");
    expect(activity).not.toHaveTextContent("awaiting_clarification");
    expect(activity).not.toHaveTextContent("retrying_after_error");
    },
  );

  it("first submit creates a thread with selected settings, submits a message, and renders conversation text", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 3,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Find NIH grants" },
              {
                id: "assistant-1",
                role: "assistant",
                text: "I found matching projects.",
              },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(await screen.findByLabelText("Model"), {
      target: { value: "gpt-5.4-mini" },
    });
    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "  Find NIH grants  " },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("I found matching projects.")).toBeInTheDocument();
    expect(screen.getByText("Find NIH grants")).toBeInTheDocument();
    expect(
      screen.queryByText("Ask a question about your dataset!"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toHaveValue("");
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://api.test/api/runtime/options",
      { headers: expect.any(Headers) },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(2, "http://api.test/api/threads", {
      method: "POST",
      headers: expect.any(Headers),
      body: JSON.stringify({ model_name: "gpt-5.4-mini" }),
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://api.test/api/threads/thread-1/messages",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({ text: "Find NIH grants" }),
      },
    );
  });

  it("submits the chat message when Enter is pressed", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 1,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Find NIH grants" },
              {
                id: "assistant-1",
                role: "assistant",
                text: "I found matching projects.",
              },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    const input = await screen.findByLabelText(
      "Ask a question about your dataset!",
    );
    fireEvent.change(input, { target: { value: "Find NIH grants" } });
    const enterEvent = createEvent.keyDown(input, { key: "Enter" });
    fireEvent(input, enterEvent);

    expect(enterEvent.defaultPrevented).toBe(true);
    expect(await screen.findByText("I found matching projects.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://api.test/api/threads/thread-1/messages",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({ text: "Find NIH grants" }),
      },
    );
  });

  it("keeps Shift+Enter available for multiline chat input", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(runtimeOptionsResponse());

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    const input = await screen.findByLabelText(
      "Ask a question about your dataset!",
    );
    fireEvent.change(input, { target: { value: "line one" } });
    const shiftEnterEvent = createEvent.keyDown(input, {
      key: "Enter",
      shiftKey: true,
    });
    fireEvent(input, shiftEnterEvent);

    expect(shiftEnterEvent.defaultPrevented).toBe(false);
    fireEvent.change(input, { target: { value: "line one\nline two" } });
    expect(input).toHaveValue("line one\nline two");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("loads runtime options from the backend runtime-options route", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(runtimeOptionsResponse());

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    expect(await screen.findByLabelText("Model")).toHaveValue("gpt-5.4");
    expect(screen.getByRole("option", { name: "gpt-5.4-mini" })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://api.test/api/runtime/options",
      { headers: expect.any(Headers) },
    );
  });

  it("shows full model tier labels without persistent profile guidance", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(runtimeOptionsResponse());

    render(
      <App
        apiBase="http://api.test"
        fetchImpl={fetchMock}
        loadConversationHistory={false}
      />,
    );

    const picker = await screen.findByLabelText("Model");
    expect(
      screen.getByRole("option", { name: "gpt-5.6-sol (Medium)" }),
    ).toBeInTheDocument();
    fireEvent.change(picker, { target: { value: "gpt-5.6-sol" } });

    expect(
      screen.queryByText(/Deepest and highest-cost tier for complex analysis/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Automatic output limit:/),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Workflow deadline:/)).not.toBeInTheDocument();
  });

  it("keeps the selected model locked during output continuation", async () => {
    const outputLimitState = threadState({
      run: {
        state: "interrupted",
        steps: 2,
        error: null,
        started_at: null,
        updated_at: null,
      },
      runtime_settings: {
        ...defaultRuntimeSettings,
        model_name: "gpt-5.6-sol",
        timeout_seconds: 600,
      },
      runtime_settings_locked: true,
      active_interrupt: {
        id: "interrupt-output",
        type: "model_output_limit",
        model_id: "gpt-5.6-sol",
        model_label: "gpt-5.6-sol (Medium)",
        automatic_token_ceiling: 50_000,
        continuation_tokens: 25_000,
        additional_output_cost: "$0.75",
        message:
          "gpt-5.6-sol (Medium) reached its 50,000-token turn limit. Continuing with another 25,000 tokens may cost up to an additional $0.75 in output charges.",
        actions: ["continue", "cancel"],
      },
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(outputLimitState));

    render(
      <App
        apiBase="http://api.test"
        fetchImpl={fetchMock}
        loadConversationHistory={false}
      />,
    );

    fireEvent.change(await screen.findByLabelText("Model"), {
      target: { value: "gpt-5.6-sol" },
    });
    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      { target: { value: "Run the survival analysis" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("heading", { name: "More output needed" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Model locked: gpt-5.6-sol (Medium)",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toBeDisabled();
    expect(screen.queryByText(/switch model/i)).not.toBeInTheDocument();
  });

  it("does not create a thread before first submit or upload", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(runtimeOptionsResponse());

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    await screen.findByLabelText("Model");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).not.toHaveBeenCalledWith(
      "http://api.test/api/threads",
      expect.anything(),
    );
  });

  it("loads a saved conversation after reload and preserves it when reset", async () => {
    const savedThreadId = "thread-saved";
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        return Promise.resolve(
          jsonResponse({
            items: [
              {
                thread_id: savedThreadId,
                title: "TB cohort survival analysis",
                title_source: "automatic",
                model_name: "gpt-5.4-mini",
                created_at: "2026-07-30T00:00:00+00:00",
                updated_at: "2026-07-30T00:00:00+00:00",
              },
            ],
          }),
        );
      }
      if (url === `http://api.test/api/threads/${savedThreadId}/state`) {
        return Promise.resolve(
          jsonResponse(
            threadState({
              thread_id: savedThreadId,
              conversation: [
                { id: "prior-user", role: "user", text: "Compare TB survival" },
              ],
              runtime_settings: {
                ...defaultRuntimeSettings,
                model_name: "gpt-5.4-mini",
              },
              runtime_settings_locked: true,
            }),
          ),
        );
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);

    const savedConversation = await screen.findByRole("button", {
      name: "TB cohort survival analysis",
    });
    fireEvent.click(savedConversation);

    expect(await screen.findByText("Compare TB survival")).toBeInTheDocument();
    fireEvent.mouseEnter(
      screen.getByRole("button", { name: "Model locked: gpt-5.4-mini" }),
    );
    expect(
      await screen.findByText(
        "Model locked for this conversation. Start a new conversation to choose a different model.",
      ),
    ).toHaveClass("composer-model-lock-popover");

    expect(
      screen.queryByRole("button", {
        name: "Start new conversation from saved conversations",
      }),
    ).not.toBeInTheDocument();
    const newConversation = screen.getByRole("button", {
      name: "New conversation",
    });
    fireEvent.click(newConversation);

    expect(
      screen.queryByText("Ready"),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "TB cohort survival analysis" }),
    ).toBeInTheDocument();
  });

  it("ignores a late state response from a previously selected conversation", async () => {
    const stateA = deferred<Response>();
    const stateB = deferred<Response>();
    const summaries = [
      {
        thread_id: "thread-a",
        title: "Thread A",
        title_source: "automatic",
        model_name: "gpt-5.6-terra",
        created_at: "2026-08-19T00:00:00+00:00",
        updated_at: "2026-08-19T00:00:00+00:00",
        last_opened_at: null,
        archived_at: null,
        awaiting_review: false,
      },
      {
        thread_id: "thread-b",
        title: "Thread B",
        title_source: "automatic",
        model_name: "gpt-5.6-terra",
        created_at: "2026-08-19T00:00:00+00:00",
        updated_at: "2026-08-19T00:00:00+00:00",
        last_opened_at: null,
        archived_at: null,
        awaiting_review: false,
      },
    ];
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        return Promise.resolve(jsonResponse({ items: summaries }));
      }
      if (url === "http://api.test/api/threads/thread-a/state") {
        return stateA.promise;
      }
      if (url === "http://api.test/api/threads/thread-b/state") {
        return stateB.promise;
      }
      if (url === "http://api.test/api/conversations/thread-a/open") {
        return Promise.resolve(jsonResponse(summaries[0]));
      }
      if (url === "http://api.test/api/conversations/thread-b/open") {
        return Promise.resolve(jsonResponse(summaries[1]));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
    fireEvent.click(await screen.findByRole("button", { name: "Thread A" }));
    fireEvent.click(screen.getByRole("button", { name: "Thread B" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "Loading selected conversation",
    );

    await act(async () => {
      stateB.resolve(jsonResponse(threadState({
        thread_id: "thread-b",
        conversation: [{ id: "b-user", role: "user", text: "Message B" }],
      })));
    });
    expect(await screen.findByText("Message B")).toBeInTheDocument();

    await act(async () => {
      stateA.resolve(jsonResponse(threadState({
        thread_id: "thread-a",
        conversation: [{ id: "a-user", role: "user", text: "Who are you" }],
      })));
    });

    expect(screen.queryByText("Who are you")).not.toBeInTheDocument();
    expect(screen.getByText("Message B")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Thread B" })).toHaveAttribute(
      "aria-current",
      "page",
    );
  });

  it("ignores a message response after the user switches conversations", async () => {
    const submitA = deferred<Response>();
    const summaries = [
      {
        thread_id: "thread-a",
        title: "Submit Thread A",
        title_source: "automatic",
        model_name: "gpt-5.6-terra",
        created_at: "2026-08-19T00:00:00+00:00",
        updated_at: "2026-08-19T00:00:00+00:00",
        last_opened_at: null,
        archived_at: null,
        awaiting_review: false,
      },
      {
        thread_id: "thread-b",
        title: "Submit Thread B",
        title_source: "automatic",
        model_name: "gpt-5.6-terra",
        created_at: "2026-08-19T00:00:00+00:00",
        updated_at: "2026-08-19T00:00:00+00:00",
        last_opened_at: null,
        archived_at: null,
        awaiting_review: false,
      },
    ];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        return Promise.resolve(jsonResponse({ items: summaries }));
      }
      if (url === "http://api.test/api/threads/thread-a/state") {
        return Promise.resolve(jsonResponse(threadState({
          thread_id: "thread-a",
          conversation: [{ id: "a-original", role: "user", text: "Original A" }],
        })));
      }
      if (url === "http://api.test/api/threads/thread-b/state") {
        return Promise.resolve(jsonResponse(threadState({
          thread_id: "thread-b",
          conversation: [{ id: "b-original", role: "user", text: "Original B" }],
        })));
      }
      if (
        url === "http://api.test/api/threads/thread-a/messages" &&
        init?.method === "POST"
      ) {
        return submitA.promise;
      }
      if (url.endsWith("/open")) {
        return Promise.resolve(jsonResponse(
          url.includes("thread-a") ? summaries[0] : summaries[1],
        ));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
    fireEvent.click(await screen.findByRole("button", { name: "Submit Thread A" }));
    expect(await screen.findByText("Original A")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "Continue A" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(screen.getByRole("button", { name: "Submit Thread B" }));
    expect(await screen.findByText("Original B")).toBeInTheDocument();

    await act(async () => {
      submitA.resolve(jsonResponse(threadState({
        thread_id: "thread-a",
        conversation: [{ id: "a-late", role: "user", text: "Late A" }],
      })));
    });

    expect(screen.queryByText("Late A")).not.toBeInTheDocument();
    expect(screen.getByText("Original B")).toBeInTheDocument();
  });

  it("keeps a resumed review response attached to its owning conversation", async () => {
    const resumeA = deferred<Response>();
    const summaries = [
      {
        thread_id: "thread-a",
        title: "Review Thread A",
        title_source: "automatic",
        model_name: "gpt-5.6-terra",
        created_at: "2026-08-19T00:00:00+00:00",
        updated_at: "2026-08-19T00:00:00+00:00",
        last_opened_at: null,
        archived_at: null,
        awaiting_review: true,
      },
      {
        thread_id: "thread-b",
        title: "Review Thread B",
        title_source: "automatic",
        model_name: "gpt-5.6-terra",
        created_at: "2026-08-19T00:00:00+00:00",
        updated_at: "2026-08-19T00:00:00+00:00",
        last_opened_at: null,
        archived_at: null,
        awaiting_review: false,
      },
    ];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        return Promise.resolve(jsonResponse({ items: summaries }));
      }
      if (url === "http://api.test/api/threads/thread-a/state") {
        return Promise.resolve(jsonResponse({ ...reviewState(), thread_id: "thread-a" }));
      }
      if (url === "http://api.test/api/threads/thread-b/state") {
        return Promise.resolve(jsonResponse(threadState({
          thread_id: "thread-b",
          conversation: [{ id: "b-review", role: "user", text: "Safe Thread B" }],
        })));
      }
      if (
        url === "http://api.test/api/threads/thread-a/interrupts/interrupt-1/resume" &&
        init?.method === "POST"
      ) {
        return resumeA.promise;
      }
      if (url.endsWith("/open")) {
        return Promise.resolve(jsonResponse(
          url.includes("thread-a") ? summaries[0] : summaries[1],
        ));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
    fireEvent.click(await screen.findByRole("button", { name: "Review Thread A" }));
    expect(
      await screen.findByText(
        "This conversation was previously paused and is awaiting your review.",
      ),
    ).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Cancel review" }));
    fireEvent.click(screen.getByRole("button", { name: "Review Thread B" }));
    expect(await screen.findByText("Safe Thread B")).toBeInTheDocument();

    await act(async () => {
      resumeA.resolve(jsonResponse(threadState({
        thread_id: "thread-a",
        conversation: [{ id: "a-resumed", role: "user", text: "Resumed A" }],
      })));
    });

    expect(screen.queryByText("Resumed A")).not.toBeInTheDocument();
    expect(screen.getByText("Safe Thread B")).toBeInTheDocument();
  });

  it("clears the saved-conversation review badge after resume and cancellation", async () => {
    let conversationRequests = 0;
    let threadStateRequests = 0;
    const summary = {
      thread_id: "thread-1",
      title: "Clarification thread",
      title_source: "automatic" as const,
      model_name: "gpt-5.6-terra",
      created_at: "2026-08-23T00:00:00+00:00",
      updated_at: "2026-08-23T00:00:00+00:00",
      last_opened_at: null,
      archived_at: null,
      awaiting_review: true,
    };
    const clarificationState = threadState({
      run: {
        state: "interrupted",
        steps: 2,
        error: null,
        error_code: null,
        user_message: null,
        started_at: null,
        updated_at: null,
      },
      conversation: [
        { id: "user-1", role: "user", text: "Create a cohort" },
      ],
      active_interrupt: {
        id: "interrupt-clarification",
        type: "agent_clarification",
        question: "Which follow-up window should be used?",
        reason: "The outcome exists at multiple visits.",
        options: [
          { id: "month-12", label: "Use the 12-month visit." },
        ],
      },
    });
    const runningState = threadState({
      run: {
        state: "running",
        steps: 3,
        error: null,
        error_code: null,
        user_message: null,
        started_at: null,
        updated_at: null,
      },
      conversation: [
        { id: "user-1", role: "user", text: "Create a cohort" },
      ],
    });
    const cancelledState = threadState({
      run: {
        state: "cancelled",
        steps: 3,
        error: null,
        error_code: null,
        user_message: null,
        started_at: null,
        updated_at: null,
      },
      conversation: [
        {
          id: "user-1",
          role: "user",
          text: "Create a cohort",
          status: "cancelled",
        },
      ],
    });
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        conversationRequests += 1;
        return Promise.resolve(
          jsonResponse({
            items: [
              {
                ...summary,
                awaiting_review: conversationRequests < 3,
              },
            ],
          }),
        );
      }
      if (url === "http://api.test/api/threads/thread-1/state") {
        threadStateRequests += 1;
        return Promise.resolve(
          jsonResponse(threadStateRequests === 1 ? clarificationState : runningState),
        );
      }
      if (url === "http://api.test/api/conversations/thread-1/open") {
        return Promise.resolve(jsonResponse(summary));
      }
      if (
        url ===
          "http://api.test/api/threads/thread-1/interrupts/interrupt-clarification/resume" &&
        init?.method === "POST"
      ) {
        return Promise.resolve(jsonResponse(runningState));
      }
      if (
        url === "http://api.test/api/threads/thread-1/cancel" &&
        init?.method === "POST"
      ) {
        return Promise.resolve(jsonResponse(cancelledState));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);

    expect(await screen.findByText("Awaiting review")).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Clarification thread" }),
    );
    fireEvent.click(
      await screen.findByRole("radio", { name: "Let the agent decide" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => {
      expect(screen.queryByText("Awaiting review")).not.toBeInTheDocument();
    });
    expect(conversationRequests).toBe(3);

    fireEvent.click(await screen.findByRole("button", { name: "Cancel run" }));

    await waitFor(() => {
      expect(screen.queryByText("Awaiting review")).not.toBeInTheDocument();
      expect(
        screen.getByLabelText("Ask a question about your dataset!"),
      ).toBeEnabled();
    });
    expect(conversationRequests).toBe(4);
  });

  it("ignores a cancellation response after switching conversations", async () => {
    const cancelA = deferred<Response>();
    const summaries = [
      {
        thread_id: "thread-a",
        title: "Running Thread A",
        title_source: "automatic",
        model_name: "gpt-5.6-terra",
        created_at: "2026-08-19T00:00:00+00:00",
        updated_at: "2026-08-19T00:00:00+00:00",
        last_opened_at: null,
        archived_at: null,
        awaiting_review: false,
      },
      {
        thread_id: "thread-b",
        title: "Idle Thread B",
        title_source: "automatic",
        model_name: "gpt-5.6-terra",
        created_at: "2026-08-19T00:00:00+00:00",
        updated_at: "2026-08-19T00:00:00+00:00",
        last_opened_at: null,
        archived_at: null,
        awaiting_review: false,
      },
    ];
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        return Promise.resolve(jsonResponse({ items: summaries }));
      }
      if (url === "http://api.test/api/threads/thread-a/state") {
        return Promise.resolve(jsonResponse(threadState({
          thread_id: "thread-a",
          run: { state: "running", steps: 1, error: null },
          conversation: [{ id: "a-running", role: "user", text: "Running A" }],
        })));
      }
      if (url === "http://api.test/api/threads/thread-b/state") {
        return Promise.resolve(jsonResponse(threadState({
          thread_id: "thread-b",
          conversation: [{ id: "b-idle", role: "user", text: "Idle B" }],
        })));
      }
      if (
        url === "http://api.test/api/threads/thread-a/cancel" &&
        init?.method === "POST"
      ) {
        return cancelA.promise;
      }
      if (url.endsWith("/open")) {
        return Promise.resolve(jsonResponse(
          url.includes("thread-a") ? summaries[0] : summaries[1],
        ));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
    fireEvent.click(await screen.findByRole("button", { name: "Running Thread A" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel run" }));
    fireEvent.click(screen.getByRole("button", { name: "Idle Thread B" }));
    expect(await screen.findByText("Idle B")).toBeInTheDocument();

    await act(async () => {
      cancelA.resolve(jsonResponse(threadState({
        thread_id: "thread-a",
        conversation: [{ id: "a-cancelled", role: "user", text: "Cancelled A" }],
      })));
    });

    expect(screen.queryByText("Cancelled A")).not.toBeInTheDocument();
    expect(screen.getByText("Idle B")).toBeInTheDocument();
  });

  it("deletes an open saved conversation and returns to a blank conversation", async () => {
    const savedThreadId = "thread-saved";
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        return Promise.resolve(jsonResponse({
          items: [{
            thread_id: savedThreadId,
            title: "TB cohort survival analysis",
            title_source: "automatic",
            model_name: "gpt-5.4-mini",
            created_at: "2026-07-30T00:00:00+00:00",
            updated_at: "2026-07-30T00:00:00+00:00",
            archived_at: null,
          }],
        }));
      }
      if (url === `http://api.test/api/threads/${savedThreadId}/state`) {
        return Promise.resolve(jsonResponse(threadState({
          thread_id: savedThreadId,
          conversation: [{ id: "prior-user", role: "user", text: "Compare TB survival" }],
          runtime_settings_locked: true,
        })));
      }
      if (url === `http://api.test/api/conversations/${savedThreadId}` && init?.method === "DELETE") {
        return Promise.resolve(new Response(null, { status: 204 }));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);

    fireEvent.click(await screen.findByRole("button", { name: "TB cohort survival analysis" }));
    expect(await screen.findByText("Compare TB survival")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Delete TB cohort survival analysis" }));

    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: "TB cohort survival analysis" }),
      ).not.toBeInTheDocument();
    });
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
  });

  it("resumes a saved locked conversation without resending its model", async () => {
    const savedThreadId = "thread-saved";
    let resumedMessageBody: BodyInit | null | undefined;
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        return Promise.resolve(
          jsonResponse({
            items: [
              {
                thread_id: savedThreadId,
                title: "TB cohort survival analysis",
                title_source: "automatic",
                model_name: "gpt-5.4-mini",
                created_at: "2026-07-30T00:00:00+00:00",
                updated_at: "2026-07-30T00:00:00+00:00",
              },
            ],
          }),
        );
      }
      if (url === `http://api.test/api/threads/${savedThreadId}/state`) {
        return Promise.resolve(
          jsonResponse(
            threadState({
              thread_id: savedThreadId,
              runtime_settings: {
                ...defaultRuntimeSettings,
                model_name: "gpt-5.4-mini",
              },
              runtime_settings_locked: true,
            }),
          ),
        );
      }
      if (url === `http://api.test/api/threads/${savedThreadId}/messages`) {
        resumedMessageBody = init?.body;
        return Promise.resolve(jsonResponse(threadState({ thread_id: savedThreadId })));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);

    fireEvent.click(
      await screen.findByRole("button", { name: "TB cohort survival analysis" }),
    );
    await screen.findByRole("button", {
      name: "Model locked: gpt-5.4-mini",
    });
    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "Continue the analysis" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        `http://api.test/api/threads/${savedThreadId}/messages`,
        expect.anything(),
      );
    });
    expect(resumedMessageBody).toBe(JSON.stringify({ text: "Continue the analysis" }));
  });

  it("confirms an available replacement before continuing saved history", async () => {
    const savedThreadId = "thread-saved";
    const confirm = vi.spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValue(true);
    let submittedBody: BodyInit | null | undefined;
    const claudeOptions: RuntimeOptions = {
      ...runtimeOptions,
      defaults: {
        ...defaultRuntimeSettings,
        model_name: "claude-opus-5",
      },
      models: [
        modelOption("claude-opus-5", "Claude Opus 5", {
          provider: "anthropic",
          provider_label: "Anthropic",
          supports_sampling_controls: false,
        }),
      ],
    };
    const historicalState = threadState({
      thread_id: savedThreadId,
      conversation: [
        { id: "prior-user", role: "user", text: "Compare TB survival" },
      ],
      runtime_settings: {
        ...defaultRuntimeSettings,
        model_name: "gpt-5.6-terra",
      },
      runtime_settings_locked: true,
      model_name: "gpt-5.6-terra",
      model_label: "gpt-5.6-terra (Medium)",
      model_available: false,
      model_replacement_required: true,
    } as unknown as Partial<ApiThreadState>);
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse(claudeOptions));
      }
      if (url === "http://api.test/api/conversations") {
        return Promise.resolve(jsonResponse({
          items: [{
            thread_id: savedThreadId,
            title: "TB cohort survival analysis",
            title_source: "automatic",
            model_name: "gpt-5.6-terra",
            created_at: "2026-07-30T00:00:00+00:00",
            updated_at: "2026-07-30T00:00:00+00:00",
          }],
        }));
      }
      if (url === `http://api.test/api/threads/${savedThreadId}/state`) {
        return Promise.resolve(jsonResponse(historicalState));
      }
      if (url.endsWith("/open")) {
        return Promise.resolve(jsonResponse({
          thread_id: savedThreadId,
          title: "TB cohort survival analysis",
          title_source: "automatic",
          model_name: "gpt-5.6-terra",
          created_at: "2026-07-30T00:00:00+00:00",
          updated_at: "2026-07-30T00:00:00+00:00",
        }));
      }
      if (
        url === `http://api.test/api/threads/${savedThreadId}/messages`
        && init?.method === "POST"
      ) {
        submittedBody = init.body;
        return Promise.resolve(jsonResponse(threadState({
          thread_id: savedThreadId,
          runtime_settings: claudeOptions.defaults,
          runtime_settings_locked: true,
        })));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
    fireEvent.click(await screen.findByRole("button", {
      name: "TB cohort survival analysis",
    }));

    expect(await screen.findByText(/used gpt-5.6-terra \(Medium\)/i))
      .toBeInTheDocument();
    const replacement = screen.getByRole("combobox", {
      name: "Replacement model",
    });
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    fireEvent.change(replacement, { target: { value: "claude-opus-5" } });
    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "Continue the analysis" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(submittedBody).toBeUndefined();
    expect(screen.getByLabelText("Ask a question about your dataset!"))
      .toHaveValue("Continue the analysis");
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => {
      expect(submittedBody).toBe(JSON.stringify({
        text: "Continue the analysis",
        model_name: "claude-opus-5",
      }));
    });
    expect(confirm).toHaveBeenLastCalledWith(
      "This conversation used gpt-5.6-terra (Medium). Continue with Claude Opus 5?",
    );
  });

  it("normalizes an unavailable historical model before creating a new conversation", async () => {
    const savedThreadId = "thread-saved";
    let createThreadBody: BodyInit | null | undefined;
    const claudeOptions: RuntimeOptions = {
      ...runtimeOptions,
      defaults: {
        ...defaultRuntimeSettings,
        model_name: "claude-opus-5",
      },
      models: [
        modelOption("claude-opus-5", "Claude Opus 5", {
          provider: "anthropic",
          provider_label: "Anthropic",
          supports_sampling_controls: false,
        }),
      ],
    };
    const historicalState = threadState({
      thread_id: savedThreadId,
      conversation: [
        { id: "prior-user", role: "user", text: "Compare TB survival" },
      ],
      runtime_settings: {
        ...defaultRuntimeSettings,
        model_name: "gpt-5.6-terra",
      },
      runtime_settings_locked: true,
      model_name: "gpt-5.6-terra",
      model_label: "gpt-5.6-terra (Medium)",
      model_available: false,
      model_replacement_required: true,
    } as unknown as Partial<ApiThreadState>);
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse(claudeOptions));
      }
      if (url === "http://api.test/api/conversations") {
        return Promise.resolve(jsonResponse({
          items: [{
            thread_id: savedThreadId,
            title: "TB cohort survival analysis",
            title_source: "automatic",
            model_name: "gpt-5.6-terra",
            created_at: "2026-07-30T00:00:00+00:00",
            updated_at: "2026-07-30T00:00:00+00:00",
          }],
        }));
      }
      if (url === `http://api.test/api/threads/${savedThreadId}/state`) {
        return Promise.resolve(jsonResponse(historicalState));
      }
      if (url === `http://api.test/api/conversations/${savedThreadId}/open`) {
        return Promise.resolve(jsonResponse({
          thread_id: savedThreadId,
          title: "TB cohort survival analysis",
          title_source: "automatic",
          model_name: "gpt-5.6-terra",
          created_at: "2026-07-30T00:00:00+00:00",
          updated_at: "2026-07-30T00:00:00+00:00",
        }));
      }
      if (url === "http://api.test/api/threads" && init?.method === "POST") {
        createThreadBody = init.body;
        return Promise.resolve(createThreadResponse("thread-new"));
      }
      if (
        url === "http://api.test/api/threads/thread-new/messages"
        && init?.method === "POST"
      ) {
        return Promise.resolve(jsonResponse(threadState({
          thread_id: "thread-new",
          run: {
            state: "done",
            steps: 1,
            error: null,
            started_at: null,
            updated_at: null,
          },
          conversation: [
            { id: "new-user", role: "user", text: "New Claude request" },
          ],
        })));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
    fireEvent.click(await screen.findByRole("button", {
      name: "TB cohort survival analysis",
    }));
    expect(await screen.findByText("Compare TB survival")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "New Claude request" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(createThreadBody).toBeDefined());
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue(
      "claude-opus-5",
    );
    expect(createThreadBody).toBe(JSON.stringify({
      model_name: "claude-opus-5",
    }));
  });

  it("keeps the newest saved-conversation list when an earlier refresh finishes late", async () => {
    const initialHistory = deferred<Response>();
    const refreshedHistory = deferred<Response>();
    let historyRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        historyRequests += 1;
        return historyRequests === 1
          ? initialHistory.promise
          : refreshedHistory.promise;
      }
      if (url === "http://api.test/api/threads") {
        return Promise.resolve(createThreadResponse("thread-terra"));
      }
      if (url === "http://api.test/api/threads/thread-terra/messages") {
        return Promise.resolve(jsonResponse(threadState({ thread_id: "thread-terra" })));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      { target: { value: "Who are you?" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(historyRequests).toBe(2));
    refreshedHistory.resolve(
      jsonResponse({
        items: [
          {
            thread_id: "thread-terra",
            title: "Assistant identity",
            title_source: "automatic",
            model_name: "gpt-5.6-terra",
            created_at: "2026-07-30T00:01:00+00:00",
            updated_at: "2026-07-30T00:01:00+00:00",
          },
          {
            thread_id: "thread-gpt55",
            title: "Analyze diabetes outcomes",
            title_source: "automatic",
            model_name: "gpt-5.5",
            created_at: "2026-07-30T00:00:00+00:00",
            updated_at: "2026-07-30T00:00:00+00:00",
          },
        ],
      }),
    );
    expect(await screen.findByRole("button", { name: "Assistant identity" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Analyze diabetes outcomes" })).toBeInTheDocument();

    await act(async () => {
      initialHistory.resolve(
        jsonResponse({
          items: [
            {
              thread_id: "thread-gpt55",
              title: "Analyze diabetes outcomes",
              title_source: "automatic",
              model_name: "gpt-5.5",
              created_at: "2026-07-30T00:00:00+00:00",
              updated_at: "2026-07-30T00:00:00+00:00",
            },
          ],
        }),
      );
    });

    expect(screen.getByRole("button", { name: "Assistant identity" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Analyze diabetes outcomes" })).toBeInTheDocument();
  });

  it("refreshes an untitled conversation until its generated title arrives", async () => {
    vi.useFakeTimers();
    let historyRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        historyRequests += 1;
        const title = historyRequests >= 4
          ? "Concurrent response check"
          : "Untitled conversation";
        return Promise.resolve(jsonResponse({
          items: historyRequests === 1 ? [] : [{
            thread_id: "thread-1",
            title,
            title_source: "automatic",
            model_name: "gpt-5.4",
            created_at: "2026-08-05T00:00:00+00:00",
            updated_at: "2026-08-05T00:00:00+00:00",
          }],
        }));
      }
      if (url === "http://api.test/api/threads") {
        return Promise.resolve(createThreadResponse());
      }
      if (url === "http://api.test/api/threads/thread-1/messages") {
        return Promise.resolve(jsonResponse(threadState({
          run: {
            state: "done",
            steps: 1,
            error: null,
            error_code: null,
            user_message: null,
            started_at: null,
            updated_at: null,
          },
          conversation: [
            { id: "user-1", role: "user", text: "Check concurrency" },
            { id: "assistant-1", role: "assistant", text: "Agent response ready" },
          ],
        })));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(historyRequests).toBe(1);
    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "Check concurrency" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("Agent response ready")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Untitled conversation" }),
    ).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(historyRequests).toBe(3);
    expect(
      screen.getByRole("button", { name: "Untitled conversation" }),
    ).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(
      screen.getByRole("button", { name: "Concurrent response check" }),
    ).toBeInTheDocument();
    const stoppedAt = historyRequests;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(historyRequests).toBe(stoppedAt);
  });

  it("stops title refresh when the active conversation is replaced", async () => {
    vi.useFakeTimers();
    let historyRequests = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        historyRequests += 1;
        return Promise.resolve(jsonResponse({
          items: historyRequests === 1 ? [] : [{
            thread_id: "thread-1",
            title: "Untitled conversation",
            title_source: "automatic",
            model_name: "gpt-5.4",
            created_at: "2026-08-05T00:00:00+00:00",
            updated_at: "2026-08-05T00:00:00+00:00",
          }],
        }));
      }
      if (url === "http://api.test/api/threads") {
        return Promise.resolve(createThreadResponse());
      }
      if (url === "http://api.test/api/threads/thread-1/messages") {
        return Promise.resolve(jsonResponse(threadState({
          run: {
            state: "done",
            steps: 1,
            error: null,
            error_code: null,
            user_message: null,
            started_at: null,
            updated_at: null,
          },
        })));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(historyRequests).toBe(1);
    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "Check cancellation" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    screen.getByRole("button", { name: "Untitled conversation" });
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    const stoppedAt = historyRequests;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(historyRequests).toBe(stoppedAt);
  });

  it("keeps DB-RAG review controls out of the sidebar", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(reviewState()));

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    expect(screen.queryByLabelText("Concept display mode")).not.toBeInTheDocument();
    await screen.findByLabelText("Model");

    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Find projects about diabetes" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("heading", { name: "Review dataset plan" }),
    ).toBeInTheDocument();
    expect(screen.getByText("diabetes")).toBeInTheDocument();
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toBeDisabled();
    expect(
      screen.getByText("Complete the review above before sending a new message."),
    ).toBeInTheDocument();
  });

  it("re-enables chat after cancelling a DB-RAG plan review", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(reviewState()))
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 2,
              error: null,
              error_code: null,
              user_message: null,
              started_at: null,
              updated_at: null,
            },
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 3,
              error: null,
              error_code: null,
              user_message: null,
              started_at: null,
              updated_at: null,
            },
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 0,
              error: null,
              error_code: null,
              user_message: null,
              started_at: null,
              updated_at: null,
            },
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Find projects about diabetes" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.click(screen.getByRole("button", { name: "Cancel review" }));

    await act(async () => {
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://api.test/api/threads/thread-1/interrupts/interrupt-1/resume",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({ action: "cancel" }),
      },
    );
    const composer = screen.getByLabelText("Ask a question about your dataset!");
    expect(composer).toBeDisabled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "http://api.test/api/threads/thread-1/state",
      { headers: expect.any(Headers) },
    );
    expect(composer).toBeEnabled();
    expect(
      screen.queryByText(/pending review could not be displayed/i),
    ).not.toBeInTheDocument();

    fireEvent.change(composer, { target: { value: "Start a new task" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => {
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      "http://api.test/api/threads/thread-1/messages",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({ text: "Start a new task" }),
      },
    );
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toBeDisabled();
  });

  it("does not expose developer debug controls", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(runtimeOptionsResponse());

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    await screen.findByLabelText("Ask a question about your dataset!");
    expect(screen.queryByLabelText("Show debug state")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Debug state")).not.toBeInTheDocument();
  });

  it("starts a new conversation while the selected thread is running", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 1,
              error: null,
              error_code: null,
              user_message: null,
              started_at: 1,
              updated_at: 1,
            },
            conversation: [
              { id: "running-user", role: "user", text: "Long analysis" },
            ],
          }),
        ),
      );

    render(
      <App
        apiBase="http://api.test"
        fetchImpl={fetchMock}
        loadConversationHistory={false}
      />,
    );
    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      { target: { value: "Long analysis" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByRole("button", { name: "Cancel run" });

    const newConversation = screen.getByRole("button", {
      name: "New conversation",
    });
    expect(newConversation).toBeEnabled();
    fireEvent.click(newConversation);

    expect(screen.queryByText("Long analysis")).not.toBeInTheDocument();
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toBeEnabled();
  });

  it("polls while a submitted message is running and renders the final state", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 1,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Find NIH grants" },
            ],
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 4,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Find NIH grants" },
              {
                id: "assistant-1",
                role: "assistant",
                text: "The background run has completed.",
              },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    await act(async () => {
      await Promise.resolve();
    });

    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Find NIH grants" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.queryByText(/Run status:/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Agent activity")).toHaveTextContent(
      "Agent is working",
    );
    expect(screen.getByLabelText("Agent activity")).toHaveTextContent(
      "Working on your request.",
    );
    expect(screen.getByLabelText("Agent activity")).not.toHaveTextContent(
      "needs_code",
    );
    expect(screen.getByLabelText("Agent activity")).toHaveTextContent(
      "Completed graph steps: 1",
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(screen.getByText("The background run has completed.")).toBeInTheDocument();
    expect(screen.queryByText(/Run status:/)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://api.test/api/threads/thread-1/state",
      { headers: expect.any(Headers) },
    );
  });

  it("refreshes the saved conversation when a running thread reaches review", async () => {
    vi.useFakeTimers();
    let conversationRequests = 0;
    const summary = {
      thread_id: "thread-1",
      title: "Generated review title",
      title_source: "automatic",
      model_name: "gpt-5.6-terra",
      created_at: "2026-08-21T00:00:00+00:00",
      updated_at: "2026-08-21T00:00:00+00:00",
      last_opened_at: null,
      archived_at: null,
      awaiting_review: false,
    };
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/conversations") {
        conversationRequests += 1;
        return Promise.resolve(jsonResponse({
          items: conversationRequests === 1
            ? []
            : [{ ...summary, awaiting_review: conversationRequests > 2 }],
        }));
      }
      if (url === "http://api.test/api/threads" && init?.method === "POST") {
        return Promise.resolve(createThreadResponse());
      }
      if (
        url === "http://api.test/api/threads/thread-1/messages" &&
        init?.method === "POST"
      ) {
        return Promise.resolve(jsonResponse(threadState({
          run: {
            state: "running",
            steps: 1,
            error: null,
            error_code: null,
            user_message: null,
            started_at: 1,
            updated_at: 1,
          },
          conversation: [{ id: "user-1", role: "user", text: "Build a dataset" }],
        })));
      }
      if (url === "http://api.test/api/threads/thread-1/state") {
        return Promise.resolve(jsonResponse(reviewState()));
      }
      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} />);
    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      { target: { value: "Build a dataset" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: "Generated review title" }))
      .toBeInTheDocument();
    expect(screen.queryByText("Awaiting review")).not.toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(screen.getByText("Awaiting review")).toBeInTheDocument();
  });

  it("cancels a running turn, retains its message, and re-enables the composer", async () => {
    const cancelResponse = deferred<Response>();
    const runningState = threadState({
      run: {
        state: "running",
        steps: 1,
        error: null,
        started_at: null,
        updated_at: null,
      },
      conversation: [
        { id: "user-1", role: "user", text: "Analyze the cohort" },
      ],
    });
    const cancelledState = threadState({
      run: {
        state: "cancelled",
        steps: 1,
        error: null,
        started_at: null,
        updated_at: null,
      },
      conversation: [
        {
          id: "user-1",
          role: "user",
          text: "Analyze the cohort",
          status: "cancelled",
        },
      ],
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(runningState))
      .mockReturnValueOnce(cancelResponse.promise);

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(await screen.findByLabelText("Ask a question about your dataset!"), {
      target: { value: "Analyze the cohort" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel run" }));

    expect(screen.getByRole("button", { name: "Cancelling run" })).toBeDisabled();
    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://api.test/api/threads/thread-1/cancel",
      { method: "POST", headers: expect.any(Headers) },
    );

    cancelResponse.resolve(jsonResponse(cancelledState));

    expect(await screen.findByText("Cancelled")).toBeInTheDocument();
    expect(screen.getByLabelText("Ask a question about your dataset!")).toBeEnabled();
    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "Continue where we left off" },
    });
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  it("ignores a poll response that finishes after cancellation", async () => {
    vi.useFakeTimers();
    const firstPoll = deferred<Response>();
    const runningState = threadState({
      run: {
        state: "running",
        steps: 1,
        error: null,
        started_at: null,
        updated_at: null,
      },
      conversation: [{ id: "user-1", role: "user", text: "Analyze the cohort" }],
    });
    const cancelledState = threadState({
      run: {
        state: "cancelled",
        steps: 1,
        error: null,
        started_at: null,
        updated_at: null,
      },
      conversation: [
        {
          id: "user-1",
          role: "user",
          text: "Analyze the cohort",
          status: "cancelled",
        },
      ],
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(runningState))
      .mockReturnValueOnce(firstPoll.promise)
      .mockResolvedValueOnce(jsonResponse(cancelledState));

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);
    await act(async () => Promise.resolve());
    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "Analyze the cohort" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => Promise.resolve());
    await act(async () => vi.advanceTimersByTimeAsync(1000));

    fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("Cancelled")).toBeInTheDocument();

    await act(async () => {
      firstPoll.resolve(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 4,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Analyze the cohort" },
              { id: "assistant-late", role: "assistant", text: "Late result" },
            ],
          }),
        ),
      );
      await Promise.resolve();
    });

    expect(screen.queryByText("Late result")).not.toBeInTheDocument();
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
  });

  it("keeps generated figure previews stable when polling repeats the same visible state", async () => {
    vi.useFakeTimers();
    Object.defineProperties(URL, {
      createObjectURL: { configurable: true, value: vi.fn(() => "blob:figure") },
      revokeObjectURL: { configurable: true, value: vi.fn() },
    });
    const runningState = threadState({
      run: {
        state: "running",
        steps: 1,
        error: null,
        started_at: 10,
        updated_at: 11,
      },
      conversation: [
        { id: "user-1", role: "user", text: "Create a dataset" },
        {
          id: "assistant-1",
          role: "assistant",
          text: "Approved analysis output.",
          attachments: [
            {
              id: "figure-1",
              kind: "figure",
              label: "Figure generated by approved final output.",
              filename: "",
              mime: "image/png",
              byte_size: null,
              relationship: "output",
              origin_message_id: null,
            },
          ],
        },
      ],
      diagnostics: { workflow_milestone: "db_rag_sql_generation" },
    });
    const repeatedRunningState = threadState({
      ...runningState,
      run: {
        ...runningState.run,
        updated_at: 12,
      },
    });
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/threads") {
        return Promise.resolve(createThreadResponse());
      }
      if (url === "http://api.test/api/threads/thread-1/messages") {
        return Promise.resolve(jsonResponse(runningState));
      }
      if (url === "http://api.test/api/threads/thread-1/state") {
        return Promise.resolve(jsonResponse(repeatedRunningState));
      }
      if (url === "http://api.test/api/threads/thread-1/attachments/figure-1") {
        return Promise.resolve(new Response("figure", { status: 200 }));
      }
      return Promise.reject(
        new Error(`Unexpected request: ${String(init?.method ?? "GET")} ${url}`),
      );
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Create a dataset" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    const figure = screen.getByRole("img", {
      name: "Figure generated by approved final output.",
    });
    expect(figure).toHaveAttribute(
      "src",
      "blob:figure",
    );
    expect(
      fetchMock.mock.calls.filter(
        ([url]) => url === "http://api.test/api/threads/thread-1/attachments/figure-1",
      ),
    ).toHaveLength(2);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
      await Promise.resolve();
    });

    expect(
      fetchMock.mock.calls.filter(
        ([url]) =>
          url === "http://api.test/api/threads/thread-1/attachments/figure-1",
      ),
    ).toHaveLength(2);
    expect(
      screen.getByRole("img", {
        name: "Figure generated by approved final output.",
      }),
    ).toBeInTheDocument();
  });

  it("serializes polling so slow poll responses cannot overlap", async () => {
    vi.useFakeTimers();
    const firstPoll = deferred<Response>();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 1,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Find NIH grants" },
            ],
          }),
        ),
      )
      .mockReturnValueOnce(firstPoll.promise);

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    await act(async () => {
      await Promise.resolve();
    });
    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Find NIH grants" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await act(async () => {
      await Promise.resolve();
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(fetchMock).toHaveBeenCalledTimes(4);

    await act(async () => {
      firstPoll.resolve(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 4,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Find NIH grants" },
              {
                id: "assistant-1",
                role: "assistant",
                text: "Completed after slow polling.",
              },
            ],
          }),
        ),
      );
      await Promise.resolve();
    });

    expect(screen.getByText("Completed after slow polling.")).toBeInTheDocument();
    expect(screen.queryByText(/Run status:/)).not.toBeInTheDocument();
  });

  it("loads runtime options once under React StrictMode effect replay", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(runtimeOptionsResponse());

    render(
      <StrictMode>
        <App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />
      </StrictMode>,
    );

    await screen.findByLabelText("Model");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://api.test/api/runtime/options",
      { headers: expect.any(Headers) },
    );
  });

  it("disables Send until runtime options load", async () => {
    const loadOptions = deferred<Response>();
    const fetchMock = vi.fn().mockReturnValueOnce(loadOptions.promise);

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByLabelText("Attach files")).toBeDisabled();

    await act(async () => {
      loadOptions.resolve(runtimeOptionsResponse());
      await Promise.resolve();
    });

    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByLabelText("Attach files")).toBeEnabled();
    fireEvent.change(
      screen.getByLabelText("Ask a question about your dataset!"),
      { target: { value: "Ready to send" } },
    );
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
  });

  it("renders DB-RAG review and final approve sends the resume payload", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(reviewState()))
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 3,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "assistant-1", role: "assistant", text: "Approved." },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Find projects about diabetes" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("heading", { name: "Review dataset plan" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toBeDisabled();
    expect(
      screen.getByText("Complete the review above before sending a new message."),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("button", { name: "Approve plan and extract" }),
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://api.test/api/threads/thread-1/interrupts/interrupt-1/resume",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({
          action: "approve",
          selected_column_keys: ["projects.project_id"],
        }),
      },
    );
    expect(await screen.findByText("Approved.")).toBeInTheDocument();
  });

  it("renders inline shared clarification and resumes with the selected option", async () => {
    const clarificationState = threadState({
      run: {
        state: "interrupted",
        steps: 2,
        error: null,
        started_at: null,
        updated_at: null,
      },
      active_interrupt: {
        id: "interrupt-clarification",
        type: "agent_clarification",
        question: "Which follow-up window should be used?",
        reason: "The outcome exists at multiple visits.",
        options: [
          { id: "month-12", label: "Use the 12-month visit." },
          { id: "last", label: "Use the last observed visit." },
        ],
      },
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(clarificationState))
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 3,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              {
                id: "assistant-clarified",
                role: "assistant",
                text: "I used the 12-month visit.",
              },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      { target: { value: "Create an outcome dataset" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("heading", { name: "Clarification needed" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Which follow-up window should be used?"),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByRole("radio", { name: "Use the 12-month visit." }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://api.test/api/threads/thread-1/interrupts/interrupt-clarification/resume",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({
          action: "answer",
          answer: "Use the 12-month visit.",
        }),
      },
    );
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toBeEnabled();
  });

  it("cancels a clarification, pauses, and waits for the next user message", async () => {
    const clarificationState = threadState({
      run: {
        state: "interrupted",
        steps: 2,
        error: null,
        started_at: null,
        updated_at: null,
      },
      active_interrupt: {
        id: "interrupt-clarification",
        type: "agent_clarification",
        question: "Which follow-up window should be used?",
        reason: "The outcome exists at multiple visits.",
        options: [
          { id: "month-12", label: "Use the 12-month visit." },
          { id: "last", label: "Use the last observed visit." },
        ],
      },
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(clarificationState))
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 3,
              error: null,
              started_at: null,
              updated_at: null,
            },
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 1,
              error: null,
              started_at: null,
              updated_at: null,
            },
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      { target: { value: "Create an outcome dataset" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByRole("heading", { name: "Clarification needed" });

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://api.test/api/threads/thread-1/interrupts/interrupt-clarification/resume",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({ action: "cancel" }),
      },
    );
    const composer = screen.getByLabelText("Ask a question about your dataset!");
    expect(composer).toBeEnabled();
    expect(fetchMock).toHaveBeenCalledTimes(4);

    fireEvent.change(composer, { target: { value: "Start a different task" } });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5));
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "http://api.test/api/threads/thread-1/messages",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({
          text: "Start a different task",
        }),
      },
    );
  });

  it("collapses a delegated clarification into a readable trace while its resume request is pending", async () => {
    const clarificationState = threadState({
      run: {
        state: "interrupted",
        steps: 2,
        error: null,
        started_at: null,
        updated_at: null,
      },
      active_interrupt: {
        id: "interrupt-clarification",
        type: "agent_clarification",
        question: "Which follow-up window should be used?",
        reason: "The outcome exists at multiple visits.",
        options: [
          { id: "month-12", label: "Use the 12-month visit." },
          { id: "last", label: "Use the last observed visit." },
        ],
      },
    });
    const resumeResponse = deferred<Response>();
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(clarificationState))
      .mockImplementationOnce(() => resumeResponse.promise);

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      { target: { value: "Create an outcome dataset" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    await screen.findByRole("heading", { name: "Clarification needed" });

    fireEvent.click(
      screen.getByRole("radio", { name: "Let the agent decide" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(
      screen.queryByRole("radiogroup", { name: "Your answer" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Clarification trace", { selector: "summary" }),
    ).toBeInTheDocument();
    fireEvent.click(
      screen.getByText("Clarification trace", { selector: "summary" }),
    );
    expect(screen.getByText("Let the agent decide.")).toBeInTheDocument();
  });

  it("disables composer and review controls while keeping cancellation available", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(reviewState()))
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 3,
              error: null,
              started_at: null,
              updated_at: null,
            },
            active_interrupt: reviewState().active_interrupt,
            conversation: [
              {
                id: "assistant-1",
                role: "assistant",
                text: "Searching again.",
              },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Find projects about diabetes" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    fireEvent.change(await screen.findByLabelText("Revision feedback"), {
      target: { value: "Prefer title fields." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Request revision" }));

    await screen.findByLabelText("Agent activity");
    expect(screen.getByLabelText("Agent activity")).toHaveTextContent(
      "Agent is working",
    );
    expect(screen.queryByRole("button", { name: "Send" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cancel run" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Request revision" })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Approve plan and extract" }),
    ).toBeDisabled();
  });

  it("revision sends the minimal revise resume payload", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(reviewState()))
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 3,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              {
                id: "assistant-1",
                role: "assistant",
                text: "Searching again.",
              },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Find projects about diabetes" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    fireEvent.change(await screen.findByLabelText("Revision feedback"), {
      target: { value: "Prefer title fields." },
    });
    fireEvent.click(screen.getByLabelText("Projects · TITLE"));
    fireEvent.click(screen.getByRole("button", { name: "Request revision" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://api.test/api/threads/thread-1/interrupts/interrupt-1/resume",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({
          action: "revise",
          feedback: "Prefer title fields.",
          selected_column_keys: ["projects.project_id", "projects.title"],
        }),
      },
    );
  });

  it("refreshes state and shows API error status and detail for 409 conflicts", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse({ detail: "thread is already running" }, { status: 409 }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 2,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              {
                id: "assistant-1",
                role: "assistant",
                text: "Still working on the previous request.",
              },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Find NIH grants" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "409: thread is already running",
    );
    expect(
      await screen.findByText("Still working on the previous request."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Run status:/)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://api.test/api/threads/thread-1/state",
      { headers: expect.any(Headers) },
    );
  });

  it("renders downloadable figure and dataset outputs inside the assistant message", async () => {
    Object.defineProperties(URL, {
      createObjectURL: { configurable: true, value: vi.fn(() => "blob:artifact") },
      revokeObjectURL: { configurable: true, value: vi.fn() },
    });
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 4,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Create a subset" },
              {
                id: "assistant-1",
                role: "assistant",
                text: "SQL used:\n```sql\nSELECT 1;\n```",
                attachments: [
                  {
                    id: "figure-1",
                    kind: "figure",
                    label: "Figure generated by approved final output.",
                    filename: "",
                    mime: "image/png",
                    byte_size: null,
                    relationship: "output",
                    origin_message_id: null,
                  },
                  {
                    id: "subset-1",
                    kind: "subset",
                    label: "LTFU subset",
                    filename: "",
                    mime: "text/csv",
                    byte_size: null,
                    relationship: "output",
                    origin_message_id: null,
                  },
                ],
              },
            ],
            datasets: [{ id: "subset-1", label: "LTFU subset", row_count: 1 }],
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-1",
          dataset_version: 1,
          sql: "SELECT 1;",
          sql_artifact: { id: "sql-1", kind: "validated_sql", version: 1 },
          sql_sha256: "hash",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-1",
          columns: ["subject_id"],
          rows: [{ subject_id: "SUB-1" }],
          row_count: 1,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-1",
          schema: { columns: ["subject_id"] },
        }),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Create a subset" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("heading", {
        name: "Epidemiology Research Agent",
      }),
    ).toBeInTheDocument();
    expect(await screen.findByText("SELECT 1;")).toBeInTheDocument();
    expect(screen.queryByText("Generated datasets")).not.toBeInTheDocument();
    expect(
      screen.queryByText(["Generated", "files"].join(" ")),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByRole("img", {
        name: "Figure generated by approved final output.",
      }),
    ).toHaveAttribute(
      "src",
      "blob:artifact",
    );
    expect(await screen.findByRole("button", { name: "Download figure" })).toBeEnabled();
    expect(await screen.findByRole("button", { name: "Download LTFU subset" })).toBeEnabled();

    fetchMock.mockResolvedValue(
      jsonResponse({
        columns: ["subject_id"],
        rows: [{ subject_id: "SUB-1" }],
        row_count: 1,
      }),
    );
    fireEvent.click(screen.getByText("Dataset details", { selector: "summary" }));

    expect(await screen.findByText("SUB-1")).toBeInTheDocument();
  });

  it("does not duplicate backend output outside the assistant chat message", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 4,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "How many rows?" },
              {
                id: "assistant-1",
                role: "assistant",
                text: "There are 17 index cases with diabetes.",
              },
            ],
            output: {
              qa_response: "There are 17 index cases with diabetes.",
              dataset_id: "subset-17",
            },
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "How many rows?" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByText("There are 17 index cases with diabetes."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Agent output")).not.toBeInTheDocument();
    expect(screen.queryByText("QA answer")).not.toBeInTheDocument();
    expect(screen.queryByText("subset-17")).not.toBeInTheDocument();
  });

  it("locks settings controls when backend state reports a locked thread", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 2,
              error: null,
              started_at: null,
              updated_at: null,
            },
            runtime_settings: {
              ...defaultRuntimeSettings,
              model_name: "gpt-5.4-mini",
              temperature: 0.2,
            },
            runtime_settings_locked: true,
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Lock runtime settings" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("button", {
        name: "Model locked: gpt-5.4-mini",
      }),
    ).toBeEnabled();
    expect(screen.queryByLabelText("Model")).not.toBeInTheDocument();
  });

  it("reset clears the current thread so the next submit creates a new settings-backed thread", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse("thread-1"))
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 2,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-1", role: "user", text: "Old question" },
              { id: "assistant-1", role: "assistant", text: "Old answer" },
            ],
            runtime_settings: {
              ...defaultRuntimeSettings,
              model_name: "gpt-5.4-mini",
            },
          }),
        ),
      )
      .mockResolvedValueOnce(createThreadResponse("thread-2"))
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            thread_id: "thread-2",
            run: {
              state: "done",
              steps: 1,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              { id: "user-2", role: "user", text: "New question" },
              { id: "assistant-2", role: "assistant", text: "New answer" },
            ],
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Old question" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Old answer")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));

    await waitFor(() => {
      expect(screen.queryByText("Old answer")).not.toBeInTheDocument();
    });
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "New question" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByText("New answer")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://api.test/api/threads",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({ model_name: "gpt-5.4-mini" }),
      },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "http://api.test/api/threads/thread-2/messages",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({ text: "New question" }),
      },
    );
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).includes("/api/threads/thread-1/reset"),
      ),
    ).toBe(false);
  });

  it("does not render a thread export control", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(jsonResponse(threadState()));

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(screen.getByLabelText("Ask a question about your dataset!"), {
      target: { value: "Create a thread" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(screen.queryByRole("link", { name: "Save Current Thread" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save Current Thread" })).not.toBeInTheDocument();
  });

  it("offers message attachments without the legacy universal upload panel", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(runtimeOptionsResponse());

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    expect(await screen.findByLabelText("Attach files")).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Upload your dataset (.csv)"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Upload your schema (.json)"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Upload files" }),
    ).not.toBeInTheDocument();
  });

  it("places the paperclip attachment control beside Send", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(runtimeOptionsResponse());

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    const attachmentButton = await screen.findByRole("button", {
      name: "Attach files",
    });
    const sendButton = screen.getByRole("button", { name: "Send" });

    expect(attachmentButton).toHaveAttribute("title", "Attach files");
    expect(attachmentButton.parentElement).toContainElement(sendButton);
  });

  it("renders analysis result review and resumes the exact interrupt", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "interrupted",
              steps: 3,
              error: null,
              started_at: null,
              updated_at: null,
            },
            active_interrupt: analysisReviewInterrupt(),
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 4,
              error: null,
              started_at: null,
              updated_at: null,
            },
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Fit a logistic regression" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("heading", { name: "Review analysis results" }),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toBeDisabled();
    fireEvent.click(
      screen.getByRole("button", { name: "Approve Results" }),
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://api.test/api/threads/thread-1/interrupts/interrupt-analysis/resume",
      {
        method: "POST",
        headers: expect.any(Headers),
        body: JSON.stringify({ action: "approve" }),
      },
    );
  });

  it("renders DB-RAG dataset review and submits feedback", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "interrupted",
              steps: 4,
              error: null,
              started_at: null,
              updated_at: null,
            },
            active_interrupt: datasetReviewInterrupt(
              "subset-1",
              "interrupt-dataset",
            ),
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-1",
          columns: ["gender"],
          rows: [{ gender: "male" }],
          row_count: 2,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-1",
          schema: { gender: { dataType: "string" } },
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-1",
          sql: "SELECT gender FROM baseline_subjects",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "running",
              steps: 5,
              error: null,
              started_at: null,
              updated_at: null,
            },
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(await screen.findByLabelText("Ask a question about your dataset!"), {
      target: { value: "Create a diabetes subset." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("heading", { name: "Review extracted dataset" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("male")).toBeInTheDocument();
    fireEvent.change(
      screen.getByLabelText("Feedback for the next dataset attempt"),
      {
        target: { value: "Use only baseline rows." },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Request revision" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenLastCalledWith(
        "http://api.test/api/threads/thread-1/interrupts/interrupt-dataset/resume",
        {
          method: "POST",
          headers: expect.any(Headers),
          body: JSON.stringify({
            action: "revise",
            feedback: "Use only baseline rows.",
          }),
        },
      );
    });
  });

  it("re-enables chat after cancelling a DB-RAG dataset review", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "interrupted",
              steps: 4,
              error: null,
              started_at: null,
              updated_at: null,
            },
            active_interrupt: datasetReviewInterrupt(
              "subset-cancel",
              "interrupt-dataset-cancel",
            ),
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-cancel",
          columns: ["subject_id"],
          rows: [{ subject_id: "SUB-1" }],
          row_count: 1,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-cancel",
          schema: { subject_id: { dataType: "string" } },
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-cancel",
          sql: "SELECT subject_id FROM baseline_subjects",
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "done",
              steps: 5,
              error: null,
              started_at: null,
              updated_at: null,
            },
          }),
        ),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(
      await screen.findByLabelText("Ask a question about your dataset!"),
      {
        target: { value: "Create a dataset" },
      },
    );
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenLastCalledWith(
        "http://api.test/api/threads/thread-1/interrupts/interrupt-dataset-cancel/resume",
        {
          method: "POST",
          headers: expect.any(Headers),
          body: JSON.stringify({ action: "cancel" }),
        },
      );
    });
    expect(
      screen.getByLabelText("Ask a question about your dataset!"),
    ).toBeEnabled();
  });

  it("hides matching dataset-created assistant messages while dataset review is active", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(runtimeOptionsResponse())
      .mockResolvedValueOnce(createThreadResponse())
      .mockResolvedValueOnce(
        jsonResponse(
          threadState({
            run: {
              state: "interrupted",
              steps: 4,
              error: null,
              started_at: null,
              updated_at: null,
            },
            conversation: [
              {
                id: "message-user",
                role: "user",
                text: "Create a diabetes subset.",
              },
              {
                id: "message-assistant",
                role: "assistant",
                text: "Dataset `subset-1` was created with 2 rows. Preview or download it from Generated datasets below.",
              },
            ],
            active_interrupt: datasetReviewInterrupt(
              "subset-1",
              "interrupt-dataset",
            ),
          }),
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-1",
          columns: ["gender"],
          rows: [{ gender: "male" }],
          row_count: 2,
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          dataset_id: "subset-1",
          schema: { gender: { dataType: "string" } },
        }),
      );

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(await screen.findByLabelText("Ask a question about your dataset!"), {
      target: { value: "Create a diabetes subset." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("heading", { name: "Review extracted dataset" }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Dataset `subset-1` was created/)).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Preview or download it from Generated datasets below/),
    ).not.toBeInTheDocument();
    expect(await screen.findByText("male")).toBeInTheDocument();
  });

  it("keeps pending dataset review inline without a global datasets panel", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);

      if (url === "http://api.test/api/runtime/options") {
        return Promise.resolve(runtimeOptionsResponse());
      }
      if (url === "http://api.test/api/threads") {
        return Promise.resolve(createThreadResponse());
      }
      if (url === "http://api.test/api/threads/thread-1/messages") {
        return Promise.resolve(
          jsonResponse(
            threadState({
              run: {
                state: "interrupted",
                steps: 4,
                error: null,
                started_at: null,
                updated_at: null,
              },
              active_interrupt: datasetReviewInterrupt(
                "pending-subset",
                "interrupt-dataset",
              ),
              datasets: [
                {
                  id: "approved-subset",
                  label: "Approved subset",
                  row_count: 3,
                },
                {
                  id: "pending-subset",
                  label: "Pending subset",
                  row_count: 2,
                },
              ],
            }),
          ),
        );
      }
      if (
        url ===
        "http://api.test/api/threads/thread-1/datasets/pending-subset/preview?limit=100"
      ) {
        return Promise.resolve(
          jsonResponse({
            dataset_id: "pending-subset",
            columns: ["gender"],
            rows: [{ gender: "male" }],
            row_count: 2,
          }),
        );
      }
      if (
        url ===
        "http://api.test/api/threads/thread-1/datasets/pending-subset/schema"
      ) {
        return Promise.resolve(
          jsonResponse({
            dataset_id: "pending-subset",
            schema: { gender: { dataType: "string" } },
          }),
        );
      }
      if (
        url ===
        "http://api.test/api/threads/thread-1/datasets/approved-subset/preview?limit=100"
      ) {
        return Promise.resolve(
          jsonResponse({
            dataset_id: "approved-subset",
            columns: ["project_id"],
            rows: [{ project_id: "P-1" }],
            row_count: 3,
          }),
        );
      }
      if (
        url ===
        "http://api.test/api/threads/thread-1/datasets/approved-subset/schema"
      ) {
        return Promise.resolve(
          jsonResponse({
            dataset_id: "approved-subset",
            schema: { project_id: { dataType: "string" } },
          }),
        );
      }

      return Promise.reject(new Error(`Unexpected request: ${url}`));
    });

    render(<App apiBase="http://api.test" fetchImpl={fetchMock} loadConversationHistory={false} />);

    fireEvent.change(await screen.findByLabelText("Ask a question about your dataset!"), {
      target: { value: "Create a diabetes subset." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send" }));

    expect(
      await screen.findByRole("heading", { name: "Review extracted dataset" }),
    ).toBeInTheDocument();
    expect(await screen.findByText("male")).toBeInTheDocument();
    expect(screen.queryByText("Dataset ID: pending-subset")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("region", { name: "Generated datasets" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Dataset ID: approved-subset")).not.toBeInTheDocument();
  });
});
