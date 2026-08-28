# Alarm Plan

Alert when a person shows up on a camera at a time — and optionally in a part of the
frame — where one was configured to matter. Planned, not built.

The detector already finds people and files them as events; this feature adds rules
that decide which of those events deserve a push notification, and an LLM
confirmation step in front of the notification, because the detector has been caught
filing a machine as a person and an alarm that cries wolf gets muted within a week.

## What a rule is

One rule is a channel, a daily time window, and optionally a polygon over the frame.
A person event that matches all three fires the alarm. That single mechanism covers
both readings of "expected": "tell me when the cleaner arrives" and "nobody should be
at the gate after ten" are the same rule with different windows — the window always
means *when a detection alarms*.

Windows are judged against `render_timezone`, like the keyframe hour: alarms are for
people, and people think in their own wall clock. A window may wrap midnight
(`22:00-06:00` is 22:00 through 06:00 the next day), which the matching code has to
handle explicitly and the tests have to cover, because naive `start <= t < end`
comparison silently makes an overnight window empty.

## Configuration

Rules live in the INI with everything else, one section per rule:

```ini
[alarm]
enabled = true
llm_confirm = true
llm_model = claude-opus-5
anthropic_api_key = sk-ant-...
notify_url = http://homeassistant.lan:8123/api/webhook/timelapsed-alarm
on_llm_error = alarm          ; fail-open: an unreachable API must not silence alarms

[alarm:gate-night]
channel = 5
between = 22:00-06:00
days = mon-sun                ; optional, defaults to every day
mask = 0.10,0.35 0.92,0.35 0.92,1.0 0.10,1.0   ; optional, normalized polygon
min_frames = 2                ; frames an event must reach before it can alarm
cooldown_minutes = 10
```

The mask is a polygon in coordinates normalized to the frame, so changing
`capture.resolution` does not quietly invalidate every rule. A detection matches when
its box center falls inside the polygon; the box's *edges* clipping a region is
exactly the passer-by case masks exist to exclude.

The API key sits next to the NVR password in `/etc/timelapsed.ini`, root-owned and
outside the repo. Same posture, same reasoning.

`validate_config` grows warnings for the ways this can be misconfigured quietly: a
rule naming a channel that is not captured, a mask vertex outside 0..1, and
`llm_confirm` enabled with no API key.

## Where it runs

Inside the analyzer, not a new daemon. The analyzer already sees every detection the
moment it exists, and it is the index's one writer — a separate alarm daemon would
either poll the index and add a second writer, or re-run detection it already ran.

A new `timelapsed/analysis/alarms.py` provides an `AlarmEngine`, injected into
`FrameAnalyzer` the way `store_crop` is. After `tracker.update`, the pipeline hands it
each `(detection, event, frame path, captured_at)` and the engine walks its gates:

1. **Rule match** — person event, channel has a rule, capture time inside the
   window, box center inside the mask.
2. **Debounce** — the event must have reached `min_frames`. Single-frame ghosts are
   the classic false positive, and waiting one more capture interval is cheap.
3. **Live guard** — the frame must be recent against the wall clock (about ten
   minutes). Without this, a backfill of last week's stills fires a week of alarms
   on sight. This guard is what makes the alarm path safe to leave enabled while
   the analyzer chews through a backlog.
4. **Cooldown** — nothing fires for a rule within `cooldown_minutes` of its last
   alarm. Events close after 60 quiet seconds and reopen when the person is found
   again, so without a cooldown one person working in view all afternoon is a
   notification every time the detector blinks.
5. **Once per event** — an `alarmed` flag on the open event, so a long visit is one
   alarm however many frames extend it.

The LLM call and the webhook run on a small worker thread. Both are network calls
that take seconds, and the frame loop's budget is measured in milliseconds; a slow
confirmation must cost alarm latency, never analysis throughput. The worker posts
results back on a queue the analyzer drains each pass, so alarm rows are still
written by the one thread that owns the index.

## LLM confirmation

The measured failure this exists for: the detector filing a machine as a person.
Confirmation is one Claude API call per candidate alarm — after the gates above,
that is a handful of calls a day, not per frame.

The call sends two images: the full frame downscaled for context, and the detection
crop for detail. Structured output keeps the answer parseable:

```python
response = client.messages.create(
    model=config.alarm_llm_model,
    max_tokens=1024,
    output_config={
        "effort": "low",
        "format": VERDICT_SCHEMA,   # {"verdict": "person" | "not_person" | "unsure",
    },                              #  "description": str}
    messages=[{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": frame}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": crop}},
        {"type": "text", "text": PROMPT},
    ]}],
)
```

`person` and `unsure` both alarm; only a confident `not_person` suppresses. An API
error or timeout falls back to the `on_llm_error` policy, which defaults to
alarming — an alarm system that goes quiet when an external API is down has failed
in the worst available direction.

Every candidate writes a row whichever way the verdict goes. The suppressed rows,
with the model's one-line description of what it actually saw, are the tuning data:
they are how you find out whether the detector lies often enough to justify the
step at all, and what it lies about.

A frame at capture resolution is a couple of thousand input tokens, so a call is
about a cent on the default model and the daily spend is coins. `llm_model` is
configurable; `claude-haiku-4-5` is several times cheaper if volume ever grows.

## Notification

One webhook POST, defined by `notify_url`, carrying JSON: rule name, channel, the
event's timestamps, the verdict and description, and viewer URLs for the crop and
the timeline at that moment. The URLs are tailnet-only, which is fine — the intended
receiver is Home Assistant on the same LAN, which turns the webhook into a
companion-app push with the crop attached. Timelapsed stays decoupled: ntfy or a
Telegram bot are the same POST pointed elsewhere.

Delivery failure is recorded on the alarm row and retried once. There is no durable
retry queue on purpose; an alarm delivered an hour late is noise, not an alarm.

## Storage

Schema version 4 adds one table:

```sql
CREATE TABLE alarm (
    id           INTEGER PRIMARY KEY,
    rule         TEXT    NOT NULL,
    event_id     INTEGER REFERENCES event(id) ON DELETE SET NULL,
    channel      TEXT    NOT NULL,
    triggered_at INTEGER NOT NULL,
    verdict      TEXT    NOT NULL,  -- 'person' | 'not_person' | 'unsure' | 'llm_error' | 'llm_skipped'
    description  TEXT,
    notified_at  INTEGER,
    delivery     TEXT              -- 'sent' | 'failed' | 'suppressed'
);
```

Cooldown state is derived from `MAX(triggered_at)` per rule rather than kept in its
own table, so a restart cannot disagree with the record. Rows age out with the
existing event retention.

## Viewer

`GET /api/alarms` lists them, and the timeline gains alarm markers — the lanes and
the fetch already exist, a marker is cheap. Later, a mask helper page: the latest
still for a channel, click out a polygon, and it prints the normalized coordinates
to paste into the INI. Hand-writing polygon coordinates against a mental image of
the frame is the part of this design that does not survive contact with a human.

## Latency, honestly

Capture interval, plus the analyzer's one-interval holdback, plus the debounce
frame, plus a few seconds of LLM call: under a minute at a 10-second interval. This
is "someone showed up" alerting, not a real-time intrusion siren, and the docs
should say so.

## Build order

1. **Rule engine, log-only.** Config parsing, the gates, alarm rows written,
   nothing sent, no LLM. Run it for days and read what would have fired — the same
   measure-first shakedown every threshold in this project went through.
2. **LLM confirmation.** Compare verdicts against the phase-1 rows before trusting
   suppression: that comparison is the measurement of how often the detector
   actually lies.
3. **Webhook delivery**, and the Home Assistant automation behind it.
4. **Viewer polish** — the alarms list and the mask helper.

## Files touched

| File | Change |
|---|---|
| `timelapsed/config.py`, `timelapsed/schema.py` | `[alarm]` and `[alarm:<name>]` parsing, `AlarmRule`, validation warnings |
| `timelapsed/analysis/alarms.py` | new — the engine: gates, LLM client, webhook, worker thread |
| `timelapsed/analysis/index.py` | schema v4, alarm writes and queries |
| `timelapsed/analysis/pipeline.py` | hand pairs to the engine after association |
| `timelapsed/analyzer.py` | build the engine, drain the worker queue each pass |
| `timelapsed/web.py` | `/api/alarms`, timeline markers |
| `pyproject.toml` | the `anthropic` SDK |
| `docs/`, `timelapsed.ini.example` | this plan graduates into an Alarms.md when built |

The LLM call and the webhook are injected callables, the same pattern the pipeline
already uses for crops and identities, so the tests for window wrapping, mask
hit-testing, cooldowns, the live guard and the error paths all run without a
network.
