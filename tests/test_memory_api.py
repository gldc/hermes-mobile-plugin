"""Tests for the /api/plugins/mobile/memory/... CRUD routes.

Same harness as test_plugin_api.py: mount the router exactly like the
dashboard web server does and attach a Session (or None) via middleware.
Memory routes accept ANY authenticated dashboard session — including
loopback mode where the host's legacy session-token middleware is the
gate and ``request.state.session`` is None — so unlike the device
routes there is no 403 provider check to exercise here.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli.dashboard_auth import Session

from hermes_mobile import plugin_api
from hermes_mobile.plugin_api import MEMORY_FILE_MAX_BYTES


def make_session(user_id: str, provider: str = "github") -> Session:
    return Session(
        user_id=user_id,
        email="dev",
        display_name="dev",
        org_id="",
        provider=provider,
        expires_at=2**31,
        access_token="at",
        refresh_token="rt",
    )


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a tmp dir; routes resolve <home>/memories."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(hermes_home):
    # No configure(memories_dir=...) override: default_memories_dir()
    # must resolve through the tmp HERMES_HOME.
    plugin_api.configure()
    app = FastAPI()

    @app.middleware("http")
    async def attach_session(request, call_next):
        request.state.session = getattr(app.state, "test_session", None)
        return await call_next(request)

    app.include_router(plugin_api.router, prefix="/api/plugins/mobile")
    app.state.test_session = make_session("github:123")
    try:
        yield TestClient(app)
    finally:
        plugin_api.configure()


BASE = "/api/plugins/mobile/memory/files"


# ---------------------------------------------------------------------------
# allowlist rejection — {name} is never path-joined
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "SOUL.md",
        "memory.md",  # case-sensitive
        "..%2F..%2Fconfig.yaml",
        "%2e%2e%2fMEMORY.md",
        "MEMORY.md.bak",
        "%2e",  # decodes to "." as the path param; allowlist rejects
    ],
)
def test_non_allowlisted_names_404(client, hermes_home, name):
    assert client.get(f"{BASE}/{name}").status_code == 404
    assert client.put(f"{BASE}/{name}", json={"content": "x"}).status_code == 404
    # Nothing was written anywhere under the tmp home.
    mem_dir = hermes_home / "memories"
    assert not mem_dir.exists() or list(mem_dir.iterdir()) == []


def test_traversal_name_cannot_escape(client, hermes_home):
    resp = client.put(f"{BASE}/..%2F..%2Fescape.md", json={"content": "pwned"})
    assert resp.status_code == 404
    assert not list(hermes_home.rglob("escape.md"))


# ---------------------------------------------------------------------------
# round-trip read/write against a tmp HERMES_HOME
# ---------------------------------------------------------------------------


def test_list_before_any_write(client):
    resp = client.get(BASE)
    assert resp.status_code == 200
    files = resp.json()["files"]
    assert [f["name"] for f in files] == ["MEMORY.md", "USER.md"]
    assert all(f["size"] == 0 and not f["exists"] for f in files)


def test_read_missing_allowlisted_file_is_empty(client):
    resp = client.get(f"{BASE}/MEMORY.md")
    assert resp.status_code == 200
    assert resp.json() == {"name": "MEMORY.md", "content": ""}


@pytest.mark.parametrize("name", ["MEMORY.md", "USER.md"])
def test_write_read_roundtrip(client, hermes_home, name):
    content = f"# {name}\n\n- likes umlauts: äöü\n"
    resp = client.put(f"{BASE}/{name}", json={"content": content})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["name"] == name
    assert body["size"] == len(content.encode("utf-8"))

    # On disk, under the tmp HERMES_HOME, with private perms.
    path = hermes_home / "memories" / name
    assert path.read_text(encoding="utf-8") == content
    assert (path.stat().st_mode & 0o777) == 0o600

    # Back through the API.
    assert client.get(f"{BASE}/{name}").json() == {"name": name, "content": content}

    listed = {f["name"]: f for f in client.get(BASE).json()["files"]}
    assert listed[name]["exists"] is True
    assert listed[name]["size"] == len(content.encode("utf-8"))
    assert listed[name]["mtime"] == pytest.approx(path.stat().st_mtime)


def test_overwrite_replaces_content_atomically(client, hermes_home):
    client.put(f"{BASE}/USER.md", json={"content": "v1"})
    client.put(f"{BASE}/USER.md", json={"content": "v2"})
    assert client.get(f"{BASE}/USER.md").json()["content"] == "v2"
    # No tmp-file droppings left behind by os.replace.
    leftovers = [p for p in (hermes_home / "memories").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_write_works_in_loopback_mode_without_session(client):
    """Loopback/--insecure: request.state.session is None but the host's
    session-token middleware already authenticated the call."""
    client.app.state.test_session = None
    assert client.put(f"{BASE}/MEMORY.md", json={"content": "ok"}).status_code == 200
    assert client.get(f"{BASE}/MEMORY.md").json()["content"] == "ok"


# ---------------------------------------------------------------------------
# size cap
# ---------------------------------------------------------------------------


def test_size_cap_rejects_oversize(client, hermes_home):
    too_big = "x" * (MEMORY_FILE_MAX_BYTES + 1)
    resp = client.put(f"{BASE}/MEMORY.md", json={"content": too_big})
    assert resp.status_code == 413
    assert not (hermes_home / "memories" / "MEMORY.md").exists()


def test_size_cap_measures_utf8_bytes_not_chars(client):
    # 3-byte chars: char count under the cap, byte count over it.
    over = "€" * (MEMORY_FILE_MAX_BYTES // 3 + 1)
    assert client.put(f"{BASE}/USER.md", json={"content": over}).status_code == 413


def test_size_cap_boundary_exactly_max_ok(client, hermes_home):
    exact = "x" * MEMORY_FILE_MAX_BYTES
    resp = client.put(f"{BASE}/MEMORY.md", json={"content": exact})
    assert resp.status_code == 200
    assert (
        hermes_home / "memories" / "MEMORY.md"
    ).stat().st_size == MEMORY_FILE_MAX_BYTES


# ---------------------------------------------------------------------------
# configure() override (used by other tests / embedding)
# ---------------------------------------------------------------------------


def test_configure_memories_dir_override(tmp_path):
    plugin_api.configure(memories_dir=tmp_path / "elsewhere")
    try:
        app = FastAPI()
        app.include_router(plugin_api.router, prefix="/api/plugins/mobile")
        c = TestClient(app)
        assert c.put(f"{BASE}/USER.md", json={"content": "hi"}).status_code == 200
        assert (tmp_path / "elsewhere" / "USER.md").read_text() == "hi"
    finally:
        plugin_api.configure()
