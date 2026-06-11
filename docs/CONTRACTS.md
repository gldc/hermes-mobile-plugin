# Hermes Plugin Contracts — extracted from hermes-agent source

Source tree: `~/hermes-agent` (read-only reference).
All line numbers refer to that checkout as of 2026-06-11 (branch `main`,
HEAD `ad9012097`). Verbatim signatures are copied exactly from source.

This document is the authoritative contract reference for the
`hermes-mobile-plugin` repo. Everything the mobile plugin registers must
match these surfaces exactly.

---

## 1. PluginContext (`hermes_cli/plugins.py`)

`PluginContext` is the facade handed to each plugin's `register(ctx)`
function (class defined at `hermes_cli/plugins.py:290`).

### 1.1 `register_dashboard_auth_provider` — plugins.py:561

```python
def register_dashboard_auth_provider(self, provider) -> None:
```

- `provider` MUST be an instance of
  `hermes_cli.dashboard_auth.DashboardAuthProvider` (see §3). Internally
  delegates to `hermes_cli.dashboard_auth.register_provider(provider)`
  (plugins.py:574-586).
- **Failure semantics** (plugins.py:569-572): misbehaving providers
  (wrong type, duplicate `name`) are logged at WARNING and **silently
  ignored — never raised** — so a broken plugin cannot crash the host.
- On success the host logs `provider.name` and `provider.display_name`
  (plugins.py:594-597) — both attributes must be non-empty.
- The provider is used by the dashboard OAuth auth gate, which engages
  when the dashboard binds to a non-loopback host without `--insecure`
  (plugins.py:566-567). `web_server.start_server` sets
  `app.state.auth_required = should_require_auth(host, allow_public)`
  (web_server.py:10722) and **fails closed** if auth is required but no
  provider registered (web_server.py:10724-10729).

### 1.2 `register_cli_command` — plugins.py:390

```python
def register_cli_command(
    self,
    name: str,
    help: str,
    setup_fn: Callable,
    handler_fn: Callable | None = None,
    description: str = "",
) -> None:
```

- Registers a terminal subcommand, e.g. `hermes mobile ...`
  (plugins.py:398).
- `setup_fn` receives an **argparse subparser** and should add any
  arguments/sub-subparsers. If `handler_fn` is provided it is set as the
  default dispatch function via `set_defaults(func=...)`
  (plugins.py:400-402).
- Stored in `manager._cli_commands[name]` as a dict with keys
  `name, help, description, setup_fn, handler_fn, plugin`
  (plugins.py:403-410).
- Distinct from `register_command(name, handler, description, args_hint)`
  (plugins.py:415) which registers **in-session slash commands**
  (`/foo`) with handler signature `fn(raw_args: str) -> str | None`
  (sync or async); names conflicting with built-ins are rejected with a
  warning (plugins.py:424-439).

### 1.3 `register_platform` — plugins.py:770-822

```python
def register_platform(
    self,
    name: str,
    label: str,
    adapter_factory: Callable,
    check_fn: Callable,
    validate_config: Callable | None = None,
    required_env: list | None = None,
    install_hint: str = "",
    **entry_kwargs: Any,
) -> None:
```

- Docstring (plugins.py:781-800): "The adapter_factory receives a
  ``PlatformConfig`` and returns a ``BasePlatformAdapter`` subclass
  instance. The gateway calls ``check_fn()`` before instantiation to
  verify dependencies. Extra keyword arguments are forwarded to
  ``PlatformEntry`` (e.g. ``setup_fn``, ``emoji``, ``allowed_users_env``,
  ``platform_hint``). Unknown keys raise TypeError from the dataclass
  constructor."
- Canonical example from the docstring (plugins.py:793-800):

  ```python
  ctx.register_platform(
      name="irc",
      label="IRC",
      adapter_factory=lambda cfg: IRCAdapter(cfg),
      check_fn=lambda: True,
      emoji="💬",
      setup_fn=irc_interactive_setup,
  )
  ```

- Implementation builds a `gateway.platform_registry.PlatformEntry` with
  `source="plugin"` and `plugin_name` defaulting to the manifest name,
  then `platform_registry.register(entry)` (plugins.py:802-817).

#### PlatformEntry fields the factory/config plug into (`gateway/platform_registry.py:38-140+`)

```python
@dataclass
class PlatformEntry:
    name: str                                    # config.yaml identifier, e.g. "mobile"
    label: str                                   # human label
    adapter_factory: Callable[[Any], Any]        # PlatformConfig -> adapter instance
    check_fn: Callable[[], bool]                 # deps available?
    validate_config: Optional[Callable[[Any], bool]] = None
    is_connected: Optional[Callable[[Any], bool]] = None
    required_env: list = field(default_factory=list)
    install_hint: str = ""
    setup_fn: Optional[Callable[[], None]] = None    # interactive setup wizard
    source: str = "plugin"
    plugin_name: str = ""
    allowed_users_env: str = ""                  # e.g. "MOBILE_ALLOWED_USERS"
    allow_all_env: str = ""                      # e.g. "MOBILE_ALLOW_ALL_USERS"
    max_message_length: int = 0                  # 0 = no limit (smart-chunking)
    pii_safe: bool = False
    emoji: str = "🔌"
    allow_update_command: bool = True
    platform_hint: str = ""                      # injected into system prompt
    env_enablement_fn: Optional[Callable[[], Optional[dict]]] = None
    apply_yaml_config_fn: Optional[Callable[[dict, dict], Optional[dict]]] = None
    # plus: cron_deliver_env_var, standalone_sender_fn (see registry file past line 138
    # and ADDING_A_PLATFORM.md lines 32-39)
```

Real-world reference call: the bundled Discord plugin's `register(ctx)`
(`plugins/platforms/discord/adapter.py:6598-6630`) passes
`adapter_factory=_build_adapter, check_fn=check_discord_requirements,
is_connected=..., required_env=["DISCORD_BOT_TOKEN"], setup_fn=...,
apply_yaml_config_fn=..., allowed_users_env=..., allow_all_env=...,
cron_deliver_env_var=..., standalone_sender_fn=...,
max_message_length=2000`.

### 1.4 How/when `register(ctx)` is invoked for user plugins

- Layout contract (plugins.py module docstring, lines 10-20): user
  plugins live at `~/.hermes/plugins/<name>/` and "Each directory plugin
  must contain a ``plugin.yaml`` manifest **and** an ``__init__.py`` with
  a ``register(ctx)`` function."
- Discovery: `PluginManager.discover_and_load()` (plugins.py:1053) scans
  bundled (`<repo>/plugins/`), user (`get_hermes_home() / "plugins"`,
  plugins.py:1104-1109), project (`./.hermes/plugins/`, gated by env
  `HERMES_ENABLE_PROJECT_PLUGINS`, plugins.py:1111-1121), then pip
  entry-points.
- Precedence: later sources win on key collision — user plugins override
  bundled (plugins.py:1128-1139).
- **Gating** (plugins.py:1182-1213): bundled plugins with
  `kind in {"backend", "platform"}` auto-load; everything else —
  including **user-installed platform plugins in `~/.hermes/plugins/`** —
  is opt-in via the `plugins.enabled` config list (PluginManifest kind
  comment, plugins.py:258-262: "user-installed platform plugins in
  ~/.hermes/plugins/ still gated by ``plugins.enabled`` (untrusted
  code)"). Disabled plugins get
  `error = "not enabled in config (run `hermes plugins enable {key}` to activate)"`
  (plugins.py:1203-1207). Explicit `plugins.disabled` always wins
  (plugins.py:1143-1150).
- Loading: `_load_plugin(manifest)` (plugins.py:1432) imports the
  module (`_load_directory_module` for user/project/bundled), looks up
  `module.register`, then:

  ```python
  ctx = PluginContext(manifest, self)
  register_fn(ctx)
  ```

  (plugins.py:1454-1455). No `register()` attribute →
  `loaded.error = "no register() function"` warning (plugins.py:1450-1452).
- Loading happens once per process at startup (`discover_plugins()` is
  the module-level entry, plugins.py:1706). The same plugin is loaded by
  the CLI, the gateway, and the dashboard web server process — so a
  single `register(ctx)` should branch-register everything (auth
  provider, platform, CLI commands) and be safe when only a subset of
  the host surfaces is present.

---

## 2. Plugin discovery, `plugin.yaml`, and the dashboard `api` mount

### 2.1 `plugin.yaml` manifest (PluginManifest, plugins.py:236-269)

Parsed by `_parse_manifest` (plugins.py:1307-1396). All fields are
optional with defaults; `name` falls back to the directory name
(plugins.py:1324).

```yaml
name: mobile               # registry/display name (default: dir name)
kind: platform             # standalone (default) | backend | exclusive | platform | model-provider
version: 1.0.0
description: >
  ...
author: You
requires_env:              # list of strings OR rich dicts
  - name: MOBILE_PUSH_KEY
    description: "..."
    prompt: "..."
    url: "https://..."
    password: true
optional_env:              # same rich-dict shape (setup-wizard surface)
  - name: MOBILE_ALLOWED_USERS
    description: "..."
    prompt: "..."
    password: false
provides_tools: []         # informational
provides_hooks: []         # informational
```

Reference manifest: `plugins/platforms/discord/plugin.yaml` (kind:
platform, requires_env rich dicts with `name/description/prompt/url/
password`, optional_env with allowed-users / allow-all / home-channel
vars).

Notes:
- Unknown `kind` values fall back to `standalone` with a warning
  (plugins.py:1330-1336).
- The registry key is path-derived: a flat plugin at
  `~/.hermes/plugins/mobile/` has key `mobile`; nested category plugins
  get `category/name` (plugins.py:264-269, 1325).
- `kind: platform` for a USER plugin does NOT auto-load — still requires
  `hermes plugins enable mobile` (see §1.4).

### 2.2 Dashboard plugin manifest (`dashboard/manifest.json`) and API mount

The dashboard web server has a **separate** discovery pass,
`_discover_dashboard_plugins()` (web_server.py:~10140-10246). It scans:

1. `~/.hermes/plugins/<name>/dashboard/manifest.json` (source `user`)
2. bundled `<repo>/plugins/.../dashboard/manifest.json`
3. project `./.hermes/plugins/` only if `HERMES_ENABLE_PROJECT_PLUGINS`
   is truthy (web_server.py:10158-10173)

(NOTE: dashboard discovery is keyed purely off the presence of
`dashboard/manifest.json` — it does not check `plugins.enabled`. The
kanban plugin, for example, ships only `dashboard/manifest.json` +
`dashboard/plugin_api.py` with no top-level `plugin.yaml`.)

Manifest example (`plugins/kanban/dashboard/manifest.json`, verbatim):

```json
{
  "name": "kanban",
  "label": "Kanban",
  "description": "Multi-agent collaboration board — ...",
  "icon": "Package",
  "version": "1.0.0",
  "tab": {
    "path": "/kanban",
    "position": "after:skills"
  },
  "entry": "dist/index.js",
  "css": "dist/style.css",
  "api": "plugin_api.py"
}
```

Recognized fields (web_server.py:10184-10242): `name`, `label`,
`description`, `icon` (default "Puzzle"), `version`, `tab`
(`path` default `/{name}`, `position` default `"end"`, optional
`override`, `hidden`), `slots` (list of slot names), `entry` (default
`dist/index.js`), `css`, `api`.

**`api` field → FastAPI router mount** — `_mount_plugin_api_routes()`
(web_server.py:10623-10697):

- `api` must be a **relative path inside the plugin's `dashboard/`
  directory** (validated at discovery, web_server.py:10211-10227, and
  re-checked with `resolve().relative_to()` defence-in-depth at
  web_server.py:10651-10664). Absolute paths / `..` traversal are
  refused (RCE fix GHSA-5qr3-c538-wm9j).
- Backend import restricted to `bundled` and `user` sources; **project
  plugins' Python `api` is never auto-imported** (web_server.py:10630-10648).
- The file is imported as module `hermes_dashboard_plugin_{name}`
  (registered in `sys.modules` BEFORE `exec_module` so pydantic forward
  refs resolve, web_server.py:10669-10685) and must expose a
  module-level **`router`** attribute (a FastAPI `APIRouter`,
  web_server.py:10686-10688).
- Mounted with:

  ```python
  app.include_router(router, prefix=f"/api/plugins/{plugin['name']}")
  ```

  (web_server.py:10690) — i.e. routes land under `/api/plugins/<name>/...`.
  Mounting happens before the SPA catch-all (web_server.py:10696-10697).
- Any import/mount failure is logged at WARNING and skipped — it cannot
  crash the dashboard (web_server.py:10692-10693).

Reference implementation: `plugins/kanban/dashboard/plugin_api.py` —
declares `router = APIRouter()` (line 56) and plain
`@router.get("/board")`-style handlers; paths are relative to the
prefix.

#### Auth wrapping plugin routes

- **Loopback / `--insecure` mode**: plugin HTTP routes go through the
  dashboard's legacy session-token middleware (`web_server.auth_middleware`,
  per-process random `_SESSION_TOKEN`) just like core API routes — every
  `/api/plugins/...` request must present the session bearer token or the
  session cookie set when the dashboard HTML loads
  (plugin_api.py docstring, lines 14-33).
- **Gated (non-loopback) mode**: `gated_auth_middleware`
  (`hermes_cli/dashboard_auth/middleware.py:172`) engages when
  `app.state.auth_required` is True. `/api/plugins/...` is NOT in
  `PUBLIC_API_PATHS` / `_GATE_PUBLIC_PREFIXES` (middleware.py:38-50), so
  every plugin route requires a valid session.
- **Reading the verified session in a route handler**: the middleware
  attaches the verified `Session` to **`request.state.session`**
  (middleware.py:8-9, set at middleware.py:263 on the refresh path and
  middleware.py:309 on the verify path). A plugin handler does:

  ```python
  from fastapi import Request

  @router.get("/me")
  def me(request: Request):
      session = getattr(request.state, "session", None)  # None in loopback mode
      ...
  ```

- Unauthenticated `/api/*` requests get **401 JSON**
  `{"error": "unauthenticated"|"session_expired", "detail": "Unauthorized",
  "reason": ..., "login_url": "/login?next=..."}` (middleware.py:113-131);
  HTML routes get 302 → `/login`.
- **WebSockets**: browsers can't set Authorization on upgrade. Delegate
  to the canonical gate `hermes_cli.web_server._ws_auth_ok(ws)` which
  accepts legacy `?token=<_SESSION_TOKEN>` (loopback), single-use
  `?ticket=` (gated OAuth; browser SDK `buildWsUrl` mints one per
  connect), or process-lifetime `?internal=` — see
  `plugins/kanban/dashboard/plugin_api.py:64-94` for the exact pattern.

---

## 3. DashboardAuthProvider ABC (`hermes_cli/dashboard_auth/base.py`)

### 3.1 `Session` dataclass — base.py:9-25 (verbatim)

```python
@dataclass(frozen=True)
class Session:
    """A verified identity. Returned by ``complete_login`` and ``verify_session``.

    All fields are mandatory. Providers that don't have a concept of orgs
    should set ``org_id`` to an empty string. ``access_token`` and
    ``refresh_token`` are opaque to Hermes — provider-specific.
    """

    user_id: str
    email: str
    display_name: str
    org_id: str
    provider: str
    expires_at: int  # unix seconds; the access_token's exp claim
    access_token: str
    refresh_token: str
```

### 3.2 `LoginStart` — base.py:28-41

```python
@dataclass(frozen=True)
class LoginStart:
    redirect_url: str
    cookie_payload: dict[str, str]
```

Cookies set from `cookie_payload` MUST be HttpOnly + Secure (over HTTPS)
+ SameSite=Lax with TTL ≤ 10 minutes (base.py:35-37).

### 3.3 Exceptions — base.py:44-72

| Exception | Meaning | Middleware translation |
|---|---|---|
| `ProviderError` | IDP unreachable / transient | HTTP 503 |
| `InvalidCodeError` | OAuth callback code/state failed | HTTP 400 |
| `InvalidCredentialsError` | username/password rejected | HTTP 401 (generic — never a username oracle) |
| `RefreshExpiredError` | refresh token dead | clear cookies, 302 → `/login` |

### 3.4 Abstract surface — base.py:75-184

```python
class DashboardAuthProvider(ABC):
    name: str = ""           # lowercase identifier, stable forever — REQUIRED
    display_name: str = ""   # user-facing label on login page — REQUIRED
    supports_password: bool = False

    @abstractmethod
    def start_login(self, *, redirect_uri: str) -> LoginStart: ...

    @abstractmethod
    def complete_login(
        self,
        *,
        code: str,
        state: str,
        code_verifier: str,
        redirect_uri: str,
    ) -> Session: ...

    @abstractmethod
    def verify_session(self, *, access_token: str) -> Optional[Session]: ...

    @abstractmethod
    def refresh_session(self, *, refresh_token: str) -> Session: ...

    @abstractmethod
    def revoke_session(self, *, refresh_token: str) -> None: ...

    # non-abstract; only called when supports_password is True
    def complete_password_login(self, *, username: str, password: str) -> "Session": ...
```

Failure semantics (base.py:91-103, verbatim summary):
- `start_login` may raise `ProviderError` if the IDP is unreachable.
- `complete_login` raises `InvalidCodeError` on bad code/state;
  `ProviderError` if the IDP is unreachable.
- `verify_session` **returns `None`** on expiry / unknown token (MUST NOT
  raise for tokens it doesn't recognise — providers stack, middleware.py:194-197);
  raises `ProviderError` only when the IDP is unreachable. Middleware treats
  expiry → refresh, unreachable → 503.
- `refresh_session` raises `RefreshExpiredError` when the refresh token
  is also invalid (forces re-login); `ProviderError` on network failure.
- `revoke_session` is best-effort and **must not raise**.
- `complete_password_login` (base.py:156-184): raises
  `InvalidCredentialsError` (generic 401) or `ProviderError` (503);
  implementations SHOULD spend constant time on unknown users (dummy
  hash verify).

### 3.5 `assert_protocol_compliance(cls)` — base.py:187-220

Raises `TypeError` unless `cls` has non-empty `name` and `display_name`,
callable `start_login/complete_login/verify_session/refresh_session/
revoke_session`, and an empty `__abstractmethods__` set. Call it in the
plugin's unit tests:

```python
def test_protocol_compliance():
    assert_protocol_compliance(MyProvider)
```

### 3.6 Reference implementation — `plugins/dashboard_auth/basic/__init__.py`

The "basic" provider is the canonical pattern for a self-minting,
zero-infrastructure provider (the mobile plugin's session model can copy
this exactly):

- `class BasicAuthProvider(DashboardAuthProvider)` (line 201): `name =
  "basic"`, `display_name = "Username & Password"`,
  `supports_password = True`.
- **Token minting** (`_mint_session`, lines 293-312): stateless
  HMAC-SHA256-signed JSON blobs. Access token payload
  `{"sub": user_id, "kind": "access", "exp": now + ttl}` (default TTL
  12h, line 89); refresh token `{"sub", "kind": "refresh", "exp": now +
  30d}` (line 90). Signing: `base64.urlsafe_b64encode(raw_json +
  hmac_sha256(secret, raw_json))` with fixed-length binary sig suffix,
  no delimiter (`_sign`/`_unsign`, lines 176-193).
- `verify_session` (lines 263-271): unsign, check `kind == "access"` and
  `exp > now`, else return `None`.
- `refresh_session` (lines 273-283): unsign, check `kind == "refresh"`
  and unexpired, else raise `RefreshExpiredError`; on success mints a
  whole new Session (rotates both tokens).
- `revoke_session` (lines 285-289): stateless → no-op, never raises.
- **Password hashing** (`hash_password`, lines 115-136): stdlib
  `hashlib.scrypt` (n=2^14, r=8, p=1, dklen=32, 16-byte salt) producing
  `scrypt$n$r$p$<salt_b64>$<dk_b64>`; constant-time verify with a dummy
  hash for unknown usernames (lines 139-168, 244-259).
- **`register(ctx)` wiring** (lines 394-491): reads config section
  `dashboard.basic_auth` from config.yaml via
  `hermes_cli.config.load_config()/cfg_get` (lines 335-353) with
  **env-wins-over-config** resolution (lines 356-361):
  `HERMES_DASHBOARD_BASIC_AUTH_{USERNAME,PASSWORD_HASH,PASSWORD,SECRET,
  TTL_SECONDS}`. If not configured, it sets a module-level
  `LAST_SKIP_REASON` string and returns **without registering** (so the
  gate's fail-closed branch can explain why) — lines 107, 420-439.
  Finally calls `ctx.register_dashboard_auth_provider(provider)`
  (line 487).
- Secret resolution (lines 364-391): base64 → hex → raw-UTF-8 fallback;
  if unset, random per-process secret (sessions don't survive restart).

---

## 4. BasePlatformAdapter (`gateway/platforms/base.py`)

Class at base.py:1793. Constructor (base.py:1826):

```python
def __init__(self, config: PlatformConfig, platform: Platform):
```

(`self.config`, `self.platform` stored; plugin platform names become
dynamic `Platform` enum members via `Platform._missing_` —
gateway/config.py:136-189, so `Platform("mobile")` works once the plugin
platform is registered/bundled.)

### 4.1 Abstract methods (exactly four)

```python
@abstractmethod
async def connect(self) -> bool:                  # base.py:2243-2250
    """Connect to the platform and start receiving messages.
    Returns True if connection was successful."""

@abstractmethod
async def disconnect(self) -> None:               # base.py:2252-2255
    """Disconnect from the platform."""

@abstractmethod
async def send(                                   # base.py:2257-2277
    self,
    chat_id: str,
    content: str,
    reply_to: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None
) -> SendResult:
    """Send a message to a chat.
    Args:
        chat_id: The chat/channel ID to send to
        content: Message content (may be markdown)
        reply_to: Optional message ID to reply to
        metadata: Additional platform-specific options
    Returns:
        SendResult with success status and message ID
    """

@abstractmethod
async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:   # base.py:4708-4717
    """Get information about a chat/channel.
    Returns dict with at least:
    - name: Chat name
    - type: "dm", "group", "channel"
    """
```

### 4.2 `SendResult` — base.py:1541-1562

```python
@dataclass
class SendResult:
    """Result of sending a message."""
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    raw_response: Any = None
    retryable: bool = False  # True for transient connection errors — base will retry automatically
    continuation_message_ids: tuple = ()
```

### 4.3 Key non-abstract surface (override as needed)

- `edit_message(chat_id, message_id, content, *, finalize=False) -> SendResult`
  (base.py:2314) — optional; return `success=False` if unsupported and
  callers fall back to sending a new message.
- `send_typing(chat_id, metadata=None)` / `stop_typing(chat_id)`
  (base.py:2534, 2543), `send_image`, `send_voice`, `send_video`,
  `send_document`, `send_image_file`, `send_animation`,
  `send_multiple_images` (base.py:2551-2781) — default stubs exist.
- `create_handoff_thread(parent_chat_id, name) -> Optional[str]`
  (base.py:2287) — return `None` if threads unsupported.
- Inbound flow: build a `MessageEvent` (base.py:1412, with `text`,
  `message_type: MessageType` (base.py:1391), `source: SessionSource`
  via `self.build_source(...)`) and dispatch with
  `self.handle_message(event)`.
- Capability flags: `supports_code_blocks: bool = False` (base.py:1812),
  `typed_command_prefix: str = "/"` (base.py:1824),
  `REQUIRES_EDIT_FINALIZE: bool = False` (base.py:2285),
  `message_len_fn` property (base.py:1897), `format_message(content)`
  (base.py:4719, default identity).
- Helpers exported by base: `cache_image_from_bytes`,
  `cache_audio_from_bytes`, `cache_document_from_bytes`,
  `cache_media_bytes` (base.py:1335), `validate_media_delivery_path`
  (base.py:1028), `resolve_proxy_url`, `utf16_len`.

### 4.4 `PlatformConfig` the factory receives — gateway/config.py:317-377

```python
@dataclass
class PlatformConfig:
    """Configuration for a single messaging platform."""
    enabled: bool = False
    token: Optional[str] = None        # Bot token (Telegram, Discord)
    api_key: Optional[str] = None      # API key if different from token
    home_channel: Optional[HomeChannel] = None
    reply_to_mode: str = "first"       # "off" | "first" | "all"
    gateway_restart_notification: bool = True
    extra: Dict[str, Any] = field(default_factory=dict)   # platform-specific settings
```

All platform-specific settings go in `extra` — seed it from env via
`PlatformEntry.env_enablement_fn` or from YAML via
`apply_yaml_config_fn` (see §1.3 and ADDING_A_PLATFORM.md:17-39).

### 4.5 ADDING_A_PLATFORM.md highlights (`gateway/platforms/ADDING_A_PLATFORM.md`)

- Plugin path (lines 5-15): plugin dir in `~/.hermes/plugins/` with
  `plugin.yaml` + `adapter.py`; adapter inherits `BasePlatformAdapter`,
  registers via `ctx.register_platform()` in `register(ctx)`. "Zero
  changes to core Hermes code." The plugin system handles adapter
  creation, config parsing, user authorization, cron delivery,
  send_message routing, prompt hints, status display, gateway setup.
- Optional hooks (lines 17-42): `env_enablement_fn`,
  `apply_yaml_config_fn`, `cron_deliver_env_var`,
  `standalone_sender_fn`, and rich `requires_env`/`optional_env`
  manifest entries that feed the setup wizard.
- Method table (lines 75-95): required `__init__`/`connect`/`disconnect`
  /`send`/`send_typing`/`send_image`/`get_chat_info`; optional
  `send_document`/`send_voice`/`send_video`/`send_animation`/
  `send_image_file`.
- Patterns (lines 104-114): use `self.build_source(...)`, dispatch via
  `self.handle_message(event)`, filter self-messages, redact sensitive
  ids in logs, reconnect with exponential backoff + jitter, set
  `MAX_MESSAGE_LENGTH` when the platform caps message size.
- Working examples: `plugins/platforms/irc/`, `plugins/platforms/teams/`,
  `plugins/platforms/google_chat/`, `plugins/platforms/line/`.

---

## 5. Session refresh in the auth middleware — the mobile QR bootstrap path

`hermes_cli/dashboard_auth/middleware.py`, `gated_auth_middleware`
(lines 172-310). Cookie names (`hermes_cli/dashboard_auth/cookies.py:67-68`):

```python
SESSION_AT_COOKIE = "hermes_session_at"
SESSION_RT_COOKIE = "hermes_session_rt"
```

(`__Secure-`/`__Host-` prefixed variants are also read via
`_read_with_fallback`, cookies.py:228-231.)

**Confirmed: a request carrying ONLY the refresh-token cookie triggers
`provider.refresh_session`.** Exact flow:

1. `at, _rt = read_session_cookies(request)` (middleware.py:188). If
   neither token is present → 401/redirect (middleware.py:189-192).
2. The verify loop only runs `if at:` (middleware.py:209). The code
   comments this explicitly (middleware.py:199-207): "When the
   access-token cookie is absent but a refresh-token cookie is present,
   skip verification and go straight to the refresh path below. This is
   the COMMON expiry case... the access-token cookie is set with
   ``Max-Age = access_token_expires_in`` (~15 min)... while the
   refresh-token cookie lives for 30 days."
3. With `session is None`, `_attempt_refresh(request, refresh_token=_rt)`
   (middleware.py:260, function at 326-367) iterates registered
   providers calling

   ```python
   new_session = provider.refresh_session(refresh_token=refresh_token)
   ```

   (middleware.py:342). First provider returning a Session wins;
   `RefreshExpiredError` stops the chain (an RT belongs to exactly one
   provider) and forces re-login; `ProviderError` logs and forces clean
   re-login (middleware.py:343-364).
4. On success the middleware sets `request.state.session = new_session`,
   serves the request, and **re-sets rotated cookies** on the response
   via `set_session_cookies(response, access_token=...,
   refresh_token=..., access_token_expires_in=max(60, exp-now),
   use_https=detect_https(request), prefix=prefix_from_request(request))`
   (middleware.py:261-290). Writing the rotated RT back is mandatory —
   stale RT replay can revoke the whole session (middleware.py:265-269).
5. If refresh fails, dead cookies are cleared and the client is forced
   to `/login` (middleware.py:292-307).

**Mobile implication**: a mobile device that receives a QR-delivered
refresh token can bootstrap a full session by sending one request with
only `Cookie: hermes_session_rt=<token>` (no access token) to any
non-public dashboard path. The middleware will call the registered
provider's `refresh_session`, attach the verified Session to
`request.state.session`, and return rotated `hermes_session_at` +
`hermes_session_rt` cookies in the response. The mobile auth provider's
`refresh_session` therefore IS the device-login endpoint — it must mint
the QR refresh tokens in a format its own `refresh_session` accepts
(the `basic` provider's HMAC-blob scheme in §3.6 is the model), and its
`verify_session` must return `None` (never raise) for tokens minted by
other providers.

---

## Appendix: contract checklist for the mobile plugin

- [ ] `~/.hermes/plugins/mobile/plugin.yaml` (`kind: platform` if it
      ships a gateway adapter) + `__init__.py` with `register(ctx)`.
- [ ] User must run `hermes plugins enable mobile` (user plugins are
      opt-in, §1.4).
- [ ] `register(ctx)` calls `ctx.register_dashboard_auth_provider(...)`
      (skip-with-reason when unconfigured, like `basic`), optionally
      `ctx.register_platform(...)` and `ctx.register_cli_command(...)`.
- [ ] `dashboard/manifest.json` with `"api": "plugin_api.py"`;
      `plugin_api.py` exposes module-level `router = APIRouter()`;
      routes mount at `/api/plugins/mobile/...` and read
      `request.state.session` (None in loopback mode).
- [ ] Auth provider: subclass `DashboardAuthProvider`, frozen `Session`
      with all 8 fields, exact failure semantics of §3.4, unit test with
      `assert_protocol_compliance`.
- [ ] Platform adapter: subclass `BasePlatformAdapter`, implement
      `connect/disconnect/send/get_chat_info`, return `SendResult`,
      factory signature `(PlatformConfig) -> adapter`.
