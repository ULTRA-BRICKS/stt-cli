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

import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agenttools_service import Service

# The hidden entry the service execs in the foreground — see `commands/mic.py::_serve`. Kept
# as a constant here so the argv the descriptor builds and the argv the CLI recognises as
# "this is the hidden entry, not a real user command" can never drift apart.
SERVE_TOKEN = "__serve"
# The hidden entry of the detached process that turns the daemon on after a user's first
# `stt` command (`commands/mic.py::autostart_after`) — never typed by a person.
AUTOSTART_TOKEN = "__autostart"
# The hidden daemon flag carrying the enabling shell's PATH — see `_serve_argv`.
PATH_FLAG = "--search-path"


def mic_service(*, extra_argv: list[str] | None = None) -> Service:
    """Build the :class:`agenttools_service.Service` descriptor for the `stt mic` daemon.

    ``extra_argv`` is the daemon's own flags (``--hotkey-press``, ``--model``, ``--device``, ...),
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
    # launchd runs the job with PATH=/usr/bin:/bin:/usr/sbin:/sbin, where neither a Homebrew
    # ffmpeg nor whisper-server is found — a daemon granted its permissions would then die
    # on "required tool not found: ffmpeg". The PATH of whoever enabled it is where those
    # tools were found, so it travels with the argv (`commands/mic.py::_serve` applies it).
    search_path = os.environ.get("PATH")
    path_flag = [f"{PATH_FLAG}={search_path}"] if search_path else []
    return [*_entry(), "mic", SERVE_TOKEN, *path_flag, *extra_argv]


def autostart_worker_argv() -> list[str]:
    """The argv of the detached process that turns the daemon on (`AUTOSTART_TOKEN`). It
    inherits this process's environment — PATH, `STT_HOME` — so the daemon it enables is
    the one this command would have enabled itself."""
    return [*_entry(), "mic", AUTOSTART_TOKEN]


def _entry() -> list[str]:
    """How to run this very `stt` from a fresh process: `bin/stt` in a source checkout
    (see `_serve_argv` for why), the installed package's module otherwise."""
    wrapper = _dev_wrapper()
    return (
        [sys.executable, str(wrapper)]
        if wrapper is not None
        else [sys.executable, "-m", "stt_cli.cli"]
    )


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


# ---------------------------------------------------------------------------------------
# Launchd liveness — the OS-authoritative check `ServiceManager.status()` deliberately does
# not do. See `commands/mic.py::_status` and ticket #54: for an OS-managed (`enable`d)
# service, the shared lib's own `status()` docstring says "running is inferred from the
# unit being installed... deliberately out of scope here; use the OS's own tools for
# authoritative OS-managed liveness." This is that OS tool.
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LaunchdProbe:
    """What ``launchctl print`` says about the daemon's OS-managed instance right now.

    ``alive`` is ``True`` only when launchd itself reports a live ``pid`` for the job — a
    crash-looping job between restarts (state ``spawn scheduled``) has none, which is
    exactly the "running while nothing is up" gap #54 is about. ``last_exit_code`` is
    whatever launchd last saw the job exit with, whether or not it is alive right now — the
    crash-loop's own diagnostic, not a statement about this instant.
    """

    alive: bool
    pid: int | None
    last_exit_code: int | None


_STATE_RE = re.compile(r"^\tstate = ", re.MULTILINE)
_PID_RE = re.compile(r"^\tpid = (\d+)$", re.MULTILINE)
_LAST_EXIT_RE = re.compile(r"^\tlast exit code = (-?\d+)$", re.MULTILINE)


def probe_launchd(
    label: str,
    *,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
) -> LaunchdProbe | None:
    """Ask launchd itself whether ``label`` has a live process right now.

    ``launchctl print gui/<uid>/<label>`` prints a top-level ``pid = N`` line ONLY while a
    process is actually running; a job stuck retrying (``state = spawn scheduled``, the
    exact machine state #54 was filed from — ``runs=50``, ``last exit code=8``, no live
    process) has no such line. That single fact is the OS-authoritative liveness
    ``ServiceManager.status()`` cannot provide for an `enable`d service.

    Returns ``None`` off macOS, when launchctl has nothing registered for ``label`` at all
    (not enabled, or a domain launchctl itself cannot reach), OR when the output it DID
    print doesn't look like ``launchctl print``'s documented shape at all (no top-level
    ``state = `` line found) — a format ``launchctl print`` is Apple's own undocumented
    debug output, with no stability guarantee, and "the regex didn't match" must not be
    read as "the field is confirmed absent" if the output itself is unrecognized; a real
    format change would otherwise silently misreport a healthy daemon as dead. The caller
    falls back to the shared lib's own read in every ``None`` case, rather than treating
    "no evidence" (of either kind) as "dead".

    ``runner`` is injectable (same shape as ``agenttools_service``'s own autostart-command
    runner) so this is testable without a real ``launchctl`` or a real launchd job.
    """
    if runner is None and sys.platform != "darwin":
        return None
    run = runner or _default_launchctl_runner
    try:
        result = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not _STATE_RE.search(result.stdout):
        return None
    pid_match = _PID_RE.search(result.stdout)
    exit_match = _LAST_EXIT_RE.search(result.stdout)
    return LaunchdProbe(
        alive=pid_match is not None,
        pid=int(pid_match.group(1)) if pid_match else None,
        last_exit_code=int(exit_match.group(1)) if exit_match else None,
    )


def kickstart(
    label: str,
    *,
    runner: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
) -> bool:
    """Have launchd (re)start ``label`` NOW — ``launchctl kickstart -k`` — rather than
    after its restart throttle. `stt mic --fix` uses it on a daemon that is down, or that
    is running code from before it could ask for its own permissions (#98), so the process
    that asks is one running the current code. True when launchctl accepted it."""
    run = runner or _default_launchctl_runner
    try:
        result = run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{label}"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _default_launchctl_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # A quick, synchronous system-state query — not a `proc.run` (the transcription/live
    # path's async, timed, output-draining runner for engines and media), the same way
    # `agenttools_service`'s own autostart-command runner (the launchctl/systemctl calls
    # `enable`/`disable` already make) is a plain synchronous `subprocess.run`, not that.
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=5, check=False)
