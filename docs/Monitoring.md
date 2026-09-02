# Monitoring

The four daemons report to New Relic APM. Off by default: nothing starts, and
nothing leaves the guest, until a licence key is set. The free tier's monthly
ingest allowance is far beyond what this deployment produces, so the only cost
is the key.

## Turning it on

`install.sh` drops a template at `/etc/timelapsed-newrelic.env`; every unit
reads it with `EnvironmentFile=-`, so the file being absent or empty simply
leaves the agent off. To enable:

```bash
sudoedit /etc/timelapsed-newrelic.env     # paste the INGEST - LICENSE key
sudo systemctl restart timelapsed timelapsed-web timelapsed-analyzer timelapsed-archiver
```

Each unit names its own APM service, so the daemons chart separately:
`timelapsed-capture`, `timelapsed-web`, `timelapsed-analyzer`,
`timelapsed-archiver`.

The key is a credential and the file is root-only, the same reasoning as the
NVR password in `/etc/timelapsed.ini`. The agent sends no request parameters
and no local variables, so the NVR credentials in the config object never ride
along with an error.

## What reports

The switch and every hook live in `telemetry.py`; each helper is a no-op
without the agent, which is why the daemons and the tests read the same either
way. The agent itself is started from the package `__init__` -- it instruments
Flask and requests with import hooks, so it has to be up before either is
imported anywhere.

**timelapsed-web** is ordinary Flask APM: a web transaction per route, error
traces for anything a handler raises, and the catalogue and index reads inside
them. `/healthz` deliberately reports nothing -- nginx and the docs' curl poll
it, and a poll is throughput noise.

**timelapsed-capture** records a background transaction per capture cycle
(named per channel) and one per render window, so a slow NVR snapshot, a
rollover pileup or a long ffmpeg run each have their own chart. The snapshot
HTTP call shows up as an external inside the cycle. Sleep is excluded from the
cycle transaction on purpose: the duration is the work, which is what the
loop's own >80% warning is about.

**timelapsed-analyzer** records a transaction per analysis pass, footage
sweep, identity consolidation and index prune. Idle passes -- the every-few-
seconds "nothing new" poll -- are dropped rather than charted, so throughput
means frames actually analysed.

**timelapsed-archiver** records a transaction per segment fetch -- a pass
drains the whole queue, and during a backfill that is days of downloading,
far longer than any single transaction could usefully report -- plus one per
reclaim that actually removed files. An idle daemon charts nothing, which is
the normal look for a replica that is caught up.

The capture daemon's workers are forked processes, and the agent's harvest
thread does not survive a fork -- a worker that inherits the parent's agent
records transactions nobody ever sends. `telemetry.child()` resets the agent
at the top of every spawned process (capture workers and render processes
both), which is why capture data exists at all.

Every `logger.exception` in the daemons files the same error with the agent,
so the Errors page and the journal tell one story. Log lines themselves are
forwarded by the agent's logging integration, which is on by default.

Alongside the transactions there are a few custom metrics, all under
`Custom/`: images stored, keyframes promoted and cycle overruns for capture;
frames analysed for the analyzer; segments and bytes archived for the
archiver, plus its backlog gauges (`backlog_segments`, `backlog_oldest_days`
and `deferred_segments`, reported every pass and every fetch -- the zero is
the heartbeat that tells an idle archiver apart from a dead one). The backlog
gauges count segments waiting out a failure backoff too: those are exactly the
segments the replica is missing, and a gauge that forgot them once read hours
behind as fully caught up while thousands of refused segments sat parked.
`deferred_segments` alone is that failing subset. Both volumes
report their headroom as `disk_free_gb` gauges: the capture workers cover the
library filesystem every cycle, the archiver covers the archive volume with
each backlog report. They exist for
dashboards and alerts -- "no images stored for ten minutes" and "the replica
is falling behind" are the alerts that matter most and none of the built-in
signals say either directly.
