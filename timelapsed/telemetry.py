"""New Relic, behind one switch and one module.

The agent is a hard dependency but a soft presence: nothing here does anything
until NEW_RELIC_LICENSE_KEY (or NEW_RELIC_CONFIG_FILE) is in the environment,
which on a deployed guest comes from /etc/timelapsed-newrelic.env via the
systemd units. Tests and local runs see no env var, no agent, no threads.

initialize() is called from the package's __init__ so it runs before Flask and
requests are imported anywhere: the agent instruments libraries with import
hooks, and a library imported before the hooks exist is a library it may
silently fail to wrap. That placement also covers every worker process the
daemons spawn -- a spawned child re-imports the package and initialises fresh,
and a forked child inherits an initialised agent, which re-registers itself
after the fork the way it does under any preforking server.

Every helper is a no-op when the agent is off, so the daemons read the same
with or without monitoring and the tests never know the difference.
"""
import logging
import os
from contextlib import contextmanager, nullcontext

logger = logging.getLogger(__name__)

_agent = None
_enabled = False


def initialize() -> None:
    """Start the agent if the environment asks for it.

    Imports newrelic only on that path: the agent package is heavy enough that
    an unconditional import would tax every test run and CLI invocation for a
    feature that is off everywhere but the guest.
    """
    global _agent, _enabled
    if _enabled:
        return
    if not (os.environ.get("NEW_RELIC_LICENSE_KEY") or os.environ.get("NEW_RELIC_CONFIG_FILE")):
        return

    try:
        import newrelic.agent as agent
    except ImportError:
        logger.warning("NEW_RELIC_LICENSE_KEY is set but the newrelic package is not installed")
        return

    # Each systemd unit names its own service (timelapsed-capture,
    # timelapsed-web, ...); this is only the fallback for a bare env.
    os.environ.setdefault("NEW_RELIC_APP_NAME", "timelapsed")
    agent.initialize()
    # Eager, not lazy: activation on the first transaction silently drops that
    # transaction, and for the capture daemon the first transaction is the
    # startup backfill sweep -- the most interesting cycle of the day.
    agent.register_application(timeout=10.0)
    _agent = agent
    _enabled = True


@contextmanager
def task(name: str):
    """One background transaction: a capture cycle, an analysis pass, a render.

    Callers put their try/except inside the block and pair notice_error() with
    logger.exception, so an error is filed exactly once, on the transaction it
    happened in.
    """
    if not _enabled:
        yield
        return
    with _agent.BackgroundTask(_agent.application(), name=name, group="Timelapsed"):
        yield


def trace(name: str):
    """A named segment inside the current transaction, for spans worth timing
    on their own -- one archive fetch inside an archive pass."""
    if not _enabled:
        return nullcontext()
    return _agent.FunctionTrace(name=name)


def ignore() -> None:
    """Drop the current transaction: health checks, passes that did nothing."""
    if _enabled:
        _agent.ignore_transaction()


def notice_error() -> None:
    """File the in-flight exception with the agent.

    Sits beside every logger.exception call. Inside a transaction the error
    lands on it; outside one it is recorded against the application, so a
    failure in a corner no transaction covers still shows up.
    """
    if not _enabled:
        return
    if _agent.current_transaction() is not None:
        _agent.notice_error()
    else:
        _agent.notice_error(application=_agent.application())


def attribute(key: str, value) -> None:
    """A custom attribute on the current transaction (channel, window, ...)."""
    if _enabled:
        _agent.add_custom_attribute(key, value)


def record_metric(name: str, value) -> None:
    """A custom metric, named Custom/... by convention."""
    if not _enabled:
        return
    application = None if _agent.current_transaction() is not None else _agent.application()
    _agent.record_custom_metric(name, value, application=application)
