"""Smoke tests: config validation, schema, scoring, downtime maths."""
import copy, os, sys, tempfile, time, yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import config, scoring
from app.db import Database

raw = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml"), encoding="utf-8"))
raw["guild"]["id"] = 111111111111111111
raw["guild"]["admin_channel_id"] = 222222222222222222
raw["guild"]["audit_channel_id"] = 333333333333333333

def write(data):
    fd, path = tempfile.mkstemp(suffix=".yaml"); os.close(fd)
    open(path, "w", encoding="utf-8").write(yaml.safe_dump(data))
    return path

cfg = config.load(write(raw))
print("PASS  valid config loads, scan_time =", cfg.schedule.scan_time)

def must_fail(mutate, label):
    bad = copy.deepcopy(raw); mutate(bad)
    try:
        config.load(write(bad))
    except Exception as exc:
        print(f"PASS  {label}: {str(exc).splitlines()[-1][:90]}")
    else:
        print(f"FAIL  {label} was accepted")

must_fail(lambda d: d["thresholds"].update(stage_2_days=95), "unordered thresholds")
must_fail(lambda d: d["guild"].update(admin_channel_id=1234), "non-snowflake channel id")
must_fail(lambda d: d["messages"].update(kick_dm="Hallo {nickname}"), "unknown placeholder")
must_fail(lambda d: d["backfill"].update(lookback_days=30), "backfill shorter than stage 3")
must_fail(lambda d: d["logging"].update(to_discord=True), "logs to discord")
must_fail(lambda d: d["guild"].update(audit_channel_id=d["guild"]["admin_channel_id"]), "same channel twice")
must_fail(lambda d: d["scoring"]["weights"].pop("rejoin"), "missing weight")
must_fail(lambda d: d.update(unknown_section={"a": 1}), "unknown config section")

# --- scoring -------------------------------------------------------------
now = int(time.time())
half = 30
s1 = scoring.bumped(0.0, now, now, half, 1.0)
s2 = scoring.decayed(s1, now, now + 30 * 86400, half)
assert abs(s2 - 0.5) < 1e-9, s2
print(f"PASS  one message decays from {s1:.2f} to {s2:.2f} after one half-life")

s3 = scoring.bumped(s1, now, now + 30 * 86400, half, 1.0)
assert abs(s3 - 1.5) < 1e-9, s3
print(f"PASS  decay-then-add order correct: {s3:.2f}")

assert scoring.stage_for(89, 30, 60, 90) == 2
assert scoring.stage_for(90, 30, 60, 90) == 3
assert scoring.stage_for(0, 30, 60, 90) == 0
print("PASS  stage boundaries")

# --- database ------------------------------------------------------------
db = Database(tempfile.mkdtemp() + "/activity.db")
G, U = 111111111111111111, 999999999999999999
db.upsert_member(G, U, now - 100 * 86400)
db.touch(G, U, "message", now - 95 * 86400, 1.0)

# 10 days of downtime inside the inactive window must not count
db.downtime_add(now - 20 * 86400, now - 10 * 86400)
row = db.get(G, U)
downtime = db.downtime_seconds_between(row.last_activity_at, now)
assert abs(downtime - 10 * 86400) < 5, downtime
inactive_days = scoring.effective_inactive_seconds(row.last_activity_at, now, downtime) / 86400
assert 84.9 < inactive_days < 85.1, inactive_days
print(f"PASS  95 days quiet minus 10 days downtime = {inactive_days:.1f} effective days")

db.set_stage(G, U, 2); db.set_notified_stage(G, U, 2)
db.touch(G, U, "heartbeat", now, 6.0)
row = db.get(G, U)
assert row.stage == 0 and row.notified_stage == 0
print("PASS  any activity resets stage and notified_stage")

db.mark_left(G, U, now)
row = db.get(G, U)
assert row.last_message_at is None and row.score == 0 and row.rejoin_count == 0
assert row.first_joined_at is not None
print("PASS  leaving wipes timestamps but keeps first_joined_at and counters")

db.record_rejoin(G, U, now, 3.0)
row = db.get(G, U)
assert row.rejoin_count == 1 and row.left_at is None and row.joined_at == now
print("PASS  rejoin restarts the clock and increments the counter")

assert db.purge_left(now + 1) == 0, "present member must not be purged"
db.mark_left(G, U, now - 200 * 86400)
assert db.purge_left(now - 180 * 86400) == 1
print("PASS  rows of long-gone members are purged")

db.voice_open(G, U, now - 3600)
assert db.voice_close(G, U) == now - 3600
assert db.voice_close(G, U) is None
print("PASS  voice session open/close")
