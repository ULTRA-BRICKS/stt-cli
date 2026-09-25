"""mic_service — `stt mic` as a MANAGED SERVICE (run/start/status/stop/enable/disable).

The lifecycle machinery is NOT hand-rolled here: it comes from the shared
``agenttools_service`` lib (agent-tools/lib/agenttools_service) — the one service-manager the
agent-tools ecosystem already shares between the review dashboard, spec-web, config-web and
now the `stt mic` daemon, so every long-running server gets identical pidfile / launchd /
systemd-unit handling instead of a per-tool copy. This module builds only the piece that is
`stt mic`-specific: the :class:`agenttools_service.Service` descriptor (its foreground argv,
and — unlike the dashboard/spec-web — no port, since the daemon is not a network server; it
is reached through a global hotkey, not a socket).

``mic_service()`` (the ``Service``-building function) is imported lazily by
``commands/mic.py`` — only where an action actually needs it — so the service stack (and
``agenttools_service`` itself) never loads on the hot, ad-hoc `stt mic` path. ``SERVE_TOKEN``
is a plain string constant with no such cost, so `mic.py` imports it directly at module
scope; the lazy-import discipline is about `agenttools_service`, not about this whole file.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenttools_service import Service

# The hidden entry the service execs in the foreground — see `commands/mic.py::_serve`. Kept
# as a constant here so the argv the descriptor builds and the argv the CLI recognises as
# "this is the hidden entry, not a real user command" can never drift apart.
SERVE_TOKEN = "__serve"


def mic_service(*, extra_argv: list[str] | None = None) -> Service:
    """Build the :class:`agenttools_service.Service` descriptor for the `stt mic` daemon.

    ``extra_argv`` is the daemon's own flags (``--hotkey``, ``--model``, ``--device``, ...),
    already rendered as CLI tokens by `commands/mic.py` from whatever the user typed on
    `run`/`start`/`enable` — this module stays a thin, flag-agnostic builder, and every
    `stt mic` daemon flag is owned in exactly one place (the CLI parser), not duplicated
    here as a second list of keyword arguments that could drift out of sync with it.
    """
    from agenttools_service import Service

    return Service(
        name="mic",
        argv=_serve_argv(extra_argv or []),
        port=None,  # not a network server — reached by hotkey, not by socket
        tool="stt",
        description="stt mic — live dictation, idling behind a global hotkey",
    )


def _serve_argv(extra_argv: list[str]) -> list[str]:
    """The FOREGROUND command the service runs (``run``) or detaches (``start``) — and,
    critically, the ONE argv ``enable`` bakes verbatim into a launchd LaunchAgent (or a
    systemd unit) that some LATER process — launchd, at next login, in a minimal
    environment with none of THIS process's cwd or ``sys.path`` — re-execs unchanged. It
    therefore has to be self-sufficient on its own, not merely correct here-and-now.

    A real package install (``uv tool install`` / ``pipx install`` / ``pip install``)
    already is: `stt_cli` sits in ``sys.executable``'s own site-packages there, so
    ``[sys.executable, "-m", "stt_cli.cli", ...]`` needs nothing else, regardless of who
    re-execs it or from where.

    The symlink/dev-checkout install (`install.sh`'s fallback, and this repo's own
    ``./bin/stt``) is NOT: `stt_cli` is only importable there because ``bin/stt`` inserts
    the repo root onto ``sys.path`` at the top of every invocation. Baking bare
    ``-m stt_cli.cli`` into the persisted argv drops that insertion — it can still look
    like it works interactively (a `-m` invocation implicitly adds `cwd` to `sys.path`,
    and `cwd` was the repo root at the time), then breaks the moment launchd re-runs the
    identical argv from its own, cwd-less context: ``ModuleNotFoundError: No module named
    'stt_cli'`` on every relaunch, while ``status``/``launchctl`` keep reporting "running"
    because nothing about enabling the LaunchAgent itself failed. ``_dev_wrapper()``
    re-points the daemon at ``bin/stt`` itself in that case instead — found via this
    module's own ``__file__``, never `cwd`, so it is unaffected by whatever directory
    launchd happens to be running in when it re-execs this later.
    """
    wrapper = _dev_wrapper()
    entry = (
        [sys.executable, str(wrapper)]
        if wrapper is not None
        else [sys.executable, "-m", "stt_cli.cli"]
    )
    return [*entry, "mic", SERVE_TOKEN, *extra_argv]


def _dev_wrapper() -> Path | None:
    """``bin/stt`` next to the checkout `stt_cli` is currently running from, IF it is
    running from a source checkout (the symlink install path) rather than a real package
    install — ``None`` there, since bare ``-m stt_cli.cli`` is already self-sufficient.
    Resolved from THIS module's own ``__file__`` (``stt_cli/live/mic_service.py``, so the
    repo root is two parents up), never `cwd`/`sys.path[0]`, so a caller's working
    directory — an interactive shell's, launchd's own default, a detached daemon's — can
    never change the answer.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    wrapper = repo_root / "bin" / "stt"
    return wrapper if wrapper.is_file() else None
