import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, createApiClient, type ApiClient } from "./apiClient";
import AgentActivityTimeline from "./AgentActivityTimeline";
import AttachmentComposer from "./AttachmentComposer";
import AnalysisResultReview from "./AnalysisResultReview";
import AppShell from "./AppShell";
import ClarificationTrace from "./ClarificationTrace";
import { DEFAULT_API_BASE } from "./config";
import ConversationMessage from "./ConversationMessage";
import DbRagReview from "./DbRagReview";
import DbRagDatasetReview from "./DbRagDatasetReview";
import Clarification from "./Clarification";
import ConversationHistory from "./ConversationHistory";
import ModelOutputLimit from "./ModelOutputLimit";
import EmbeddingFallbackNotice from "./EmbeddingFallbackNotice";
import type {
  ApiThreadState,
  ActiveInterrupt,
  AttachmentManifestSummary,
  AttachmentUploadError,
  ClarificationExchange,
  ConversationMessage as ConversationMessageType,
  ResumeInterruptPayload,
  RuntimeOptions,
  RuntimeSettings,
  ConversationSummary,
} from "./types";
import { AGENT_DECIDE_ANSWER } from "./types";
import type { FormEvent, KeyboardEvent } from "react";

interface Props {
  apiClient: ApiClient;
  loadConversationHistory?: boolean;
}

interface TestProps {
  fetchImpl?: typeof fetch;
  apiBase?: string;
  loadConversationHistory?: boolean;
}

const POLL_INTERVAL_MS = 1000;
const TITLE_POLL_TIMEOUT_MS = 120_000;
const UNTITLED_CONVERSATION = "Untitled conversation";

function assertNever(value: never): never {
  throw new Error(`Unsupported interrupt: ${JSON.stringify(value)}`);
}

const EMPTY_RUNTIME_SETTINGS: RuntimeSettings = {
  model_name: "",
  temperature: null,
  top_p: null,
  max_steps: null,
  timeout_seconds: null,
  db_rag_embedding_model: "",
  db_rag_reranker_model: "",
};

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const detail =
      typeof error.detail === "string"
        ? error.detail
        : error.detail
          ? JSON.stringify(error.detail)
          : error.message;
    return `${error.status}: ${detail}`;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return "An unexpected error occurred.";
}

export function normalizeNewConversationRuntimeSettings(
  current: RuntimeSettings,
  options: RuntimeOptions,
): RuntimeSettings {
  return options.models.some((model) => model.id === current.model_name)
    ? current
    : options.defaults;
}

export function isPendingMessageAcknowledged(
  conversationMessage: ConversationMessageType,
  pendingMessage: ConversationMessageType,
): boolean {
  const attachmentIds = (message: ConversationMessageType) =>
    (message.attachments ?? [])
      .map((attachment) => attachment.id)
      .sort()
      .join("\u0000");
  return (
    conversationMessage.role === "user" &&
    conversationMessage.text === pendingMessage.text &&
    attachmentIds(conversationMessage) === attachmentIds(pendingMessage)
  );
}

function diagnosticText(value: unknown): string {
  return typeof value === "string" && value.trim() ? value.trim() : "";
}

function activityStatusText(_state: ApiThreadState | null): string {
  return "Working on your request.";
}

function isDatasetReviewCompletionMessage(
  message: ConversationMessageType,
  datasetId: string | null,
) {
  if (!datasetId || message.role !== "assistant") {
    return false;
  }
  const escapedDatasetId = datasetId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(
    `Dataset\\s+\`${escapedDatasetId}\`\\s+was created`,
    "i",
  ).test(message.text);
}

interface ActivityMessageProps {
  detail: string;
  steps: number | null | undefined;
  title: string;
}

function ActivityMessage({ detail, steps, title }: ActivityMessageProps) {
  return (
    <li className="message message-assistant message-activity">
      <section
        aria-label="Agent activity"
        aria-live="polite"
        className="activity-panel"
      >
        <span className="activity-spinner" aria-hidden="true" />
        <div>
          <h3>{title}</h3>
          <p>{detail}</p>
          {steps ? (
            <p className="activity-meta">Completed graph steps: {steps}</p>
          ) : null}
        </div>
      </section>
    </li>
  );
}

export default function App({
  apiClient,
  loadConversationHistory = true,
}: Props) {
  const createThreadPromiseRef = useRef<Promise<string> | null>(null);
  const runtimeOptionsPromiseRef = useRef<ReturnType<
    typeof apiClient.getRuntimeOptions
  > | null>(null);
  const savedConversationsRequestRef = useRef(0);
  const savedConversationsMutationRef = useRef(0);
  const pollGenerationRef = useRef(0);
  const selectionGenerationRef = useRef(0);
  const selectedThreadIdRef = useRef<string | null>(null);
  const [threadId, setThreadId] = useState<string | null>(null);
  const fetchAttachmentBlob = useCallback(
    (attachmentId: string) => {
      if (!threadId) {
        return Promise.reject(new Error("Thread is unavailable."));
      }
      return apiClient.fetchAttachmentBlob(threadId, attachmentId);
    },
    [apiClient, threadId],
  );
  const [state, setState] = useState<ApiThreadState | null>(null);
  const [runtimeOptions, setRuntimeOptions] = useState<RuntimeOptions | null>(
    null,
  );
  const [savedConversations, setSavedConversations] = useState<ConversationSummary[]>([]);
  const [titlePollingThreadId, setTitlePollingThreadId] = useState<string | null>(null);
  const [selectedRuntimeSettings, setSelectedRuntimeSettings] =
    useState<RuntimeSettings>(EMPTY_RUNTIME_SETTINGS);
  const [replacementModelName, setReplacementModelName] = useState("");
  const [message, setMessage] = useState("");
  const [pendingUserMessage, setPendingUserMessage] =
    useState<ConversationMessageType | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [runFailureMessage, setRunFailureMessage] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isResuming, setIsResuming] = useState(false);
  const [isCancelling, setIsCancelling] = useState(false);
  const [stagedAttachments, setStagedAttachments] = useState<
    AttachmentManifestSummary[]
  >([]);
  const [attachmentErrors, setAttachmentErrors] = useState<
    AttachmentUploadError[]
  >([]);
  const [isUploadingAttachments, setIsUploadingAttachments] = useState(false);
  const [submittedClarifications, setSubmittedClarifications] = useState<
    Record<string, ClarificationExchange>
  >({});
  const [isModelLockHintVisible, setIsModelLockHintVisible] = useState(false);
  const [isLoadingConversation, setIsLoadingConversation] = useState(false);
  const [restoredReviewThreadId, setRestoredReviewThreadId] = useState<
    string | null
  >(null);

  useEffect(() => {
    let isMounted = true;
    runtimeOptionsPromiseRef.current ??= apiClient.getRuntimeOptions();

    runtimeOptionsPromiseRef.current
      .then((options) => {
        if (isMounted) {
          setRuntimeOptions(options);
          setSelectedRuntimeSettings(options.defaults);
        }
      })
      .catch((optionsError: unknown) => {
        if (isMounted) {
          setError(errorMessage(optionsError));
        }
      });

    return () => {
      isMounted = false;
    };
  }, [apiClient]);

  useEffect(() => {
    if (!loadConversationHistory) {
      return;
    }
    void refreshSavedConversations();
  }, [apiClient, loadConversationHistory]);

  useEffect(() => {
    if (
      !loadConversationHistory ||
      !titlePollingThreadId ||
      titlePollingThreadId !== threadId
    ) {
      return;
    }

    let cancelled = false;
    let timeoutId: number | undefined;
    const deadline = Date.now() + TITLE_POLL_TIMEOUT_MS;

    async function pollOnce() {
      const items = await refreshSavedConversations();
      if (cancelled) {
        return;
      }
      const activeConversation = items?.find(
        (item) => item.thread_id === titlePollingThreadId,
      );
      if (
        activeConversation &&
        activeConversation.title !== UNTITLED_CONVERSATION
      ) {
        setTitlePollingThreadId((current) =>
          current === titlePollingThreadId ? null : current,
        );
        return;
      }
      if (Date.now() < deadline) {
        timeoutId = window.setTimeout(pollOnce, POLL_INTERVAL_MS);
      }
    }

    void pollOnce();
    return () => {
      cancelled = true;
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [apiClient, loadConversationHistory, threadId, titlePollingThreadId]);

  useEffect(() => {
    if (state?.runtime_settings) {
      setSelectedRuntimeSettings(state.runtime_settings);
    }
  }, [state?.runtime_settings]);

  const activityRunByUserMessageId = useMemo(
    () =>
      new Map(
        (state?.activity_runs ?? []).map((run) => [run.user_message_id, run]),
      ),
    [state?.activity_runs],
  );

  const projectedClarificationIds = new Set(
    (state?.conversation ?? []).flatMap((conversationMessage) =>
      (conversationMessage.clarifications ?? []).map(
        (clarification) => clarification.interrupt_id,
      ),
    ),
  );
  const optimisticClarifications = Object.values(submittedClarifications).filter(
    (clarification) => !projectedClarificationIds.has(clarification.interrupt_id),
  );

  useEffect(() => {
    setSubmittedClarifications((current) => {
      const pending = Object.fromEntries(
        Object.entries(current).filter(
          ([interruptId]) => !projectedClarificationIds.has(interruptId),
        ),
      );
      return Object.keys(pending).length === Object.keys(current).length
        ? current
        : pending;
    });
  }, [state]);

  function applyThreadState(nextState: ApiThreadState) {
    if (nextState.runtime_settings) {
      setSelectedRuntimeSettings(nextState.runtime_settings);
    } else if (nextState.model_name) {
      setSelectedRuntimeSettings((current) => ({
        ...current,
        model_name: nextState.model_name ?? current.model_name,
      }));
    }
    if (!nextState.model_replacement_required) {
      setReplacementModelName("");
    }
    if (
      (nextState.run.state === "error" || nextState.run.state === "timeout") &&
      nextState.run.user_message
    ) {
      setRunFailureMessage(nextState.run.user_message);
    } else if (nextState.run.state !== "running") {
      setRunFailureMessage(null);
    }
    setState(nextState);
  }

  function applyOwnedThreadState(
    ownerThreadId: string,
    generation: number,
    nextState: ApiThreadState,
  ): boolean {
    if (
      selectionGenerationRef.current !== generation ||
      selectedThreadIdRef.current !== ownerThreadId
    ) {
      return false;
    }
    if (nextState.thread_id !== ownerThreadId) {
      setState(null);
      setError(
        "The selected conversation returned mismatched thread data. Please try again.",
      );
      setIsLoadingConversation(false);
      return false;
    }
    applyThreadState(nextState);
    setIsLoadingConversation(false);
    return true;
  }

  async function refreshSavedConversations(): Promise<ConversationSummary[] | null> {
    const requestId = savedConversationsRequestRef.current + 1;
    savedConversationsRequestRef.current = requestId;
    const mutationId = savedConversationsMutationRef.current;
    try {
      const response = await apiClient.listConversations();
      if (
        requestId !== savedConversationsRequestRef.current ||
        mutationId !== savedConversationsMutationRef.current
      ) {
        return null;
      }
      const items = response.items ?? [];
      setSavedConversations(items);
      return items;
    } catch {
      // A history refresh must not block the active analysis workflow.
      return null;
    }
  }

  function invalidateSavedConversationRequests() {
    savedConversationsMutationRef.current += 1;
  }

  async function openConversation(nextThreadId: string) {
    const generation = selectionGenerationRef.current + 1;
    selectionGenerationRef.current = generation;
    pollGenerationRef.current += 1;
    selectedThreadIdRef.current = nextThreadId;
    setThreadId(nextThreadId);
    setState(null);
    setReplacementModelName("");
    setPendingUserMessage(null);
    setSubmittedClarifications({});
    setError(null);
    setRunFailureMessage(null);
    setIsSubmitting(false);
    setIsResuming(false);
    setIsCancelling(false);
    setIsUploadingAttachments(false);
    setIsLoadingConversation(true);
    setRestoredReviewThreadId(nextThreadId);
    try {
      const nextState = await apiClient.getThreadState(nextThreadId);
      if (!applyOwnedThreadState(nextThreadId, generation, nextState)) {
        return;
      }
    } catch (openError) {
      if (
        selectionGenerationRef.current === generation &&
        selectedThreadIdRef.current === nextThreadId
      ) {
        setState(null);
        setIsLoadingConversation(false);
        setError(errorMessage(openError));
      }
      return;
    }
    try {
      const opened = await apiClient.markConversationOpened(nextThreadId);
      if (
        selectionGenerationRef.current !== generation ||
        selectedThreadIdRef.current !== nextThreadId
      ) {
        return;
      }
      setSavedConversations((current) =>
        current.map((item) =>
          item.thread_id === nextThreadId ? opened : item,
        ),
      );
      void refreshSavedConversations();
      setError(null);
    } catch (openError) {
      if (
        selectionGenerationRef.current === generation &&
        selectedThreadIdRef.current === nextThreadId
      ) {
        setError(errorMessage(openError));
      }
    }
  }

  async function renameConversation(threadIdToRename: string, title: string) {
    try {
      const renamed = await apiClient.renameConversation(threadIdToRename, title);
      setSavedConversations((current) =>
        current.map((item) =>
          item.thread_id === threadIdToRename ? renamed : item,
        ),
      );
      setError(null);
    } catch (renameError) {
      setError(errorMessage(renameError));
      throw renameError;
    }
  }

  async function archiveConversation(threadIdToArchive: string) {
    try {
      invalidateSavedConversationRequests();
      const archived = await apiClient.archiveConversation(threadIdToArchive);
      setSavedConversations((current) =>
        current.map((item) =>
          item.thread_id === threadIdToArchive ? archived : item,
        ),
      );
      if (threadId === threadIdToArchive) {
        newConversation();
      }
      setError(null);
    } catch (archiveError) {
      setError(errorMessage(archiveError));
      throw archiveError;
    }
  }

  async function restoreConversation(threadIdToRestore: string) {
    try {
      invalidateSavedConversationRequests();
      const restored = await apiClient.restoreConversation(threadIdToRestore);
      setSavedConversations((current) =>
        current.map((item) =>
          item.thread_id === threadIdToRestore ? restored : item,
        ),
      );
      await openConversation(threadIdToRestore);
      setError(null);
    } catch (restoreError) {
      setError(errorMessage(restoreError));
      throw restoreError;
    }
  }

  async function deleteConversation(threadIdToDelete: string) {
    try {
      invalidateSavedConversationRequests();
      setTitlePollingThreadId((current) =>
        current === threadIdToDelete ? null : current,
      );
      await apiClient.deleteConversation(threadIdToDelete);
      setSavedConversations((current) =>
        current.filter((item) => item.thread_id !== threadIdToDelete),
      );
      if (threadId === threadIdToDelete) {
        newConversation();
      }
      setError(null);
    } catch (deleteError) {
      setError(errorMessage(deleteError));
      throw deleteError;
    }
  }

  useEffect(() => {
    if (!threadId || state?.run.state !== "running" || isCancelling) {
      return;
    }

    const activeThreadId = threadId;
    const selectionGeneration = selectionGenerationRef.current;
    let timeoutId: number | undefined;
    let isCancelled = false;
    const generation = pollGenerationRef.current + 1;
    pollGenerationRef.current = generation;

    async function pollOnce() {
      try {
        const nextState = await apiClient.getThreadState(activeThreadId);
        if (isCancelled || pollGenerationRef.current !== generation) {
          return;
        }

        if (
          !applyOwnedThreadState(
            activeThreadId,
            selectionGeneration,
            nextState,
          )
        ) {
          return;
        }
        setError(null);

        if (nextState.run.state === "running") {
          timeoutId = window.setTimeout(pollOnce, POLL_INTERVAL_MS);
        } else if (loadConversationHistory) {
          void refreshSavedConversations();
        }
      } catch (pollError) {
        if (
          !isCancelled &&
          pollGenerationRef.current === generation &&
          selectionGenerationRef.current === selectionGeneration &&
          selectedThreadIdRef.current === activeThreadId
        ) {
          setError(errorMessage(pollError));
          timeoutId = window.setTimeout(pollOnce, POLL_INTERVAL_MS);
        }
      }
    }

    timeoutId = window.setTimeout(pollOnce, POLL_INTERVAL_MS);

    return () => {
      isCancelled = true;
      pollGenerationRef.current += 1;
      if (timeoutId !== undefined) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [apiClient, isCancelling, state?.run.state, threadId]);

  async function handleRequestError(
    requestError: unknown,
    ownerThreadId: string,
    generation: number,
  ) {
    if (requestError instanceof ApiError && requestError.status === 409) {
      try {
        const refreshedState = await apiClient.getThreadState(ownerThreadId);
        applyOwnedThreadState(ownerThreadId, generation, refreshedState);
      } catch {
        // Keep the original conflict visible if the refresh also fails.
      }
    }

    if (
      selectionGenerationRef.current === generation &&
      selectedThreadIdRef.current === ownerThreadId
    ) {
      setError(errorMessage(requestError));
    }
  }

  async function ensureThread() {
    if (threadId) {
      return threadId;
    }
    if (!runtimeOptions) {
      return null;
    }

    const executableSettings = normalizeNewConversationRuntimeSettings(
      selectedRuntimeSettings,
      runtimeOptions,
    );
    if (executableSettings !== selectedRuntimeSettings) {
      setSelectedRuntimeSettings(executableSettings);
    }
    createThreadPromiseRef.current ??= apiClient
      .createThread(executableSettings.model_name)
      .then((response) => response.thread_id)
      .catch((createError: unknown) => {
        createThreadPromiseRef.current = null;
        throw createError;
      });

    const nextThreadId = await createThreadPromiseRef.current;
    if (!selectedThreadIdRef.current) {
      selectionGenerationRef.current += 1;
      selectedThreadIdRef.current = nextThreadId;
    }
    setThreadId(nextThreadId);
    return nextThreadId;
  }

  async function submitMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();

    const text = message.trim();
    const attachmentIds = stagedAttachments.map(
      (attachment) => attachment.id,
    );
    if (
      (!text && !attachmentIds.length) ||
      !runtimeOptions ||
      isSubmitting ||
      isResuming ||
      isUploadingAttachments ||
      attachmentErrors.length > 0 ||
      state?.run.state === "running"
    ) {
      return;
    }

    const replacementModel = state?.model_replacement_required
      ? runtimeOptions.models.find((model) => model.id === replacementModelName)
      : undefined;
    if (state?.model_replacement_required && !replacementModel) {
      setError("Choose an available model before continuing this conversation.");
      return;
    }
    if (
      replacementModel &&
      !window.confirm(
        `This conversation used ${state?.model_label ?? state?.model_name ?? "an unavailable model"}. Continue with ${replacementModel.label}?`,
      )
    ) {
      return;
    }

    const optimisticMessageId = `pending-user-${Date.now()}`;
    const optimisticMessage: ConversationMessageType = {
      id: optimisticMessageId,
      role: "user",
      text,
      created_at: null,
      attachments: stagedAttachments.map((attachment) => ({
        id: attachment.id,
        kind: attachment.kind,
        label: attachment.filename,
        filename: attachment.filename,
        mime: attachment.mime,
        byte_size: attachment.byte_size,
        relationship: "input",
        origin_message_id: optimisticMessageId,
      })),
    };
    setIsSubmitting(true);
    setPendingUserMessage(optimisticMessage);
    setMessage("");
    let activeThreadId: string | null = null;
    let generation: number | null = null;
    try {
      activeThreadId = await ensureThread();
      if (!activeThreadId) {
        setPendingUserMessage(null);
        setMessage(text);
        return;
      }
      generation = selectionGenerationRef.current;

      const nextState = await apiClient.submitMessage(
        activeThreadId,
        text,
        attachmentIds,
        replacementModel?.id,
      );
      if (!applyOwnedThreadState(activeThreadId, generation, nextState)) {
        return;
      }
      if (loadConversationHistory) {
        setTitlePollingThreadId(activeThreadId);
      }
      setStagedAttachments([]);
      setAttachmentErrors([]);
      setError(null);
    } catch (submitError) {
      if (
        activeThreadId &&
        generation !== null &&
        selectionGenerationRef.current === generation &&
        selectedThreadIdRef.current === activeThreadId
      ) {
        setPendingUserMessage(null);
        setMessage(text);
        await handleRequestError(submitError, activeThreadId, generation);
      } else {
        if (!activeThreadId) {
          setPendingUserMessage(null);
          setMessage(text);
          setError(errorMessage(submitError));
        }
      }
    } finally {
      if (
        !activeThreadId ||
        generation === null ||
        (selectionGenerationRef.current === generation &&
          selectedThreadIdRef.current === activeThreadId)
      ) {
        setIsSubmitting(false);
      }
    }
  }

  async function selectAttachments(files: File[]) {
    if (!runtimeOptions || !files.length || isBusy) {
      return;
    }
    setIsUploadingAttachments(true);
    let activeThreadId: string | null = null;
    let generation: number | null = null;
    try {
      activeThreadId = await ensureThread();
      if (!activeThreadId) {
        return;
      }
      generation = selectionGenerationRef.current;
      const result = await apiClient.uploadAttachments(activeThreadId, files);
      if (
        selectionGenerationRef.current !== generation ||
        selectedThreadIdRef.current !== activeThreadId
      ) {
        return;
      }
      setStagedAttachments((current) => {
        const byId = new Map(
          [...current, ...result.attachments].map((item) => [item.id, item]),
        );
        return [...byId.values()];
      });
      const attemptedFilenames = new Set(files.map((file) => file.name));
      setAttachmentErrors((current) => [
        ...current.filter(
          (uploadError) => !attemptedFilenames.has(uploadError.filename),
        ),
        ...result.errors,
      ]);
      setError(null);
    } catch (uploadError) {
      if (activeThreadId && generation !== null) {
        await handleRequestError(uploadError, activeThreadId, generation);
      } else {
        setError(errorMessage(uploadError));
      }
    } finally {
      if (
        !activeThreadId ||
        generation === null ||
        (selectionGenerationRef.current === generation &&
          selectedThreadIdRef.current === activeThreadId)
      ) {
        setIsUploadingAttachments(false);
      }
    }
  }

  async function removeStagedAttachment(attachmentId: string) {
    if (!threadId || isBusy) {
      return;
    }
    const ownerThreadId = threadId;
    const generation = selectionGenerationRef.current;
    try {
      await apiClient.discardStagedAttachment(ownerThreadId, attachmentId);
      if (
        selectionGenerationRef.current !== generation ||
        selectedThreadIdRef.current !== ownerThreadId
      ) {
        return;
      }
      setStagedAttachments((current) =>
        current.filter((attachment) => attachment.id !== attachmentId),
      );
      setError(null);
    } catch (removeError) {
      await handleRequestError(removeError, ownerThreadId, generation);
    }
  }

  function dismissAttachmentError(index: number) {
    setAttachmentErrors((current) =>
      current.filter((_uploadError, currentIndex) => currentIndex !== index),
    );
  }

  function handleMessageKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) {
      return;
    }

    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }

  async function resumeActiveInterrupt(
    ownerThreadId: string,
    ownerInterruptId: string,
    payload: ResumeInterruptPayload,
  ) {
    if (
      selectedThreadIdRef.current !== ownerThreadId ||
      state?.thread_id !== ownerThreadId ||
      state.active_interrupt?.id !== ownerInterruptId ||
      isSubmitting ||
      isResuming ||
      state?.run.state === "running"
    ) {
      return;
    }
    const generation = selectionGenerationRef.current;
    const interruptAtSubmission = state.active_interrupt;

    const activeClarification =
      payload.action === "answer" &&
      interruptAtSubmission.type === "agent_clarification"
        ? interruptAtSubmission
        : null;
    const submittedClarification = activeClarification
      ? {
          interrupt_id: activeClarification.id,
          question: diagnosticText(activeClarification.question),
          reason: diagnosticText(activeClarification.reason),
          answer:
            payload.action === "answer"
              ? payload.answer === AGENT_DECIDE_ANSWER
                ? "Let the agent decide."
                : payload.answer.trim()
              : "",
        }
      : null;
    if (submittedClarification?.answer) {
      setSubmittedClarifications((current) => ({
        ...current,
        [submittedClarification.interrupt_id]: submittedClarification,
      }));
    }
    setIsResuming(true);
    try {
      const nextState = await apiClient.resumeInterrupt(
        ownerThreadId,
        ownerInterruptId,
        payload,
      );
      if (applyOwnedThreadState(ownerThreadId, generation, nextState)) {
        setError(null);
        if (loadConversationHistory) {
          void refreshSavedConversations();
        }
      }
    } catch (resumeError) {
      if (
        submittedClarification &&
        selectionGenerationRef.current === generation &&
        selectedThreadIdRef.current === ownerThreadId
      ) {
        setSubmittedClarifications((current) => {
          const { [submittedClarification.interrupt_id]: _removed, ...pending } = current;
          return pending;
        });
      }
      await handleRequestError(resumeError, ownerThreadId, generation);
    } finally {
      if (
        selectionGenerationRef.current === generation &&
        selectedThreadIdRef.current === ownerThreadId
      ) {
        setIsResuming(false);
      }
    }
  }

  async function cancelActiveRun() {
    if (!threadId || state?.run.state !== "running" || isCancelling) {
      return;
    }

    const activeThreadId = threadId;
    const generation = selectionGenerationRef.current;
    pollGenerationRef.current += 1;
    setIsCancelling(true);
    try {
      const nextState = await apiClient.cancelRun(activeThreadId);
      if (applyOwnedThreadState(activeThreadId, generation, nextState)) {
        setError(null);
        if (loadConversationHistory) {
          void refreshSavedConversations();
        }
      }
    } catch (cancelError) {
      await handleRequestError(cancelError, activeThreadId, generation);
    } finally {
      if (
        selectionGenerationRef.current === generation &&
        selectedThreadIdRef.current === activeThreadId
      ) {
        setIsCancelling(false);
      }
    }
  }

  function newConversation() {
    if (isConversationTransitionBusy) {
      return;
    }

    createThreadPromiseRef.current = null;
    selectionGenerationRef.current += 1;
    pollGenerationRef.current += 1;
    selectedThreadIdRef.current = null;
    setThreadId(null);
    setState(null);
    setReplacementModelName("");
    setMessage("");
    setPendingUserMessage(null);
    setError(null);
    setRunFailureMessage(null);
    setStagedAttachments([]);
    setAttachmentErrors([]);
    setIsUploadingAttachments(false);
    setIsCancelling(false);
    setIsLoadingConversation(false);
    setRestoredReviewThreadId(null);
    setSubmittedClarifications({});
    setIsModelLockHintVisible(false);
    if (runtimeOptions) {
      setSelectedRuntimeSettings((current) =>
        normalizeNewConversationRuntimeSettings(current, runtimeOptions),
      );
    }
  }

  const activeInterrupt = state?.active_interrupt;
  const activeClarificationWasSubmitted = Boolean(
    activeInterrupt && submittedClarifications[activeInterrupt.id],
  );
  const pendingDatasetReviewId =
    activeInterrupt?.type === "dataset_review"
      ? activeInterrupt.artifact.id
      : null;
  const isRunInFlight = state?.run.state === "running";
  const hasUnprojectableInterrupt =
    state?.run.error_code === "INTERRUPT_PROJECTION_FAILED";
  const isAwaitingHumanReview =
    Boolean(activeInterrupt) || hasUnprojectableInterrupt;
  const isConversationTransitionBusy =
    isSubmitting ||
    isResuming ||
    isCancelling ||
    isUploadingAttachments ||
    isLoadingConversation;
  const isBusy = isConversationTransitionBusy || isRunInFlight;
  const isComposerDisabled =
    !runtimeOptions || isBusy || isAwaitingHumanReview;
  const requiresModelReplacement = Boolean(state?.model_replacement_required);
  const isSendDisabled =
    isComposerDisabled ||
    (requiresModelReplacement && !replacementModelName) ||
    (!message.trim() && stagedAttachments.length === 0) ||
    attachmentErrors.length > 0;
  const showInitialChatPrompt = !(state?.conversation.length);
  const modelLocked = Boolean(
    state?.runtime_settings_locked && !requiresModelReplacement,
  );
  const modelProviderGroups = useMemo(() => {
    const groups: Array<{
      label: string;
      models: NonNullable<typeof runtimeOptions>["models"];
    }> = [];
    for (const model of runtimeOptions?.models ?? []) {
      const groupLabel = model.provider_label || "Models";
      const group = groups.find((entry) => entry.label === groupLabel);
      if (group) {
        group.models.push(model);
      } else {
        groups.push({ label: groupLabel, models: [model] });
      }
    }
    return groups;
  }, [runtimeOptions]);
  const selectedModel = runtimeOptions?.models.find(
    (model) => model.id === selectedRuntimeSettings.model_name,
  );
  const selectedModelLabel =
    selectedModel?.label ?? selectedRuntimeSettings.model_name;
  const settingsLocked = Boolean(modelLocked || isBusy);
  const workflowStatus = activityStatusText(state);
  const embeddingStartupStatus =
    state?.embedding_startup_status ?? runtimeOptions?.embedding_startup_status;
  const activityTitle = isSubmitting
    ? "Submitting your message"
    : isResuming
      ? "Sending your review decision"
      : isCancelling
        ? "Cancelling current run"
      : isUploadingAttachments
        ? "Uploading your files"
        : isRunInFlight
          ? "Agent is working"
          : "";
  const activityDetail = isRunInFlight
    ? isCancelling
      ? "Restoring the latest durable checkpoint."
      : workflowStatus || "Running the workflow and checking for the next result."
    : isSubmitting
      ? "Creating or updating the thread, then handing your message to the backend."
      : isResuming
        ? "Resuming the workflow from the review panel."
        : isUploadingAttachments
          ? "Staging files for this message."
          : "";
  const chatDisabledReason = hasUnprojectableInterrupt
    ? "This workflow is paused because its pending review could not be displayed."
    : isAwaitingHumanReview
      ? "Complete the review above before sending a new message."
    : isBusy
      ? "Wait for the current action to finish before sending another message."
      : "";
  const conversationMessages = pendingDatasetReviewId
    ? (state?.conversation ?? []).filter(
        (conversationMessage) =>
          !isDatasetReviewCompletionMessage(
            conversationMessage,
            pendingDatasetReviewId,
          ),
      )
    : (state?.conversation ?? []);
  const shouldRenderPendingUser =
    pendingUserMessage &&
    !conversationMessages.some(
      (conversationMessage) =>
        isPendingMessageAcknowledged(
          conversationMessage,
          pendingUserMessage,
        ),
    );
  const visibleConversationMessages = shouldRenderPendingUser
    ? [...conversationMessages, pendingUserMessage]
    : conversationMessages;
  const hasVisibleConversation = visibleConversationMessages.length > 0;
  const latestVisibleUserMessage = [...visibleConversationMessages]
    .reverse()
    .find((conversationMessage) => conversationMessage.role === "user");
  const hasActivityForLatestUser = Boolean(
    latestVisibleUserMessage &&
      activityRunByUserMessageId.has(latestVisibleUserMessage.id),
  );
  const showGenericActivity = Boolean(
    activityTitle && !hasActivityForLatestUser,
  );

  function renderActiveInterrupt(interrupt: ActiveInterrupt) {
    if (!threadId) {
      return null;
    }
    const ownerThreadId = threadId;
    const onResume = (payload: ResumeInterruptPayload) =>
      resumeActiveInterrupt(ownerThreadId, interrupt.id, payload);
    switch (interrupt.type) {
      case "dataset_plan_review":
        return (
          <DbRagReview
            disabled={isBusy}
            interrupt={interrupt}
            onDecision={onResume}
          />
        );
      case "dataset_review":
        return (
          <DbRagDatasetReview
            apiClient={apiClient}
            disabled={isBusy}
            interrupt={interrupt}
            onResume={onResume}
            threadId={threadId}
          />
        );
      case "analysis_result_review":
        return (
          <AnalysisResultReview
            apiClient={apiClient}
            disabled={isBusy}
            interrupt={interrupt}
            onResume={onResume}
            threadId={threadId}
          />
        );
      case "agent_clarification":
        return activeClarificationWasSubmitted ? null : (
          <Clarification
            disabled={isBusy}
            interrupt={interrupt}
            onResume={onResume}
          />
        );
      case "model_output_limit":
        return (
          <ModelOutputLimit
            disabled={isBusy}
            interrupt={interrupt}
            onResume={onResume}
          />
        );
      default:
        return assertNever(interrupt);
    }
  }

  return (
    <AppShell
      headerAction={
        <button
          className="new-conversation-button"
          disabled={isConversationTransitionBusy}
          onClick={newConversation}
          type="button"
        >
          New conversation
        </button>
      }
      sidebar={
        <div className="settings-panel">
          <ConversationHistory
            activeThreadId={threadId}
            actionsDisabled={isBusy}
            items={savedConversations}
            onArchive={archiveConversation}
            onDelete={deleteConversation}
            onOpen={openConversation}
            onRename={renameConversation}
            onRestore={restoreConversation}
          />
        </div>
      }
      conversation={
        <>
          {isLoadingConversation ? (
            <section className="conversation-loading" role="status">
              Loading selected conversation…
            </section>
          ) : null}
          {error ? (
            <div className="error-banner" role="alert">
              {error}
            </div>
          ) : null}
          {runFailureMessage ? (
            <div className="run-failure-card" role="alert">
              {runFailureMessage}
            </div>
          ) : null}
          <EmbeddingFallbackNotice status={embeddingStartupStatus} />
          <section
            className="conversation-panel"
            aria-label="Conversation messages"
          >
            {hasVisibleConversation || showGenericActivity ? (
              <ol className="message-list" aria-label="Conversation messages">
                {visibleConversationMessages.map((conversationMessage) => {
                  const activityRun =
                    conversationMessage.role === "user"
                      ? activityRunByUserMessageId.get(conversationMessage.id)
                      : undefined;
                  return (
                    <Fragment key={conversationMessage.id}>
                      <ConversationMessage
                        fetchAttachmentBlob={fetchAttachmentBlob}
                        getDatasetPreview={(attachmentId, limit) => {
                          if (!threadId) {
                            return Promise.reject(
                              new Error("Thread is unavailable."),
                            );
                          }
                          return apiClient.getDatasetPreview(
                            threadId,
                            attachmentId,
                            limit,
                          );
                        }}
                        getDatasetSchema={(attachmentId) => {
                          if (!threadId) {
                            return Promise.reject(
                              new Error("Thread is unavailable."),
                            );
                          }
                          return apiClient.getDatasetSchema(
                            threadId,
                            attachmentId,
                          );
                        }}
                        getDatasetProvenance={(attachmentId) => {
                          if (!threadId) {
                            return Promise.reject(
                              new Error("Thread is unavailable."),
                            );
                          }
                          return apiClient.getDatasetProvenance(
                            threadId,
                            attachmentId,
                          );
                        }}
                        getAnalysisResult={(attachmentId) => {
                          if (!threadId) {
                            return Promise.reject(
                              new Error("Thread is unavailable."),
                            );
                          }
                          return apiClient.getAnalysisResult(
                            threadId,
                            attachmentId,
                          );
                        }}
                        message={conversationMessage}
                      />
                      {activityRun ? (
                        <AgentActivityTimeline run={activityRun} />
                      ) : null}
                    </Fragment>
                  );
                })}
                {showGenericActivity ? (
                  <ActivityMessage
                    detail={activityDetail}
                    steps={state?.run.steps}
                    title={activityTitle}
                  />
                ) : null}
              </ol>
            ) : null}
          </section>

          {activeInterrupt && restoredReviewThreadId === threadId ? (
            <p className="restored-review-notice">
              This conversation was previously paused and is awaiting your review.
            </p>
          ) : null}
          {activeInterrupt ? renderActiveInterrupt(activeInterrupt) : null}
          {optimisticClarifications.length ? (
            <ClarificationTrace exchanges={optimisticClarifications} />
          ) : null}
        </>
      }
      input={
        <>
          <form className="message-form" onSubmit={submitMessage}>
            {showInitialChatPrompt && !hasVisibleConversation ? (
              <label htmlFor="message-input">
                Ask questions about your data or query from existing database
              </label>
            ) : null}
            {chatDisabledReason ? (
              <p className="message-form-note">{chatDisabledReason}</p>
            ) : null}
            {requiresModelReplacement ? (
              <section className="model-replacement-notice" role="alert">
                <p>
                  This conversation used {state?.model_label ?? state?.model_name},
                  which is not currently available. Choose an available model to continue.
                </p>
                <label>
                  Replacement model
                  <select
                    aria-label="Replacement model"
                    disabled={isBusy || !runtimeOptions}
                    onChange={(event) => {
                      setReplacementModelName(event.target.value);
                      setError(null);
                    }}
                    value={replacementModelName}
                  >
                    <option value="">Choose a model</option>
                    {modelProviderGroups.map((group) => (
                      <optgroup key={group.label} label={group.label}>
                        {group.models.map((model) => (
                          <option key={model.id} value={model.id}>
                            {model.label}
                          </option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                </label>
              </section>
            ) : null}
            <textarea
              aria-label="Ask a question about your dataset!"
              disabled={isComposerDisabled}
              id="message-input"
              onChange={(event) => setMessage(event.target.value)}
              onKeyDown={handleMessageKeyDown}
              placeholder={
                isAwaitingHumanReview
                  ? "Complete the review above before continuing."
                  : hasVisibleConversation
                    ? undefined
                    : "e.g. Create a baseline index-case dataset with age, sex, marital status, smoking, alcohol use, and diabetes medication use. Give me a simple summary table."
              }
              rows={4}
              value={message}
            />
            <AttachmentComposer
              action={
                <>
                  {requiresModelReplacement ? null : modelLocked ? (
                    <div className="composer-locked-model">
                      <button
                        aria-controls="model-lock-hint"
                        aria-describedby={
                          isModelLockHintVisible ? "model-lock-hint" : undefined
                        }
                        aria-label={`Model locked: ${selectedModelLabel}`}
                        className="composer-locked-model-button"
                        onBlur={() => setIsModelLockHintVisible(false)}
                        onFocus={() => setIsModelLockHintVisible(true)}
                        onMouseEnter={() => setIsModelLockHintVisible(true)}
                        onMouseLeave={() => setIsModelLockHintVisible(false)}
                        type="button"
                      >
                        <span aria-hidden="true">🔒</span>
                        {selectedModelLabel}
                      </button>
                      {isModelLockHintVisible ? (
                        <p
                          className="composer-model-lock-popover"
                          id="model-lock-hint"
                          role="tooltip"
                        >
                          Model locked for this conversation. Start a new conversation to choose a different model.
                        </p>
                      ) : null}
                    </div>
                  ) : (
                    <select
                      aria-label="Model"
                      className="composer-model-picker"
                      disabled={settingsLocked || !runtimeOptions}
                      onChange={(event) =>
                        setSelectedRuntimeSettings((current) => ({
                          ...current,
                          model_name: event.target.value,
                        }))
                      }
                      value={selectedRuntimeSettings.model_name}
                    >
                      {modelProviderGroups.map((group) => (
                        <optgroup key={group.label} label={group.label}>
                          {group.models.map((model) => (
                            <option key={model.id} value={model.id}>
                              {model.label}
                            </option>
                          ))}
                        </optgroup>
                      ))}
                    </select>
                  )}
                  {isRunInFlight || isCancelling ? (
                    <button
                      aria-label={isCancelling ? "Cancelling run" : "Cancel run"}
                      className="cancel-run-button"
                      disabled={isCancelling}
                      onClick={() => void cancelActiveRun()}
                      type="button"
                    >
                      {isCancelling ? "Cancelling…" : "Cancel"}
                    </button>
                  ) : (
                    <button disabled={isSendDisabled} type="submit">Send</button>
                  )}
                </>
              }
              disabled={isComposerDisabled}
              errors={attachmentErrors}
              isUploading={isUploadingAttachments}
              onDismissError={dismissAttachmentError}
              onFilesSelected={selectAttachments}
              onRemove={removeStagedAttachment}
              staged={stagedAttachments}
            />
          </form>
        </>
      }
    />
  );
}

export function AppForTesting({
  fetchImpl,
  apiBase = DEFAULT_API_BASE,
  loadConversationHistory = true,
}: TestProps) {
  const apiClient = useMemo(
    () => createApiClient({ apiBase, fetchImpl }),
    [apiBase, fetchImpl],
  );
  return (
    <App
      apiClient={apiClient}
      loadConversationHistory={loadConversationHistory}
    />
  );
}
