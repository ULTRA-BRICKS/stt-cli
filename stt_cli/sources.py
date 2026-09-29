"""sources — where a distributable dictionary comes from, verified before anything uses it.

ONE PROTOCOL, TWO PLACES
    ``stt dict search`` and ``stt dict install`` ask a source two questions: the catalog index
    (`catalog.parse_index`'s `Catalog`) and one file's exact bytes. The bundled source answers
    from ``stt_cli/dictionaries/`` inside the installed package, offline; the remote source
    answers from the marketplace website. The command line does not branch on which one it
    holds, so the website can come and go without changing a single command.

EVERY FILE IS CHECKED AGAINST THE INDEX, EVEN THE BUNDLED ONES
    The SHA-256 in the index is compared with the bytes before they are returned, and a
    mismatch is an error before anything is written. From the network that catches a
    truncated or tampered download; from the package it catches a hand-edited install, which
    is a different fault with a different fix — so the two raise different errors.

THE NETWORK IS IMPORTED ONLY WHEN IT IS USED
    ``urllib.request`` is imported inside `_https_get`, never at module level: the command
    modules import this one to build ``stt --help``, and a transcription or a dictation
    session must never pay for, or touch, the network (``docs/specs/dictionaries.md`` §5).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import IO, TYPE_CHECKING, Protocol

from . import catalog
from ._errors import NetworkError, SttError, UsageError
from .dictionary import MAX_DICTIONARY_BYTES
from .jsonio import in_units, read_text

if TYPE_CHECKING:
    import urllib.request
    from http.client import HTTPMessage

# The marketplace website (#86). Renaming the site means changing this one constant.
MARKETPLACE = "https://stt-dicts-marketplace.vercel.app"

# Long enough for a slow link to deliver a few megabytes, short enough that an unreachable
# site is an answer rather than a hang.
TIMEOUT_SECONDS = 20.0

# `(url, limit) -> bytes`: one HTTPS GET, refused past ``limit`` bytes.
Fetcher = Callable[[str, int], bytes]


class DictionarySource(Protocol):
    """Somewhere dictionaries can be listed and fetched from."""

    @property
    def name(self) -> str:
        """Where this source reads from, for messages."""
        ...

    def index(self) -> catalog.Catalog:
        """The parsed catalog index."""
        ...

    def fetch(self, entry: catalog.CatalogEntry) -> bytes:
        """The exact bytes of one dictionary file, their SHA-256 verified against ``entry``."""
        ...


class BundledSource:
    """The dictionaries shipped inside the package: offline, and usable before any website."""

    def __init__(self, directory: Path = catalog.DIRECTORY) -> None:
        self.directory = directory

    @property
    def name(self) -> str:
        return "the dictionaries bundled with stt"

    def index(self) -> catalog.Catalog:
        target = self.directory / catalog.INDEX
        return catalog.parse_index(self._read(target), origin=str(target))

    def fetch(self, entry: catalog.CatalogEntry) -> bytes:
        target = self.directory / f"{entry.id}.json"
        blob = self._read(target)
        if _sha256(blob) != entry.sha256:
            # Exit 1, not a network error: nothing was downloaded, the package itself differs
            # from what it says it holds, and retrying cannot change that.
            raise SttError(
                what=f"the bundled dictionary {entry.id!r} does not match the catalog index",
                why=f"{target} was changed after stt was installed",
                how="nothing was installed; reinstall stt",
            )
        return blob

    def _read(self, target: Path) -> bytes:
        text = read_text(
            target,
            how="reinstall stt",
            limit=MAX_DICTIONARY_BYTES,
            too_big=catalog.TOO_BIG,
        )
        if text is None:
            raise SttError(
                what=f"could not read {target}",
                why="the file is missing from the installed package",
                how="reinstall stt",
            )
        return text.encode("utf-8")


class RemoteSource:
    """The marketplace website: ``GET <base>/api/dictionaries`` and ``.../<id>``."""

    def __init__(self, base: str = MARKETPLACE, *, get: Fetcher | None = None) -> None:
        if not base.startswith("https://"):
            raise UsageError(
                what=f"the dictionary marketplace address {base!r} is not HTTPS",
                why="the index is what every download is verified against, so it must not be "
                "readable or replaceable on the way",
                how="use an https:// address",
            )
        self.base = base.rstrip("/")
        self._get = get or _https_get

    @property
    def name(self) -> str:
        return self.base

    def index(self) -> catalog.Catalog:
        url = f"{self.base}/api/dictionaries"
        return catalog.parse_index(self._get(url, MAX_DICTIONARY_BYTES), origin=url)

    def fetch(self, entry: catalog.CatalogEntry) -> bytes:
        url = f"{self.base}/api/dictionaries/{entry.id}"
        blob = self._get(url, MAX_DICTIONARY_BYTES)
        found = _sha256(blob)
        if found != entry.sha256:
            raise NetworkError(
                what=f"the downloaded dictionary {entry.id!r} does not match the catalog index",
                why=f"its SHA-256 is {found}, the index says {entry.sha256}",
                how="nothing was installed; retry, and report it if it happens again",
            )
        return blob


def _sha256(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def _https_get(url: str, limit: int) -> bytes:
    """One small HTTPS GET, with `hf._request`'s error handling: every failure is diagnosed.

    Synchronous ``urllib``, outside the transcription path, like the login flow. A redirect
    may not leave HTTPS, and a body past ``limit`` is refused rather than read whole.
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "stt-cli"})
    opener = urllib.request.build_opener(_https_only())
    how = "check the network, or install from the bundled dictionaries (without --remote)"
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            blob: bytes = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise NetworkError(
            what=f"{url} answered HTTP {exc.code}", why=str(exc.reason), how=how
        ) from exc
    except urllib.error.URLError as exc:
        raise NetworkError(what=f"could not reach {url}", why=str(exc.reason), how=how) from exc
    except OSError as exc:  # a TLS or socket failure, or a timeout, that urllib did not wrap
        raise NetworkError(what=f"could not reach {url}", why=str(exc), how=how) from exc
    if len(blob) > limit:
        raise NetworkError(
            what=f"{url} sent more than {in_units(limit)}",
            why="a dictionary or an index is a list of names, not a corpus",
            how="nothing was installed; report it if it happens again",
        )
    return blob


def _https_only() -> type[urllib.request.HTTPRedirectHandler]:
    """A redirect handler that follows a redirect only to another HTTPS address.

    Built on demand so this module never imports ``urllib.request`` at the top.
    """
    import urllib.error
    import urllib.request

    class HttpsOnly(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self,
            req: urllib.request.Request,
            fp: IO[bytes],
            code: int,
            msg: str,
            headers: HTTPMessage,
            newurl: str,
        ) -> urllib.request.Request | None:
            if not newurl.lower().startswith("https://"):
                raise urllib.error.HTTPError(
                    newurl, code, "refused a redirect away from HTTPS", headers, fp
                )
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return HttpsOnly
