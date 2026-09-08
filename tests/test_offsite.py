"""The off-site copy: pass selection, the rclone invocation, and what a run
leaves behind for the status page and the agent."""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from timelapsed import telemetry
from timelapsed.offsite import (
    STATUS_FILENAME,
    OffsiteCopy,
    read_key_file,
    rclone_environment,
)

NOW = datetime(2026, 9, 8, 2, 0, tzinfo=timezone.utc)

FAKE_RCLONE = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_RCLONE_LOG"], "a") as log:
    log.write(json.dumps({{"args": args, "tz": os.environ.get("TZ"),
                           "config": os.environ.get("RCLONE_CONFIG")}}) + "\\n")
if args[0] == "size":
    print(json.dumps({{"count": 42, "bytes": 4200, "sizeless": 0}}))
    sys.exit(0)
print(json.dumps({{"level": "info", "msg": "starting", "time": "t"}}))
print(json.dumps({{"level": "info", "msg": "stats", "time": "t",
                  "stats": {{"bytes": 1024, "totalBytes": 4096, "speed": 512.0, "eta": 6}}}}))
print("a line that is not json at all")
print(json.dumps({{"level": "error", "msg": "one refused upload", "time": "t"}}))
print(json.dumps({{"level": "info", "msg": "stats", "time": "t",
                  "stats": {{"bytes": 4096, "totalBytes": 4096, "speed": 512.0, "eta": 0}}}}))
sys.exit(int(os.environ.get("FAKE_RCLONE_EXIT", "0")))
"""


@pytest.fixture
def fake_rclone(tmp_path):
    script = tmp_path / "rclone"
    script.write_text(FAKE_RCLONE)
    script.chmod(0o755)
    return script


@pytest.fixture
def metrics(monkeypatch):
    recorded: list[tuple[str, float]] = []
    monkeypatch.setattr(telemetry, "record_metric", lambda name, value: recorded.append((name, value)))
    return recorded


def make_copy(tmp_path, fake_rclone, exit_code=0, now=NOW):
    root = tmp_path / "archive"
    root.mkdir(exist_ok=True)
    environment = {
        **os.environ,
        "FAKE_RCLONE_LOG": str(tmp_path / "calls.log"),
        "FAKE_RCLONE_EXIT": str(exit_code),
        "TZ": "America/Sao_Paulo",
        "RCLONE_CONFIG": "/dev/null",
    }
    return OffsiteCopy(
        root=root, remote="b2:timelapsed/archive", state_dir=tmp_path / "state",
        environment=environment, rclone=str(fake_rclone), now=lambda: now,
    )


def calls(tmp_path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "calls.log").read_text().splitlines()]


def status(copy: OffsiteCopy) -> dict:
    return json.loads((copy.root / STATUS_FILENAME).read_text())


# --- the key file ---------------------------------------------------------

def test_the_key_file_names_the_bucket_after_the_key_by_default(tmp_path):
    key = tmp_path / "backblaze.cfg"
    key.write_text("# comment\nkeyID= 004abc\nkeyName = timelapsed\napplicationKey=K004xyz \n")

    credentials = read_key_file(key)

    assert credentials is not None
    assert (credentials.key_id, credentials.application_key) == ("004abc", "K004xyz")
    assert credentials.bucket == "timelapsed"


def test_an_explicit_bucket_wins_over_the_key_name(tmp_path):
    key = tmp_path / "backblaze.cfg"
    key.write_text("keyID=a\nkeyName=timelapsed\napplicationKey=b\nbucket=footage\n")

    credentials = read_key_file(key)

    assert credentials is not None and credentials.bucket == "footage"


def test_no_key_file_means_nothing_to_do_and_a_half_key_is_an_error(tmp_path):
    assert read_key_file(tmp_path / "missing.cfg") is None
    (tmp_path / "half.cfg").write_text("keyID=a\n")
    with pytest.raises(ValueError):
        read_key_file(tmp_path / "half.cfg")


def test_the_remote_is_defined_by_environment_alone(tmp_path):
    from timelapsed.offsite import Credentials

    env = rclone_environment(Credentials("id", "secret", "timelapsed"), tmp_path, "America/Sao_Paulo")

    assert env["RCLONE_CONFIG"] == "/dev/null"
    assert env["RCLONE_CONFIG_B2_TYPE"] == "b2"
    assert (env["RCLONE_CONFIG_B2_ACCOUNT"], env["RCLONE_CONFIG_B2_KEY"]) == ("id", "secret")
    assert env["XDG_CACHE_HOME"] == str(tmp_path / "cache")
    assert env["TZ"] == "America/Sao_Paulo"


# --- which pass ---------------------------------------------------------------

def test_the_first_run_is_a_full_backfill_and_the_next_hour_is_a_tail(tmp_path, fake_rclone, metrics):
    first = make_copy(tmp_path, fake_rclone)

    assert first.run() is True
    assert (first.pass_, first.phase) == ("full", "steady")
    report = status(first)
    assert report["state"] == "ok" and report["pass"] == "full" and report["phase"] == "steady"
    assert (report["remote_objects"], report["remote_bytes"]) == (42, 4200)
    assert report["full_finished_at"] is not None and report["progress"] is None

    copy_call, size_call = calls(tmp_path)
    assert copy_call["args"][:4] == ["copy", str(first.root), "b2:timelapsed/archive", "--filter"]
    assert "--fast-list" in copy_call["args"] and "--check-first" in copy_call["args"]
    assert copy_call["tz"] == "America/Sao_Paulo" and copy_call["config"] == "/dev/null"
    assert size_call["args"][:2] == ["size", "--json"]

    second = make_copy(tmp_path, fake_rclone, now=NOW + timedelta(hours=1))
    assert second.run() is True
    assert (second.pass_, second.phase) == ("tail", "steady")
    tail_args = calls(tmp_path)[2]["args"]
    filters = [tail_args[i + 1] for i, flag in enumerate(tail_args) if flag == "--filter"]
    # Exclusions first, then the two newest UTC days in, then everything out.
    assert filters == ["- .*", "- *.tmp", "- lost+found/**", "+ /*/20260908/**", "+ /*/20260907/**", "- **"]
    assert "--fast-list" not in tail_args
    # A tail pass measures nothing: the bucket numbers are the last full pass's.
    assert len(calls(tmp_path)) == 3
    assert status(second)["remote_objects"] == 42


def test_a_day_old_full_pass_brings_another(tmp_path, fake_rclone, metrics):
    copy = make_copy(tmp_path, fake_rclone)
    copy.state_dir.mkdir()
    copy.full_stamp.touch()
    stale = time.time() - 21 * 3600
    os.utime(copy.full_stamp, (stale, stale))

    later = make_copy(tmp_path, fake_rclone, now=datetime.now(tz=timezone.utc))
    assert later.choose_pass() == "full"
    assert later.phase == "steady"
    assert later.choose_pass(force_full=False) == "full"


def test_the_shared_flags_shape_every_pass(tmp_path, fake_rclone):
    copy = make_copy(tmp_path, fake_rclone)
    copy.choose_pass()
    command = copy.command()

    assert command[-8:] == ["--use-json-log", "--log-level", "NOTICE", "--stats", "10m",
                            "--stats-one-line", "--stats-log-level", "NOTICE"]
    assert command[command.index("--bwlimit") + 1] == "07:00,8M 23:00,14M"
    assert command[command.index("--retries") + 1] == "1"
    assert command[command.index("--order-by") + 1] == "name,ascending"


# --- what a run leaves behind --------------------------------------------------

def test_stats_lines_become_progress_and_uploaded_bytes(tmp_path, fake_rclone, metrics):
    copy = make_copy(tmp_path, fake_rclone)
    copy.choose_pass()

    copy.handle_line(json.dumps({"msg": "stats", "stats": {
        "bytes": 1024, "totalBytes": 4096, "speed": 512.0, "eta": 6,
    }}))
    assert status(copy)["progress"] == "1.0 KiB / 4.0 KiB, 25%, 512 B/s, ETA 0m6s"
    assert status(copy)["state"] == "running"

    copy.handle_line(json.dumps({"msg": "stats", "stats": {
        "bytes": 4096, "totalBytes": 4096, "speed": 512.0, "eta": 0,
    }}))
    copy.handle_line("garbage that is not json")
    copy.handle_line(json.dumps({"level": "error", "msg": "one refused upload"}))

    # Deltas, so a sum over time is throughput; the ratio is the backfill gauge.
    assert [m for m in metrics if m[0] == "Custom/offsite/bytes_uploaded"] == [
        ("Custom/offsite/bytes_uploaded", 1024.0), ("Custom/offsite/bytes_uploaded", 3072.0)]
    assert [m[1] for m in metrics if m[0] == "Custom/offsite/progress_ratio"] == [0.25, 1.0]


def test_a_finished_run_reports_its_verdict_to_the_agent(tmp_path, fake_rclone, metrics):
    copy = make_copy(tmp_path, fake_rclone)

    copy.run()

    names = dict(metrics)
    assert names["Custom/offsite/failed_runs"] == 0
    assert names["Custom/offsite/remote_bytes"] == 4200 and names["Custom/offsite/remote_objects"] == 42
    assert "Custom/offsite/run_seconds" in names


def test_an_rclone_failure_is_a_failed_run_with_no_stamp(tmp_path, fake_rclone, metrics):
    copy = make_copy(tmp_path, fake_rclone, exit_code=1)

    assert copy.run() is False

    report = status(copy)
    assert report["state"] == "failed" and report["errors"] == 1
    assert report["phase"] == "backfill" and report["full_finished_at"] is None
    assert not copy.full_stamp.exists()
    assert dict(metrics)["Custom/offsite/failed_runs"] == 1
    # The next run tries the full pass again rather than declaring steady state.
    again = make_copy(tmp_path, fake_rclone, exit_code=1)
    assert again.choose_pass() == "full" and again.phase == "backfill"


def test_the_status_file_lands_atomically_and_world_readable(tmp_path, fake_rclone):
    copy = make_copy(tmp_path, fake_rclone)
    copy.choose_pass()

    copy.write_status("running")

    path: Path = copy.root / STATUS_FILENAME
    assert path.exists() and not (copy.root / (STATUS_FILENAME + ".tmp")).exists()
    assert path.stat().st_mode & 0o777 == 0o644
    assert json.loads(path.read_text())["written_at"] == "2026-09-08T02:00:00+00:00"
