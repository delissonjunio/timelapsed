# Recognition Feasibility

Measured, not estimated. Before building face and plate recognition, the
candidate models were run over real stills from this deployment to find out what
the cameras can actually support. **One of the two answers is no.**

Everything below came from a 2026-08-27 spike against the live library on the production guest.
Plate characters are redacted; the reads were well-formed, which is the finding.

## Summary

| Capability | Verdict | Why |
|---|---|---|
| Person / vehicle activity | **Works** | 59% of frames on the busiest channel contain a person; false positives disappear at score ≥ 0.50 |
| Plate reading | **Works on ch 5 only** | 7/7 vehicle frames yielded a detectable plate; OCR returns well-formed Brazilian plates at 51–65 px |
| Face recognition | **Not achievable** | faces top out at **38 px**; SFace needs ~80 px. Zero usable faces in 491 person boxes |

## Method

Two samples, both from the live library:

- **Sparse** — every 60th still (one per 10 minutes) from all six channels over
  25 hours: 876 frames, 180 MB. Covers a full day/night cycle; the site is
  UTC−3, so local night is 23:00–09:00 UTC.
- **Dense** — *every* still from ch 5 for two busy hours (13:00 and 19:00 UTC):
  709 frames. This is the density the live system actually captures, and it
  exists because a 1-in-60 sample is not a fair test of whether anyone ever
  stands close to a camera.

Models: YOLOX-tiny (Apache-2.0) for person/vehicle, YuNet + SFace via OpenCV for
faces, `yolo-v9-s-608-license-plate-end2end` + `fast-plate-ocr cct-s-v2-global`
for plates. All CPU-only, matching the guest.

## Person and vehicle detection — works

At the first pass, with a 0.35 score threshold, the numbers were absurd: the
*interior* camera showed vehicles in 57% of frames and the night lot in 70%.
Both were false positives on fixed objects — a neighbouring building seen over
the wall on ch 6, a pile of tools on the floor of ch 1.

**Raising the threshold to 0.50 removed every one of them.** No masking, no
static-scenery suppression needed:

| ch | Name | person | vehicle | events/day |
|---|---|---|---|---|
| 1 | OFICINA INTERNO | – | 8 | 6 |
| 5 | PORTAO SOCIAL SUPERIOR | 24 | 9 | 26 |
| 6 | GERAL LOTE | 1 | 15 | 10 |
| 7 | FUNDOS OFICINA | – | – | 0 |
| 8 | RUA OFICINA | – | 3 | 3 |
| 9 | FRENTE OFICINA | – | – | 0 |

Extrapolated to live capture: **~45 events/day**, ~3 200 detection rows/day,
**~2.6 MB/day** of crops at three per event. Storage is a non-issue — an order
of magnitude below the original estimate, because grouping consecutive
detections into events collapses a parked car from thousands of rows into one.

ch 7 and ch 9 saw essentially no activity in 25 hours.

Cost: **18 ms/frame** on Apple silicon; expect roughly 60–90 ms on the guest's
Ryzen core, so under one core-hour per day for all six channels at 10 s capture.

## Plates — works, on one channel

| ch | vehicle frames | with a plate | plate width med / max |
|---|---|---|---|
| 1 | 83 | 1 | 52 px |
| 5 | **7** | **7** | **52 / 65 px** |
| 6 | 60 | 2 | 250 px — false positives on a building |
| 8 | 3 | 2 | 39 / 44 px |
| 9 | 4 | 0 | – |

**ch 5 PORTAO SOCIAL SUPERIOR is the plate channel** — 100% of its vehicle
frames yielded a plate, and OCR produced 7 well-formed Brazilian plates
(both the old `ABC1234` and the Mercosul `ABC1D23` layouts).

This corrects the assumption made from framing alone, which had picked ch 8
RUA OFICINA as the likely candidate. ch 8 sees almost no traffic and its plates
are ~40 px — too small.

Reliability depends on plate size, and the boundary is sharp:

- At **65 px**, an old-format plate read **exactly, twice, at 0.95–1.00
  confidence**. Legible to the eye in the crop.
- At **52 px**, a Mercosul plate produced four *different* readings across four
  frames — single characters disagreeing each time. Genuinely marginal; the
  crop is barely legible to a human either.

> **Single-frame reads cannot be trusted at this resolution.** What makes plates
> usable is voting across the frames of one event: a car that sits for a minute
> gives six reads to reconcile, and per-position majority voting recovers the
> plate that no single frame gets right. Event grouping is what makes this work,
> so it is a prerequisite, not an optimisation.

Guards required: OCR confidence ≥ ~0.7 plus a Brazilian format regex. Together
these reject the ch 6 building false positives, which read as garbage at 0.5–0.6.

## Faces — not achievable

This is the negative result, and it is not close.

The dense ch 5 sample — 709 consecutive frames, the best channel for people:

| Measure | Value |
|---|---|
| Frames containing a person | 417 / 709 (**59%**) |
| Person boxes | 491 |
| Body height | median **347 px**, p90 438, max 492 |
| Faces detected | **22** (4.5% of person boxes, several of them false positives) |
| Face width | median **28 px**, max **38 px** |
| Faces ≥ 60 px, the floor for recognition | **0** |

SFace aligns to 112×112 and needs roughly **80 px** of native face width for a
stable embedding. The best face this deployment produced in two busy hours was
**38 px** — under half. The one clearly real face found (a man in a red cap) is
recognisable as *a face* and carries nowhere near enough detail to identify.

The embeddings confirm it: pairwise cosine similarity had a median of 0.22 with
scattered pairs above SFace's 0.363 threshold. That scatter is false-positive
noise, not identity — at 28 px there is no signal to cluster on.

**Three independent causes, none fixable in software:**

1. **Mounting geometry.** Cameras sit high and wide for area coverage. A person
   at 347 px body height has a ~43 px head by construction — the ratio is fixed.
2. **Pose.** People are working, walking away, or looking down. The single
   clearest person detection in the whole sample (score 0.92) is facing away
   from the camera entirely.
3. **Sampling.** At 10 s intervals a visit yields few frames, so the chance of
   catching a frontal pose in any of them is low.

Note that cause 1 alone is decisive. Even continuous 30 fps capture at a perfect
frontal pose would still only give a 43 px face from these positions.

### What would change the answer

- **A camera at face height on a chokepoint** — the gate on ch 5 is the obvious
  spot, since that is where people already are. This is how the problem is
  normally solved: a dedicated face camera framed for heads, not for area.
- The NVR advertises `isSupportFaceSnap` and `isSupportFaceContrast` with a
  20 000-face library, but that does **not** rescue this. Its analysis runs on
  the same camera streams with the same optics, and it is currently unusable
  anyway: every smart event is disabled, recording is off on all channels, and
  the 932 GB HDD reports zero free space.

## Reproducing

```bash
# sample every 60th still from each channel
ssh delisson@100.84.156.77 \
  'cd /var/lib/timelapsed && for ch in 1 5 6 7 8 9; do
     sudo -n ls $ch/image | sort | awk -v c=$ch "NR%60==1{print c\"/image/\"\$0}"
   done | sudo -n tee /tmp/spike.list | wc -l'
ssh delisson@100.84.156.77 'sudo -n tar -cf - -C /var/lib/timelapsed -T /tmp/spike.list' > frames.tar

# person/vehicle box sizes and hit rates
python detect.py && python report2.py
# plate widths and OCR reads
python plates.py
# the fair face test, at full capture density
python dense_faces.py
```
