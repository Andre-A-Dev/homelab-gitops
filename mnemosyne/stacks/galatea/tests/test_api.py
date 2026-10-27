"""Route-level tests against mocked devices."""

import pytest

from app.clients import prusalink, shelly
from app.clients.prusalink import PrinterSnapshot
from app.main import app

AUTH = {"X-Galatea-Token": "test-token"}


@pytest.fixture
def client():
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def plug(monkeypatch):
    state = {"on": True, "switched": []}
    monkeypatch.setattr(shelly, "get_plug", lambda: {"on": state["on"], "power_w": 12.0,
                                                    "voltage_v": 230, "current_a": 0.05, "energy_kwh": 1.0})
    monkeypatch.setattr(shelly, "set_plug", lambda on: state["switched"].append(on))
    return state


def printer(monkeypatch, **kw):
    snap = PrinterSnapshot(reachable=kw.pop("reachable", True), **kw)
    calls = {"fresh": []}

    def get_status(fresh=False):
        calls["fresh"].append(fresh)
        return snap
    monkeypatch.setattr(prusalink, "get_status", get_status)
    return calls


# --- Auth --------------------------------------------------------------------

def test_api_requires_token(client):
    assert client.get("/api/filament").status_code == 401
    assert client.get("/api/filament", headers={"X-Galatea-Token": "wrong"}).status_code == 401


def test_probes_and_page_are_open(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200
    assert client.get("/").status_code == 200


# --- Interlock wiring --------------------------------------------------------

def test_power_off_blocked_while_printing(client, plug, monkeypatch):
    calls = printer(monkeypatch, state="PRINTING", nozzle_c=215.0, job_id=7)
    r = client.post("/api/plug/off", json={}, headers=AUTH)
    assert r.status_code == 409 and r.get_json()["reason"] == "print_active"
    assert plug["switched"] == []
    assert calls["fresh"] == [True], "safety decision must use a fresh status"


def test_power_off_blocked_when_hot_after_print(client, plug, monkeypatch):
    printer(monkeypatch, state="FINISHED", nozzle_c=120.0)
    r = client.post("/api/plug/off", json={}, headers=AUTH)
    assert r.status_code == 409 and r.get_json()["reason"] == "hotend_hot"
    assert plug["switched"] == []


def test_power_off_blocked_when_printer_unreachable(client, plug, monkeypatch):
    printer(monkeypatch, reachable=False)
    r = client.post("/api/plug/off", json={}, headers=AUTH)
    assert r.status_code == 409 and r.get_json()["reason"] == "printer_unreachable"
    assert plug["switched"] == []


def test_power_off_allowed_when_idle_and_cool(client, plug, monkeypatch):
    printer(monkeypatch, state="IDLE", nozzle_c=24.0)
    r = client.post("/api/plug/off", json={}, headers=AUTH)
    assert r.status_code == 200
    assert plug["switched"] == [False]


def test_force_bypasses_interlock(client, plug, monkeypatch):
    printer(monkeypatch, state="PRINTING", nozzle_c=215.0, job_id=7)
    r = client.post("/api/plug/off", json={"force": True}, headers=AUTH)
    assert r.status_code == 200 and plug["switched"] == [False]


def test_force_must_be_boolean_true(client, plug, monkeypatch):
    printer(monkeypatch, state="PRINTING", nozzle_c=215.0, job_id=7)
    r = client.post("/api/plug/off", json={"force": "yes"}, headers=AUTH)
    assert r.status_code == 409 and plug["switched"] == []


def test_power_off_when_already_off_is_noop(client, plug, monkeypatch):
    plug["on"] = False
    printer(monkeypatch, reachable=False)
    r = client.post("/api/plug/off", json={}, headers=AUTH)
    assert r.status_code == 200 and plug["switched"] == []


# --- Printer commands --------------------------------------------------------

def test_stop_requires_confirmation(client, monkeypatch):
    printer(monkeypatch, state="PRINTING", job_id=7)
    assert client.post("/api/printer/stop", json={}, headers=AUTH).status_code == 400


def test_pause_sends_job_id(client, monkeypatch):
    printer(monkeypatch, state="PRINTING", job_id=7)
    sent = []
    monkeypatch.setattr(prusalink, "pause", lambda job_id: sent.append(job_id) or 204)
    r = client.post("/api/printer/pause", headers=AUTH)
    assert r.status_code == 200 and sent == [7]


def test_resume_refused_during_attention(client, monkeypatch):
    printer(monkeypatch, state="ATTENTION", job_id=7)
    monkeypatch.setattr(prusalink, "resume", lambda job_id: pytest.fail("must not be called"))
    r = client.post("/api/printer/resume", headers=AUTH)
    assert r.status_code == 409


def test_stop_attempted_during_attention(client, monkeypatch):
    printer(monkeypatch, state="ATTENTION", job_id=7)
    sent = []
    monkeypatch.setattr(prusalink, "stop", lambda job_id: sent.append(job_id) or 204)
    r = client.post("/api/printer/stop", json={"confirm": True}, headers=AUTH)
    assert r.status_code == 200 and sent == [7]


def test_state_change_race_reported(client, monkeypatch):
    printer(monkeypatch, state="PRINTING", job_id=7)
    monkeypatch.setattr(prusalink, "pause", lambda job_id: 409)
    assert client.post("/api/printer/pause", headers=AUTH).status_code == 409


# --- Filament ----------------------------------------------------------------

def test_filament_crud_and_single_active(client):
    a = client.post("/api/filament", json={"material": "PETG", "color": "Black", "total_weight_g": 1000}, headers=AUTH)
    b = client.post("/api/filament", json={"material": "PLA", "color": "White", "total_weight_g": "750"}, headers=AUTH)
    assert a.status_code == b.status_code == 201
    assert a.get_json()["remaining_g"] == 1000

    ida, idb = a.get_json()["id"], b.get_json()["id"]
    client.patch(f"/api/filament/{ida}", json={"is_active": True}, headers=AUTH)
    client.patch(f"/api/filament/{idb}", json={"is_active": True}, headers=AUTH)
    spools = {s["id"]: s for s in client.get("/api/filament", headers=AUTH).get_json()}
    assert spools[idb]["is_active"] and not spools[ida]["is_active"]

    assert client.patch(f"/api/filament/{ida}", json={"remaining_g": -5}, headers=AUTH).status_code == 400
    assert client.delete(f"/api/filament/{ida}", headers=AUTH).status_code == 200
    assert client.delete(f"/api/filament/{ida}", headers=AUTH).status_code == 404


def test_filament_rejects_missing_fields(client):
    r = client.post("/api/filament", json={"material": "PETG"}, headers=AUTH)
    assert r.status_code == 400
