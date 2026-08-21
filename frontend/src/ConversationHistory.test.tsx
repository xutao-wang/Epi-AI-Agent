import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ConversationHistory from "./ConversationHistory";


const item = {
  thread_id: "thread-1",
  title: "TB cohort survival",
  title_source: "automatic" as const,
  model_name: "gpt-5.6-terra",
  created_at: "2026-07-30T00:00:00+00:00",
  updated_at: "2026-07-30T00:00:00+00:00",
  last_opened_at: "2026-07-30T18:24:00+00:00",
  archived_at: null,
  awaiting_review: false,
};


describe("ConversationHistory", () => {
  it("opens a saved conversation and permits a manual rename", async () => {
    const onOpen = vi.fn();
    const onRename = vi.fn().mockResolvedValue(undefined);

    render(
      <ConversationHistory
        activeThreadId={null}
        items={[item]}
        onOpen={onOpen}
        onRename={onRename}
        onArchive={vi.fn()}
        onRestore={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: item.title }));
    expect(onOpen).toHaveBeenCalledWith(item.thread_id);

    expect(
      screen.getByRole("button", { name: "Rename " + item.title }),
    ).toHaveTextContent("Rename");
    expect(
      screen.getByRole("button", { name: "Rename " + item.title }).parentElement,
    ).toHaveClass("conversation-history-actions");
    fireEvent.mouseEnter(screen.getByRole("button", { name: item.title }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Last opened");

    fireEvent.click(
      screen.getByRole("button", { name: "Rename " + item.title }),
    );
    expect(screen.getByRole("button", { name: "Save" })).toHaveClass(
      "conversation-history-save-button",
    );
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveClass(
      "conversation-history-cancel-button",
    );
    fireEvent.change(screen.getByLabelText("Conversation title"), {
      target: { value: "TB survival by regimen" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(onRename).toHaveBeenCalledWith(item.thread_id, "TB survival by regimen");
    expect(
      await screen.findByRole("button", { name: item.title }),
    ).toBeInTheDocument();
  });

  it("shows archived conversations separately and restores one", () => {
    const onRestore = vi.fn().mockResolvedValue(undefined);
    const archived = {
      ...item,
      thread_id: "thread-2",
      title: "Archived TB cohort survival",
      archived_at: "2026-07-30T01:00:00+00:00",
    };

    render(
      <ConversationHistory
        activeThreadId={null}
        items={[item, archived]}
        onOpen={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onRestore={onRestore}
        onDelete={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: archived.title })).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("Archived conversations"));
    fireEvent.click(screen.getByRole("button", { name: `Restore ${archived.title}` }));

    expect(onRestore).toHaveBeenCalledWith(archived.thread_id);
  });

  it("requires confirmation before deleting a conversation", () => {
    const onDelete = vi.fn();
    vi.spyOn(window, "confirm").mockReturnValue(false);

    render(
      <ConversationHistory
        activeThreadId={null}
        items={[item]}
        onOpen={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onRestore={vi.fn()}
        onDelete={onDelete}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: `Delete ${item.title}` }));

    expect(onDelete).not.toHaveBeenCalled();
  });

  it("disables archive and delete for the active busy conversation", () => {
    render(
      <ConversationHistory
        actionsDisabled
        activeThreadId={item.thread_id}
        items={[item]}
        onOpen={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onRestore={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    expect(screen.getByRole("button", { name: `Archive ${item.title}` })).toBeDisabled();
    expect(screen.getByRole("button", { name: `Delete ${item.title}` })).toBeDisabled();
  });

  it("labels only the conversation that is awaiting review", () => {
    render(
      <ConversationHistory
        activeThreadId={null}
        items={[
          {
            ...item,
            thread_id: "thread-a",
            title: "Thread A",
            awaiting_review: true,
          },
          {
            ...item,
            thread_id: "thread-b",
            title: "Thread B",
          },
        ]}
        onOpen={vi.fn()}
        onRename={vi.fn()}
        onArchive={vi.fn()}
        onRestore={vi.fn()}
        onDelete={vi.fn()}
      />,
    );

    expect(screen.getByText("Awaiting review")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Thread A" }).parentElement,
    ).toHaveTextContent("Awaiting review");
    expect(
      screen.getByRole("button", { name: "Thread B" }).parentElement,
    ).not.toHaveTextContent("Awaiting review");
  });
});
