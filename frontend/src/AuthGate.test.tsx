import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { User } from "oidc-client-ts";
import AuthGate from "./AuthGate";
import type { BrowserAuthClient } from "./authClient";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, reject, resolve };
}

function activeUser(): User {
  return {
    access_token: "access-token",
    expired: false,
    profile: { sub: "user-1", email: "analyst@example.com" },
  } as User;
}

function authClient(overrides: Partial<BrowserAuthClient> = {}): BrowserAuthClient {
  return {
    apiClient: {} as BrowserAuthClient["apiClient"],
    authMode: "cognito",
    sessionId: "11111111-1111-4111-8111-111111111111",
    isSigninCallback: () => false,
    completeSignin: vi.fn().mockResolvedValue(activeUser()),
    getUser: vi.fn().mockResolvedValue(activeUser()),
    signIn: vi.fn().mockResolvedValue(undefined),
    signOut: vi.fn().mockResolvedValue(undefined),
    subscribeToAccessTokenExpired: vi.fn(() => () => undefined),
    ...overrides,
  };
}

describe("AuthGate", () => {
  it("renders loading until public configuration is available", () => {
    const pending = deferred<BrowserAuthClient>();
    render(
      <AuthGate createClient={() => pending.promise}>
        {() => <p>Signed in app</p>}
      </AuthGate>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Loading application");
    expect(screen.queryByText("Signed in app")).not.toBeInTheDocument();
  });

  it("renders a safe configuration error", async () => {
    render(
      <AuthGate createClient={() => Promise.reject(new Error("secret-config-detail"))}>
        {() => <p>Signed in app</p>}
      </AuthGate>,
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Application authentication is unavailable",
    );
    expect(screen.getByRole("alert")).not.toHaveTextContent("secret-config-detail");
  });

  it("renders signed out and starts the redirect only on user action", async () => {
    const client = authClient({ getUser: vi.fn().mockResolvedValue(null) });
    render(
      <AuthGate createClient={() => Promise.resolve(client)}>
        {() => <p>Signed in app</p>}
      </AuthGate>,
    );

    const button = await screen.findByRole("button", { name: "Sign in" });
    expect(client.signIn).not.toHaveBeenCalled();
    button.click();
    expect(client.signIn).toHaveBeenCalledOnce();
  });

  it("shows callback processing before rendering the signed-in child", async () => {
    const callback = deferred<User>();
    const client = authClient({
      isSigninCallback: () => true,
      completeSignin: vi.fn(() => callback.promise),
    });
    render(
      <AuthGate createClient={() => Promise.resolve(client)}>
        {({ user }) => <p>Welcome {String(user?.profile.email)}</p>}
      </AuthGate>,
    );

    expect(await screen.findByRole("status")).toHaveTextContent("Completing sign in");
    callback.resolve(activeUser());
    expect(await screen.findByText("Welcome analyst@example.com")).toBeInTheDocument();
  });

  it("renders local mode as ready without an identity redirect", async () => {
    const client = authClient({ authMode: "local", getUser: vi.fn().mockResolvedValue(null) });
    render(
      <AuthGate createClient={() => Promise.resolve(client)}>
        {({ user }) => <p>Native app {String(user)}</p>}
      </AuthGate>,
    );

    expect(await screen.findByText("Native app null")).toBeInTheDocument();
    expect(client.signIn).not.toHaveBeenCalled();
  });
});
