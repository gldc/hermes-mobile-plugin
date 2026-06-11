# Memory file API — `/api/plugins/mobile/memory/...`

Exact route contract for the dashboard plugin's memory CRUD surface
(implemented in `hermes_mobile/plugin_api.py`, mounted by the dashboard
web server at prefix `/api/plugins/mobile`).

## Auth

Any **authenticated dashboard session** may call these routes — they are
NOT restricted to the `mobile-device` auth provider (unlike
`/push-token`, `/mailbox`, `/me`).

- **Gated (non-loopback) mode**: send the normal dashboard session
  cookies (`hermes_session_at` / `hermes_session_rt`). The host's
  `gated_auth_middleware` rejects unauthenticated calls with **401**
  before the route runs. Any provider's session works (GitHub OAuth,
  `basic`, `mobile-device`, ...).
- **Loopback / `--insecure` mode**: send the legacy per-process session
  bearer token / cookie, like every other `/api/...` route.

The handlers themselves perform no further identity checks.

## File allowlist

Exactly two file names are valid, case-sensitive:

- `MEMORY.md`
- `USER.md`

They live at `<hermes home>/memories/<name>` (hermes home =
`$HERMES_HOME` or `~/.hermes`). The `{name}` URL segment is **never used
as a path component** — it is matched against the fixed allowlist and
any other value returns **404** with
`{"detail": "unknown memory file (allowed: MEMORY.md, USER.md)"}`.

---

## `GET /api/plugins/mobile/memory/files`

List the editable memory files. Always returns **both** allowlisted
names, in order `MEMORY.md`, `USER.md`, whether or not the files exist
yet.

**200 response**

```json
{
  "files": [
    {"name": "MEMORY.md", "size": 1234, "mtime": 1765432100.5, "exists": true},
    {"name": "USER.md",   "size": 0,    "mtime": 0,            "exists": false}
  ]
}
```

- `size` — bytes on disk (`0` if the file does not exist).
- `mtime` — Unix seconds as a float (`0` if the file does not exist).
- `exists` — whether the file currently exists on disk.

**Errors**: `503 {"detail": "memory store unavailable"}` on filesystem
errors other than not-found.

---

## `GET /api/plugins/mobile/memory/files/{name}`

Read one memory file.

**200 response**

```json
{"name": "MEMORY.md", "content": "# Memory\n..."}
```

- A missing-but-allowlisted file returns `content: ""` with **200**
  (so an editor can open a blank document; the file is created on first
  PUT). There is no 404 for allowlisted names.

**Errors**

- `404` — `{name}` not in the allowlist.
- `503 {"detail": "memory store unavailable"}` — filesystem read error.

---

## `PUT /api/plugins/mobile/memory/files/{name}`

Full-file replace. Creates the file (and the `memories/` directory) if
absent.

**Request body** (JSON)

```json
{"content": "entire new file content as a UTF-8 string"}
```

`content` is required and must be a string (FastAPI/pydantic returns
**422** for a missing/non-string field).

**Semantics**

- **Size cap**: the UTF-8 **encoding** of `content` must be
  ≤ **262144 bytes** (256 KiB, `MEMORY_FILE_MAX_BYTES`). Exactly 262144
  is accepted; 262145 is rejected. Note the cap counts bytes, not
  characters.
- **Atomic**: content is written to a temp file in the same directory
  (mode `0600`), fsynced, then `os.replace`d over the target. Readers
  never observe a partial file; on failure the old content is intact.

**200 response**

```json
{"ok": true, "name": "MEMORY.md", "size": 42}
```

- `size` — bytes written (UTF-8 length of `content`).

**Errors**

- `404` — `{name}` not in the allowlist (nothing is written).
- `413 {"detail": "content exceeds 262144 bytes"}` — over the size cap
  (nothing is written).
- `422` — malformed body (missing/non-string `content`).
- `503 {"detail": "memory store unavailable"}` — filesystem write error.

---

## Error envelope notes for the app

- Plugin-route errors raised by the handlers use FastAPI's standard
  `{"detail": "..."}` shape.
- Auth failures in gated mode are produced by the host middleware
  *before* the route and use the dashboard envelope:
  `401 {"error": "unauthenticated"|"session_expired", "detail": "Unauthorized", "reason": ..., "login_url": "/login?next=..."}`.

## Quick examples

```sh
# list
curl -b cookies.txt https://host:9119/api/plugins/mobile/memory/files

# read
curl -b cookies.txt https://host:9119/api/plugins/mobile/memory/files/USER.md

# write
curl -b cookies.txt -X PUT \
  -H 'Content-Type: application/json' \
  -d '{"content": "# User\n- name: Gianluca\n"}' \
  https://host:9119/api/plugins/mobile/memory/files/USER.md
```
