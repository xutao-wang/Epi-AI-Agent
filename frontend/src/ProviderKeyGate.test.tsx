import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ProviderKeyGate from "./ProviderKeyGate";
import type { ApiClient } from "./apiClient";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((promiseResolve) => {
    resolve = promiseResolve;
  });
  return { promise, resolve };
}

function apiClient(overrides: Partial<ApiClient> = {}): ApiClient {
  return {
    requiresProviderKey: true,
    getProviderKeyStatus: vi.fn().mockResolvedValue({ configured: false }),
    setProviderKey: vi.fn().mockResolvedValue({ configured: true }),
    clearProviderKey: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  } as ApiClient;
}

describe("ProviderKeyGate", () => {
  beforeEach(() => {
    window.localStorage.clear();
    window.sessionStorage.clear();
  });

  it("renders checking before showing the key form", async () => {
    const status = deferred<{ configured: boolean }>();
    render(
      <ProviderKeyGate
        apiClient={apiClient({ getProviderKeyStatus: vi.fn(() => status.promise) })}
        onSignOut={vi.fn()}
      >
        <p>Ready app</p>
      </ProviderKeyGate>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Checking provider key");
    status.resolve({ configured: false });
    expect(await screen.findByLabelText("OpenAI API key")).toBeInTheDocument();
  });

  it("uses a component-local password input with browser completion off", async () => {
    render(
      <ProviderKeyGate apiClient={apiClient()} onSignOut={vi.fn()}>
        <p>Ready app</p>
      </ProviderKeyGate>,
    );

    const input = await screen.findByLabelText("OpenAI API key");
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");
  });

  it("clears the key before mounting the ready app and never persists or renders it", async () => {
    const key = "sk-browser-secret-value";
    const client = apiClient();
    render(
      <ProviderKeyGate apiClient={client} onSignOut={vi.fn()}>
        <p>Ready app</p>
      </ProviderKeyGate>,
    );

    const input = await screen.findByLabelText("OpenAI API key");
    fireEvent.change(input, { target: { value: key } });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));

    await waitFor(() => expect(client.setProviderKey).toHaveBeenCalledWith(key));
    expect(await screen.findByText("Ready app")).toBeInTheDocument();
    expect(screen.queryByDisplayValue(key)).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(key);
    expect(window.location.href).not.toContain(key);
    expect(JSON.stringify(window.localStorage)).not.toContain(key);
    expect(JSON.stringify(window.sessionStorage)).not.toContain(key);
  });

  it("shows a redacted validation error and keeps the key out of rendered state", async () => {
    const key = "sk-invalid-secret-value";
    const client = apiClient({
      setProviderKey: vi.fn().mockRejectedValue(new Error(`Rejected ${key}`)),
    });
    render(
      <ProviderKeyGate apiClient={client} onSignOut={vi.fn()}>
        <p>Ready app</p>
      </ProviderKeyGate>,
    );

    fireEvent.change(await screen.findByLabelText("OpenAI API key"), {
      target: { value: key },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save key" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The provider key could not be validated",
    );
    expect(document.body.textContent).not.toContain(key);
    expect(screen.getByLabelText("OpenAI API key")).toHaveValue("");
  });

  it("bypasses key status checks in local native mode", async () => {
    const client = apiClient({ requiresProviderKey: false });
    render(
      <ProviderKeyGate apiClient={client} onSignOut={vi.fn()}>
        <p>Native ready app</p>
      </ProviderKeyGate>,
    );

    expect(screen.getByText("Native ready app")).toBeInTheDocument();
    expect(client.getProviderKeyStatus).not.toHaveBeenCalled();
  });
});
