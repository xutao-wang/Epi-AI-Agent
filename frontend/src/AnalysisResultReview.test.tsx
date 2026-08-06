import "@testing-library/jest-dom/vitest";
// @ts-expect-error Vitest resolves Node built-ins; the browser build excludes tests.
import { readFileSync } from "node:fs";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AnalysisResultReview from "./AnalysisResultReview";
import type { ActiveInterrupt } from "./types";

type AnalysisReviewInterrupt = Extract<
  ActiveInterrupt,
  { type: "analysis_result_review" }
>;

function interrupt(): AnalysisReviewInterrupt {
  return {
    id: "interrupt-analysis-1",
    type: "analysis_result_review",
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
        code_summary: "Fit Kaplan-Meier and Cox models.",
      },
      output_text:
        "Kaplan-Meier estimates\nLog-rank p-value=0.031\nCox HR=1.41",
      warnings: ["Sparse cells in the low-adherence group."],
      warnings_truncated: false,
      runtime: {
        language: "Python",
        version: "3.12.10",
        packages: [{ name: "pandas", version: "2.2.3" }],
      },
      tables: [{ id: "table-1", kind: "table", version: 1 }],
      figures: [{ id: "figure-1", kind: "figure", version: 1 }],
      feedback_history: [
        { action: "revise", feedback: "Use robust standard errors." },
      ],
    },
  };
}

const originalClipboard = navigator.clipboard;

afterEach(() => {
  Object.defineProperty(navigator, "clipboard", {
    configurable: true,
    value: originalClipboard,
  });
  vi.useRealTimers();
});

beforeEach(() => {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:analysis-artifact"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
});

function renderReview(value = interrupt()) {
  const onResume = vi.fn();
  const apiClient = {
    fetchArtifactBlob: vi.fn().mockImplementation(
      () => new Promise<Blob>(() => undefined),
    ),
    getTablePreview: vi.fn().mockImplementation(
      () => new Promise<never>(() => undefined),
    ),
  };
  render(
    <AnalysisResultReview
      apiClient={apiClient}
      interrupt={value}
      onResume={onResume}
      threadId="thread-1"
    />,
  );
  return { apiClient, onResume };
}

describe("AnalysisResultReview", () => {
  it("keeps the review panel within the assistant chat width", () => {
    const styles = readFileSync("src/styles.css", "utf8");

    expect(styles).toMatch(
      /\.analysis-result-review-panel\s*\{[^}]*width:\s*min\(100%,\s*920px\);/s,
    );
    expect(styles).toMatch(
      /\.analysis-result-review-panel\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\);/s,
    );
  });

  it("shows generated code once before output and keeps both sections collapsible", () => {
    renderReview();

    const codeSummary = screen.getByText("Code");
    const outputSummary = screen.getByText("Output");
    const codeDetails = codeSummary.closest("details");
    const outputDetails = outputSummary.closest("details");

    expect(codeDetails).toHaveAttribute("open");
    expect(outputDetails).toHaveAttribute("open");
    expect(Boolean(
      (codeDetails?.compareDocumentPosition(outputDetails as Node) ?? 0)
        & Node.DOCUMENT_POSITION_FOLLOWING,
    )).toBe(true);
    expect(screen.getAllByText("print('Kaplan-Meier estimates')")).toHaveLength(1);
    expect(screen.queryByText("Generated code")).not.toBeInTheDocument();

    const copyCode = screen.getByRole("button", { name: "Copy code" });
    const copyOutput = screen.getByRole("button", { name: "Copy output" });
    fireEvent.click(codeSummary);
    expect(codeDetails).not.toHaveAttribute("open");
    expect(copyCode).toBeVisible();
    fireEvent.click(outputSummary);
    expect(outputDetails).not.toHaveAttribute("open");
    expect(copyOutput).toBeVisible();
  });

  it("renders review Python and output in bounded code blocks", () => {
    renderReview();

    expect(screen.getByText("print('Kaplan-Meier estimates')").closest(".code-block"))
      .toHaveClass("code-block-python");
    expect(screen.getByText("Python output").closest(".code-block"))
      .toHaveClass("code-block-text");
  });

  it("copies exact code and output without collapsing either section", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderReview();

    const codeDetails = screen.getByText("Code").closest("details");
    const outputDetails = screen.getByText("Output").closest("details");
    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(
      "print('Kaplan-Meier estimates')",
    ));
    expect(codeDetails).toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "Code copied" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy output" }));
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith(
      "Kaplan-Meier estimates\nLog-rank p-value=0.031\nCox HR=1.41",
    ));
    expect(outputDetails).toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "Code copied" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Output copied" })).toBeInTheDocument();
  });

  it("returns copied feedback to the copy state after two seconds", async () => {
    vi.useFakeTimers();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    renderReview();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    });
    expect(screen.getByRole("button", { name: "Code copied" })).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(2_000));
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
  });

  it("keeps copied feedback timers independent", async () => {
    vi.useFakeTimers();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockResolvedValue(undefined) },
    });
    renderReview();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    });
    act(() => vi.advanceTimersByTime(1_000));
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Copy output" }));
    });
    act(() => vi.advanceTimersByTime(1_000));
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Output copied" })).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(1_000));
    expect(screen.getByRole("button", { name: "Copy output" })).toBeInTheDocument();
  });

  it("leaves copy feedback unchanged when clipboard writing fails", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("clipboard denied"));
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    renderReview();

    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: "Copy code" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Code copied" })).not.toBeInTheDocument();
  });

  it("omits the Code section when generated code is empty", () => {
    const value = interrupt();
    value.view.specification.code = "";
    renderReview(value);

    expect(screen.queryByText("Code")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy code" })).not.toBeInTheDocument();
    expect(screen.getByText("Output")).toBeInTheDocument();
  });

  it("shows result evidence and keeps technical details collapsed but available", async () => {
    const onResume = vi.fn();
    const apiClient = {
      fetchArtifactBlob: vi.fn().mockResolvedValue(new Blob(["artifact"])),
      getTablePreview: vi.fn().mockResolvedValue({
        columns: ["group", "n"],
        rows: [{ group: "Good", n: "10" }],
        row_count: 1,
      }),
    };
    render(
      <AnalysisResultReview
        apiClient={apiClient}
        interrupt={interrupt()}
        onResume={onResume}
        threadId="thread-1"
      />,
    );

    expect(screen.getByText("Output").closest("details")).toHaveAttribute("open");
    expect(screen.getByText("Warnings").closest("details")).toHaveAttribute("open");
    expect(screen.getByText("Tables").closest("details")).toHaveAttribute("open");
    expect(screen.getByText("Figures").closest("details")).toHaveAttribute("open");
    const technicalDetails = screen.getByText("Show technical details").closest("details");
    expect(technicalDetails).not.toHaveAttribute("open");
    expect(screen.getByText("Python output").closest(".code-block"))
      .toHaveClass("code-block-text");
    expect(screen.getByText("1.41")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText("Good")).toBeInTheDocument());
    expect(screen.getByText("10")).toBeInTheDocument();
    expect(screen.getByText("Sparse cells in the low-adherence group.")).toBeInTheDocument();
    expect(screen.getByText("Code")).toBeInTheDocument();
    expect(screen.queryByText("Generated code")).not.toBeInTheDocument();
    expect(screen.queryByText("Analysis identity")).not.toBeInTheDocument();
    expect(screen.queryByText("Analysis artifact")).not.toBeInTheDocument();
    expect(screen.getByText("Model specification")).toBeInTheDocument();
    expect(screen.getByText("Python runtime")).toBeInTheDocument();
    expect(screen.getByText("print('Kaplan-Meier estimates')")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Show technical details"));
    expect(technicalDetails).toHaveAttribute("open");
    expect(
      await screen.findByRole("img", { name: "Pending Python analysis figure" }),
    ).toHaveAttribute("src", "blob:analysis-artifact");
    expect(screen.getByText(/permits publication/i)).toHaveTextContent(
      /does not automatically interpret/i,
    );
    fireEvent.click(screen.getByText("Output"));
    expect(screen.getByText("Output").closest("details")).not.toHaveAttribute("open");
    fireEvent.click(screen.getByRole("button", { name: "Approve Results" }));
    expect(onResume).toHaveBeenLastCalledWith({ action: "approve" });

    fireEvent.change(screen.getByLabelText("Revision feedback"), {
      target: { value: "  Use robust standard errors.  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Revise Analysis" }));
    expect(onResume).toHaveBeenLastCalledWith({
      action: "revise",
      feedback: "Use robust standard errors.",
    });

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onResume).toHaveBeenLastCalledWith({ action: "cancel" });
  });
});
