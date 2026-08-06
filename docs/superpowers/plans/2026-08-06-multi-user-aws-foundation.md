# Multi-User AWS Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing native FastAPI/React application safe for an invitation-only AWS pilot by adding Cognito-compatible authentication, user-owned conversations and artifacts, and session-memory-only user-provided OpenAI keys without requiring Docker or creating AWS resources yet.

**Architecture:** FastAPI verifies Cognito access tokens and converts them into an immutable request identity. Every conversation and file operation requires that identity's stable Cognito `sub`, while a random browser-session identifier scopes the in-memory OpenAI credential. The React application completes Authorization Code + PKCE through Cognito, supplies the bearer token and session identifier on API calls, and retrieves protected binary files through authenticated fetches. Local native mode uses the same ownership/runtime interfaces with a fixed `local-user` principal and the already-verified local key.

**Tech Stack:** Python 3.12, FastAPI, SQLite, PyJWT 2.13 with cryptography, React 19, TypeScript, oidc-client-ts 3.5, Vitest, pytest.

## Global Constraints

- Work only on the existing `aws-test` branch. Before every task, run `git branch --show-current` and stop if it is not `aws-test`.
- Do not create, modify, or charge any AWS resource in this plan.
- Do not add Docker as a runtime or test dependency. Keep `python run_fastapi.py` as a supported launch path.
- Treat only synthetic or fully de-identified research data as permitted for this pilot.
- Keep OpenAI keys out of SQLite, EBS files, S3-shaped fixtures, logs, exception details, frontend local storage, frontend session storage, URLs, and React persisted state.
- Cognito `sub` is the ownership identifier. Email is display metadata only and must never be used in an ownership query or filesystem path.
- A thread ID remains the conversation ID users recognize locally; `owner_user_id` is a separate authorization dimension.
- The health and public bootstrap-configuration routes are anonymous. All conversation, runtime, upload, artifact, export, and credential-status routes require a request identity.
- Follow test-driven development: add the focused failing test, run it and observe the intended failure, implement the minimum behavior, rerun the focused test, then run the affected suite.
- Use `.venv/bin/python` for Python commands. All production code must remain Python 3.12 compatible.
- Each completed task receives its own intentional commit. Do not mix unrelated user changes into those commits.
- Any frontend change requires both `npm --prefix frontend run build` and `.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest` before completion.
- The feature must finish with the executable real smoke `scripts/smoke_multi_user_isolation_real.py`, complete in under five minutes, and leave no server process behind.

Dependency and protocol references: [PyJWT 2.13.0](https://pypi.org/project/PyJWT/), [oidc-client-ts 3.5.0](https://www.npmjs.com/package/oidc-client-ts), and [AWS Cognito access-token verification](https://docs.aws.amazon.com/cognito/latest/developerguide/amazon-cognito-user-pools-using-tokens-verifying-a-jwt.html).

---

## Task 1: Add deployment/auth configuration and pinned dependencies

**Files:**

- Modify: `requirements.txt`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `.env.example`
- Modify: `api/deployment.py`
- Create: `api/public_config.py`
- Modify: `api/schemas.py`
- Create: `tests/test_public_config.py`
- Modify: `tests/test_centralized_epi_agent_architecture.py`

**Interfaces:**

- Consumes: environment values through a supplied `Mapping[str, str]`; no module in this task consumes provider secrets.
- Produces: `ApplicationAuthConfig`, `application_auth_config(environ: Mapping[str, str]) -> ApplicationAuthConfig`, `public_app_config(config: ApplicationAuthConfig) -> PublicAppConfig`, and `required_secret_names(auth_mode: str) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing configuration tests**

Add tests that prove:

1. `REPORT_AGENT_AUTH_MODE` accepts exactly `local` and `cognito`.
2. Cognito mode requires region, user-pool ID, app-client ID, redirect URI, and post-logout redirect URI.
3. The browser configuration exposes only issuer/authority, client ID, redirect URIs, auth mode, and whether a provider key is required.
4. Hosted mode does not list `OPENAI_API_KEY` as a deployment secret; local mode still requires it for the native launcher.
5. No browser configuration value contains an OpenAI key.

Add this exact happy-path assertion to `tests/test_public_config.py`, then add one parameterized missing-field case for each required Cognito variable:

```python
def test_cognito_public_config_contains_only_browser_safe_values() -> None:
    environ = {
        "REPORT_AGENT_AUTH_MODE": "cognito",
        "REPORT_AGENT_AWS_REGION": "us-east-1",
        "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
        "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "client-123",
        "REPORT_AGENT_AUTH_REDIRECT_URI": "https://demo.example/callback",
        "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://demo.example/",
        "OPENAI_API_KEY": "must-not-be-public",
    }
    result = public_app_config(application_auth_config(environ)).model_dump()

    assert result == {
        "auth_mode": "cognito",
        "provider_key_required": True,
        "cognito": {
            "authority": (
                "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
            ),
            "client_id": "client-123",
            "redirect_uri": "https://demo.example/callback",
            "post_logout_redirect_uri": "https://demo.example/",
        },
    }
    assert "must-not-be-public" not in json.dumps(result)
```

The central interface must be:

```python
@dataclass(frozen=True)
class ApplicationAuthConfig:
    mode: Literal["local", "cognito"]
    aws_region: str | None
    cognito_user_pool_id: str | None
    cognito_app_client_id: str | None
    redirect_uri: str | None
    post_logout_redirect_uri: str | None

    @property
    def cognito_issuer(self) -> str | None:
        if self.mode == "local":
            return None
        assert self.aws_region is not None
        assert self.cognito_user_pool_id is not None
        return (
            f"https://cognito-idp.{self.aws_region}.amazonaws.com/"
            f"{self.cognito_user_pool_id}"
        )
```

Implement `application_auth_config` by normalizing the mode, rejecting values outside `{local, cognito}`, collecting the five Cognito fields, and raising `ApplicationConfigurationError` with the missing environment-variable names in Cognito mode. Implement the mode-aware secret policy as:

```python
def required_secret_names(auth_mode: str = "local") -> tuple[str, ...]:
    normalized = str(auth_mode or "").strip().lower()
    if normalized == "local":
        return ("OPENAI_API_KEY",)
    if normalized == "cognito":
        return ()
    raise ValueError("auth_mode must be 'local' or 'cognito'")
```

The public response schema must be:

```python
class CognitoPublicConfig(BaseModel):
    authority: str
    client_id: str
    redirect_uri: str
    post_logout_redirect_uri: str

class PublicAppConfig(BaseModel):
    auth_mode: Literal["local", "cognito"]
    provider_key_required: bool
    cognito: CognitoPublicConfig | None = None
```

`public_app_config` sets `provider_key_required=False` for local mode because the native launcher seeds the verified local credential, and `True` for Cognito mode because every signed-in user must supply a key.

- [ ] **Step 2: Run the tests and confirm the expected failure**

Run:

```bash
.venv/bin/python -m pytest tests/test_public_config.py tests/test_centralized_epi_agent_architecture.py -q
```

Expected: import or assertion failures because the configuration module and mode-aware secret policy do not exist.

- [ ] **Step 3: Implement strict configuration parsing**

Implement `ApplicationAuthConfig` without reading secrets into `PublicAppConfig`. Use the Cognito issuer format:

```python
f"https://cognito-idp.{aws_region}.amazonaws.com/{user_pool_id}"
```

Use these environment names:

```dotenv
REPORT_AGENT_AUTH_MODE=local
REPORT_AGENT_AWS_REGION=
REPORT_AGENT_COGNITO_USER_POOL_ID=
REPORT_AGENT_COGNITO_APP_CLIENT_ID=
REPORT_AGENT_AUTH_REDIRECT_URI=
REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI=
```

`required_secret_names("local")` returns `("OPENAI_API_KEY",)`. `required_secret_names("cognito")` returns an empty tuple because users bring their own API keys at runtime.

- [ ] **Step 4: Pin authentication dependencies**

Add `PyJWT[crypto]==2.13.0` to `requirements.txt`. Add `oidc-client-ts` version `3.5.0` using:

```bash
npm --prefix frontend install --save-exact oidc-client-ts@3.5.0
```

Do not use a caret range for `oidc-client-ts`. Commit the generated lockfile change.

- [ ] **Step 5: Run focused and dependency integrity checks**

Run:

```bash
.venv/bin/python -m pytest tests/test_public_config.py tests/test_centralized_epi_agent_architecture.py -q
npm --prefix frontend run build
```

Expected: both commands pass.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt frontend/package.json frontend/package-lock.json .env.example api/deployment.py api/public_config.py api/schemas.py tests/test_public_config.py tests/test_centralized_epi_agent_architecture.py
git commit -m "feat: define hosted authentication configuration"
```

---

## Task 2: Verify Cognito access tokens and create request identities

**Files:**

- Create: `api/auth.py`
- Create: `tests/test_api_auth.py`
- Modify: `api/server.py`
- Modify: `tests/test_api_server.py`

**Interfaces:**

- Consumes: `ApplicationAuthConfig` and `PublicAppConfig` from Task 1.
- Produces: `AuthenticatedUser`, `RequestIdentity`, `TokenVerifier`, `LocalTokenVerifier`, `CognitoTokenVerifier`, and `request_identity_dependency(verifier)` for protected routes.

- [ ] **Step 1: Write failing unit and route tests**

Cover valid and invalid Cognito JWTs using an ephemeral RSA keypair and a local JWKS HTTP fixture. Tests must prove:

- only `RS256` is accepted;
- signature, issuer, expiration, `token_use == "access"`, and `client_id` are validated;
- the stable `sub` becomes `owner_user_id`;
- email is optional and never substitutes for `sub`;
- missing or malformed `Authorization: Bearer` returns 401;
- absent/invalid `X-Epi-Session-ID` returns 400;
- `/api/health` and `/api/public-config` remain public;
- a representative protected route rejects an anonymous request.

Add this local-principal test verbatim and place the RSA/JWKS cases in the same file:

```python
def test_local_verifier_uses_fixed_non_email_owner() -> None:
    user = LocalTokenVerifier().verify(None)

    assert user == AuthenticatedUser(owner_user_id="local-user")
    assert user.email is None
    assert user.token_expires_at_epoch is None
```

Use these production interfaces:

```python
@dataclass(frozen=True)
class AuthenticatedUser:
    owner_user_id: str
    email: str | None = None
    token_expires_at_epoch: int | None = None

@dataclass(frozen=True)
class RequestIdentity:
    user: AuthenticatedUser
    session_id: str

    @property
    def owner_user_id(self) -> str:
        return self.user.owner_user_id

class TokenVerifier(Protocol):
    def verify(self, authorization: str | None) -> AuthenticatedUser:
        raise NotImplementedError

class LocalTokenVerifier:
    def verify(self, authorization: str | None) -> AuthenticatedUser:
        return AuthenticatedUser(owner_user_id="local-user")

class CognitoTokenVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        app_client_id: str,
        jwks_url: str | None = None,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.app_client_id = app_client_id
        self.jwks_client = jwt.PyJWKClient(
            jwks_url or f"{self.issuer}/.well-known/jwks.json"
        )
```

`CognitoTokenVerifier.verify` strips an exact bearer prefix, obtains the signing key from `PyJWKClient`, applies the claim checks shown in Step 3, rejects a blank `sub`, and returns `AuthenticatedUser(owner_user_id=sub, email=optional_email, token_expires_at_epoch=int(claims["exp"]))`. `request_identity_dependency` parses `X-Epi-Session-ID` with `UUID(value)`, requires its canonical string form, and returns `RequestIdentity(user=verifier.verify(authorization), session_id=value)`. Define `LOCAL_SESSION_ID = "00000000-0000-4000-8000-000000000001"` in `api/auth.py`; local startup and frontend local mode must use that same value.

Session IDs must be canonical UUID strings. They are correlation/scoping identifiers, not authentication credentials.

- [ ] **Step 2: Run the tests and confirm failure**

```bash
.venv/bin/python -m pytest tests/test_api_auth.py tests/test_api_server.py -q
```

Expected: new auth imports fail and protected routes still accept anonymous calls.

- [ ] **Step 3: Implement verification and dependency wiring**

Use `jwt.PyJWKClient` and `jwt.decode` with explicit algorithms, audience disabled, and manual Cognito `client_id` checking:

```python
claims = jwt.decode(
    token,
    signing_key,
    algorithms=["RS256"],
    issuer=self.issuer,
    options={"verify_aud": False, "require": ["exp", "iat", "sub"]},
)
if claims.get("token_use") != "access":
    raise InvalidTokenError("token_use must be access")
if not hmac.compare_digest(str(claims.get("client_id", "")), self.app_client_id):
    raise InvalidTokenError("client_id mismatch")
```

Translate verification failures into a generic 401 response. Do not return token contents, claim values, or verifier exception text to the browser or logs.

Extend `create_app` in this task to the exact signature `create_app(runtime: ReportAgentApiRuntime, *, static_dir: Path | None = None, cors_origin_regex: str | None = None, public_config: PublicAppConfig | None = None, token_verifier: TokenVerifier | None = None) -> FastAPI`. Register health and public configuration directly on `app`; register current application endpoints on a separate `APIRouter(dependencies=[Depends(require_identity)])` and include it on `app`. Task 3 extends the same signature with credential dependencies.

Apply identity dependencies at the individual `/api` router level so public routes are unmistakably excluded.

- [ ] **Step 4: Run focused tests**

```bash
.venv/bin/python -m pytest tests/test_api_auth.py tests/test_api_server.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add api/auth.py api/server.py tests/test_api_auth.py tests/test_api_server.py
git commit -m "feat: authenticate API requests with Cognito tokens"
```

---

## Task 3: Store each user's OpenAI key only for the application session

**Files:**

- Create: `api/provider_credentials.py`
- Modify: `api/schemas.py`
- Modify: `api/server.py`
- Create: `tests/test_provider_credentials.py`
- Modify: `tests/test_api_server.py`
- Modify: `utils/provider_startup.py`

**Interfaces:**

- Consumes: `RequestIdentity` from Task 2 and `verify_active_provider(provider, api_key)` from `utils/provider_startup.py`.
- Produces: `ProviderKeyValidator`, `OpenAIProviderKeyValidator`, and `ProviderCredentialStore` with `put`, `get`, `has`, `delete`, and `prune_expired` methods.

Extend `create_app` from Task 2 with `credential_store: ProviderCredentialStore | None = None` and `provider_key_validator: ProviderKeyValidator | None = None`; no other parameter names or defaults change.

- [ ] **Step 1: Write failing credential-store tests**

Prove that:

- the storage key is exactly `(owner_user_id, session_id)`;
- two users and two sessions cannot read one another's values;
- entries expire after the configured idle TTL;
- Cognito-bound entries become inaccessible at token expiration even when the idle TTL is longer;
- clearing a session removes only that session;
- pruning and object representation never expose API key text;
- validation occurs before storage;
- failures return safe categories/messages without the submitted key;
- the store does not touch the filesystem or environment.

Include this exact cross-session test:

```python
def identity(owner_user_id: str, session_id: str) -> RequestIdentity:
    return RequestIdentity(
        user=AuthenticatedUser(owner_user_id=owner_user_id),
        session_id=session_id,
    )


def test_provider_credentials_are_isolated_by_owner_and_session() -> None:
    store = ProviderCredentialStore()
    identity_a = identity("user-a", "11111111-1111-4111-8111-111111111111")
    identity_b = identity("user-b", "22222222-2222-4222-8222-222222222222")
    store.put(identity_a, "key-a")
    store.put(identity_b, "key-b")

    assert store.get(identity_a) == "key-a"
    assert store.get(identity_b) == "key-b"
    store.delete(identity_a)
    assert store.get(identity_a) is None
    assert store.get(identity_b) == "key-b"
```

Use an injectable clock and this interface:

```python
@dataclass
class _CredentialRecord:
    api_key: str
    last_accessed_monotonic: float
    token_expires_at_epoch: int | None

    def __repr__(self) -> str:
        return "_CredentialRecord(api_key=<redacted>)"


class ProviderKeyValidator(Protocol):
    def validate(self, provider: str, api_key: str) -> None:
        raise NotImplementedError

class ProviderCredentialStore:
    def __init__(
        self,
        *,
        idle_ttl_seconds: float = 43_200,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._idle_ttl_seconds = idle_ttl_seconds
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self._entries: dict[tuple[str, str], _CredentialRecord] = {}
        self._lock = threading.RLock()

    def put(self, identity: RequestIdentity, api_key: str) -> None:
        normalized = api_key.strip()
        if not normalized:
            raise ValueError("api_key is required")
        with self._lock:
            self._entries[(identity.owner_user_id, identity.session_id)] = (
                _CredentialRecord(
                    api_key=normalized,
                    last_accessed_monotonic=self._monotonic_clock(),
                    token_expires_at_epoch=identity.user.token_expires_at_epoch,
                )
            )
```

Implement the remaining methods under the same lock. `get` removes and returns `None` when `monotonic_now - last_accessed >= idle_ttl_seconds` or `wall_now >= token_expires_at_epoch`, otherwise updates `last_accessed_monotonic` and returns its key. `has` delegates to `get` and compares with `None`; `delete` pops only the exact tuple; `prune_expired` applies both deadlines to every record and returns the deletion count.

Use a `threading.RLock`; store normalized strings in a private record with `last_accessed_monotonic` and `token_expires_at_epoch`; set the default idle TTL to 12 hours. Inject both `time.monotonic` and `time.time` for deterministic tests. Explicitly override `__repr__` on credential records and the store to show counts only.

- [ ] **Step 2: Write failing endpoint tests**

Add:

```text
GET    /api/session/provider-key  -> {"configured": false|true}
PUT    /api/session/provider-key  body {"api_key": "example-user-key"}
DELETE /api/session/provider-key  -> 204
```

The PUT response must never echo the key. All three routes require a valid request identity.

- [ ] **Step 3: Run and observe failure**

```bash
.venv/bin/python -m pytest tests/test_provider_credentials.py tests/test_api_server.py -q
```

Expected: missing store/schema/endpoints.

- [ ] **Step 4: Implement memory-only credentials**

Add schemas:

```python
class ProviderKeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)

class ProviderKeyStatus(BaseModel):
    configured: bool
```

Adapt `verify_active_provider` through a small `OpenAIProviderKeyValidator` without changing its explicit `api_key` input. Never assign submitted values to `os.environ`.

Call `runtime.release_session(identity.owner_user_id, identity.session_id)` from DELETE after erasing the credential. This method is added in Task 6; temporarily define it on the runtime test fake and let the production call fail until Task 6, keeping commits buildable by adding a no-op method to the real runtime in this task.

- [ ] **Step 5: Run focused tests and a secret scan**

```bash
.venv/bin/python -m pytest tests/test_provider_credentials.py tests/test_api_server.py tests/test_provider_startup.py -q
rg -n "api_key.*(log|print)|OPENAI_API_KEY.*=" api utils | cat
```

Expected: tests pass; scan contains configuration reads or test-safe code only, not logging/persistence of submitted keys.

- [ ] **Step 6: Commit**

```bash
git add api/provider_credentials.py api/schemas.py api/server.py api/runtime.py utils/provider_startup.py tests/test_provider_credentials.py tests/test_api_server.py tests/test_provider_startup.py
git commit -m "feat: keep user provider keys in session memory"
```

---

## Task 4: Add owner isolation to conversation history

**Files:**

- Modify: `api/conversation_history.py`
- Modify: `tests/test_conversation_history.py`
- Modify: `api/runtime.py`
- Modify: `tests/test_api_runtime.py`

**Interfaces:**

- Consumes: `RequestIdentity` and its authenticated `owner_user_id` from Task 2.
- Produces: owner-required conversation-store and runtime methods plus the one-way legacy migration `claim_unowned(owner_user_id) -> int`.

- [ ] **Step 1: Write failing ownership and migration tests**

Create conversations with the same store for `user-a` and `user-b`. Prove each user can list, read, rename, open, archive, restore, touch, and delete only their own rows. A wrong-owner lookup must behave as not found, not forbidden, to avoid revealing IDs.

Add a migration test by creating the current legacy table without `owner_user_id`, inserting a row, and then opening the new store. Prove:

- the schema gains `owner_user_id` and an owner/listing index;
- legacy rows remain unowned and invisible by default;
- `claim_unowned("local-user")` makes them visible only in local mode;
- a second claim cannot transfer an owned row.

The central isolation assertion is:

```python
def test_conversation_rows_are_owner_scoped(tmp_path: Path) -> None:
    store = ConversationHistoryStore(tmp_path / "history.db")
    store.create("user-a", "shared-thread-id", model_name="gpt-test")

    assert store.get("user-a", "shared-thread-id") is not None
    assert store.get("user-b", "shared-thread-id") is None
    assert store.rename("user-b", "shared-thread-id", "stolen") is None
    assert store.delete("user-b", "shared-thread-id") is False
    assert store.get("user-a", "shared-thread-id").title != "stolen"
```

- [ ] **Step 2: Run tests and observe cross-user behavior**

```bash
.venv/bin/python -m pytest tests/test_conversation_history.py tests/test_api_runtime.py -q
```

Expected: new signatures/assertions fail because queries currently use only `thread_id`.

- [ ] **Step 3: Change the store API and every SQL predicate**

Use owner-first signatures consistently: `create(owner_user_id, thread_id, *, model_name)`, `get(owner_user_id, thread_id)`, `list(owner_user_id)`, `archive(owner_user_id, thread_id)`, `restore(owner_user_id, thread_id)`, `delete(owner_user_id, thread_id)`, `touch(owner_user_id, thread_id)`, `mark_opened(owner_user_id, thread_id)`, `rename(owner_user_id, thread_id, title)`, `set_automatic_title(owner_user_id, thread_id, title)`, and `claim_unowned(owner_user_id)`. Their return types remain the current return types: `ConversationSummary`/optional summary, `list[ConversationSummary]`, `bool`, `None`, and `int`, respectively.

Every existing-row mutation must include both:

```sql
WHERE owner_user_id = ? AND thread_id = ?
```

Create index `conversation_history_owner_activity_idx` on `(owner_user_id, archived_at, updated_at DESC)`.

During schema initialization, also execute:

```python
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA busy_timeout=5000")
```

Use explicit `with connection:` transactions for migration and mutations; never hold a transaction while invoking the graph or provider.

- [ ] **Step 4: Thread the request identity through runtime history operations**

Change conversation-facing runtime methods to take `identity: RequestIdentity` first, then pass `identity.owner_user_id` to the store. Do not add a permissive default. Keep `ConversationSummary` free of ownership data so the browser cannot choose or override it.

- [ ] **Step 5: Run focused tests**

```bash
.venv/bin/python -m pytest tests/test_conversation_history.py tests/test_api_runtime.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add api/conversation_history.py api/runtime.py tests/test_conversation_history.py tests/test_api_runtime.py
git commit -m "feat: isolate conversation history by owner"
```

---

## Task 5: Put every generated file under its owner and thread

**Files:**

- Create: `utils/user_storage.py`
- Create: `tests/test_user_storage.py`
- Modify: `utils/attachment_artifacts.py`
- Modify: `utils/dataset_artifacts.py`
- Modify: `epi_agent/protocol.py`
- Modify: `epi_agent/agent.py`
- Modify: `epi_agent/db_rag/tools.py`
- Modify: `api/runtime.py`
- Modify: `tests/test_attachment_artifacts.py`
- Modify: `tests/test_dataset_artifacts.py`
- Modify: `tests/test_db_rag_agent_tools.py`
- Modify: `tests/test_api_runtime.py`

**Interfaces:**

- Consumes: owner-authorized threads from Task 4.
- Produces: `UserStorageLayout.thread(owner_user_id, thread_id) -> ThreadStorageScope`; all file stores consume only an authorized `ThreadStorageScope`.

- [ ] **Step 1: Write failing storage-layout and isolation tests**

The exact layout is:

```text
<runtime-root>/users/<sha256-cognito-sub>/threads/<thread-id>/
├── attachments/
├── datasets/
├── figures/
├── tables/
├── exports/
└── execution/
```

Tests must prove raw Cognito subjects/emails do not appear in paths, owner hashes are deterministic, thread IDs are validated as one safe path component, resolved paths remain under `runtime-root/users`, and two owners using the same thread/attachment/dataset IDs receive disjoint paths. Add negative route/runtime tests proving user B cannot download, inspect, or delete user A's files even after guessing every ID.

Add this path-contract test:

```python
def test_user_storage_hashes_owner_and_keeps_safe_thread_id(tmp_path: Path) -> None:
    scope = UserStorageLayout(tmp_path).thread("cognito/sub@example", "thread-123")
    owner_hash = hashlib.sha256(b"cognito/sub@example").hexdigest()

    assert scope.root == (
        tmp_path.resolve() / "users" / owner_hash / "threads" / "thread-123"
    )
    assert "cognito/sub@example" not in str(scope.root)
    assert scope.datasets == scope.root / "datasets"
```

Use these types:

```python
def _safe_path_component(value: str, *, label: str) -> str:
    component = str(value or "").strip()
    if (
        not component
        or component in {".", ".."}
        or "/" in component
        or "\\" in component
        or "\x00" in component
    ):
        raise ValueError(f"{label} must be one safe path component")
    return component


@dataclass(frozen=True)
class ThreadStorageScope:
    owner_user_id: str
    thread_id: str
    root: Path

    @property
    def attachments(self) -> Path:
        return self.root / "attachments"
    @property
    def datasets(self) -> Path:
        return self.root / "datasets"
    @property
    def figures(self) -> Path:
        return self.root / "figures"
    @property
    def tables(self) -> Path:
        return self.root / "tables"
    @property
    def exports(self) -> Path:
        return self.root / "exports"
    @property
    def execution(self) -> Path:
        return self.root / "execution"

class UserStorageLayout:
    def __init__(self, runtime_root: str | Path):
        self.runtime_root = Path(runtime_root).expanduser().resolve()

    def thread(self, owner_user_id: str, thread_id: str) -> ThreadStorageScope:
        owner = owner_user_id.strip()
        safe_thread = _safe_path_component(thread_id, label="thread_id")
        if not owner:
            raise ValueError("owner_user_id is required")
        owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        root = self.runtime_root / "users" / owner_hash / "threads" / safe_thread
        return ThreadStorageScope(owner, safe_thread, root)
```

- [ ] **Step 2: Run focused tests and observe failure**

```bash
.venv/bin/python -m pytest tests/test_user_storage.py tests/test_attachment_artifacts.py tests/test_dataset_artifacts.py tests/test_api_runtime.py -q
```

Expected: imports/signatures fail and existing paths have no owner component.

- [ ] **Step 3: Implement scoped paths and attachment storage**

Hash UTF-8 identifiers with full lowercase SHA-256. Reject blank values before hashing. Make `LocalAttachmentStore` accept a `ThreadStorageScope` for stage/read/delete/list operations; remove any method that resolves solely from a caller-supplied thread ID.

The runtime—not an HTTP request body—creates the scope after it has authorized the owner/thread pair.

- [ ] **Step 4: Remove the process-global dataset runtime root**

Delete runtime reliance on `utils.dataset_artifacts.DEFAULT_RUNTIME_ROOT`. Change generated-dataset functions to receive the already-authorized `scope.datasets` root explicitly. Add `thread_storage: ThreadStorageScope | None = None` to `ToolContext`, fill it in `build_general_epi_agent_graph`, and use it in DB-RAG persistence. Unit-only tool contexts may leave it `None`; any persistence operation must raise a clear internal configuration error if no scope exists.

This is the intended call shape:

```python
paths = generated_dataset_artifact_paths(
    dataset_root=context.thread_storage.datasets,
    dataset_id=dataset_id,
)
```

Do not combine a global root with a raw thread ID after this change.

- [ ] **Step 5: Run all artifact/tool tests**

```bash
.venv/bin/python -m pytest \
  tests/test_user_storage.py \
  tests/test_attachment_artifacts.py \
  tests/test_dataset_artifacts.py \
  tests/test_attachment_tools.py \
  tests/test_db_rag_agent_tools.py \
  tests/test_api_runtime.py -q
```

Expected: all pass and temporary output paths match the owner/thread hierarchy.

- [ ] **Step 6: Commit**

```bash
git add utils/user_storage.py utils/attachment_artifacts.py utils/dataset_artifacts.py epi_agent/protocol.py epi_agent/agent.py epi_agent/db_rag/tools.py api/runtime.py tests/test_user_storage.py tests/test_attachment_artifacts.py tests/test_dataset_artifacts.py tests/test_attachment_tools.py tests/test_db_rag_agent_tools.py tests/test_api_runtime.py
git commit -m "feat: scope runtime artifacts to user conversations"
```

---

## Task 6: Make graph and model creation owner/session/key aware

**Files:**

- Modify: `llm_vllm.py`
- Modify: `api/runtime.py`
- Modify: `api/conversation_history.py`
- Modify: `graph/builder.py`
- Modify: `api/app.py`
- Modify: `db_rag/vectorstore.py`
- Modify: `db_rag/service/dataset_naming.py`
- Modify: `db_rag/service/model_routing.py`
- Create: `tests/test_llm_vllm.py`
- Modify: `tests/test_api_runtime.py`
- Modify: `tests/test_no_study_startup.py`
- Modify: `tests/test_db_rag_dataset_naming.py`

**Interfaces:**

- Consumes: `RequestIdentity`, `ThreadStorageScope`, and `ProviderCredentialStore` from Tasks 2, 3, and 5.
- Produces: `GraphBuildContext`; `GraphFactory(settings, context)`; owner/session-aware runtime graph caching; explicit-key provider factories.

- [ ] **Step 1: Write failing explicit-key tests**

Prove:

- `build_openai_llm(model_name, api_key)` passes the key to `ChatOpenAI` explicitly and never sets `os.environ["OPENAI_API_KEY"]`;
- OpenAI clients/embeddings/title generation accept an explicit key at every online call site;
- a thread graph is built only after the authenticated session has a credential;
- cached graphs are keyed by `(owner_user_id, thread_id)` and record which `session_id` supplied their credential;
- another owner cannot address the cache entry even with the same thread ID;
- switching session credentials rebuilds an idle graph rather than retaining the old session key;
- deleting a session key evicts its idle graphs;
- a running graph may finish with its already-bound key, then is evicted and cannot start a new provider call until a key is entered again.

The provider-global regression test must include:

```python
def test_build_openai_llm_passes_key_without_mutating_environment(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    captured: dict[str, object] = {}

    class FakeChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(llm_vllm, "ChatOpenAI", FakeChatOpenAI)
    llm_vllm.build_openai_llm(model_name="gpt-test", api_key="session-key")

    assert str(captured["api_key"]) == "**********"
    assert captured["api_key"].get_secret_value() == "session-key"
    assert "OPENAI_API_KEY" not in os.environ
```

Use these production types:

```python
@dataclass(frozen=True)
class GraphBuildContext:
    owner_user_id: str
    session_id: str
    thread_id: str
    provider_api_key: str
    storage: ThreadStorageScope

GraphFactory = Callable[[RuntimeSettings, GraphBuildContext], CompiledStateGraph]
```

`ThreadRuntime` retains `credential_session_id` but never exposes or logs the key. The key is passed into graph construction and not stored as a named runtime field.

- [ ] **Step 2: Run focused tests and observe failure**

```bash
.venv/bin/python -m pytest tests/test_llm_vllm.py tests/test_api_runtime.py tests/test_db_rag_dataset_naming.py tests/test_no_study_startup.py -q
```

Expected: signature failures and an assertion showing `build_openai_llm` currently mutates the process environment.

- [ ] **Step 3: Refactor provider construction**

Construct the chat model as:

```python
return ChatOpenAI(
    model=model_name,
    api_key=SecretStr(resolved_key),
    temperature=temperature,
    top_p=top_p,
)
```

Pass `api_key=` explicitly to `OpenAI`, embedding, title, and optional dataset-naming clients. Offline study-package/index build commands may read an environment key at their CLI boundary, but must immediately pass it as an argument; library functions cannot silently read or set it.

- [ ] **Step 4: Refactor runtime construction and cache lifecycle**

All resource methods take a `RequestIdentity` plus the explicit provider key only when they can trigger an LLM operation. Read-only history calls need identity but no key. Centralize this in three exact runtime methods: `_require_owned_thread(identity: RequestIdentity, thread_id: str) -> ThreadRuntime`, `_ensure_graph(identity: RequestIdentity, thread: ThreadRuntime, provider_api_key: str) -> None`, and `release_session(owner_user_id: str, session_id: str) -> None`.

`_require_owned_thread` first queries the owner-scoped history store; a mismatched owner receives the same not-found exception as a nonexistent thread. Protect cache changes with the existing runtime lock.

Build title generation per request/session with the same explicit key; remove the runtime-global `OpenAIConversationTitleGenerator` instance from `api/app.py`.

- [ ] **Step 5: Preserve native local mode**

Refactor `api/app.py` into an application factory that reads configuration at startup, not at module import, then:

- local mode uses `LocalTokenVerifier`, claims unowned legacy conversations for `local-user`, and seeds the `local-user`/fixed-local-session credential from the verified environment key;
- Cognito mode uses `CognitoTokenVerifier`, never claims unowned rows, and starts with an empty credential store.

Keep `app = build_application()` for Uvicorn compatibility.

- [ ] **Step 6: Run focused tests**

```bash
.venv/bin/python -m pytest tests/test_llm_vllm.py tests/test_api_runtime.py tests/test_no_study_startup.py tests/test_db_rag_dataset_naming.py -q
```

Expected: all pass and environment-mutation assertions remain green.

- [ ] **Step 7: Commit**

```bash
git add llm_vllm.py api/runtime.py api/conversation_history.py graph/builder.py api/app.py db_rag/vectorstore.py db_rag/service/dataset_naming.py db_rag/service/model_routing.py tests/test_llm_vllm.py tests/test_api_runtime.py tests/test_no_study_startup.py tests/test_db_rag_dataset_naming.py
git commit -m "refactor: bind provider clients to authenticated sessions"
```

---

## Task 7: Authorize every FastAPI operation at the owner boundary

**Files:**

- Modify: `api/server.py`
- Modify: `api/runtime.py`
- Modify: `tests/test_api_server.py`
- Create: `tests/test_api_multi_user_isolation.py`

**Interfaces:**

- Consumes: `RequestIdentity`, `ProviderCredentialStore`, and the owner-aware runtime from Tasks 2–6.
- Produces: a protected API contract in which identity is dependency-derived and every provider-triggering route resolves the exact session credential.

- [ ] **Step 1: Write an endpoint authorization matrix that fails**

Create two signed identities and parameterize every protected route category:

- conversations: list/create/open/rename/archive/restore/delete;
- thread state/runtime options/message submit/reset/export;
- attachment stage/read/delete;
- dataset preview/schema/provenance/download;
- analysis result;
- artifact body/table preview;
- interrupt resume.

For each ID-bearing read/mutation, create data as user A and prove user B receives 404 and causes no state or filesystem change. Also prove anonymous requests receive 401 and malformed session IDs receive 400 before runtime invocation.

Add an owner-aware recovery case to `tests/test_api_runtime.py`: create a checkpoint containing a human-review interrupt as user A, close the runtime/store, construct a new runtime over the same SQLite file, prove user A sees the same interrupt, and prove user B receives not-found when reading or resuming it. Reuse the existing real SQLite checkpointer fixtures rather than mocking checkpoint state.

Use one explicit test per route category; this is the required shape for each guessed-ID case:

```python
def test_other_user_cannot_download_guessed_attachment(
    multi_user_client: MultiUserClient,
) -> None:
    thread_id = multi_user_client.as_user("user-a").create_thread()
    attachment_id = multi_user_client.as_user("user-a").upload_text(
        thread_id,
        filename="synthetic.csv",
        content=b"person_id,value\n1,10\n",
    )

    response = multi_user_client.as_user("user-b").get(
        f"/api/threads/{thread_id}/attachments/{attachment_id}"
    )

    assert response.status_code == 404
    assert multi_user_client.as_user("user-a").get(
        f"/api/threads/{thread_id}/attachments/{attachment_id}"
    ).content == b"person_id,value\n1,10\n"
```

Define `MultiUserClient` in `tests/test_api_multi_user_isolation.py` as a test helper that signs the fixture RSA access tokens and always supplies that user's canonical session UUID; it must call the real FastAPI routes through `TestClient`, not call runtime methods directly.

- [ ] **Step 2: Run and observe failures**

```bash
.venv/bin/python -m pytest tests/test_api_multi_user_isolation.py tests/test_api_server.py -q
```

Expected: route/runtime fake signature failures until every handler supplies identity.

- [ ] **Step 3: Wire identity and credentials into every handler**

Each handler must receive `identity: RequestIdentity = Depends(require_identity)`. Never accept `owner_user_id`, Cognito `sub`, email, or session ID in a path/query/body.

For provider-triggering calls:

```python
provider_key = credentials.get(identity)
if provider_key is None:
    raise HTTPException(status_code=428, detail={"code": "PROVIDER_KEY_REQUIRED"})
return runtime.submit_message(identity, thread_id, request, provider_key=provider_key)
```

Creating an empty thread, listing history, opening checkpoints, reading state, and downloading existing files must work without a provider key. Starting/resuming work that can call the model must require one.

Convert owner mismatch to the existing resource-not-found response. Do not reveal that another user owns the ID.

- [ ] **Step 4: Run API suites**

```bash
.venv/bin/python -m pytest tests/test_api_multi_user_isolation.py tests/test_api_server.py tests/test_api_runtime.py -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add api/server.py api/runtime.py tests/test_api_server.py tests/test_api_multi_user_isolation.py
git commit -m "feat: enforce owner authorization across the API"
```

---

## Task 8: Add Cognito PKCE login and the provider-key gate to React

**Files:**

- Create: `frontend/src/authClient.ts`
- Create: `frontend/src/AuthGate.tsx`
- Create: `frontend/src/ProviderKeyGate.tsx`
- Create: `frontend/src/authClient.test.ts`
- Create: `frontend/src/AuthGate.test.tsx`
- Create: `frontend/src/ProviderKeyGate.test.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/apiClient.ts`
- Modify: `frontend/src/apiClient.test.ts`
- Modify: `frontend/src/main.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/styles.css`

**Interfaces:**

- Consumes: `PublicAppConfig` JSON and the three session-provider-key endpoints from Tasks 1 and 3.
- Produces: `AuthGate`, `ProviderKeyGate`, `createApiClient({apiBase, fetchImpl, getAccessToken, sessionId})`, and an injected authenticated client for `App`.

- [ ] **Step 1: Write failing auth-client and API-header tests**

Prove the browser:

- fetches `/api/public-config` before constructing Cognito settings;
- uses Authorization Code + PKCE through `oidc-client-ts`;
- keeps OIDC user state in `window.sessionStorage`, not local storage;
- creates a random session UUID once per browser tab and stores only that non-secret ID in session storage;
- adds `Authorization: Bearer <access_token>` and `X-Epi-Session-ID` to every protected request;
- local mode uses session ID `00000000-0000-4000-8000-000000000001` and no bearer token;
- does not put the OpenAI key in any browser storage, URL, query parameter, error text, or rendered post-submit state;
- signs out by deleting the provider key server-side before invoking Cognito sign-out.

Add this header assertion to `frontend/src/apiClient.test.ts`:

```typescript
it("adds the access token and tab session to protected requests", async () => {
  const fetchMock = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ items: [] }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  );
  const client = createApiClient({
    fetchImpl: fetchMock,
    getAccessToken: async () => "access-token",
    sessionId: "11111111-1111-4111-8111-111111111111",
  });

  await client.listConversations();

  const request = new Request(fetchMock.mock.calls[0][0], fetchMock.mock.calls[0][1]);
  expect(request.headers.get("Authorization")).toBe("Bearer access-token");
  expect(request.headers.get("X-Epi-Session-ID")).toBe(
    "11111111-1111-4111-8111-111111111111",
  );
});
```

Refactor the client factory to:

```typescript
interface CreateApiClientOptions {
  apiBase?: string;
  fetchImpl?: typeof fetch;
  getAccessToken?: () => Promise<string | null>;
  sessionId?: string;
}

export function createApiClient(options: CreateApiClientOptions = {}) {
  const authenticatedFetch: typeof fetch = async (input, init = {}) => {
    const headers = new Headers(init.headers);
    headers.set("X-Epi-Session-ID", sessionId);
    const token = await getAccessToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
    return fetchImpl(input, { ...init, headers });
  };
  // Existing methods use authenticatedFetch.
}
```

The public-config request itself must use raw `fetchImpl` because no identity exists yet.

- [ ] **Step 2: Write failing gate tests**

`AuthGate` states: loading, configuration error, signed out, callback processing, signed in. `ProviderKeyGate` states: checking, key form, validating, validation error, ready.

The key input must use `type="password"`, `autoComplete="off"`, and component-local state. Immediately after a successful PUT, set the controlled value to `""` before mounting `<App>`.

- [ ] **Step 3: Run and observe failure**

```bash
npm --prefix frontend test -- --run frontend/src/authClient.test.ts frontend/src/AuthGate.test.tsx frontend/src/ProviderKeyGate.test.tsx frontend/src/apiClient.test.ts frontend/src/App.test.tsx
```

Expected: missing modules and missing authenticated headers.

- [ ] **Step 4: Implement auth and key gates**

Use `WebStorageStateStore({ store: window.sessionStorage })` for OIDC state. Request only `openid email`; do not request Cognito admin scopes. Treat an expired token by removing the local OIDC user and returning to signed-out state; do not silently loop redirects.

Compose the app in `main.tsx` as:

```tsx
<AuthGate>
  {({ apiClient, user, signOut }) => (
    <ProviderKeyGate apiClient={apiClient} onSignOut={signOut}>
      <App apiClient={apiClient} authenticatedUser={user} onSignOut={signOut} />
    </ProviderKeyGate>
  )}
</AuthGate>
```

Change `App` to accept an injected authenticated client rather than creating a pre-auth client itself. Preserve `fetchImpl` injection through a test-only wrapper/helper so existing tests stay deterministic.

- [ ] **Step 5: Run frontend tests and build**

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: all tests and TypeScript build pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/authClient.ts frontend/src/AuthGate.tsx frontend/src/ProviderKeyGate.tsx frontend/src/authClient.test.ts frontend/src/AuthGate.test.tsx frontend/src/ProviderKeyGate.test.tsx frontend/src/types.ts frontend/src/apiClient.ts frontend/src/apiClient.test.ts frontend/src/main.tsx frontend/src/App.tsx frontend/src/App.test.tsx frontend/src/styles.css
git commit -m "feat: add Cognito login and user key entry flow"
```

---

## Task 9: Fetch protected images, downloads, and exports with authentication

**Files:**

- Create: `frontend/src/AuthenticatedArtifact.tsx`
- Create: `frontend/src/AuthenticatedArtifact.test.tsx`
- Modify: `frontend/src/apiClient.ts`
- Modify: `frontend/src/apiClient.test.ts`
- Modify: `frontend/src/MessageAttachment.tsx`
- Modify: `frontend/src/MessageAttachment.test.tsx`
- Modify: `frontend/src/ConversationMessage.tsx`
- Modify: `frontend/src/ConversationMessage.test.tsx`
- Modify: `frontend/src/AnalysisResultReview.tsx`
- Modify: `frontend/src/AnalysisResultReview.test.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/App.test.tsx`

**Interfaces:**

- Consumes: the authenticated API client from Task 8.
- Produces: authenticated blob methods and `AuthenticatedArtifact`, the only UI path for protected images and downloads.

- [ ] **Step 1: Write failing binary-resource tests**

Direct protected API URLs assigned to `<img src>` or `<a href>` cannot carry bearer/session headers. Tests must prove that attachment images, artifact figures, dataset downloads, table downloads, and thread ZIP exports use `authenticatedFetch` and not raw protected URLs.

Add client methods:

```typescript
fetchAttachmentBlob(threadId: string, attachmentId: string): Promise<Blob>
fetchArtifactBlob(threadId: string, artifactId: string): Promise<Blob>
fetchDatasetBlob(threadId: string, datasetId: string): Promise<Blob>
fetchThreadExportBlob(threadId: string): Promise<Blob>
```

Add a reusable hook/component that creates an object URL, revokes it on replacement/unmount, and renders loading/error states. Downloads must be initiated from an explicit click handler after the authenticated blob has loaded.

Add this regression assertion to `frontend/src/AuthenticatedArtifact.test.tsx`:

```tsx
it("creates and revokes an object URL for an authenticated image", async () => {
  const load = vi.fn().mockResolvedValue(new Blob(["image"], { type: "image/png" }));
  vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:authenticated-image");
  const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);

  const { unmount } = render(
    <AuthenticatedArtifact alt="result" load={load} mode="image" filename="result.png" />,
  );
  expect(await screen.findByAltText("result")).toHaveAttribute(
    "src",
    "blob:authenticated-image",
  );
  unmount();
  expect(revoke).toHaveBeenCalledWith("blob:authenticated-image");
});
```

- [ ] **Step 2: Run and observe the direct-URL failure**

```bash
npm --prefix frontend test -- --run frontend/src/AuthenticatedArtifact.test.tsx frontend/src/MessageAttachment.test.tsx frontend/src/ConversationMessage.test.tsx frontend/src/AnalysisResultReview.test.tsx frontend/src/apiClient.test.ts
```

Expected: missing authenticated blob methods and assertions showing current `src`/`href` values are protected API URLs.

- [ ] **Step 3: Implement authenticated object-URL rendering**

`AuthenticatedArtifact` must:

- accept a stable `load(): Promise<Blob>` callback;
- call `URL.createObjectURL(blob)` only after an authenticated response;
- call `URL.revokeObjectURL(url)` in effect cleanup;
- render `<img src={objectUrl}>` for images;
- create a temporary `<a download>` and call `.click()` for downloads;
- never attach tokens to URLs.

Implement its object-URL lifecycle with this exact effect:

```tsx
useEffect(() => {
  let active = true;
  let nextUrl: string | null = null;
  setStatus("loading");
  load()
    .then((blob) => {
      if (!active) return;
      nextUrl = URL.createObjectURL(blob);
      setObjectUrl(nextUrl);
      setStatus("ready");
    })
    .catch(() => {
      if (active) setStatus("error");
    });
  return () => {
    active = false;
    if (nextUrl) URL.revokeObjectURL(nextUrl);
  };
}, [load]);
```

Refactor `MessageAttachment`, `AnalysisResultReview`, and the export action to use these methods. Remove or make private the public `conversationAttachmentUrl`, `datasetDownloadUrl`, `artifactUrl`, and `threadExportUrl` methods so UI code cannot accidentally bypass authentication.

- [ ] **Step 4: Run frontend tests and build**

```bash
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: all pass; no protected API URL is assigned directly to an image or anchor.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/AuthenticatedArtifact.tsx frontend/src/AuthenticatedArtifact.test.tsx frontend/src/apiClient.ts frontend/src/apiClient.test.ts frontend/src/MessageAttachment.tsx frontend/src/MessageAttachment.test.tsx frontend/src/ConversationMessage.tsx frontend/src/ConversationMessage.test.tsx frontend/src/AnalysisResultReview.tsx frontend/src/AnalysisResultReview.test.tsx frontend/src/App.tsx frontend/src/App.test.tsx
git commit -m "fix: authenticate protected artifact downloads"
```

---

## Task 10: Preserve native startup and add the real multi-user smoke

**Files:**

- Modify: `run_fastapi.py`
- Modify: `tests/test_run_fastapi.py`
- Create: `scripts/smoke_multi_user_isolation_real.py`
- Create: `tests/test_smoke_multi_user_isolation_real.py`
- Modify: `README.md`
- Create: `docs/working-demo.md`
- Modify: `frontend/dist/index.html`
- Regenerate: `frontend/dist/assets/` with `npm --prefix frontend run build`
- Modify: `frontend/dist/build-manifest.json`

**Interfaces:**

- Consumes: the completed local/hosted application interfaces from Tasks 1–9.
- Produces: mode-aware native startup and `scripts/smoke_multi_user_isolation_real.py`, the executable acceptance test for this project.

- [ ] **Step 1: Write failing native-mode tests**

Prove the native launcher still:

- validates and persists the local `.env` key exactly as it does today;
- sets `REPORT_AGENT_AUTH_MODE=local` unless explicitly configured;
- starts with the fixed local identity/session and exposes prior claimed conversations;
- does not require Cognito variables;
- does not require Docker;
- does not put the verified key back into any API response.

The mode-branch test must assert both paths:

```python
def test_startup_prompts_only_in_local_mode(monkeypatch) -> None:
    local = {"REPORT_AGENT_AUTH_MODE": "local", "OPENAI_API_KEY": "key"}
    hosted = {
        "REPORT_AGENT_AUTH_MODE": "cognito",
        "REPORT_AGENT_AWS_REGION": "us-east-1",
        "REPORT_AGENT_COGNITO_USER_POOL_ID": "us-east-1_example",
        "REPORT_AGENT_COGNITO_APP_CLIENT_ID": "client-123",
        "REPORT_AGENT_AUTH_REDIRECT_URI": "https://demo.example/callback",
        "REPORT_AGENT_AUTH_POST_LOGOUT_REDIRECT_URI": "https://demo.example/",
    }
    verifier = Mock()

    prepare_provider_credentials(local, verifier=verifier)
    assert verifier.call_count == 1
    verifier.reset_mock()
    prepare_provider_credentials(hosted, verifier=verifier)
    verifier.assert_not_called()
```

Also test that Cognito mode skips the terminal key prompt and rejects missing Cognito configuration during application construction.

- [ ] **Step 2: Run and observe failure**

```bash
.venv/bin/python -m pytest tests/test_run_fastapi.py -q
```

Expected: hosted-mode assertions fail until startup branching is implemented.

- [ ] **Step 3: Implement mode-aware startup**

Keep local setup behavior for researchers. In hosted mode, do not call `ensure_active_provider_credential`; users enter their key after browser login. `validate_startup` must validate the correct requirements for the selected mode.

Implement the branch in a testable helper:

```python
def prepare_provider_credentials(
    environ: MutableMapping[str, str],
    *,
    verifier: Callable[..., None] = ensure_active_provider_credential,
) -> None:
    mode = str(environ.get("REPORT_AGENT_AUTH_MODE", "local")).strip().lower()
    if mode == "local":
        verifier(environ=environ)
        return
    if mode == "cognito":
        return
    raise StartupConfigurationError(
        "REPORT_AGENT_AUTH_MODE must be 'local' or 'cognito'."
    )
```

Reject `WEB_CONCURRENCY` or `REPORT_AGENT_WEB_CONCURRENCY` values other than `1` in Cognito mode. The in-memory credential/job registries require exactly one Uvicorn worker in this release.

- [ ] **Step 4: Build a bounded real smoke**

`scripts/smoke_multi_user_isolation_real.py` must:

1. create a temporary runtime directory and SQLite database;
2. start a local JWKS HTTP endpoint with an ephemeral RSA signing key;
3. start the real FastAPI app in Cognito mode against that issuer/JWKS URL;
4. mint two protocol-real RS256 access tokens with different `sub` claims and valid `client_id`, `token_use`, `iat`, and `exp`;
5. use two distinct session UUIDs;
6. submit a real OpenAI key from `OPENAI_API_KEY` to each user's credential endpoint and validate it against the real OpenAI service;
7. create one thread and upload one synthetic CSV as user A;
8. stop FastAPI, restart it over the same temporary SQLite/runtime paths, and prove user A's conversation/file survive while both users' provider-key status is now false;
9. re-enter user A's key and verify user A can list/reopen/download the data;
10. verify user B cannot list, read, mutate, download, resume, or export it even with guessed IDs;
11. verify no key text exists in SQLite, runtime files, HTTP response bodies captured by the script, or application logs;
12. stop both servers in `finally` and finish under five minutes.

The smoke uses a protocol-real local OIDC/JWKS issuer because this project intentionally creates no AWS resources. The later AWS-delivery project must add a second smoke against the real Cognito pool before launch.

If `OPENAI_API_KEY` is absent, exit nonzero with a clear prerequisite message; do not skip.

- [ ] **Step 5: Test the smoke's deterministic helpers**

```bash
.venv/bin/python -m pytest tests/test_smoke_multi_user_isolation_real.py tests/test_run_fastapi.py -q
```

Expected: pass without requiring the real provider; helper tests cover token construction, secret scanning, process cleanup, and timeout enforcement. They do not replace the real smoke.

- [ ] **Step 6: Run the real smoke**

```bash
.venv/bin/python scripts/smoke_multi_user_isolation_real.py
```

Expected: exits 0 within five minutes and prints a concise pass summary without key material.

- [ ] **Step 7: Document local and future hosted operation**

Update documentation with:

- local native startup remains `python run_fastapi.py` and Docker is not required;
- Cognito mode variables and BYOK behavior;
- invitation-only and synthetic/de-identified-only policy;
- keys disappear on logout, server restart, or 12-hour idle expiration and must be re-entered;
- conversations/checkpoints and artifacts survive restart under owner-specific storage;
- this plan does not yet make the public AWS URL available; infrastructure/deployment is the next project.

- [ ] **Step 8: Run the full verification gate**

```bash
.venv/bin/python -m pytest -q
npm --prefix frontend test
npm --prefix frontend run build
.venv/bin/python scripts/verify_working_demo_delivery.py --write-build-manifest
git diff --check
git status --short
```

Expected: all tests/build/manifest checks pass; `git diff --check` is silent; status contains only intended Task 10 files before commit.

- [ ] **Step 9: Commit**

```bash
git add run_fastapi.py tests/test_run_fastapi.py scripts/smoke_multi_user_isolation_real.py tests/test_smoke_multi_user_isolation_real.py README.md docs/working-demo.md frontend/dist
git commit -m "test: verify native and multi-user hosted foundations"
```

---

## Final Review Gate

- [ ] Confirm `git branch --show-current` prints `aws-test`.
- [ ] Confirm `git status --short` is clean apart from user-owned unrelated changes that were deliberately preserved.
- [ ] Confirm no API operation can choose its owner from client input.
- [ ] Confirm every thread query and mutation contains both owner and thread identity.
- [ ] Confirm every runtime file path starts below the hashed owner/thread scope.
- [ ] Confirm no provider client reads or writes a process-global key after application startup.
- [ ] Confirm the browser stores only OIDC/session metadata, never the OpenAI key.
- [ ] Confirm protected binary resources use authenticated fetch plus revocable object URLs.
- [ ] Confirm local native startup works without Docker.
- [ ] Confirm Cognito mode starts without a server-owned OpenAI key.
- [ ] Confirm the real smoke and full test/build gates pass from a clean checkout.

## Explicitly Deferred to the Next Plans

These are required for the overall AWS launch but are intentionally not mixed into this application-foundation plan:

1. **Hosted runtime foundation:** systemd units, Nginx, graceful drain, EBS mount/backup/restore, study-package installer, log redaction, health checks, and native Python process hardening on Amazon Linux.
2. **AWS delivery:** Terraform/CloudFormation for EC2, encrypted EBS, S3, Cognito invitation settings, CloudFront, DNS-validated origin certificate, CloudWatch, SSM, GitHub Actions OIDC, immutable releases, rollback, alarms, budgets, and the real Cognito/CloudFront smoke.
3. **Scale-out migration:** RDS/PostgreSQL, S3 artifact storage, a job queue, replaceable web nodes, and worker autoscaling. The owner/session/storage interfaces created here are the prerequisites for that evolution.
