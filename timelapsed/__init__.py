# Before anything else in the package: the New Relic agent instruments Flask
# and requests with import hooks, so it has to be up before either is imported.
# A no-op unless the environment carries a licence key -- see telemetry.py.
from timelapsed import telemetry as _telemetry

_telemetry.initialize()
