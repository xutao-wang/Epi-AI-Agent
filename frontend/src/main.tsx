import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import AuthGate from "./AuthGate";
import ProviderKeyGate from "./ProviderKeyGate";
import "./styles.css";

const root = document.getElementById("root");

if (root) {
  createRoot(root).render(
    <StrictMode>
      <AuthGate>
        {({ apiClient, user, signOut }) => (
          <ProviderKeyGate apiClient={apiClient} onSignOut={signOut}>
            <App apiClient={apiClient} authenticatedUser={user} onSignOut={signOut} />
          </ProviderKeyGate>
        )}
      </AuthGate>
    </StrictMode>,
  );
}
