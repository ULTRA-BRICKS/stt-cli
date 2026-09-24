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
    """The FOREGROUND command the service runs (``run``) or detaches (``start``)."""
    return [sys.executable, "-m", "stt_cli.cli", "mic", SERVE_TOKEN, *extra_argv]
