import { beforeEach, describe, expect, it, vi } from "vitest";
import type { User, UserManagerSettings } from "oidc-client-ts";
import {
  TAB_SESSION_STORAGE_KEY,
  createBrowserAuthClient,
} from "./authClient";
import { LOCAL_SESSION_ID } from "./apiClient";

const cognitoConfig = {
  auth_mode: "cognito" as const,
  provider_key_required: true,
  cognito: {
    authority: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example",
    client_id: "public-client-id",
    redirect_uri: "https://app.test/auth/callback",
    post_logout_redirect_uri: "https://app.test/",
  },
};

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function oidcUser(overrides: Partial<User> = {}): User {
  return {
    access_token: "access-token",
    expired: false,
    profile: { sub: "user-1", email: "analyst@example.com" },
    ...overrides,
  } as User;
}

function userManager(overrides: Record<string, unknown> = {}) {
  return {
    getUser: vi.fn().mockResolvedValue(oidcUser()),
    removeUser: vi.fn().mockResolvedValue(undefined),
    signinRedirect: vi.fn().mockResolvedValue(undefined),
    signinRedirectCallback: vi.fn().mockResolvedValue(oidcUser()),
    signoutRedirect: vi.fn().mockResolvedValue(undefined),
    events: {
      addAccessTokenExpired: vi.fn(() => () => undefined),
    },
    ...overrides,
  };
}

describe("createBrowserAuthClient", () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
  });

  it("fetches public config before constructing Authorization Code + PKCE settings", async () => {
    const order: string[] = [];
    const fetchMock = vi.fn().mockImplementation(() => {
      order.push("config");
      return Promise.resolve(jsonResponse(cognitoConfig));
    });
    let settings: UserManagerSettings | undefined;
    const manager = userManager();

    await createBrowserAuthClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
      randomUUID: () => "11111111-1111-4111-8111-111111111111",
      userManagerFactory: (nextSettings) => {
        order.push("manager");
        settings = nextSettings;
        return manager;
      },
    });

    expect(order).toEqual(["config", "manager"]);
    expect(fetchMock).toHaveBeenCalledWith("http://api.test/api/public-config");
    expect(settings).toMatchObject({
      authority: cognitoConfig.cognito.authority,
      client_id: cognitoConfig.cognito.client_id,
      redirect_uri: cognitoConfig.cognito.redirect_uri,
      post_logout_redirect_uri: cognitoConfig.cognito.post_logout_redirect_uri,
      response_type: "code",
      scope: "openid email",
      disablePKCE: false,
      automaticSilentRenew: false,
    });
    expect(settings).not.toHaveProperty("client_secret");
  });

  it("stores OIDC state and the random tab UUID only in session storage", async () => {
    let settings: UserManagerSettings | undefined;
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(jsonResponse(cognitoConfig)),
    );

    const first = await createBrowserAuthClient({
      fetchImpl: fetchMock,
      randomUUID: () => "11111111-1111-4111-8111-111111111111",
      userManagerFactory: (nextSettings) => {
        settings = nextSettings;
        return userManager();
      },
    });
    const second = await createBrowserAuthClient({
      fetchImpl: fetchMock,
      randomUUID: () => "22222222-2222-4222-8222-222222222222",
      userManagerFactory: () => userManager(),
    });

    await settings?.stateStore?.set("authorization", "pkce-state");
    await settings?.userStore?.set("current-user", "oidc-user");

    expect(first.sessionId).toBe("11111111-1111-4111-8111-111111111111");
    expect(second.sessionId).toBe(first.sessionId);
    expect(window.sessionStorage.getItem(TAB_SESSION_STORAGE_KEY)).toBe(first.sessionId);
    expect(Object.values(window.sessionStorage)).toEqual(
      expect.arrayContaining(["pkce-state", "oidc-user", first.sessionId]),
    );
    expect(window.localStorage).toHaveLength(0);
  });

  it("uses the fixed native session and no OIDC manager in local mode", async () => {
    const managerFactory = vi.fn();
    const client = await createBrowserAuthClient({
      fetchImpl: vi.fn().mockResolvedValue(
        jsonResponse({ auth_mode: "local", provider_key_required: false, cognito: null }),
      ),
      userManagerFactory: managerFactory,
    });

    expect(client.sessionId).toBe(LOCAL_SESSION_ID);
    expect(managerFactory).not.toHaveBeenCalled();
    expect(window.sessionStorage.getItem(TAB_SESSION_STORAGE_KEY)).toBeNull();
    expect(await client.getUser()).toBeNull();
  });

  it("removes an expired OIDC user and returns signed out", async () => {
    const manager = userManager({
      getUser: vi.fn().mockResolvedValue(oidcUser({ expired: true })),
    });
    const client = await createBrowserAuthClient({
      fetchImpl: vi.fn().mockResolvedValue(jsonResponse(cognitoConfig)),
      randomUUID: () => "11111111-1111-4111-8111-111111111111",
      userManagerFactory: () => manager,
    });

    await expect(client.getUser()).resolves.toBeNull();
    expect(manager.removeUser).toHaveBeenCalledOnce();
    expect(manager.signinRedirect).not.toHaveBeenCalled();
  });

  it("deletes the server-side provider key before Cognito sign-out", async () => {
    const order: string[] = [];
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(cognitoConfig))
      .mockImplementationOnce((_input, init) => {
        order.push(`delete:${init?.method}`);
        return Promise.resolve(new Response(null, { status: 204 }));
      });
    const manager = userManager({
      signoutRedirect: vi.fn().mockImplementation(() => {
        order.push("signout");
        return Promise.resolve();
      }),
    });
    const client = await createBrowserAuthClient({
      apiBase: "http://api.test",
      fetchImpl: fetchMock,
      randomUUID: () => "11111111-1111-4111-8111-111111111111",
      userManagerFactory: () => manager,
    });

    await client.signOut();

    expect(order).toEqual(["delete:DELETE", "signout"]);
    expect(fetchMock.mock.calls[1][0]).toBe("http://api.test/api/session/provider-key");
  });
});
