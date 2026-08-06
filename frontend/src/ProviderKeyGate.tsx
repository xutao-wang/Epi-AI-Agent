import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import type { ApiClient } from "./apiClient";

interface Props {
  apiClient: ApiClient;
  children: ReactNode;
  onSignOut: () => Promise<void>;
}

type GatePhase =
  | "checking"
  | "status-error"
  | "form"
  | "validating"
  | "validation-error"
  | "ready";

export default function ProviderKeyGate({ apiClient, children, onSignOut }: Props) {
  const [phase, setPhase] = useState<GatePhase>(
    apiClient.requiresProviderKey ? "checking" : "ready",
  );
  const [apiKey, setApiKey] = useState("");
  const [isSigningOut, setIsSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState(false);

  useEffect(() => {
    if (!apiClient.requiresProviderKey) {
      return;
    }
    let cancelled = false;
    void apiClient
      .getProviderKeyStatus()
      .then((status) => {
        if (!cancelled) {
          setPhase(status.configured ? "ready" : "form");
        }
      })
      .catch(() => {
        if (!cancelled) {
          setPhase("status-error");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [apiClient]);

  async function submitKey(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const candidate = apiKey.trim();
    if (!candidate) {
      return;
    }
    setApiKey("");
    setPhase("validating");
    try {
      await apiClient.setProviderKey(candidate);
      setPhase("ready");
    } catch {
      setPhase("validation-error");
    }
  }

  async function retryStatusCheck() {
    setPhase("checking");
    try {
      const status = await apiClient.getProviderKeyStatus();
      setPhase(status.configured ? "ready" : "form");
    } catch {
      setPhase("status-error");
    }
  }

  async function signOut() {
    setIsSigningOut(true);
    setSignOutError(false);
    try {
      await onSignOut();
    } catch {
      setSignOutError(true);
      setIsSigningOut(false);
    }
  }

  if (phase === "ready") {
    return children;
  }
  if (phase === "checking") {
    return <main className="gate-shell" role="status">Checking provider key…</main>;
  }
  if (phase === "status-error") {
    return (
      <main className="gate-shell">
        <section className="gate-card">
          <h1>Connect OpenAI</h1>
          <p role="alert">
            Provider key status could not be checked. Check your connection and try again.
          </p>
          {signOutError ? (
            <p role="alert">
              Sign out could not be completed. Check your connection and try again.
            </p>
          ) : null}
          <div className="gate-actions">
            <button onClick={() => void retryStatusCheck()} type="button">
              Try again
            </button>
            <button
              className="gate-secondary-action"
              disabled={isSigningOut}
              onClick={() => void signOut()}
              type="button"
            >
              {isSigningOut ? "Signing out…" : "Sign out"}
            </button>
          </div>
        </section>
      </main>
    );
  }

  return (
    <main className="gate-shell">
      <section className="gate-card">
        <h1>Connect OpenAI</h1>
        <p>
          Enter your OpenAI API key for this signed-in session. The key is sent
          directly to the server and is not stored by this browser.
        </p>
        <form onSubmit={submitKey}>
          <label htmlFor="provider-api-key">OpenAI API key</label>
          <input
            autoComplete="off"
            disabled={phase === "validating"}
            id="provider-api-key"
            onChange={(event) => setApiKey(event.target.value)}
            type="password"
            value={apiKey}
          />
          {phase === "validation-error" ? (
            <p role="alert">
              The provider key could not be validated. Check the key and try again.
            </p>
          ) : null}
          {signOutError ? (
            <p role="alert">
              Sign out could not be completed. Check your connection and try again.
            </p>
          ) : null}
          <div className="gate-actions">
            <button disabled={!apiKey.trim() || phase === "validating"} type="submit">
              {phase === "validating" ? "Validating…" : "Save key"}
            </button>
            <button
              className="gate-secondary-action"
              disabled={phase === "validating" || isSigningOut}
              onClick={() => void signOut()}
              type="button"
            >
              {isSigningOut ? "Signing out…" : "Sign out"}
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}
