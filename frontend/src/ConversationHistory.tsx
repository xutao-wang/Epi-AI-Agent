import type { ConversationSummary } from "./types";
import { useState } from "react";

export default function ConversationHistory({
  items = [],
  activeThreadId,
  actionsDisabled = false,
  onOpen,
  onRename,
  onArchive,
  onRestore,
  onDelete,
}: {
  items: ConversationSummary[];
  activeThreadId: string | null;
  actionsDisabled?: boolean;
  onOpen: (threadId: string) => void;
  onRename: (threadId: string, title: string) => Promise<void>;
  onArchive: (threadId: string) => Promise<void>;
  onRestore: (threadId: string) => Promise<void>;
  onDelete: (threadId: string) => Promise<void>;
}) {
  const [editingThreadId, setEditingThreadId] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState("");
  const [showArchived, setShowArchived] = useState(false);
  const [visitedTooltipThreadId, setVisitedTooltipThreadId] = useState<string | null>(null);
  const savedItems = items.filter((item) => !item.archived_at);
  const archivedItems = items.filter((item) => item.archived_at);

  function beginRename(item: ConversationSummary) {
    setEditingThreadId(item.thread_id);
    setTitleDraft(item.title);
  }

  async function saveRename(threadId: string) {
    const title = titleDraft.trim();
    if (!title) {
      return;
    }
    await onRename(threadId, title);
    setEditingThreadId(null);
  }

  function confirmDelete(item: ConversationSummary) {
    if (window.confirm("Permanently delete this conversation and its stored data?")) {
      void onDelete(item.thread_id);
    }
  }

  function lastOpenedLabel(item: ConversationSummary): string {
    if (!item.last_opened_at) {
      return "Last opened: not yet opened";
    }
    return `Last opened ${new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(item.last_opened_at))}`;
  }

  return (
    <section className="conversation-history" aria-label="Saved conversations">
      <div className="conversation-history-header">
        <h2>Saved conversations</h2>
      </div>
      <ol>
        {savedItems.map((item) => (
          <li key={item.thread_id}>
            {editingThreadId === item.thread_id ? (
              <form
                className="conversation-history-rename"
                onSubmit={(event) => {
                  event.preventDefault();
                  void saveRename(item.thread_id);
                }}
              >
                <input
                  aria-label="Conversation title"
                  maxLength={120}
                  onChange={(event) => setTitleDraft(event.target.value)}
                  value={titleDraft}
                />
                <button className="conversation-history-save-button" type="submit">
                  Save
                </button>
                <button
                  className="conversation-history-cancel-button"
                  onClick={() => setEditingThreadId(null)}
                  type="button"
                >
                  Cancel
                </button>
              </form>
            ) : (
              <div className="conversation-history-item">
                <div className="conversation-history-title">
                  <button
                    aria-current={item.thread_id === activeThreadId ? "page" : undefined}
                    aria-describedby={
                      visitedTooltipThreadId === item.thread_id
                        ? `last-opened-${item.thread_id}`
                        : undefined
                    }
                    className="conversation-history-open-button"
                    onBlur={() => setVisitedTooltipThreadId(null)}
                    onClick={() => onOpen(item.thread_id)}
                    onFocus={() => setVisitedTooltipThreadId(item.thread_id)}
                    onMouseEnter={() => setVisitedTooltipThreadId(item.thread_id)}
                    onMouseLeave={() => setVisitedTooltipThreadId(null)}
                    type="button"
                  >
                    {item.title}
                  </button>
                  {item.awaiting_review ? (
                    <span
                      aria-label={`${item.title} is awaiting review`}
                      className="conversation-history-review-status"
                    >
                      Awaiting review
                    </span>
                  ) : null}
                  {visitedTooltipThreadId === item.thread_id ? (
                    <span
                      className="conversation-history-tooltip"
                      id={`last-opened-${item.thread_id}`}
                      role="tooltip"
                    >
                      {lastOpenedLabel(item)}
                    </span>
                  ) : null}
                </div>
                <div className="conversation-history-actions">
                  <button
                    aria-label={`Rename ${item.title}`}
                    className="conversation-history-rename-button"
                    onClick={() => beginRename(item)}
                    title="Rename conversation"
                    type="button"
                  >
                    Rename
                  </button>
                  <button
                    aria-label={`Archive ${item.title}`}
                    className="conversation-history-action-button"
                    disabled={actionsDisabled && item.thread_id === activeThreadId}
                    onClick={() => void onArchive(item.thread_id)}
                    type="button"
                  >
                    Archive
                  </button>
                  <button
                    aria-label={`Delete ${item.title}`}
                    className="conversation-history-action-button conversation-history-delete-button"
                    disabled={actionsDisabled && item.thread_id === activeThreadId}
                    onClick={() => confirmDelete(item)}
                    type="button"
                  >
                    Delete
                  </button>
                </div>
              </div>
            )}
          </li>
        ))}
      </ol>
      {archivedItems.length ? (
        <div className="conversation-history-archived">
          <button
            aria-expanded={showArchived}
            className="conversation-history-archived-toggle"
            onClick={() => setShowArchived((current) => !current)}
            type="button"
          >
            Archived conversations
          </button>
          {showArchived ? (
            <ol aria-label="Archived conversations">
              {archivedItems.map((item) => (
                <li className="conversation-history-item" key={item.thread_id}>
                  <span className="conversation-history-archived-title">{item.title}</span>
                  <button
                    aria-label={`Restore ${item.title}`}
                    className="conversation-history-action-button"
                    onClick={() => void onRestore(item.thread_id)}
                    type="button"
                  >
                    Restore
                  </button>
                  <button
                    aria-label={`Delete ${item.title}`}
                    className="conversation-history-action-button conversation-history-delete-button"
                    onClick={() => confirmDelete(item)}
                    type="button"
                  >
                    Delete
                  </button>
                </li>
              ))}
            </ol>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
