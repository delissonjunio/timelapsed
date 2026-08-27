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
.venv/bin/python -m pytest              # all 351
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
| `test_config.py` | INI parsing, path precedence, defaults, cadence validation, the retention/cadence warnings, the shipped template |
| `test_image_capture_library.py` | Filename round-tripping, chronological sort, window queries, nearest-match lookup, pruning, the reclaim ladder |
| `test_keyframes.py` | Daily promotion, the hardlink surviving a prune, calendar-month windows, the cumulative render |
| `test_image_processor.py` | Frame sampling maths, real renders, frame counts verified with `ffprobe`, temp-directory cleanup |
| `test_nvr_capture_agent.py` | URL construction, digest auth, timeouts, retries, non-image rejection, password never logged |
| `test_cadences.py` | Hour/day/ISO-week/calendar-month rollover, the year boundary, stepping across unequal months |
| `test_daemon.py` | The capture loop with a frozen clock, failure tolerance, pruning, render scheduling, picklability |
| `test_web.py` | Catalogue filtering, HTTP routes, Range requests, path traversal, the player's contract with the page |
| `test_analysis_pipeline.py` | Detection, event grouping, re-identification, plate voting |
| `test_analysis_index.py` | The recognition index: schema, queries, retention |
| `test_identities.py` | Naming and merging identities |
| `test_web_recognition.py` | The recognition routes and the activity lanes |
| `test_nginx_config.py` | The generated nginx config: routes, headers, what stays proxied |

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
  image_capture_library.py  filesystem store: naming, queries, promotion, pruning
  image_processor.py        frame selection and ffmpeg invocation
  web.py                    the viewer (standard library only, plus the recognition API)
  library_page.py           the people and plates page
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

Cadences are data, not code branches. A `Cadence` is a name, a nominal window, a rollover
predicate, a floor, and then a handful of defaulted fields for the cases that do not fit "a period
is exactly `window` long, sampled from the stills".

To add a fortnightly render, in `schema.py`:

```python
def _fortnight_rolled_over(now: datetime, last_run: datetime) -> bool:
    return (now.isocalendar().year, now.isocalendar().week // 2) != \
           (last_run.isocalendar().year, last_run.isocalendar().week // 2)

CADENCES["fortnightly"] = Cadence(
    "fortnightly", timedelta(days=14), _fortnight_rolled_over, _floor_to_fortnight,
)
```

For that shape, nothing else changes. `validate_config` starts warning when `image_retention_days`
is too low to feed it, the daemon schedules it, and the viewer gets a lane, a colour lookup and a
filter chip from the registry — the cadence list is injected into the page rather than written into
it. Add a case to `test_cadences.py`, and a CSS custom property named after the cadence at
`web.py:198`, which `test_every_registered_cadence_has_a_lane_colour` will otherwise fail on.

The defaulted fields exist for the cadences that are not that shape:

| Field | Default | When you need it |
| --- | --- | --- |
| `source` | `"image"` | Reading the keyframe track instead of the stills. Anything longer than `image_retention_days` has to. |
| `step_back` / `step_forward` | `± window` | Periods of unequal length. A month is 28 to 31 days, so `window` becomes nominal and the arithmetic goes through these. |
| `min_frames` / `output_fps` | the `[timelapse]` baselines | See `_parse_render_overrides` in `config.py`: a keyframe cadence does **not** inherit the baselines, because they are written explicitly into the shipped ini and are wrong for one frame a day. |
| `anchored` | `False` | A cumulative render, whose start is pinned to the oldest frame and never moves. Read the `anchored` branch of `pending_render_windows` before adding another. |

An `anchored` cadence needs care in three places, all of which have a named regression test:

* **Done-ness is on the end, not the start.** `rendered_window_starts` would latch true after the
  first render and never fire again. It uses `rendered_windows` instead.
* **Age-based retention deletes it.** Its start is day one of the project, so `prune` would take the
  current video. The capture loop skips anchored cadences and `prune_superseded` drops the previous
  file after each successful render instead.
* **`validate_config` warns** if you configure `timelapse_retention_days.<name>` for it anyway.

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
