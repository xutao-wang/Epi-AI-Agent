import { useEffect, useState, type ReactNode } from "react";
import type { User } from "oidc-client-ts";
import {
  createBrowserAuthClient,
  type BrowserAuthClient,
} from "./authClient";
import type { ApiClient } from "./apiClient";

interface AuthGateContext {
  apiClient: ApiClient;
  user: User | null;
  signOut: () => Promise<void>;
}

interface Props {
  children: (context: AuthGateContext) => ReactNode;
  createClient?: () => Promise<BrowserAuthClient>;
}

type GateState =
  | { phase: "loading" }
  | { phase: "callback" }
  | { phase: "error" }
  | { phase: "signed-out"; client: BrowserAuthClient }
  | { phase: "ready"; client: BrowserAuthClient; user: User | null };

export default function AuthGate({
  children,
  createClient = createBrowserAuthClient,
}: Props) {
  const [state, setState] = useState<GateState>({ phase: "loading" });

  useEffect(() => {
    let cancelled = false;
    let unsubscribe: () => void = () => undefined;

    void createClient()
      .then(async (client) => {
        if (cancelled) {
          return;
        }
        if (client.authMode === "cognito" && client.isSigninCallback()) {
          setState({ phase: "callback" });
          const user = await client.completeSignin();
          if (!cancelled) {
            setState({ phase: "ready", client, user });
          }
        } else {
          const user = await client.getUser();
          if (!cancelled) {
            setState(
              client.authMode === "cognito" && !user
                ? { phase: "signed-out", client }
                : { phase: "ready", client, user },
            );
          }
        }
        unsubscribe = client.subscribeToAccessTokenExpired(() => {
          if (!cancelled) {
            setState({ phase: "signed-out", client });
          }
        });
      })
      .catch(() => {
        if (!cancelled) {
          setState({ phase: "error" });
        }
      });

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [createClient]);

  if (state.phase === "loading") {
    return <main className="gate-shell" role="status">Loading application…</main>;
  }
  if (state.phase === "callback") {
    return <main className="gate-shell" role="status">Completing sign in…</main>;
  }
  if (state.phase === "error") {
    return (
      <main className="gate-shell">
        <section className="gate-card" role="alert">
          <h1>Application authentication is unavailable</h1>
          <p>Please contact the application administrator.</p>
        </section>
      </main>
    );
  }
  if (state.phase === "signed-out") {
    return (
      <main className="gate-shell">
        <section className="gate-card">
          <h1>AI Agent for RePORT</h1>
          <p>Sign in with your organization account to continue.</p>
          <button onClick={() => void state.client.signIn()} type="button">
            Sign in
          </button>
        </section>
      </main>
    );
  }

  return children({
    apiClient: state.client.apiClient,
    user: state.user,
    signOut: state.client.signOut,
  });
}
