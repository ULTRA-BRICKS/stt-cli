"""helper — `stt.app`, the small application stt builds for itself from source (#99).

WHAT IT IS FOR
    Two things a Python process cannot do on its own. macOS gives a notification an action
    button only when an application bundle with its own identifier posts it, so the Stop
    button every burst promises (docs/specs/live-dictation-ux.md, section 6.2) needs one.
    And macOS files the launchd daemon's Accessibility and Microphone grants under the
    process launchd starts: a Homebrew ``python3.14`` today, a new principal with no grants
    after every ``brew upgrade``. With the helper, launchd starts ``stt.app`` and the grants
    belong to "stt" (see `mic_service._serve_argv`).

HOW IT IS BUILT
    `build` compiles ``stt_cli/helper_app/main.swift`` with ``swiftc`` (the Command Line
    Tools Homebrew already needs), writes the ``Info.plist`` below, signs the bundle and
    installs it as ``~/Applications/stt.app``. Nothing is downloaded and no Python
    dependency is added. `stt mic helper` runs it; `stt mic enable` and `stt doctor --fix`
    run it when the installed copy is missing or older than the source (`state`).

HOW IT IS SIGNED, AND WHAT THAT BUYS
    macOS remembers a grant against the signature's designated requirement.
    - A Developer ID certificate, when the keychain has one: stable everywhere.
    - Otherwise a certificate made on this Mac for stt alone (`_local_identity`): the
      requirement is "identifier ai.hyperide.stt, signed by this certificate", so the
      grant survives rebuilds of the helper and upgrades of Python. The certificate is
      self-signed and not trusted by anyone else, which is fine: nobody else runs this copy.
      It lives in a keychain of its own under ``STT_HOME/signing`` with its password beside
      it (owner-only), so anything running as this user could sign a binary macOS would
      treat as stt. That is no wider than before: the helper runs stt's Python, which any
      such process can already edit, and before the helper the principal was Python itself.
    - Ad hoc, if neither works: the requirement is the binary's own hash, so the grant
      survives Python upgrades but not a rebuild of the helper.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .._errors import MissingDependencyError, SttError

APP_NAME = "stt.app"
BUNDLE_ID = "ai.hyperide.stt"
EXECUTABLE = "stt"
SOURCE = Path(__file__).resolve().parent.parent / "helper_app" / "main.swift"
# Bumped when the build itself changes (flags, Info.plist) without the source changing, so
# an installed copy built the old way counts as stale.
_RECIPE = 1
_DIGEST_KEY = "STTSourceDigest"
_CERT_NAME = "stt local code signing"
_TIMEOUT = 120.0
# The helper is a macOS application; everywhere else there is nothing to find or build.
_MACOS = sys.platform == "darwin"
_COMPILE_TIMEOUT = 600.0

State = Literal["current", "stale", "missing"]
Signing = Literal["developer-id", "local-certificate", "ad-hoc"]


class HelperBuildError(SttError):
    """`stt.app` could not be built, signed or installed."""


@dataclass(frozen=True, slots=True)
class Built:
    app: Path
    signing: Signing


def install_dir() -> Path:
    """Where the helper lives. `~/Applications` is a place macOS looks for applications
    (Notification Center and Privacy & Security list it by name) that needs no admin rights."""
    return Path.home() / "Applications"


def app_path() -> Path:
    return install_dir() / APP_NAME


def executable(app: Path | None = None) -> Path:
    return (app or app_path()) / "Contents" / "MacOS" / EXECUTABLE


def source_digest() -> str:
    """What the installed copy has to have been built from to count as current."""
    return hashlib.sha256(SOURCE.read_bytes() + f"\nrecipe={_RECIPE}".encode()).hexdigest()


def state() -> State:
    app = app_path()
    if not os.access(executable(app), os.X_OK):
        return "missing"
    try:
        with (app / "Contents" / "Info.plist").open("rb") as handle:
            digest = plistlib.load(handle).get(_DIGEST_KEY)
        wanted = source_digest()
    except (OSError, plistlib.InvalidFileException, ValueError):
        # Asked on every burst: an unreadable plist, or a package that lost the Swift
        # source, means "do not use it", never an exception in the middle of dictation.
        return "stale"
    return "current" if digest == wanted else "stale"


def installed() -> Path | None:
    """The helper's executable if any copy is installed, current or not."""
    if not _MACOS:
        return None
    found = executable()
    return found if os.access(found, os.X_OK) else None


def current() -> Path | None:
    """The helper's executable when an up-to-date one is installed — the only one stt will
    start, since an older build may speak an older protocol — and `None` otherwise."""
    if not _MACOS:
        return None
    return executable() if state() == "current" else None


def ensure(*, say: bool = True) -> Path | None:
    """Build and install the helper unless an up-to-date one is already there; return its
    executable, or `None` (with one line saying why, unless ``say`` is false) when it cannot
    be built here. Never raises: the hotkey stops a burst with or without the helper."""
    if not _MACOS:
        return None
    found = current()
    if found is not None:
        return found
    if say:
        print(f"stt: building {APP_NAME} (the notification Stop button)…", file=sys.stderr)
    try:
        built = build()
    except (SttError, OSError) as failed:
        if say:
            why = f"{failed.what}: {failed.why}" if isinstance(failed, SttError) else str(failed)
            print(f"stt: {APP_NAME} not built — {why}", file=sys.stderr)
        return None
    if say:
        print(f"stt: installed {built.app} ({describe(built.signing)})", file=sys.stderr)
    return executable(built.app)


def notification_permission() -> str | None:
    """What macOS lets the installed helper do, as it says it (`authorization` in
    `main.swift`): "authorized alert", "denied none", "not-determined none" — without
    asking. `None` when there is no current helper or it did not answer."""
    found = current()
    if found is None:
        return None
    try:
        said = subprocess.run(
            [str(found), "authorization"], capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return None
    return said.stdout.strip() or None


def describe(signing: Signing) -> str:
    return {
        "developer-id": "signed with your Developer ID",
        "local-certificate": f"signed with this Mac's own '{_CERT_NAME}' certificate",
        "ad-hoc": "signed ad hoc — a rebuild will need its permissions granted again",
    }[signing]


def info_plist() -> dict[str, object]:
    return {
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleName": "stt",
        "CFBundleDisplayName": "stt",
        "CFBundleExecutable": EXECUTABLE,
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": str(_RECIPE),
        "LSMinimumSystemVersion": "12.0",
        # No Dock icon and no menu bar: the helper is never an application anybody switches to.
        "LSUIElement": True,
        # Asks for "Alerts" rather than "Banners", so the Stop button stays on screen for the
        # whole burst. macOS 26 was seen to start the app on banners regardless; `stt doctor`
        # reads the style the user has and says where to switch it.
        "NSUserNotificationAlertStyle": "alert",
        "NSMicrophoneUsageDescription": (
            "stt listens only while you dictate: from the hotkey until you stop."
        ),
        _DIGEST_KEY: source_digest(),
    }


def build() -> Built:
    """Compile, sign and install `stt.app`. The copy it replaces is kept beside it until the
    new one is in place and put back if that fails, so an interrupted rebuild never leaves
    the daemon's launchd job pointing at nothing."""
    swiftc = shutil.which("swiftc")
    if swiftc is None:
        raise MissingDependencyError(
            what=f"cannot build {APP_NAME}",
            why="the Swift compiler (swiftc) is not installed",
            how="xcode-select --install, then `stt mic helper`",
        )
    target = app_path()
    previous = target.with_name(f".{APP_NAME}.previous")
    target.parent.mkdir(parents=True, exist_ok=True)
    if previous.exists() and not target.exists():
        previous.rename(target)  # a rebuild killed between its two renames
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".stt-helper-") as scratch:
        app = Path(scratch) / APP_NAME
        exe = executable(app)
        exe.parent.mkdir(parents=True)
        (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info_plist()))
        _run(
            [swiftc, "-O", "-swift-version", "5", "-framework", "AppKit",
             "-framework", "UserNotifications", str(SOURCE), "-o", str(exe)],
            what=f"compiling {APP_NAME} failed",
            timeout=_COMPILE_TIMEOUT,
        )  # fmt: skip
        signing = sign(app)
        if previous.exists():
            shutil.rmtree(previous)
        if target.exists():
            target.rename(previous)
        try:
            app.rename(target)
        except OSError:
            if previous.exists():
                previous.rename(target)
            raise
    if previous.exists():
        shutil.rmtree(previous)
    return Built(app=target, signing=signing)


def sign(app: Path) -> Signing:
    developer = _developer_id()
    if developer is not None:
        _codesign(app, developer)
        return "developer-id"
    try:
        keychain, identity = _local_identity()
        with _on_search_list(keychain):
            _codesign(app, identity)
        return "local-certificate"
    except (SttError, OSError):
        _codesign(app, "-")
        return "ad-hoc"


def _codesign(app: Path, identity: str) -> None:
    _run(
        ["codesign", "--force", "--sign", identity, "--identifier", BUNDLE_ID, str(app)],
        what=f"signing {APP_NAME} failed",
    )


def _developer_id() -> str | None:
    """The SHA-1 of a valid "Developer ID Application" identity, if the keychain has one."""
    try:
        listed = _run(["security", "find-identity", "-v", "-p", "codesigning"], what="")
    except SttError:
        return None
    for line in listed.splitlines():
        found = re.search(r'\b([0-9A-F]{40}) "Developer ID Application:', line)
        if found:
            return found.group(1)
    return None


def signing_dir() -> Path:
    from .. import config

    return config.app_home() / "signing"


def _local_identity() -> tuple[Path, str]:
    """The keychain holding this Mac's own stt signing certificate, and the certificate's
    SHA-1 — made the first time it is needed and reused after, since reusing it is the
    whole point. `codesign` accepts it without its being trusted; it only has to be found."""
    where = signing_dir()
    keychain = where / "stt-signing.keychain-db"
    secret = where / "keychain-password"
    if not keychain.is_file() or not secret.is_file():
        _make_local_identity(where, keychain, secret)
    password = secret.read_text().strip()
    _run(["security", "unlock-keychain", "-p", password, str(keychain)], what="")
    listed = _run(["security", "find-identity", "-p", "codesigning", str(keychain)], what="")
    found = re.search(rf'\b([0-9A-F]{{40}}) "{re.escape(_CERT_NAME)}"', listed)
    if found is None:
        raise HelperBuildError(
            what="the local signing certificate is unusable",
            why=f"{keychain} holds no '{_CERT_NAME}' identity",
            how=f"delete {where} and run `stt mic helper` again",
        )
    return keychain, found.group(1)


def _make_local_identity(where: Path, keychain: Path, secret: Path) -> None:
    openssl = shutil.which("openssl") or "/usr/bin/openssl"
    where.mkdir(parents=True, exist_ok=True)
    os.chmod(where, 0o700)
    keychain.unlink(missing_ok=True)
    password = secrets.token_hex(16)
    with tempfile.TemporaryDirectory() as scratch:
        tmp = Path(scratch)
        (tmp / "cert.cnf").write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
            f"[dn]\nCN={_CERT_NAME}\n"
            "[ext]\nbasicConstraints=critical,CA:false\nkeyUsage=critical,digitalSignature\n"
            "extendedKeyUsage=critical,codeSigning\n"
        )
        _run(
            [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "36500",
             "-config", str(tmp / "cert.cnf"), "-keyout", str(tmp / "key.pem"),
             "-out", str(tmp / "cert.pem")],
            what="making the local signing certificate failed",
        )  # fmt: skip
        _run(
            [openssl, "pkcs12", "-export", "-inkey", str(tmp / "key.pem"),
             "-in", str(tmp / "cert.pem"), "-name", _CERT_NAME,
             "-out", str(tmp / "identity.p12"), "-passout", f"pass:{password}"],
            what="packing the local signing certificate failed",
        )  # fmt: skip
        _run(["security", "create-keychain", "-p", password, str(keychain)], what="")
        # No auto-lock: a keychain that locks itself would ask for its password in a dialog.
        _run(["security", "set-keychain-settings", str(keychain)], what="")
        _run(
            ["security", "import", str(tmp / "identity.p12"), "-k", str(keychain),
             "-P", password, "-f", "pkcs12", "-T", "/usr/bin/codesign"],
            what="importing the local signing certificate failed",
        )  # fmt: skip
        # Without this macOS asks, in a dialog, whether codesign may use the key.
        _run(
            ["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:",
             "-s", "-k", password, str(keychain)],
            what="allowing codesign to use the local certificate failed",
        )  # fmt: skip
    fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(password + "\n")


def _search_list() -> list[str]:
    """The user's keychain search list, as `security` prints it — which is the resolved
    path (`/tmp` comes back as `/private/tmp`), so every comparison goes through realpath."""
    listed = _run(["security", "list-keychains", "-d", "user"], what="")
    return [line.strip().strip('"') for line in listed.splitlines() if line.strip()]


@contextlib.contextmanager
def _on_search_list(keychain: Path) -> Iterator[None]:
    """`codesign` finds an identity only in a keychain on the user's search list, so ours
    is on it for the one signature and taken off again — leaving whatever else was added
    meanwhile exactly as it is."""
    ours = os.path.realpath(keychain)
    before = _search_list()
    if ours in map(os.path.realpath, before):
        yield
        return
    _run(["security", "list-keychains", "-d", "user", "-s", *before, ours], what="")
    try:
        yield
    finally:
        remaining = [entry for entry in _search_list() if os.path.realpath(entry) != ours]
        _run(["security", "list-keychains", "-d", "user", "-s", *remaining], what="")


def _run(argv: list[str], *, what: str | None = None, timeout: float = _TIMEOUT) -> str:
    """One build step. Synchronous and outside `proc.run` on purpose: this is an install
    step a person asked for, not the transcription path, and it happens before any loop.
    A failure carries the tool's own last lines (a compiler error is several), never the
    argv, which may hold the signing keychain's password."""
    step = what or f"{os.path.basename(argv[0])} {argv[1]} failed"
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired as failed:
        raise HelperBuildError(
            what=step, why=f"timed out after {timeout:.0f} s", how="run `stt mic helper` again"
        ) from failed
    except OSError as failed:
        raise HelperBuildError(
            what=step, why=failed.strerror or str(failed), how="run `stt mic helper` again"
        ) from failed
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        raise HelperBuildError(
            what=step,
            why="\n".join(detail[-8:]) if detail else f"exit code {done.returncode}",
            how="fix what the message says, then `stt mic helper`",
        )
    return done.stdout
