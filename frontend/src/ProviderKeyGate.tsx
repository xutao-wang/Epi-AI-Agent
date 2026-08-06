import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import type { ApiClient } from "./apiClient";

interface Props {
  apiClient: ApiClient;
  children: ReactNode;
  onSignOut: () => Promise<void>;
}

type GatePhase = "checking" | "form" | "validating" | "validation-error" | "ready";

export default function ProviderKeyGate({ apiClient, children, onSignOut }: Props) {
  const [phase, setPhase] = useState<GatePhase>(
    apiClient.requiresProviderKey ? "checking" : "ready",
  );
  const [apiKey, setApiKey] = useState("");

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
          setPhase("form");
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

  if (phase === "ready") {
    return children;
  }
  if (phase === "checking") {
    return <main className="gate-shell" role="status">Checking provider key…</main>;
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
          <div className="gate-actions">
            <button disabled={!apiKey.trim() || phase === "validating"} type="submit">
              {phase === "validating" ? "Validating…" : "Save key"}
            </button>
            <button
              className="gate-secondary-action"
              disabled={phase === "validating"}
              onClick={() => void onSignOut()}
              type="button"
            >
              Sign out
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}
