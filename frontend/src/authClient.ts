import {
  UserManager,
  WebStorageStateStore,
  type User,
  type UserManagerSettings,
} from "oidc-client-ts";
import {
  LOCAL_SESSION_ID,
  createApiClient,
  type ApiClient,
} from "./apiClient";
import { DEFAULT_API_BASE } from "./config";
import type { PublicAppConfig } from "./types";

export const TAB_SESSION_STORAGE_KEY = "report-agent.tab-session-id";

export class AuthenticationExpiredError extends Error {
  constructor() {
    super("Your sign-in session expired. Sign in again to continue.");
    this.name = "AuthenticationExpiredError";
  }
}

export interface UserManagerLike {
  getUser(): Promise<User | null>;
  removeUser(): Promise<void>;
  signinRedirect(): Promise<void>;
  signinRedirectCallback(url?: string): Promise<User>;
  signoutRedirect(): Promise<void>;
  events: {
    addAccessTokenExpired(callback: () => Promise<void> | void): () => void;
  };
}

export interface BrowserAuthClient {
  apiClient: ApiClient;
  authMode: PublicAppConfig["auth_mode"];
  sessionId: string;
  isSigninCallback(): boolean;
  completeSignin(): Promise<User>;
  getUser(): Promise<User | null>;
  signIn(): Promise<void>;
  signOut(): Promise<void>;
  subscribeToAccessTokenExpired(callback: () => void): () => void;
}

interface CreateBrowserAuthClientOptions {
  apiBase?: string;
  fetchImpl?: typeof fetch;
  sessionStorage?: Storage;
  randomUUID?: () => string;
  locationHref?: () => string;
  navigateTo?: (url: string) => void;
  replaceUrl?: (url: string) => void;
  userManagerFactory?: (settings: UserManagerSettings) => UserManagerLike;
}

function apiUrl(apiBase: string, path: string) {
  return `${apiBase.replace(/\/+$/, "")}${path}`;
}

function isCognitoConfig(
  config: PublicAppConfig,
): config is PublicAppConfig & { cognito: NonNullable<PublicAppConfig["cognito"]> } {
  return config.auth_mode === "cognito" && config.cognito !== null;
}

async function loadPublicConfig(
  apiBase: string,
  fetchImpl: typeof fetch,
): Promise<PublicAppConfig> {
  const response = await fetchImpl(apiUrl(apiBase, "/api/public-config"));
  if (!response.ok) {
    throw new Error("Public application configuration is unavailable.");
  }
  const config = (await response.json()) as PublicAppConfig;
  if (
    (config.auth_mode !== "local" && config.auth_mode !== "cognito") ||
    (config.auth_mode === "cognito" && !config.cognito)
  ) {
    throw new Error("Public application configuration is invalid.");
  }
  return config;
}

function hasSigninCallback(url: string): boolean {
  const params = new URL(url).searchParams;
  return Boolean(params.get("state") && (params.get("code") || params.get("error")));
}

function callbackFreeUrl(url: string): string {
  const nextUrl = new URL(url);
  for (const name of [
    "code",
    "state",
    "session_state",
    "error",
    "error_description",
    "error_uri",
  ]) {
    nextUrl.searchParams.delete(name);
  }
  return `${nextUrl.pathname}${nextUrl.search}${nextUrl.hash}`;
}

export async function createBrowserAuthClient({
  apiBase = DEFAULT_API_BASE,
  fetchImpl = fetch,
  sessionStorage = window.sessionStorage,
  randomUUID = () => window.crypto.randomUUID(),
  locationHref = () => window.location.href,
  navigateTo = (url) => window.location.assign(url),
  replaceUrl = (url) => window.history.replaceState({}, document.title, url),
  userManagerFactory = (settings) => new UserManager(settings),
}: CreateBrowserAuthClientOptions = {}): Promise<BrowserAuthClient> {
  const config = await loadPublicConfig(apiBase, fetchImpl);
  if (!isCognitoConfig(config)) {
    return {
      apiClient: createApiClient({ apiBase, fetchImpl, sessionId: LOCAL_SESSION_ID }),
      authMode: "local",
      sessionId: LOCAL_SESSION_ID,
      isSigninCallback: () => false,
      completeSignin: () => Promise.reject(new Error("OIDC is disabled in local mode.")),
      getUser: async () => null,
      signIn: async () => undefined,
      signOut: async () => undefined,
      subscribeToAccessTokenExpired: () => () => undefined,
    };
  }

  let sessionId = sessionStorage.getItem(TAB_SESSION_STORAGE_KEY);
  if (!sessionId) {
    sessionId = randomUUID();
    sessionStorage.setItem(TAB_SESSION_STORAGE_KEY, sessionId);
  }
  const oidcStore = new WebStorageStateStore({ store: sessionStorage });
  const manager = userManagerFactory({
    authority: config.cognito.authority,
    client_id: config.cognito.client_id,
    redirect_uri: config.cognito.redirect_uri,
    post_logout_redirect_uri: config.cognito.post_logout_redirect_uri,
    response_type: "code",
    scope: "openid email",
    disablePKCE: false,
    automaticSilentRenew: false,
    stateStore: oidcStore,
    userStore: oidcStore,
  });
  const signedOutListeners = new Set<() => void>();

  function notifySignedOut() {
    for (const listener of signedOutListeners) {
      listener();
    }
  }

  async function expireSession() {
    try {
      await manager.removeUser();
    } catch {
      // The in-memory gate must still leave the authenticated state.
    } finally {
      notifySignedOut();
    }
  }

  async function activeUser(): Promise<User | null> {
    const user = await manager.getUser();
    if (user?.expired) {
      await expireSession();
      return null;
    }
    return user;
  }

  const apiClient = createApiClient({
    apiBase,
    fetchImpl,
    getAccessToken: async () => {
      const user = await activeUser();
      if (!user) {
        notifySignedOut();
        throw new AuthenticationExpiredError();
      }
      return user.access_token;
    },
    sessionId,
  });

  return {
    apiClient,
    authMode: "cognito",
    sessionId,
    isSigninCallback: () => hasSigninCallback(locationHref()),
    async completeSignin() {
      const callbackUrl = locationHref();
      try {
        return await manager.signinRedirectCallback(callbackUrl);
      } finally {
        replaceUrl(callbackFreeUrl(callbackUrl));
      }
    },
    getUser: activeUser,
    signIn: () => manager.signinRedirect(),
    async signOut() {
      await apiClient.clearProviderKey();
      await manager.removeUser();
      const logoutUrl = new URL(config.cognito.logout_endpoint);
      logoutUrl.searchParams.set("client_id", config.cognito.client_id);
      logoutUrl.searchParams.set(
        "logout_uri",
        config.cognito.post_logout_redirect_uri,
      );
      navigateTo(logoutUrl.toString());
    },
    subscribeToAccessTokenExpired(callback) {
      signedOutListeners.add(callback);
      const removeOidcHandler = manager.events.addAccessTokenExpired(() => {
        void expireSession();
      });
      return () => {
        signedOutListeners.delete(callback);
        removeOidcHandler();
      };
    },
  };
}
