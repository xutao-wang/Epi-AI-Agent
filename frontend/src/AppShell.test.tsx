import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import AppShell from "./AppShell";

describe("AppShell", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("renders Streamlit-like app landmarks", () => {
    render(
      <AppShell
        conversation={<p>Conversation content</p>}
        headerAction={<button type="button">New conversation</button>}
        input={<p>Input form</p>}
        sidebar={<p>Model Settings</p>}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "Epidemiology Research Agent" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("banner")).toContainElement(
      screen.getByRole("button", { name: "New conversation" }),
    );
    expect(screen.getByText("Model Settings")).toBeInTheDocument();
    expect(
      screen.queryByText("Upload your dataset CSV and schema JSON"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Conversation" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "Conversation" }),
    ).toBeInTheDocument();
    expect(
      screen
        .getByText("Conversation content")
        .compareDocumentPosition(screen.getByText("Input form")) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("collapses the sidebar so the main conversation can expand", () => {
    render(
      <AppShell
        conversation={<p>Conversation content</p>}
        input={<p>Input form</p>}
        sidebar={<p>Model Settings</p>}
      />,
    );

    const shell = screen.getByRole("main");
    expect(shell).not.toHaveClass("sidebar-collapsed");

    fireEvent.click(
      screen.getByRole("button", { name: "Hide sidebar" }),
    );

    expect(shell).toHaveClass("sidebar-collapsed");
    expect(
      screen.getByRole("button", { name: "Show sidebar" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Conversation content")).toBeInTheDocument();
  });

  it("resizes the sidebar with its drag handle", () => {
    render(
      <AppShell
        conversation={<p>Conversation content</p>}
        input={<p>Input form</p>}
        sidebar={<p>Model Settings</p>}
      />,
    );

    fireEvent.mouseDown(screen.getByRole("separator", { name: "Resize sidebar" }), {
      clientX: 300,
    });
    fireEvent.mouseMove(window, { clientX: 420 });
    fireEvent.mouseUp(window);

    expect(screen.getByRole("main")).toHaveStyle("--sidebar-width: 420px");
  });
});
