import json
import os

os.environ.setdefault("DISCORD_APPLICATION_ID", "1")
os.environ.setdefault("DISCORD_PUBLIC_KEY", "00" * 32)

import pytest
from fastapi.testclient import TestClient

import bot.server as server

OWNER = "145616236955303936"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "verify_signature", lambda *a: True)
    calls = []

    async def fake_cleanup(*args):
        calls.append(("button", args))

    async def fake_audit(*args):
        calls.append(("audit", args))

    monkeypatch.setattr(server, "_run_cleanup_and_reply", fake_cleanup)
    monkeypatch.setattr(server, "_run_housekeeping_command", fake_audit)
    c = TestClient(server.app)
    c.calls = calls
    return c


def _post(client, payload):
    return client.post("/interactions", content=json.dumps(payload)).json()


def _button(user_id, guild=True):
    who = {"member": {"user": {"id": user_id}}} if guild else {"user": {"id": user_id}}
    return {"type": 3, "token": "t", "channel_id": "c", "data": {"custom_id": "fix_memory"}, **who}


def _audit(user_id):
    return {
        "type": 2, "token": "t", "channel_id": server.BOT_COMMANDS_CHANNEL,
        "data": {"name": "audit"}, "member": {"user": {"id": user_id}},
    }


def test_ping_needs_no_owner(client, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", "")
    assert _post(client, {"type": 1}) == {"type": 1}


def test_owner_button_runs(client, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", OWNER)
    assert _post(client, _button(OWNER)) == {"type": 5}
    assert [k for k, _ in client.calls] == ["button"]


def test_owner_button_in_dm_runs(client, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", f"999, {OWNER}")
    assert _post(client, _button(OWNER, guild=False)) == {"type": 5}


def test_non_owner_button_denied(client, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", OWNER)
    r = _post(client, _button("123"))
    assert r["type"] == 4 and r["data"]["content"] == "Not allowed." and r["data"]["flags"] == 64
    assert client.calls == []


def test_non_owner_slash_denied(client, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", OWNER)
    assert _post(client, _audit("123"))["data"]["content"] == "Not allowed."
    assert client.calls == []


def test_owner_slash_runs(client, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", OWNER)
    assert _post(client, _audit(OWNER)) == {"type": 5}
    assert [k for k, _ in client.calls] == ["audit"]


def test_empty_owner_list_denies_all(client, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", "")
    assert _post(client, _button(OWNER))["data"]["content"] == "Not allowed."
    assert client.calls == []


def test_missing_user_denied(client, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", OWNER)
    payload = _button(OWNER)
    del payload["member"]
    assert _post(client, payload)["data"]["content"] == "Not allowed."


def test_owner_ids_unset_denies_all(client, monkeypatch):
    monkeypatch.delenv("OWNER_IDS", raising=False)
    assert _post(client, _button(OWNER))["data"]["content"] == "Not allowed."
    assert client.calls == []


def test_member_without_user_ignores_top_level_user(client, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", OWNER)
    payload = _button(OWNER)
    payload["member"] = {}
    payload["user"] = {"id": OWNER}
    assert _post(client, payload)["data"]["content"] == "Not allowed."


@pytest.mark.parametrize("odd", [{"member": None, "user": {"id": "145616236955303936"}}, {"member": "x"}, {"member": {"user": "x"}}, {"user": None}, {"user": 5}])
def test_odd_user_shapes_denied(client, monkeypatch, odd):
    monkeypatch.setenv("OWNER_IDS", OWNER)
    payload = {"type": 3, "token": "t", "channel_id": "c", "data": {"custom_id": "fix_memory"}, **odd}
    assert _post(client, payload)["data"]["content"] == "Not allowed."
    assert client.calls == []


def test_null_data_from_non_owner_denied_not_500(client, monkeypatch):
    monkeypatch.setenv("OWNER_IDS", OWNER)
    r = client.post("/interactions", content=json.dumps({"type": 3, "data": None, "member": {"user": {"id": "1"}}}))
    assert r.status_code == 200 and r.json()["data"]["content"] == "Not allowed."
