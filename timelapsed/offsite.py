"""Off-site copy of the footage archive to Backblaze B2.

Runs from timelapsed-offsite.timer, hourly, as root -- the key file is
root-only, like the New Relic one. Always `rclone copy`, never `sync`: the
archiver's reclaim deletes the oldest local days when the volume hits its
floor, and the whole point of this copy is that those days survive it.

B2 bills every listing, authorisation and upload-URL fetch as a Class C
transaction -- 2,500 a day free, cents per thousand after, and a daily cap in
the console that refuses everything once reached -- while the uploads
themselves are free. So the shape here is "as few invocations and listings as
possible", not "as fresh as possible":

* A FULL pass lists both sides once (`--fast-list`: one call per thousand
  objects) and copies whatever is missing, channel by channel in date order.
  The first one is the backfill; after that, one a day.
* A TAIL pass, every other hour, lists only today's and yesterday's day
  directories and copies what landed in them -- about forty calls.

Late arrivals into old days (the archiver fetches NVR history oldest-first
too) ride the next full pass. Progress goes to STATUS_FILENAME beside the
archive -- at start, on every rclone stats line, and at the end -- which the
status page reads, and to New Relic as Custom/offsite/ metrics plus one
transaction per finished run. `timelapsed-offsite full` forces a full pass;
`timelapsed-offsite rclone <args>` runs rclone with the same credentials and
remote, for restores and spot checks. See docs/Operations.md.
"""
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from timelapsed import telemetry
from timelapsed.config import get_config

logger = logging.getLogger(__name__)

# Beside the archive root. system_status reads it under this name.
STATUS_FILENAME = ".offsite-status.json"
DEFAULT_KEY_FILE = Path("/etc/backblaze.cfg")
DEFAULT_STATE_DIR = Path("/var/lib/timelapsed/offsite")
# Between full passes: daily, with slack so the hourly grid catches it.
FULL_EVERY = timedelta(hours=20)
# rclone reads this in local time; run() exports TZ from [timelapse] timezone
# so "night" is the household's night on the uplink, not UTC's.
BANDWIDTH_TIMETABLE = "07:00,8M 23:00,14M"
STATS_EVERY = "10m"
# The daemons' own status and scratch files, and ext4's lost+found.
EXCLUDES = ("- .*", "- *.tmp", "- lost+found/**")


@dataclass(frozen=True)
class Credentials:
    key_id: str
    application_key: str
    bucket: str


def read_key_file(path: Path) -> Credentials | None:
    """keyID= / applicationKey= / keyName= [/ bucket=] lines; None when absent.

    Absence is the off switch: the timer fires everywhere, and a host with no
    key file has nothing to copy and nothing to complain about.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    key_id = values.get("keyID", "")
    application_key = values.get("applicationKey", "")
    if not key_id or not application_key:
        raise ValueError(f"{path} needs keyID= and applicationKey=")
    return Credentials(
        key_id=key_id,
        application_key=application_key,
        bucket=values.get("bucket") or values.get("keyName") or "timelapsed",
    )


def rclone_environment(credentials: Credentials, state_dir: Path, timezone_name: str) -> dict[str, str]:
    """The remote defined entirely by environment: no rclone.conf read or written."""
    return {
        **os.environ,
        "RCLONE_CONFIG": "/dev/null",
        "RCLONE_CONFIG_B2_TYPE": "b2",
        "RCLONE_CONFIG_B2_ACCOUNT": credentials.key_id,
        "RCLONE_CONFIG_B2_KEY": credentials.application_key,
        # The unit's ProtectHome hides /root.
        "XDG_CACHE_HOME": str(state_dir / "cache"),
        "TZ": timezone_name,
    }


def _size(value: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds >= 86400:
        return f"{seconds // 86400}d{seconds % 86400 // 3600}h"
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60}m"
    return f"{seconds // 60}m{seconds % 60}s"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class OffsiteCopy:
    """One run: choose the pass, drive rclone, keep the status file honest."""

    def __init__(
        self,
        root: Path,
        remote: str,
        state_dir: Path,
        environment: dict[str, str],
        rclone: str = "rclone",
        now: Callable[[], datetime] = lambda: datetime.now(tz=timezone.utc),
    ) -> None:
        self.root = root
        self.remote = remote
        self.state_dir = state_dir
        self.environment = environment
        self.rclone = rclone
        self.now = now
        self.full_stamp = state_dir / "full.stamp"    # mtime: last clean full pass
        self.size_cache = state_dir / "size.json"     # `rclone size --json` after it
        self.pass_ = "tail"
        self.phase = "steady"
        self.started_at = now()
        self.errors = 0
        self._reported_bytes = 0

    # --- what to do -------------------------------------------------------

    def choose_pass(self, force_full: bool = False) -> str:
        """full when nothing has ever completed, when the last one is a day
        old, or when asked; tail otherwise. The phase is backfill until the
        first full pass has finished cleanly."""
        try:
            finished = datetime.fromtimestamp(self.full_stamp.stat().st_mtime, tz=timezone.utc)
        except OSError:
            self.phase = "backfill"
            self.pass_ = "full"
            return self.pass_
        self.phase = "steady"
        self.pass_ = "full" if force_full or self.now() - finished >= FULL_EVERY else "tail"
        return self.pass_

    def command(self) -> list[str]:
        """The rclone invocation for the chosen pass.

        Filters are one ordered list: the daemons' dotfiles and scratch out
        first, then, for a tail pass, only the two newest day directories in
        and everything else out. Segments land by rename, so nothing is ever
        half-written; --min-age is belt and braces. --retries 1 because a
        retry re-lists, and the failures worth retrying are the ones rclone
        already retries at the request level.
        """
        command = [self.rclone, "copy", str(self.root), self.remote]
        for rule in EXCLUDES:
            command += ["--filter", rule]
        if self.pass_ == "full":
            command += ["--fast-list", "--check-first", "--order-by", "name,ascending"]
        else:
            today = self.now().astimezone(timezone.utc)
            for day in (today, today - timedelta(days=1)):
                command += ["--filter", f"+ /*/{day:%Y%m%d}/**"]
            command += ["--filter", "- **"]
        # Segments are ~8 MB and the path to B2 is long: one upload stream
        # settles around a megabyte a second, so the cap is only reachable
        # with a dozen in flight. Measured 3 MiB/s at four transfers.
        command += [
            "--min-age", "2m",
            "--transfers", "12", "--checkers", "16",
            "--bwlimit", BANDWIDTH_TIMETABLE,
            "--retries", "1", "--low-level-retries", "10",
            "--use-json-log", "--log-level", "NOTICE",
            "--stats", STATS_EVERY, "--stats-one-line", "--stats-log-level", "NOTICE",
        ]
        return command

    # --- the run ----------------------------------------------------------

    def run(self, force_full: bool = False) -> bool:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.choose_pass(force_full)
        self.write_status("running")
        logger.info("%s pass: %s -> %s", self.pass_, self.root, self.remote)

        process = subprocess.Popen(
            self.command(), env=self.environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            self.handle_line(line.rstrip("\n"))
        returncode = process.wait()

        if returncode == 0 and self.pass_ == "full":
            self.full_stamp.touch()
            self.phase = "steady"
            self.measure_bucket()
        if returncode != 0:
            self.errors = 1
        state = "ok" if returncode == 0 else "failed"
        self.write_status(state)
        self.report_run(state)
        logger.info("done: pass=%s state=%s phase=%s", self.pass_, state, self.phase)
        return returncode == 0

    def handle_line(self, line: str) -> None:
        """One line of rclone's JSON log: relay the message, and turn a stats
        line into a status write and a metric, so a day-long backfill is
        progress rather than silence."""
        try:
            entry = json.loads(line)
        except ValueError:
            if line:
                logger.info("%s", line)
            return
        message = str(entry.get("msg", "")).strip()
        stats = entry.get("stats")
        if not isinstance(stats, dict):
            if message:
                (logger.warning if entry.get("level") in ("error", "critical", "warning")
                 else logger.info)("%s", message)
            return

        done = float(stats.get("bytes") or 0)
        total = float(stats.get("totalBytes") or 0)
        speed = float(stats.get("speed") or 0)
        ratio = done / total if total else None
        progress = (
            f"{_size(done)} / {_size(total)}, "
            f"{ratio * 100:.0f}%, " if ratio is not None else f"{_size(done)}, "
        ) + f"{_size(speed)}/s, ETA {_duration(stats.get('eta'))}"
        logger.info("%s", progress)

        telemetry.record_metric("Custom/offsite/bytes_uploaded", done - self._reported_bytes)
        self._reported_bytes = done
        if ratio is not None:
            telemetry.record_metric("Custom/offsite/progress_ratio", ratio)
        self.write_status("running", progress=progress)

    def measure_bucket(self) -> None:
        """What the bucket holds, cached for every later status write. One
        more listing, so only after a full pass."""
        try:
            output = subprocess.run(
                [self.rclone, "size", "--json", "--fast-list", self.remote],
                env=self.environment, capture_output=True, text=True, timeout=3600,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("Could not measure the bucket", exc_info=True)
            return
        if output.returncode != 0:
            logger.warning("Could not measure the bucket: %s", output.stderr.strip()[-200:])
            return
        self.size_cache.write_text(output.stdout)

    def bucket_size(self) -> tuple[int | None, int | None]:
        try:
            payload = json.loads(self.size_cache.read_text())
            return int(payload["count"]), int(payload["bytes"])
        except (OSError, ValueError, KeyError, TypeError):
            return None, None

    # --- reporting --------------------------------------------------------

    def write_status(self, state: str, progress: str | None = None) -> None:
        objects, size = self.bucket_size()
        try:
            full_finished: str | None = _iso(
                datetime.fromtimestamp(self.full_stamp.stat().st_mtime, tz=timezone.utc)
            )
        except OSError:
            full_finished = None
        payload = {
            "written_at": _iso(self.now()),
            "remote": self.remote,
            "state": state,
            "pass": self.pass_,
            "phase": self.phase,
            "errors": self.errors,
            "run_started_at": _iso(self.started_at),
            "run_seconds": round((self.now() - self.started_at).total_seconds()),
            "progress": progress,
            "remote_objects": objects,
            "remote_bytes": size,
            "full_finished_at": full_finished,
        }
        scratch = self.root / (STATUS_FILENAME + ".tmp")
        scratch.write_text(json.dumps(payload))
        scratch.chmod(0o644)
        scratch.replace(self.root / STATUS_FILENAME)

    def report_run(self, state: str) -> None:
        """One transaction per finished run -- the heartbeat and the outcome
        -- plus the gauges the dashboard charts between runs."""
        seconds = (self.now() - self.started_at).total_seconds()
        objects, size = self.bucket_size()
        with telemetry.task(f"offsite/{self.pass_}"):
            telemetry.attribute("pass", self.pass_)
            telemetry.attribute("phase", self.phase)
            telemetry.attribute("state", state)
            telemetry.attribute("run_seconds", seconds)
            telemetry.attribute("bytes_uploaded", self._reported_bytes)
            telemetry.record_metric("Custom/offsite/run_seconds", seconds)
            telemetry.record_metric("Custom/offsite/failed_runs", 1 if state == "failed" else 0)
            if size is not None:
                telemetry.record_metric("Custom/offsite/remote_bytes", size)
            if objects is not None:
                telemetry.record_metric("Custom/offsite/remote_objects", objects)


def run() -> None:
    from rich.logging import RichHandler

    config = get_config()
    logging.basicConfig(
        level=config.logging_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )
    args = sys.argv[1:]

    if config.archive_root is None:
        logger.error("No [archive] root is configured. Nothing to copy.")
        sys.exit(0)
    key_file = Path(os.environ.get("OFFSITE_KEY_FILE", DEFAULT_KEY_FILE))
    credentials = read_key_file(key_file)
    if credentials is None:
        logger.info("No %s; nothing to copy.", key_file)
        sys.exit(0)

    state_dir = Path(os.environ.get("OFFSITE_STATE_DIR", DEFAULT_STATE_DIR))
    state_dir.mkdir(parents=True, exist_ok=True)
    environment = rclone_environment(credentials, state_dir, str(config.render_timezone))
    if args[:1] == ["rclone"]:
        os.execvpe("rclone", ["rclone", *args[1:]], environment)

    copy = OffsiteCopy(
        root=config.archive_root,
        remote=f"b2:{credentials.bucket}/archive",
        state_dir=state_dir,
        environment=environment,
    )
    ok = copy.run(force_full=args[:1] == ["full"])
    telemetry.flush()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    run()
