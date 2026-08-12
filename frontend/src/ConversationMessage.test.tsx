import "@testing-library/jest-dom/vitest";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import ConversationMessage from "./ConversationMessage";

describe("ConversationMessage", () => {
  const fetchAttachmentBlob = () => Promise.resolve(new Blob(["attachment"]));

  it("marks user and assistant messages with distinct role classes", () => {
    const { rerender } = render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "user-1",
          role: "user",
          text: "who are you",
        }}
      />,
    );

    expect(screen.getByText("who are you").closest("li")).toHaveClass(
      "message-user",
    );

    rerender(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "assistant-1",
          role: "assistant",
          text: "I can help with RePORT datasets.",
        }}
      />,
    );

    expect(screen.getByText("I can help with RePORT datasets.").closest("li"))
      .toHaveClass("message-assistant");
  });

  it("marks a retained cancelled user message without hiding its content", () => {
    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "user-cancelled",
          role: "user",
          text: "Analyze the attached cohort",
          status: "cancelled",
        }}
      />,
    );

    expect(screen.getByText("Analyze the attached cohort")).toBeInTheDocument();
    expect(screen.getByText("Cancelled")).toHaveAttribute(
      "aria-label",
      "Message status: Cancelled",
    );
    expect(screen.getByText("Cancelled")).toHaveClass("message-status-cancelled");
    expect(screen.getByText("Analyze the attached cohort").closest("li"))
      .toHaveClass("message-cancelled");
  });

  it.each(["user", "assistant"] as const)(
    "uses the bounded layout contract for %s messages",
    (role) => {
      render(
        <ConversationMessage
          fetchAttachmentBlob={fetchAttachmentBlob}
          message={{ id: `${role}-bounded`, role, text: "Long output" }}
        />,
      );

      expect(screen.getByText("Long output").closest("li")).toHaveClass(
        "message-bounded",
      );
      expect(screen.getByText("Long output").closest(".message-bubble"))
        .toHaveClass("message-bubble-bounded");
    },
  );

  it("renders markdown bullets and inline code in assistant text", () => {
    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "assistant-1",
          role: "assistant",
          text: "Included columns:\n- Dataset ID: `subset-1`\n- Row count: 362",
        }}
      />,
    );

    expect(screen.getByText("Included columns:")).toBeInTheDocument();
    expect(screen.getByRole("list")).toBeInTheDocument();
    expect(screen.getByText("subset-1")).toHaveClass("inline-code");
    expect(screen.getByText(/Row count:/)).toBeInTheDocument();
  });

  it("renders numbered population clarification choices on separate lines", () => {
    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "assistant-1",
          role: "assistant",
          text: [
            "This database contains both index cases and household contacts. Which population should this extraction use?",
            "1. index cases (Cohort A / active pulmonary TB)",
            "2. household contacts (Cohort B / contacts, including progressors)",
          ].join("\n"),
        }}
      />,
    );

    expect(
      screen.getByText(
        "This database contains both index cases and household contacts. Which population should this extraction use?",
      ),
    ).toBeInTheDocument();
    const choices = screen.getByRole("list");
    expect(choices).toHaveTextContent(
      "index cases (Cohort A / active pulmonary TB)",
    );
    expect(within(choices).getAllByRole("listitem")).toHaveLength(2);
    expect(
      screen.getByText(
        "household contacts (Cohort B / contacts, including progressors)",
      ),
    ).toBeInTheDocument();
  });

  it("renders shared display-history markdown blocks", () => {
    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "assistant-1",
          role: "assistant",
          text: [
            "### Dataset summary",
            "The **population cohort** was applied.",
            "",
            "| Column | Purpose |",
            "| --- | --- |",
            "| FOA_CHAOUT | Outcome status |",
          ].join("\n"),
        }}
      />,
    );

    expect(
      screen.getByRole("heading", { level: 3, name: "Dataset summary" }),
    ).toBeInTheDocument();
    expect(screen.getByText("population cohort").tagName).toBe("STRONG");
    const table = screen.getByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Column" }))
      .toBeInTheDocument();
    expect(within(table).getByRole("cell", { name: "Outcome status" }))
      .toBeInTheDocument();
  });

  it("renders escaped inline math-like text as readable prose", () => {
    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "assistant-1",
          role: "assistant",
          text: [
            "Results:",
            "- Never married: OR $\\approx 1.08 \\times 10^{-21}$, 95% CI $[0, \\infty]$, $p=1.000$",
            "- Cross-tab confirmed that only raw category mapped to $1$.",
          ].join("\n"),
        }}
      />,
    );

    expect(screen.getByText(/OR ≈ 1.08 × 10\^{-21}/)).toBeInTheDocument();
    expect(screen.getByText(/95% CI \[0, ∞\]/)).toBeInTheDocument();
    expect(screen.getByText(/p=1.000/)).toBeInTheDocument();
    expect(screen.getByText(/mapped to 1\./)).toBeInTheDocument();
    expect(screen.queryByText(/\\approx/)).not.toBeInTheDocument();
    expect(screen.queryByText(/\$p=1\.000\$/)).not.toBeInTheDocument();
  });

  it("renders hover message actions with a timestamp and copy control but no edit action", async () => {
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });

    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "user-1",
          role: "user",
          text: "lets proceed",
          created_at: "2026-07-02T20:54:00Z",
        }}
      />,
    );

    const timestamp = screen.getByLabelText("Message timestamp");
    expect(timestamp).toHaveAttribute("datetime", "2026-07-02T20:54:00Z");
    expect(timestamp.textContent).toMatch(/\d/);
    expect(screen.queryByRole("button", { name: /edit/i })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy message" }));
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("lets proceed");
    });
    expect(screen.getByRole("button", { name: "Message copied" }))
      .toBeInTheDocument();

    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: originalClipboard,
    });
  });

  it.each(["user", "assistant"] as const)(
    "shows a check mark after copying a %s message",
    async (role) => {
      const originalClipboard = navigator.clipboard;
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: vi.fn().mockResolvedValue(undefined) },
      });

      render(
        <ConversationMessage
          fetchAttachmentBlob={fetchAttachmentBlob}
          message={{
            id: `${role}-1`,
            role,
            text: `${role} message`,
          }}
        />,
      );

      fireEvent.click(screen.getByRole("button", { name: "Copy message" }));

      await waitFor(() => {
        expect(screen.getByRole("button", { name: "Message copied" }))
          .toHaveClass("message-copy-button-copied");
      });
      expect(screen.getByTestId("message-copy-checkmark")).toBeInTheDocument();

      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: originalClipboard,
      });
    },
  );

  it("renders SQL in a code block with a copy button", async () => {
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });

    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "assistant-1",
          role: "assistant",
          text: "SQL used:\n```sql\nSELECT 1;\n```",
        }}
      />,
    );

    expect(screen.getByText("SELECT 1;")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy SQL" }));
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith("SELECT 1;");
    });
    expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();

    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: originalClipboard,
    });
  });

  it("renders Python in a code block with a language-specific copy button", async () => {
    const originalClipboard = navigator.clipboard;
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });

    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "assistant-1",
          role: "assistant",
          text: "Python code:\n```python\nprint('subset ready')\n```",
        }}
      />,
    );

    expect(screen.getByText("Python code:")).toBeInTheDocument();
    expect(screen.getByText("print('subset ready')")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Copy Python" }));
    await waitFor(() => {
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        "print('subset ready')",
      );
    });

    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: originalClipboard,
    });
  });

  it("renders DB-RAG dataset completion concisely with collapsed SQL details", () => {
    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "assistant-1",
          role: "assistant",
          text: [
            "Read-only SQL execution completed with 2 result row(s). Dataset `dataset-art-1` was created with 2 rows.",
            "",
            "Included columns:",
            "- Analysis columns: `Form 2A.IC_AGE`",
            "",
            "SQL used:",
            "```sql",
            "select IC_AGE from \"Form 2A\"",
            "```",
          ].join("\n"),
        }}
      />,
    );

    expect(screen.getByText("dataset-art-1")).toHaveClass("inline-code");
    expect(screen.getByText(/Preview or download/)).toBeInTheDocument();
    const details = screen
      .getByText("SQL and selection details")
      .closest("details");
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText("SQL used")).toBeInTheDocument();
    expect(screen.getByText('select IC_AGE from "Form 2A"')).toBeInTheDocument();
  });

  it("renders a collapsed clarification trace below a final assistant response", () => {
    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "assistant-final",
          role: "assistant",
          text: "The dataset is ready.",
          clarifications: [
            {
              interrupt_id: "clarification-1",
              question: "Which visit should be used?",
              reason: "The request did not specify a time point.",
              answer: "Use the 12-month visit.",
            },
          ],
        }}
      />,
    );

    const trace = screen.getByText("Clarification trace").closest("details");
    expect(trace).not.toHaveAttribute("open");

    fireEvent.click(screen.getByText("Clarification trace"));

    expect(trace).toHaveAttribute("open");
    expect(screen.getByText("Use the 12-month visit.")).toBeInTheDocument();
  });

  it("renders an approved figure attachment with an authenticated download", async () => {
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      value: vi.fn(() => "blob:conversation-figure"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      value: vi.fn(),
    });
    const fetchAttachmentBlob = vi.fn().mockResolvedValue(new Blob(["figure"]));
    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
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
        }}
      />,
    );

    const figure = await screen.findByRole("img", {
      name: "Figure generated by approved final output.",
    });
    expect(figure).toHaveAttribute("src", "blob:conversation-figure");
    expect(figure.closest(".message-bubble")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download figure" })).toBeInTheDocument();
  });

  it("groups reused files and gives the originating message a stable target", () => {
    render(
      <ConversationMessage
        fetchAttachmentBlob={fetchAttachmentBlob}
        message={{
          id: "assistant-2",
          role: "assistant",
          text: "I used the uploaded protocol.",
          attachments: [
            {
              id: "attachment-pdf",
              kind: "document",
              label: "protocol.pdf",
              filename: "protocol.pdf",
              mime: "application/pdf",
              byte_size: 2048,
              relationship: "used",
              origin_message_id: "user-1",
            },
          ],
        }}
      />,
    );

    expect(screen.getByRole("listitem")).toHaveAttribute(
      "id",
      "message-assistant-2",
    );
    expect(screen.getByRole("heading", { name: "Used files" }))
      .toBeInTheDocument();
    expect(screen.getByRole("link", { name: "protocol.pdf" })).toHaveAttribute(
      "href",
      "#message-user-1",
    );
  });
});
