# Development

## Setup

```bash
git clone git@github.com:delissonjunio/timelapsed.git
cd timelapsed
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]' || .venv/bin/pip install requests backoff rich python-dateutil pytest
```

`ffmpeg` and `ffprobe` must be on `PATH` — the test suite renders real videos and probes them.

```bash
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Debian/Ubuntu
```

## Running the tests

```bash
.venv/bin/python -m pytest              # all 112
.venv/bin/python -m pytest tests/test_image_processor.py -v
.venv/bin/python -m pytest -k cadence
```

Tests that need `ffmpeg` are marked and skip automatically when it is missing, so the suite still
runs (with reduced coverage) on a machine without it.

### What the tests actually exercise

The suite fakes exactly one thing — the NVR — and uses the real article everywhere else. Renders go
through real `ffmpeg` and are verified with `ffprobe`; the web tests run a real HTTP server on a
random port and drive it with `urllib`.

| File | Covers |
| --- | --- |
| `test_config.py` | INI parsing, path precedence, defaults, cadence validation, the retention/cadence warning |
| `test_image_capture_library.py` | Filename round-tripping, chronological sort, window queries, nearest-match lookup, pruning |
| `test_image_processor.py` | Frame sampling maths, real renders, frame counts verified with `ffprobe`, temp-directory cleanup |
| `test_nvr_capture_agent.py` | URL construction, digest auth, timeouts, retries, non-image rejection, password never logged |
| `test_cadences.py` | Hour/day/ISO-week rollover, including the year boundary |
| `test_daemon.py` | The capture loop with a frozen clock, failure tolerance, pruning, render scheduling, picklability |
| `test_web.py` | Catalogue filtering, HTTP routes, Range requests, path traversal |

The clock is frozen in `test_daemon.py` by patching `timelapsed.timelapsed.datetime` and driving
`time.sleep`, so a test covering a week of rollovers runs in milliseconds.

## Project layout

```
timelapsed/
  __main__.py               python -m timelapsed
  timelapsed.py             the daemon: run(), capture_continuously(), RenderScheduler
  config.py                 INI loading, cadence parsing, validate_config()
  schema.py                 Config, VideoResolution, Cadence, the CADENCES registry
  nvr_capture_agent.py      HTTP snapshot fetching
  image_capture_library.py  filesystem store: naming, queries, pruning
  image_processor.py        frame selection and ffmpeg invocation
  web.py                    the viewer (standard library only, plus the recognition API)
  analyzer.py               the recognition daemon: run(), run_once(), prune()
  analysis/
    index.py                SQLite schema and queries
    models.py               ONNX wrappers: detector, re-ID, plate detect + OCR
    pipeline.py             frame -> detections -> events, plate voting
    identities.py           appearance matching and grouping
deploy/
  install.sh                Debian/Ubuntu installer
  fetch-models.sh           downloads and verifies the ONNX models
  timelapsed.service        capture daemon unit
  timelapsed-web.service    viewer unit
  timelapsed-analyzer.service  recognition daemon unit
docs/                       this wiki
tests/                      pytest suite
```

The dependency direction is one-way: `timelapsed.py` knows about everything, `web.py` knows about
the library and config, and `image_capture_library.py` knows about nothing but the filesystem.
`analysis/` is the same shape: `index.py` knows only SQLite, `models.py` only ONNX and numpy,
and `pipeline.py` wires them together without either knowing where crops live.

## Adding a cadence

Cadences are data, not code branches. To add, say, a monthly render:

1. Add a rollover predicate in `schema.py`:

   ```python
   def _month_rolled_over(now: datetime, last_run: datetime) -> bool:
       return (now.year, now.month) != (last_run.year, last_run.month)
   ```

2. Register it:

   ```python
   CADENCES["monthly"] = Cadence("monthly", timedelta(days=30), _month_rolled_over)
   ```

Nothing else changes. `validate_config` will automatically start warning when
`image_retention_days` is too low to feed it, the daemon will schedule it, and the viewer will
offer it as a filter chip. Add a case to `test_cadences.py`.

## Conventions

* **Everything is UTC on disk.** Local-time filenames break chronological ordering twice a year.
* **The filename is the index.** Any new artefact type needs a fixed-width timestamp prefix so
  lexical order stays chronological.
* **Failures in the capture loop are logged and swallowed.** One dead camera must not take down the
  other channels. Failures at startup, by contrast, should be loud.
* **Comments explain why, not what.** The non-obvious decisions (why a process per channel, why
  sampling rather than concatenation, why Range support is mandatory) are commented at the point
  they are made.

## Releasing

```bash
# bump version in pyproject.toml
git tag -a v1.1.0 -m "..."
git push origin main --tags
```

## Documentation

Documentation lives in `docs/` and is versioned alongside the code, so a change and its
documentation land in the same commit and can be reviewed together.

GitHub's built-in wiki is not used: wikis are unavailable on private repositories on the free plan.
`docs/` renders on GitHub, works offline in a clone, and shows up in diffs — which is arguably the
better arrangement anyway. If the repository is ever made public, or the account moves to GitHub
Pro, the pages can be pushed to the wiki as-is:

```bash
git clone git@github.com:delissonjunio/timelapsed.wiki.git
cp docs/*.md timelapsed.wiki/    # then rename README.md to Home.md
```

Wiki links would need the `.md` suffixes stripped; `docs/` links need them present.
