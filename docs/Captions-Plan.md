# Captions Plan

Turn the speech on archived footage into closed captions on the player and a transcript for the
clip. Planned, not built; measured 2026-09-04 against the live archive.

Every zermatt segment carries the camera's audio (AAC, mono, 16 kHz) and the viewer can now play
it. The plan is the pipeline behind a `CC` button: find the few seconds of each segment where
somebody is actually talking, run speech-to-text on those seconds alone, and store the result
beside the segment in the form the browser already knows how to draw.

The shape of it is a funnel, and the funnel is the whole design: a gate that costs almost
nothing decides where the expensive model runs. On this footage that is one part in forty.

## What was measured

A sample of 18 archived segments from 2026-09-04, evening local time, six zermatt channels,
20.7 minutes of audio, pulled to a Mac and run through the candidate tools.

**A loudness gate is useless.** `ffmpeg silencedetect` at −35 dB calls 30–70 % of every segment
"not silent" — the gate camera, the workshop, the street. That is traffic, birds, wind and
machinery, and it means an energy threshold cannot cut the work down. The first gate has to know
what a voice is.

**Silero VAD is that gate, and it is nearly free.** It flagged 28 s of the 1,244 s as speech —
2.3 %, in three of the eighteen segments — and cost 1.7 s of CPU for the whole sample, decode
included. It is a small ONNX model, and `onnxruntime` is already a dependency of the analyzer. It
returns regions, not a verdict: `[start, end]` pairs with padding, which is exactly the unit the
next stage wants.

**Whisper on those regions is affordable but not reliable.** faster-whisper `small` (int8, four
threads, the Mac) transcribed a 56 s clip carrying 12 s of flagged speech in 5.8 s. What it
produced across the three flagged clips:

| Clip | Silero said | Whisper `small` produced |
| --- | --- | --- |
| Gate camera, 6 s | one region | two plausible Portuguese sentences — and different words for the same six seconds on a second run |
| Workshop, 12 s | three regions | fourteen segments of the single letter "e", `no_speech_prob` 0.68 — a hallucination on noise |
| Street, 8 s | two regions | nothing on one run; "Não!" repeated four times on another |

`base` was worse on every clip. The lesson is the same one the recognition spike taught about
faces: these microphones are mounted for area coverage, and a voice several metres away in
street noise is at the edge of what the model can do. Some of it comes out; a filter has to
throw away what did not, and the UI has to be honest that captions are best-effort.

**Scale.** The zermatt archive lands roughly 800 segments and ≈ 50 video-hours a day. At 2.3 %
that is about 70 minutes of speech a day to transcribe. The guest is Proxmox CT 303: four cores
of a Ryzen 5 5600G and 6 GB, already running capture, the analyzer and the archiver. Every timing
above is a Mac number and only says what is cheap relative to what; the first task below is to
measure the same three clips on the guest.

## The pipeline

Per archived segment, in order, each stage only where the previous one said so:

1. **Has an audio track?** Free: `ffprobe` on the file, or better, the archiver already knows
   from the remux. Intelbras segments carry video only and stop here.
2. **Where might there be speech?** Decode to 16 kHz mono PCM with `ffmpeg` (milliseconds per
   minute of audio) and run Silero VAD over it. Output: speech regions, padded by ~400 ms and
   merged across gaps under two seconds. Regions shorter than a second are dropped — a shout is
   not a caption. Most segments end here with zero regions, and that fact is recorded so the
   segment is never looked at again.
3. **What was said?** faster-whisper (CTranslate2, CPU, int8) on the regions alone, language
   pinned to Portuguese, `condition_on_previous_text=False` so one hallucination cannot seed the
   next. The regions are handed to the model as clip timestamps; the model never sees the other
   97 % of the audio.
4. **Was that real?** The hallucination filter, tuned on the failures above: drop a cue whose
   `no_speech_prob` is above 0.5; drop a run of cues with identical text (the "e" × 14 and
   "Não!" × 4 cases); drop cues whose compression ratio says the text is a loop. What survives
   is kept with its scores, so the UI can dim a weak cue instead of asserting it.
5. **Store it.** A WebVTT file beside the segment and a row in the ledger.

The model runs where the gate points and nowhere else. That is what buys the option of a better
model: at one part in forty, `medium` — three times the cost of `small` — is still a couple of
core-hours a day, and its output on distant speech is markedly better. Which model is a
measurement, not a decision, and it is task one.

## Storage

**`<segment>.vtt` next to `<segment>.mp4`.** WebVTT is what a `<track>` element consumes, so the
browser draws the captions itself — in full screen too, because the track belongs to the video.
The same file is the transcript: a track in `hidden` mode still loads its cues, and a list of
cues with timestamps is the transcript panel. One artifact, no parser, no second format.

A VTT `NOTE` block at the top carries the provenance — model, language, VAD settings, the
generating commit — so a file can be told apart from one made by a later, better run.

**A ledger, `captions.sqlite3` beside the archive**, owned by the transcriber daemon alone: one
row per segment examined — when, whether it had audio, seconds of speech the gate found, whether
it was transcribed, with which model, how many cues survived, CPU seconds spent. The ledger is
what makes "done" cheap to answer for the 97 % of segments that had nothing to say, without a
marker file per segment. It is a rebuildable cache in the same sense as the footage mirror:
delete it and the daemon re-examines the archive. The recognition index is not touched; it has
one writer and this is not it.

**Retention follows the footage.** `reclaim` in the archiver unlinks `*.mp4` by age and drops
whole days for the free-space floor; the age-out loop has to unlink the sidecar too (the day
`rmtree` already takes it), and the ledger drops rows whose segment is gone. Whether the *text*
should outlive the video — it is tiny, and "what was said at the gate last month" is a real
question — is an open question below, because it is also the most sensitive thing the system
would keep.

## Where it runs

**A fifth daemon, `timelapsed-transcriber`**, alongside the archiver and shaped like it: reads
the archive tree, writes sidecars and its own ledger, publishes `.transcriber-status.json` for
the status page, reports to New Relic as its own app with custom metrics (segments examined,
speech seconds found, cues written, CPU seconds, backlog). It does not run inside the archiver:
a model inference in the fetch loop would stall replication, and the archiver's memory cap is
sized for buffers, not a model.

It polls the archive for segments the ledger has not seen, **newest first** — captions matter
most on the footage somebody is about to open — and works back through history in idle time.
A configurable lookback (days) bounds the backfill so a fresh install does not spend a week on
footage nobody will watch. A per-day CPU budget bounds the daily spend the same way, and the
backlog gauge says when the budget is too small.

Unit file: `Nice=19`, `CPUWeight` below the archiver's, `MemoryMax` sized for the chosen model
plus headroom (measure; roughly 0.5 GB for `small` int8, 1.5 GB for `medium`), the same sandbox
lines as the archiver, `ReadWritePaths` the archive root. Models live under the analysis root
like the recognition models and are fetched by `deploy/fetch-models.sh`, never at runtime from
Hugging Face by a service user with no network expectations.

Dependencies: `faster-whisper` brings `ctranslate2`, `tokenizers`, `huggingface-hub` and `av`
(PyAV, with its own ffmpeg libraries). Silero VAD ships inside faster-whisper as ONNX. About
150 MB of wheels plus the model. `openai-whisper` itself is not an option: it needs torch, and
the analyzer already chose ONNX Runtime over torch for exactly this guest.

Configuration, one section:

```ini
[captions]
enabled = false
model = small                ; base | small | medium | large-v3-turbo, measured before chosen
language = pt
threads = 2
lookback_days = 7            ; how far back a fresh install transcribes
daily_cpu_minutes = 90       ; the budget; the backlog gauge says when it is too small
vad_threshold = 0.5
min_region_seconds = 1.0
```

## The viewer

* `/api/archive` grows a `captions` field per segment: the sidecar's URL, or null. The
  catalogue already stats each segment; a sibling stat is free.
* `/archive/<channel>/<day>/<name>.vtt` is served like the video. `resolve()` currently refuses
  anything but `.mp4`; it learns one more suffix.
* The one `<video>` gains one `<track kind="captions" srclang="pt">`. Its `src` is swapped with
  the clip and cleared for clips without captions. Mode `showing` when the `CC` toggle is on,
  `hidden` otherwise — hidden still loads the cues, which the transcript needs.
* A `CC` button beside the speaker on the transport bar, drawn only when the segment has a
  sidecar. On by default when captions exist: they are silent and they are the point. The choice
  sticks for the visit, like sound.
* A **Transcript** button opens a panel under the player: every cue as a line, with its
  wall-clock moment, click to seek. Built from `track.cues`; no second fetch. Weak cues — the
  daemon tags them `<c.weak>` in the VTT — are dimmed, not hidden, and the panel says so.
* Sightings: a person event whose footage has cues could show a speech glyph on the timeline and
  in the library. Later; it needs the ledger queryable from the web daemon.

## Sequence

1. **Measure on the guest.** The three flagged clips and a full day of one channel through the
   pipeline on CT 303: Silero speed, then `base`, `small`, `medium` and `large-v3-turbo` at int8
   for CPU seconds per speech second, peak RSS and what each one makes of the gate-camera clip.
   Pick the best model whose daily cost fits beside the other daemons. Tune the hallucination
   filter on the day's output. This is a script in `scripts/`, not the daemon.
2. **The daemon, the ledger, the sidecars.** No UI. Run it over the last week, read the VTTs,
   fix the filter, watch the status file and the New Relic gauges for a few days.
3. **The viewer.** `CC`, the transcript panel, the API field, the route.
4. **The status page and alerts**, then search: SQLite FTS over the cues is a one-liner once
   the ledger exists, and "when did anyone mention the gate" is the question this was for.

## Open questions

* **Language.** Pinned to Portuguese, or auto-detected per region? Pinning is faster and
  removes a failure mode; auto costs one extra pass per region and catches the odd visitor.
* **Model budget.** How much of the guest is this worth? A couple of core-hours a day for
  `medium` is affordable now that the analyzer's backfill is done, but it is the same four cores.
* **Transcripts after the video.** Keep the text past the footage retention, or delete it with
  the segment? Text is tiny and searchable; it is also a record of neighbours' conversations.
  The default in this plan is delete-with-the-segment.
* **Weak cues.** Dimmed, or dropped? The sample says a fair share of what survives will be
  half-right. Showing it dimmed is honest; dropping it is quieter.
* **Off-box transcription.** A hosted API would remove all the CPU cost and do a better job
  on distant speech, at the price of shipping the audio of everyone who walks past the house
  to a third party. This plan runs everything on the guest. Say if that trade should be
  revisited.

## Privacy

Captions are the most sensitive artifact the system would keep: not that a person passed the
gate, but what they said while doing it. They live beside the footage under the same retention,
reach only the tailnet-facing viewer, never leave the guest, and are made by a model running
locally. The sample transcripts from the spike were read to judge quality and are not in this
document.
