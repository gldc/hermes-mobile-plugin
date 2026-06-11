# hermes-mobile-plugin

Server-side companion plugin that lets the (separate) Hermes mobile app
talk to a running [hermes-agent](https://github.com/NousResearch/hermes-agent)
over a **private network** (Tailscale/VPN). Works against stock
hermes-agent — no fork changes, nothing exposed to the public internet.

## What it provides

| Surface | What it does |
| --- | --- |
| **Auth provider** (`mobile-device`) | Per-device dashboard sessions: ~15-minute access tokens, 30-day rotating refresh tokens, SHA-256 hashes only at rest, refresh-token **reuse detection** (a replayed rotated-out token revokes the whole device). Devices live in `~/.hermes/mobile/devices.json`. |
| **CLI** (`hermes mobile`) | `pair` (mint a device + QR), `devices` (list), `revoke <device_id>`. |
| **Platform adapter** (`mobile`) | Makes a paired phone a `send_message`/cron-delivery target: messages append to a per-device mailbox (`~/.hermes/mobile/mailbox/<device_id>.jsonl`) and fire a **redacted** Expo push ("New message from Hermes"). |
| **Dashboard API** (`/api/plugins/mobile/…`) | `POST /push-token` (register the device's Expo push token), `GET /mailbox` (return + drain queued messages), `GET /me` (device self-info). These routes require a `mobile-device` session — other providers' sessions get 403. |
| **Memory API** (`/api/plugins/mobile/memory/…`) | CRUD for hermes' built-in memory files `MEMORY.md` / `USER.md` (`~/.hermes/memories/`): `GET /memory/files` (list with size/mtime), `GET /memory/files/{name}` (read), `PUT /memory/files/{name}` (atomic full-file replace, ≤ 256 KiB). Open to **any** authenticated dashboard session (browser or device); file names are matched against a fixed allowlist and never path-joined. Full contract: [`docs/MEMORY_API.md`](docs/MEMORY_API.md). |

## Install

```sh
git clone https://github.com/gldc/hermes-mobile-plugin
cd hermes-mobile-plugin
./install.sh                      # symlinks the repo into ~/.hermes/plugins/hermes-mobile
hermes plugins enable hermes-mobile   # user plugins are opt-in
```

`install.sh` is idempotent and refuses to overwrite anything at
`~/.hermes/plugins/hermes-mobile` that is not a symlink.

Optional: `pip install qrcode` for terminal QR rendering during pairing
(without it, `hermes mobile pair` prints the JSON payload to copy
manually).

### Uninstall

```sh
hermes mobile devices            # note any paired devices
hermes mobile revoke <device_id> # revoke each device (recommended)
rm ~/.hermes/plugins/hermes-mobile
rm -rf ~/.hermes/mobile          # device records + mailboxes (optional)
```

## Pairing walkthrough

1. **Bind the dashboard to your tailnet.** Run the dashboard on the
   Tailscale interface (a non-loopback bind engages gated auth mode, so
   every request needs a valid session). The default pairing port is
   `9119`.
2. **Mint a device** on the gateway host:

   ```sh
   hermes mobile pair --name "my-iphone"
   # explicit URL if auto-detection picks the wrong interface:
   hermes mobile pair --name "my-iphone" --url http://100.x.y.z:9119
   ```

   This prints (and QR-encodes) a JSON payload:
   `{"url": "...", "rt": "<refresh token>", "device_id": "..."}`.
3. **Scan the QR with the mobile app** (or paste the JSON). The app
   stores the credentials in the iOS Keychain and bootstraps a session
   by sending one request with only the `hermes_session_rt` cookie —
   the auth middleware calls this plugin's `refresh_session`, which
   rotates the token and returns fresh `hermes_session_at` +
   `hermes_session_rt` cookies.
4. **Done.** The app registers its Expo push token via
   `POST /api/plugins/mobile/push-token` and syncs agent-initiated
   messages from `GET /api/plugins/mobile/mailbox`.

Re-pairing (run `pair` again, revoke the old device) is the recovery
path for an expired or revoked refresh token.

### Sending to the phone

Once paired, the device is a normal platform target — e.g. cron jobs or
`send_message` with platform `mobile` and `chat_id = <device_id>`.

## Security notes

- **Private-network only.** The gateway is never exposed publicly:
  bind the dashboard to the Tailscale/VPN interface. Transport
  encryption comes from WireGuard; nothing here weakens the dashboard's
  gated auth mode (it adds a provider, it doesn't bypass the gate).
- **The pairing QR contains a live secret.** The printed JSON embeds a
  working 30-day refresh token. Treat the QR/terminal output like a
  password; the CLI warns accordingly. Anyone who captures it can act
  as that device until you `hermes mobile revoke <device_id>`.
- **Tokens are hashed at rest.** `devices.json` stores SHA-256 hashes
  only, written atomically with `0600` permissions. Refresh tokens
  rotate on every use, and replaying a rotated-out token revokes the
  device (stolen-token containment).
- **Push is redacted by default.** Notification payloads transit Expo
  and APNs, so the adapter sends only "New message from Hermes" — never
  message content. The mailbox (fetched over the VPN) is the source of
  truth; push is a best-effort "go look" signal, and push failures
  never block delivery.
- **Per-device blast radius.** Each phone has its own credential chain;
  revoking one device touches nothing else.

## Development

```sh
# hermes-agent source checkout required on PYTHONPATH (read-only):
PYTHONPATH=/path/to/hermes-agent python -m pytest tests/ -q
```

Layout: `hermes_mobile/` (device_store, auth_provider, cli, push,
mailbox, adapter, plugin_api), `dashboard/` (manifest + router shim the
dashboard web server imports), root `__init__.py` + `plugin.yaml`
(hermes directory-plugin entry point), `docs/CONTRACTS.md` (the exact
hermes surfaces this plugin builds against), `docs/MEMORY_API.md` (the
memory-file route contract the mobile app consumes).
