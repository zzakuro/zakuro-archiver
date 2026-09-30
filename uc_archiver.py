"""uc-archiver: fetch a file from a vikingfile catalogue, edit it, repack it.

Standalone on purpose. It shares no code with zakuro-tool, because it is a
different job with a different risk profile -- this one downloads multi-gigabyte
archives from the internet and unpacks whatever is inside them, so it needs to
             be readable on its own and shippable into a container by itself. The only thing
             it has in common is the `[Zakuro]` tag on the output name.

The flow, and why each step is here:

  catalogue  a .json listing games, each with a title and a vikingfile link.
             One catalogue is one profile: the remove and add lists are saved
             against it, so a source is set up once and reused after.
  wait       the catalogue's own file size is a human string ("53.3 GB") and
             is empty on some entries, so it cannot be trusted to say whether
             a file is ready. `check-file` is polled until the host answers
             with a real name and byte count, which is also how a file that
             is still being prepared is waited out rather than failed on.
  download   resumable, with a progress line, and a free-space check first:
             these run to 155 GB, and running out of disk halfway leaves a
             partial file that the next run would try to resume.
  extract    through WinRAR where it is installed, 7-Zip otherwise. Entries
             are checked for paths that climb out of the destination before
             anything is written, because an archive from the internet is
             untrusted input, and an entry that climbs out with `..` is a real thing.
  edit       remove patterns, add files.
  repack     with the [Zakuro] tag, then tested.

Stdlib only, so the container needs nothing but a RAR binary.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# Sits beside this file rather than inside it: the downloader is the part
# most likely to be replaced, and keeping it separate means the pipeline above
# reads as a pipeline and not as a networking tutorial.
import parallel

API = "https://vikingfile.com/api"
FILE_URL = "https://vikingfile.com/f/{}"
# The host writes sizes in decimal (3.14 MB) but the values are powers of 1024
# in practice, so the scale is matched the way the site means it rather than
# the way SI would. A wrong constant here would fail every size check.
_UNIT_SCALE = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
               "TB": 1024 ** 4, "KIB": 1024, "MIB": 1024 ** 2,
               "GIB": 1024 ** 3, "TIB": 1024 ** 4}
# How far a size read off a share page may be off, as a fraction. Three
# significant figures of a gigabyte is about 0.2% of it, so half a percent
# covers the rounding with room to spare -- and is only ever applied to a
# figure that came from a page. A Content-Length is exact and is not given
# this.
_SIZE_TOLERANCE = 0.005
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
# The tag that goes on the output name, so anything this made sorts together.
TAG = "[Zakuro]"

# Below this, repacking is pointless and something is wrong upstream.
MIN_ARCHIVE_BYTES = 64 * 1024

LEVELS = ("store", "fast", "normal", "high", "max")
RAR_LEVEL = {"store": "-m0", "fast": "-m1", "normal": "-m3", "high": "-m4", "max": "-m5"}


# --------------------------------------------------------------- small helpers
def say(msg: str) -> None:
    # First, because a progress line is sitting on this row while a download
    # runs, and a log line printed over the top of it makes both unreadable.
    parallel.clear_bar()
    print(msg, flush=True)
    # Also the active job's log when a job is running, so the console and the
    # web UI read the same lines rather than one translating the other.
    import jobs
    jobs.report(msg)


# The downloader logs through this rather than importing it, which would be a
# circle. Set once, here, where both names already exist.
parallel.report = say


def warn(msg: str) -> None:
    parallel.clear_bar()
    print(f"  ! {msg}", flush=True)
    import jobs
    jobs.report(f"  ! {msg}")


def step(msg: str) -> None:
    # Every phase begins with one of these, which makes it the one place a stop
    # request has to be honoured. Checking anywhere else means either missing a
    # phase or threading the check through every one of them.
    #
    # The name goes to the job as well, because this is the only place the
    # pipeline says what phase it is in. Passing it only for the stop check left
    # a job reading "starting" from the first resolve to the finished archive.
    import jobs
    jobs.checkpoint(msg)
    line = f"== {msg} =="
    jobs.report(line)
    parallel.clear_bar()
    print(f"\n{line}", flush=True)


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def free_bytes(path: Path) -> int | None:
    """Free space on the volume holding `path`, or None if it cannot be read.

    None rather than 0 on purpose. A caller that treats 0 as "no room" would
    refuse every run on a volume it cannot measure, and one that treats 0 as
    "unknown" is indistinguishable from a real zero -- which is how a check
    that could not run turns into a check that silently passed and a disk that
    fills up anyway.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(path).free
    except OSError:
        return None


# Disk claimed by runs in this process that have not finished yet. The check a
# run does on its own is right for one download and wrong for several, and
# running the catalogue is several: ten runs each wanting 12 GB all read the
# same free space and all agree there is room.
_room_lock = threading.Lock()
_room_claimed = 0


class _RoomClaim:
    """Hold this run's share of the disk until it ends, however it ends.

    The check used to be each run looking at the volume and each passing, which
    is fine for one download and wrong for several. The claim is taken against a
    shared total, so the second run in a queue is measured against what the
    first one is really using rather than against a number that is about to stop
    being true. It is given back on the way out, including on a failure, so a
    run that dies does not shrink the disk for everything after it.

    A volume that cannot be measured is a skipped check and says so, rather than
    a refusal: an unreadable disk is not a full one, and treating it as full
    would stop every run on it.
    """

    def __init__(self, base: Path, want: int, label: str) -> None:
        self.base, self.want, self.label = base, want, label
        self.amount = 0

    def __enter__(self) -> "_RoomClaim":
        global _room_claimed
        if not self.want:
            return self
        have = free_bytes(self.base)
        if have is None:
            warn(f"could not read the free space on {self.base}, so the room "
                 f"check is being skipped")
            return self
        with _room_lock:
            room = have - _room_claimed
            if room < self.want * 1.05:
                raise SystemExit(
                    f"not enough room for {self.label}: {human(self.want)} "
                    f"needed, {human(room)} free once {human(_room_claimed)} "
                    f"is already claimed by other runs in {self.base}"
                )
            _room_claimed += self.want
            self.amount = self.want
        return self

    def __exit__(self, *_exc) -> bool:
        global _room_claimed
        if self.amount:
            with _room_lock:
                _room_claimed = max(0, _room_claimed - self.amount)
            self.amount = 0
        return False


def _room_others_hold() -> int:
    """Bytes other runs in this process have claimed."""
    with _room_lock:
        return _room_claimed


# ------------------------------------------------------------------- catalogue
@dataclass
class Entry:
    """One row of the catalogue."""

    index: int
    title: str
    uri: str
    file_size: str = ""
    upload_date: str = ""

    @property
    def hash(self) -> str:
        return hash_from_uri(self.uri)

    @property
    def download_url(self) -> str:
        return FILE_URL.format(self.hash)


@dataclass
class Catalogue:
    name: str
    path: Path
    entries: list[Entry] = field(default_factory=list)
    source: str = ""

    def find(self, needle: str) -> list[Entry]:
        """Entries whose title matches, case-insensitively.

        A plain substring, so "higurashi" finds the chapter you meant without
        the version suffix getting in the way, and every match is returned
        rather than the first, because several chapters usually match.
        """
        low = needle.lower()
        return [e for e in self.entries if low in e.title.lower()]

    def by_index(self, raw: str) -> Entry | None:
        try:
            n = int(raw)
        except ValueError:
            return None
        return self.entries[n - 1] if 1 <= n <= len(self.entries) else None


def hash_from_uri(uri: str) -> str:
    """The short hash out of a vikingfile link.

    Accepts a bare hash as well as the full link, because a person pasting one
    out of a browser address bar gets either.
    """
    text = (uri or "").strip().rstrip("/")
    if not text:
        return ""
    tail = text.rsplit("/", 1)[-1]
    match = re.fullmatch(r"[A-Za-z0-9_-]{6,64}", tail)
    return tail if match else ""


def load_catalogue(path: str | Path) -> Catalogue:
    p = Path(path).expanduser()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"catalogue not found: {p}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{p} is not valid JSON: {exc}") from exc

    rows = data.get("downloads") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise SystemExit(f"{p} has no 'downloads' list")

    cat = Catalogue(
        name=str(data.get("name") or p.stem) if isinstance(data, dict) else p.stem,
        path=p,
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        uris = row.get("uris") or []
        uri = next((u for u in uris if isinstance(u, str) and hash_from_uri(u)), "")
        if not uri:
            continue
        cat.entries.append(
            Entry(
                index=len(cat.entries) + 1,
                title=str(row.get("title") or uri),
                uri=uri,
                file_size=str(row.get("fileSize") or ""),
                upload_date=str(row.get("uploadDate") or ""),
            )
        )
    if not cat.entries:
        raise SystemExit(f"{p} lists no downloadable files")
    return cat


def print_catalogue(cat: Catalogue, rows: list[Entry] | None = None) -> None:
    shown = rows if rows is not None else cat.entries
    say(f"{cat.name}: {len(cat.entries)} file(s)  ({cat.path})")
    if rows is not None and len(shown) != len(cat.entries):
        say(f"  showing {len(shown)} match(es)")
    for e in shown:
        size = e.file_size or "-"
        say(f"  {e.index:>4}  {size:>10}  {e.title}")


# ------------------------------------------------------------- vikingfile api
class Viking:
    """The two endpoints this needs, and nothing else."""

    def __init__(self, timeout: int = 60) -> None:
        self.timeout = timeout

    def _post(self, path: str, data: dict) -> list:
        body = urllib.parse.urlencode(data, doseq=True).encode()
        req = urllib.request.Request(
            f"{API}/{path}", data=body,
            headers={"User-Agent": USER_AGENT,
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        payload = json.loads(raw)
        # check-file answers with a list even for one hash.
        return payload if isinstance(payload, list) else [payload]

    def check(self, file_hash: str) -> dict:
        """What the host knows about one hash: exists, and its real name/size.

        Returns {} when the host does not have it, or cannot be reached.
        """
        try:
            rows = self._post("check-file", {"hash": file_hash})
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError) as exc:
            warn(f"check-file did not answer ({exc})")
            return {}
        for row in rows:
            if isinstance(row, dict) and row.get("exist"):
                return row
        return {}

    def check_many(self, hashes: list[str], batch: int = 100) -> dict[str, dict]:
        """Ask about a lot of hashes at once.

        The endpoint takes up to 100 per call, so a 260-entry catalogue is
        three requests instead of 260. Returns {hash: record} for the ones the
        host still has.
        """
        found: dict[str, dict] = {}
        for start in range(0, len(hashes), batch):
            chunk = hashes[start:start + batch]
            try:
                rows = self._post("check-file", {"hash[]": chunk})
            except (urllib.error.URLError, OSError, json.JSONDecodeError,
                    TimeoutError) as exc:
                warn(f"check-file did not answer for {len(chunk)} hashes ({exc})")
                continue
            for row in rows:
                if isinstance(row, dict) and row.get("exist"):
                    found[str(row.get("hash") or "")] = row
        return found

    def wait_until_ready(self, file_hash: str, tries: int = 30,
                         pause: float = 4.0) -> dict:
        """Poll check-file until the file is there, or give up.

        The wait is the point. A catalogue written by hand, or one that was
        up before a re-upload, can name a file the host is still preparing;
        starting the download then gets a 404 or a truncated body, and a
        half-written file is worse than a clear failure because the next run
        would try to resume it.
        """
        for attempt in range(1, tries + 1):
            row = self.check(file_hash)
            if row:
                return row
            if attempt < tries:
                say(f"   not ready yet ({attempt}/{tries}), waiting {pause:.0f}s")
                time.sleep(pause)
        return {}


# ------------------------------------------------------------------- resolve
@dataclass
class Share:
    """What a share page really is, once the challenge has cleared.

    `name` and `size` come off the page rather than out of the catalogue. The
    catalogue's own figure is not to be trusted: on a real entry it claimed
    8 MB for a file that is 3.14 MB, and a size check built on it would either
    pass a truncated download or refuse a good one.
    """

    page_url: str
    name: str
    size: int
    download_url: str

    def to_dict(self) -> dict:
        return {"page_url": self.page_url, "name": self.name, "size": self.size,
                "download_url": self.download_url}


class ShareUnavailable(RuntimeError):
    """The page loaded but no link turned up."""


# How long to let the page load for, in milliseconds.
#
# It was 90 seconds, which is under what the challenge actually takes on a slow
# machine: one measured solve ran 91 seconds by itself, so the page was being
# cut off at almost exactly the moment it succeeded. UC_PAGE_TIMEOUT changes it.
PAGE_TIMEOUT_MS = 300_000

# How many goes at the page before calling it a failure, and how long between
# them. Each re-run happens inside the same browser session, so the Cloudflare
# clearance is still held and the page has a token to work with.
LINK_WAIT_TRIES = 4
LINK_WAIT_SECONDS = 4.0


def scrapling_problem() -> str | None:
    """Why the browser cannot be used, or None when it can."""
    try:
        import scrapling  # noqa: F401
    except ImportError:
        return ("scrapling is not installed -- pip install \"scrapling[all]\" "
                "then scrapling install")
    try:
        from scrapling.fetchers import StealthySession  # noqa: F401
    except Exception as exc:
        return f"scrapling is installed but unusable: {exc}"
    return None


def resolve_share(url_or_hash: str, headed: bool = False,
                  timeout: int = 0, tries: int = 0,
                  pause: float = 0.0) -> Share:
    """Scrape the share page for the link the download actually comes from.

    This is the step the API cannot do. `check-file` says a file exists, what
    it is called and how big it is, but it hands back no link. The link is
    behind a Cloudflare Turnstile, and the page fills it in itself: the widget
    calls `cloudflareCallback`, which POSTs `cf-turnstile-response=<token>` to
    the page's own URL and gets `{"link": "..."}` back, and only then does
    `#download-link` get an href.

    So a plain HTTP GET of the page returns an anchor that is still `hidden`
    with no href, which looks exactly like the challenge having failed. The
    page has to be driven by a real browser and read *after* that POST lands.

    Scrapling's StealthySession does the solving and re-locates the selectors
    if the site moves them, which matters here: the host has already changed
    domain once (vikingfile.com -> vik1ngfile.site) and the download path has
    changed shape before.
    """
    problem = scrapling_problem()
    if problem:
        raise ShareUnavailable(problem)

    timeout = timeout or _env_int("UC_PAGE_TIMEOUT", PAGE_TIMEOUT_MS)
    page_url = FILE_URL.format(url_or_hash) if not url_or_hash.startswith("http") \
        else url_or_hash
    from scrapling.fetchers import StealthySession

    # Retried, because a challenge that does not clear is usually a transient
    # thing -- a busy widget, a rate limit from the last one, a network wobble
    # -- and not evidence that anything is wrong. Observed on a real run: the
    # same share resolved cleanly on the retry having failed outright the
    # first time, and without this it would have failed the whole entry.
    #
    # Observed again on a later run, on a different share, which is what moved
    # the numbers: attempt 1 of 3 came back with no link. This is the step the
    # whole tool rests on -- the API cannot do it -- so three tries was thin.
    # Five, ten seconds apart, and the pause grows, because a challenge that
    # is being rate limited wants longer than a challenge that was merely busy.
    tries = tries or _env_int("UC_RESOLVE_TRIES", 5)
    pause = pause or _env_float("UC_RESOLVE_PAUSE", 10.0)
    last = ""
    for attempt in range(1, max(1, tries) + 1):
        try:
            # _open_share already waits for the link, so it hands back the share
            # rather than the page.
            return _open_share(StealthySession, page_url, headed, timeout)
        except ShareUnavailable as exc:
            last = str(exc)
            if attempt < tries:
                wait = pause * attempt
                say(f"   link did not come out (attempt {attempt}/{tries}), "
                    f"waiting {wait:.0f}s")
                time.sleep(wait)
    raise ShareUnavailable(last)


def _open_share(session_cls, page_url: str, headed: bool, timeout: int):
    """Load the page in a browser and read the download link out of it.

    Kept separate from the parsing so the logger handling cannot wrap it: a
    `return` inside a `finally` swallows whatever exception was in flight, and
    if the fetch fails there is no page to read, so the handler would fail
    again on an unbound name and hide the real error behind a NameError.

    The re-fetch loop is the fix for something this got wrong for a long time.
    `network_idle=True` waits for the network to go quiet -- and a share page
    waiting on Cloudflare is quiet. Nothing is in flight at the moment the
    challenge clears, so the fetch returns *before* the page's own
    `cloudflareCallback` POST has even been sent, and the anchor reads "no
    href, Generating download link". On a fast machine the generation wins that
    race; on a slower one it does not, and every attempt reports the link did
    not come out. Re-fetching inside the same session keeps the Cloudflare
    clearance and re-runs the page's own script, which by then has a token to
    work with.
    """
    import logging

    # scrapling logs "No Cloudflare challenge found" at ERROR when a page needs
    # no challenge -- which happens once a clearance is held, and is the normal
    # case on a second run. Left alone it reads like this tool failing.
    noisy = logging.getLogger("scrapling")
    previous = noisy.level
    noisy.setLevel(logging.CRITICAL)
    try:
        # A fresh session each time. Holding one open across many shares is
        # faster, but the host issues a challenge per share and a stale
        # clearance is a confusing failure much later on.
        with session_cls(headless=not headed, solve_cloudflare=True,
                         network_idle=True) as session:
            page = session.fetch(page_url, timeout=timeout)
            for attempt in range(LINK_WAIT_TRIES):
                share = _share_or_none(page, page_url)
                if share is not None:
                    return share
                if attempt + 1 < LINK_WAIT_TRIES:
                    time.sleep(LINK_WAIT_SECONDS)
                    page = session.fetch(page_url, timeout=timeout)
            raise ShareUnavailable(
                f"the page loaded but never produced a download link -- it is "
                f"still saying \"Generating download link\" after "
                f"{LINK_WAIT_TRIES} goes at it. Cloudflare cleared but the "
                f"link did not follow. Try again, or run with --headed."
            )
    finally:
        noisy.setLevel(previous)


def _share_or_none(page, page_url: str) -> Share | None:
    """Read the share page, or None when the link is not there yet.

    The anchor is on the page before the link is: it reads "Generating download
    link" with no href until the page's own POST comes back. So "no link" is a
    normal intermediate state, not an error, and the caller gets to look again.
    """
    try:
        return _share_from(page, page_url)
    except ShareUnavailable:
        return None


def _share_from(page, page_url: str) -> Share:
    """Read the name, the size and the link out of a loaded share page."""

    def text(selector: str) -> str:
        try:
            value = page.css(selector).get()
        except Exception:
            return ""
        return (value or "").strip() if isinstance(value, str) else ""

    name = text("#filename::text")
    size_text = text("#size::text")
    href = text("#download-link::attr(href)")
    if not name:
        # The ids have moved before. The title carries the same filename.
        name = re.sub(r"\s*[-|]\s*(ViKiNG|UC|vikingfile).*$", "",
                      text("title::text"), flags=re.I).strip()
    if not size_text:
        size_text = text("#file-information p::text")
    if not href:
        # Second chance before giving up: the id may have moved with everything
        # else, and what we are after is a /d/ path anywhere on the page.
        for anchor in page.css("a"):
            try:
                candidate = (anchor.attrib.get("href") or "").strip()
            except Exception:
                continue
            if "/d/" in candidate:
                href = candidate
                break
    if not href:
        raise ShareUnavailable(
            "the page gave no download link. The Cloudflare challenge may "
            "not have cleared, or the site changed shape -- try again, or "
            "run with --headed to watch it happen."
        )
    if href.startswith("/"):
        # Against the page's own origin, not a domain written down here. The
        # host has already moved once (vikingfile.com -> vik1ngfile.site) and
        # both answer today, so a hardcoded one costs nothing today and is a
        # silent wrong-host link the day it does. urljoin against the URL we
        # actually loaded is right wherever the site lives.
        href = urllib.parse.urljoin(page_url, href)
    return Share(
        page_url=page_url,
        name=name or "download.bin",
        size=parse_page_size(size_text),
        download_url=href,
    )


def parse_page_size(text: str) -> int:
    """"2.47 GB" -> a byte count, or 0 when it cannot be read.

    The host writes sizes with a binary scale behind a decimal label --
    "3.14 MB" for 3292956 bytes is 3.1409 MiB -- so the table is keyed on
    1024, not 1000.

    The result is approximate by construction: three significant figures of a
    gigabyte is about 5 MB of rounding, which is why nothing downstream
    compares against it exactly.
    """
    digits = re.sub(r"[^0-9.]", "", text or "")
    unit = (re.sub(r"[^A-Za-z]", "", text or "") or "B").upper()
    scale = _UNIT_SCALE.get(unit)
    if not digits or scale is None:
        return 0
    try:
        return int(float(digits) * scale)
    except ValueError:
        return 0


# ------------------------------------------------------------------- locking
#
# Two runs of the same entry will deadlock, and the symptom is a hang with no
# error: both open the same partial for writing, neither gets a byte out, and
# it looks like a slow network rather than a contradiction. It happened twice
# here before this existed, and both times the fix was "did I start it twice".
#
# The job manager knows about jobs within one process, so the web interface
# cannot hit this. Two command lines in two terminals can, and the tool has no
# way to ask the other one.

def _lock_path(download_path: Path) -> Path:
    return download_path.with_name(download_path.name + ".lock")


class AlreadyRunning(RuntimeError):
    """Another process is already fetching this file."""


class _RunLock:
    """An exclusive claim on one download, released however the run ends.

    Holds the lock file open for its lifetime. On Windows an open handle with
    no sharing is refused by a second process, which is the whole mechanism --
    there is no window in which both believe they hold it. On a filesystem
    that ignores the exclusivity (some network mounts) it degrades to a plain
    file, which is still better than nothing because the contents record the
    pid and the check below catches a stale one.
    """

    def __init__(self, download_path: Path, what: str) -> None:
        self.path = _lock_path(download_path)
        self.what = what
        self._fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            owner = self._owner()
            if owner is None:
                # Nobody holds it. A previous run died without cleaning up.
                self.path.unlink(missing_ok=True)
                return self.__enter__()
            raise AlreadyRunning(
                f"another run is already fetching {self.what} "
                f"(pid {owner}). Stop it first, or delete {self.path.name} "
                f"if you are sure nothing is running."
            ) from None
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *_exc):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)
        return False

    def _owner(self):
        try:
            pid = int(self.path.read_text(encoding="ascii").strip() or 0)
        except (OSError, ValueError):
            return None
        if not pid:
            return None
        # A pid that is not running cannot be holding the file.
        if sys.platform == "win32":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            ctypes.windll.kernel32.CloseHandle(handle)
            return pid
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return pid


# ------------------------------------------------------------------ download
def download(url: str, dest: Path, expect: int = 0, timeout: int = 60,
             referer: str = "", connections: int = 0, cancel=None,
             progress=None) -> int:
    """Fetch `url` to `dest`, resuming a partial file if there is one.

    As many connections as asked for where the host will serve ranges, and one
    where it will not. The host throttles per connection -- which is why its
    own page offers "4 parallel threads" -- so a single stream leaves most of
    the link idle. That is the whole reason this is not just one GET.

    The fallback is the point. Ranges are probed first and a host that
    ignores them gets one connection and nothing changes for it, because four
    threads each sent the whole file would produce an archive the right size
    and full of holes. A chunk that comes back short also brings the whole
    thing back to one connection.

    Returns the byte count on disk. Raises on a short read, because an archive
    that stops halfway is the one failure that wastes the most time: it only
    shows up when the extractor cannot read it, long after the download.
    """
    connections = connections or _default_connections()
    parallel.set_budget(_env_int("UC_TOTAL_CONNECTIONS", connections))
    dest.parent.mkdir(parents=True, exist_ok=True)

    # The bar, once, for whichever connection ends up doing the work -- so the
    # fallback below shows progress rather than going quiet halfway. A caller
    # that brought its own (a job) keeps it, and it has no stream, because the
    # web page is already drawing this same object.
    bar = progress or parallel.Progress(total=0, stream=_cli_stream())

    if connections > 1:
        try:
            ranged, total = parallel.probe(url, referer=referer,
                                           timeout=min(timeout, 30))
        except Exception:
            ranged, total = False, 0
        if ranged and total:
            bar.total = total
            if expect and abs(total - expect) > expect * _SIZE_TOLERANCE:
                warn(f"the server says {human(total)} and the page said "
                     f"{human(expect)}; going with the server")
            # Try the requested count, then fewer, if the host rate limits.
            #
            # The count is a starting point rather than a setting, because the
            # host is not consistent about what it will give: eight connections
            # carried 7.15 MB/s against a share that was not throttling, and
            # got a 429 against one that was. Nothing is lost by walking down,
            # because the file and the sidecar both survive and the next
            # attempt resumes the gaps rather than starting the file again.
            plan = _connection_plan(connections)
            for attempt, wanted in enumerate(plan):
                if attempt:
                    warn(f"throttled at {plan[attempt - 1]} connections; "
                         f"dropping to {wanted}")
                try:
                    got = parallel.fetch_parallel(
                        url, dest, total, connections=wanted, timeout=timeout,
                        referer=referer, cancel=cancel, progress=bar)
                except parallel.Throttled:
                    if wanted == plan[-1]:
                        raise SystemExit(
                            f"the host is rate limiting this share at every "
                            f"connection count tried; "
                            f"{human(parallel._covered(dest))} of "
                            f"{human(total)} downloaded, re-run to continue"
                        )
                    continue
                except parallel.RangeUnsupported as exc:
                    # It sent the whole file instead of the slice asked for.
                    # That thread wrote nothing -- the check is before the
                    # write -- so spans recorded by the others are still
                    # verified 206 reads and are kept the same way. What is
                    # not kept is the file, because a host that ignores ranges
                    # cannot be trusted for the rest.
                    kept = parallel.compact_prefix(dest)
                    warn(f"{exc}; falling back to a single connection"
                         + (f", keeping {human(kept)}" if kept else ""))
                    if not kept:
                        dest.unlink(missing_ok=True)
                    bar.restart()
                    break
                except parallel.DownloadCancelled:
                    parallel.clear_bar()
                    raise
                except (OSError, urllib.error.URLError) as exc:
                    # The spans already written are real bytes -- a rate limit
                    # stops threads, it does not corrupt them -- so the run from
                    # byte 0 is kept and the single connection picks it up. Only
                    # if there is no usable prefix does the file go.
                    kept = parallel.compact_prefix(dest)
                    if kept:
                        warn(f"parallel download failed ({exc}); keeping "
                             f"{human(kept)} of it and continuing on one "
                             f"connection")
                    else:
                        warn(f"parallel download failed ({exc}); falling back "
                             f"to a single connection")
                        dest.unlink(missing_ok=True)
                    bar.restart()
                    break
                else:
                    bar.finish()
                    return got

    try:
        got = _download_single(url, dest, expect=expect, timeout=timeout,
                               referer=referer, cancel=cancel, progress=bar)
    except BaseException:
        parallel.clear_bar()
        raise
    bar.finish()
    return got


def _connection_plan(connections: int) -> list[int]:
    """The connection counts to try, in order, largest first.

    Always ends at 1, because a single connection is the one path that cannot
    be rate limited by connection count -- and the one the host's own page
    implies is acceptable. A caller that asked for 1 gets only 1: someone who
    pinned it has already decided.
    """
    lower = [c for c in parallel.DOWNGRADE_TO if c < connections]
    return [connections] + lower + ([1] if 1 not in lower and connections > 1
                                    else [])


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _cli_stream():
    """Where a progress line goes, or None when there is no terminal to draw on.

    A redirected or piped run gets nothing rather than a wall of carriage
    returns, so the log stays readable. The numbers are still on the object --
    this is only about where they are drawn.
    """
    import sys
    try:
        return sys.stdout if sys.stdout.isatty() else None
    except (AttributeError, ValueError):
        return None


def _download_single(url: str, dest: Path, expect: int = 0, timeout: int = 60,
                     referer: str = "", cancel=None, progress=None) -> int:
    """One connection, resuming, with the checks the old path had."""
    return parallel.fetch_single(
        url, dest, expect=expect, timeout=timeout, referer=referer,
        cancel=cancel, progress=progress, size_tolerance=_SIZE_TOLERANCE)


def _default_connections() -> int:
    """How many connections to use, from the environment."""
    raw = os.environ.get("UC_CONNECTIONS", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return parallel.DEFAULT_CONNECTIONS


# --------------------------------------------------------------- the archiver
class Archiver:
    """WinRAR where it is installed, 7-Zip otherwise.

    Both read and write. Neither is imported -- a missing one is a message, not
    a crash -- and if both are present the first is preferred and the other is
    kept as a second opinion, because the catalogues are full of .7z and a
    build that cannot read one should reach for the tool that can rather than
    stopping.
    """

    def __init__(self, explicit: str | None = None) -> None:
        self.primary: Path | None = None
        self.secondary: Path | None = None
        found: list[Path] = []
        for candidate in self._candidates(explicit):
            if not candidate:
                continue
            path = Path(candidate)
            if not path.is_file() or path in found:
                continue
            found.append(path)
            if len(found) == 2:
                break
        if not found:
            raise SystemExit(
                "no archiver found.\n"
                "  Windows: install WinRAR, or point --archiver at Rar.exe\n"
                "  Linux  : RARLAB publishes 'RAR for Linux x64' at rarlab.com,\n"
                "            or install p7zip-full and it will be picked up"
            )
        self.primary = found[0]
        self.secondary = found[1] if len(found) > 1 else None

    @staticmethod
    def _candidates(explicit: str | None):
        if explicit:
            yield explicit
        if os.name == "nt":
            yield r"C:\Program Files\WinRAR\Rar.exe"
            yield r"C:\Program Files (x86)\WinRAR\Rar.exe"
            yield r"C:\Program Files\7-Zip\7z.exe"
            yield r"C:\Program Files (x86)\7-Zip\7z.exe"
        else:
            yield "/usr/local/bin/rar"
            yield "/usr/bin/rar"
            yield "/opt/rar/rar"
        for name in ("rar", "7z", "7za", "7zz"):
            yield shutil.which(name)

    @property
    def kind(self) -> str:
        return "rar" if "rar" in self.primary.name.lower() else "7z"

    @property
    def label(self) -> str:
        extra = f", with {self.secondary.name} as backup" if self.secondary else ""
        return f"{self.kind} ({self.primary}){extra}"

    def _run(self, exe: Path, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(exe), *args], cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, errors="replace", check=False,
        )

    def extract(self, archive: Path, dest: Path) -> None:
        """Unpack, trying the preferred tool and then the other one.

        The retry is not paranoia for its own sake: these catalogues are full
        of .7z, and a build with only one archiver available should still get
        the job done if the other one is sitting there.
        """
        dest.mkdir(parents=True, exist_ok=True)
        tried: list[str] = []
        for exe in [p for p in (self.primary, self.secondary) if p]:
            seven = "rar" not in exe.name.lower()
            args = (["x", "-o+", "-y", str(archive), str(dest) + os.sep] if not seven
                    else ["x", "-y", f"-o{dest}", str(archive)])
            proc = self._run(exe, args)
            tried.append(f"{exe.name} exit {proc.returncode}")
            if proc.returncode in (0, 1) and _has_files(dest):
                if seven is False and len(tried) > 1:
                    warn(f"{self.primary.name} could not read it; used {exe.name}")
                return
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise SystemExit(
            f"extract failed ({'; '.join(tried)}): "
            f"{detail[-1] if detail else 'no output'}"
        )

    def list_names(self, archive: Path) -> list[str]:
        """Every entry name in the archive, without extracting it."""
        for exe in [p for p in (self.primary, self.secondary) if p]:
            if "rar" in exe.name.lower():
                proc = self._run(exe, ["lb", str(archive)])
                if proc.returncode == 0:
                    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
            else:
                proc = self._run(exe, ["l", "-slt", str(archive)])
                if proc.returncode == 0:
                    return [ln.split(" = ", 1)[1] for ln in proc.stdout.splitlines()
                            if ln.startswith("Path = ")]
        return []

    def unpacked_size(self, archive: Path) -> int:
        """How big this archive gets once it is unpacked, in bytes.

        Asked of the archiver rather than worked out, because the only number
        anyone has before unpacking is the compressed one, and the two are not
        related by anything predictable. A real entry compressed 23.5:1 -- a
        3.1 MB .7z holding 73.7 MB -- while another sat near 1.3:1. A free
        space check against the download size would have cleared the first and
        then run out of disk part way through unpacking it.

        Returns 0 when the archiver will not say, which is the caller being
        told "unknown" rather than "fine".
        """
        for exe in [p for p in (self.primary, self.secondary) if p]:
            if "rar" in exe.name.lower():
                # `vt` is the technical listing. It ends with a totals line
                # that is "<bytes> <count> <count> files, <n> folders" --
                # matching on that rather than on any two-number line, because
                # every file in the listing is also two numbers and a size.
                proc = self._run(exe, ["vt", str(archive)])
                if proc.returncode != 0:
                    continue
                found = re.search(r"^\s*(\d+)\s+\d+\s+\d+ files", proc.stdout,
                                   re.MULTILINE)
                if found:
                    return int(found.group(1))
            else:
                proc = self._run(exe, ["l", "-slt", str(archive)])
                if proc.returncode != 0:
                    continue
                # -slt gives one "Size = n" per entry and no total of its own,
                # so it has to be added up. Taking the largest instead would
                # have reported a single 70 MB file as the size of a 73 MB
                # archive, which is the kind of wrong that still looks
                # plausible.
                sizes = [int(m) for m in
                         re.findall(r"^Size = (\d+)\s*$", proc.stdout, re.MULTILINE)]
                if sizes:
                    return sum(sizes)
        return 0

    def pack(self, source: Path, archive: Path, level: str = "normal",
             test: bool = True) -> None:
        archive.parent.mkdir(parents=True, exist_ok=True)
        exe = self.primary
        if self.kind == "rar":
            args = ["a", "-r", "-y", RAR_LEVEL.get(level, "-m3"), str(archive), "." + os.sep]
        else:
            seven_level = {"store": 0, "fast": 1, "normal": 6, "high": 7, "max": 9}.get(level, 6)
            args = ["a", f"-mx={seven_level}", "-y", str(archive), "."]
        proc = self._run(exe, args, cwd=source)
        if proc.returncode != 0 or not archive.is_file():
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise SystemExit(f"pack failed: {detail[-1] if detail else proc.returncode}")
        if test:
            self.test_archive(archive)

    def test_archive(self, archive: Path) -> None:
        proc = self._run(self.primary, ["t", str(archive)])
        if proc.returncode != 0:
            raise SystemExit("the new archive does not verify; not calling it done")


def _has_files(root: Path) -> bool:
    try:
        return any(p.is_file() for p in root.rglob("*"))
    except OSError:
        return False


# ---------------------------------------------------------------- the editing
def is_unsafe_entry(name: str) -> bool:
    """Would extracting this entry write outside the destination?

    An archive that arrived over the internet is untrusted. Absolute paths,
    drive letters, UNC prefixes and `..` segments are all ways an entry can
    land somewhere it was never meant to, and the archiver will happily do it
    unless something stops to check.

    The path is normalised first, so a `dir/..` that resolves back to where it
    started is judged as the harmless no-op it is, while a `dir/../../x` that
    genuinely climbs out is still caught. Judging the raw string instead would
    flag the first and, if anyone ever loosened it to fix that, miss the
    second.
    """
    if not name:
        return False
    text = name.replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return True
    parts = [p for p in text.split("/") if p and p != "."]
    depth = 0
    for part in parts:
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        else:
            depth += 1
    return False


def screen_entries(names: list[str], what: str = "archive") -> list[str]:
    """Refuse to extract if anything in the listing is unsafe."""
    bad = [n for n in names if is_unsafe_entry(n)]
    if bad:
        sample = ", ".join(bad[:5])
        more = f" (+{len(bad) - 5} more)" if len(bad) > 5 else ""
        raise SystemExit(
            f"refusing to extract: the {what} contains {len(bad)} entry/entries "
            f"that point outside the destination folder: {sample}{more}"
        )
    return names


def _norm(value: str) -> str:
    """One spelling for a path or a pattern, whatever platform wrote it.

    Separators become `/` and case is folded. Both halves matter: a profile
    written on Windows says `Redist\\stuff` or `Online`, and the container
    has to answer the same way, or the same release comes out different
    depending on where it was built.

    A trailing separator is dropped. `Redist\\` is what a hand-written profile
    on Windows actually says, and leaving it on made the pattern match
    nothing at all -- `Redist` on its own would not have matched it either
    way, so a typo like that silently removed nothing and said so.
    """
    return value.replace("\\", "/").rstrip("/").lower()


def remove_matches(root: Path, patterns: list[str], dry_run: bool = False) -> list[str]:
    """Delete what the patterns name, deepest first.

    Every match goes, not the first one found: a library has the same folder
    at the root and under `bin/` and under `x64/`, and "remove Online" means
    all of them.

    A pattern matches against the path relative to the root, against the bare
    name, and against each path component in turn -- so `logs` takes
    `logs/old.log` without a wildcard, `*.dll` takes them at any depth, and
    `Redist` takes that folder and everything under it.

    Matching ignores case on every platform. `fnmatch` alone does not: it
    folds case through `os.path.normcase`, which lowercases on Windows and
    leaves it alone on Linux, so `Online` would have taken `online` and
    `ONLINE` in the first and only `Online` in the second.
    """
    wanted = [_norm(p) for p in patterns if p and p.strip()]
    removed: list[str] = []
    victims: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.exists():
            continue
        rel = _norm(path.relative_to(root).as_posix())
        name = _norm(path.name)
        for pattern in wanted:
            if (fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern)
                    or fnmatch.fnmatch(rel, f"{pattern}/*")
                    or rel == pattern or rel.startswith(f"{pattern}/")
                    or any(fnmatch.fnmatch(part, pattern) for part in rel.split("/"))):
                victims.append(path)
                break
    for path in victims:
        if not path.exists():
            continue
        rel = path.relative_to(root).as_posix()
        removed.append(rel)
        if dry_run:
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as exc:
            warn(f"could not remove {rel}: {exc}")
    return removed


def split_extra(entry: str) -> tuple[str | None, Path]:
    """`NAME=path` renames on the way in; a bare path keeps its own name.

    Decided by whether the right-hand side actually exists, not by looking for
    an `=`. A Windows path may well have one in a directory name, and
    `C:/my=games/thing.exe` split on the first `=` is `C:/my` renamed to
    `games/thing.exe`, which fails on both halves at once.

    The name may be a path, so a file can be put somewhere specific rather
    than only at the root: `bin/x64/steam_api64.dll=C:/patches/...` is the
    usual thing to want and used to be refused outright. What is still
    refused is anything that would climb out of the game folder or name a
    drive -- a target is a place inside the tree, not anywhere on the disk.
    """
    if "=" in entry:
        name, _, raw = entry.partition("=")
        name, raw = name.strip(), raw.strip()
        if name and raw and Path(raw).exists() and _is_inside_target(name):
            return name.replace("\\", "/").lstrip("./"), Path(raw).expanduser()
    return None, Path(entry).expanduser()


def _is_inside_target(name: str) -> bool:
    """Is this a path that stays inside the game folder?

    Separators are fine -- `bin/x64/thing.dll` is the point of allowing them.
    Climbing out with `..`, starting at the root, or carrying a drive letter
    are not, since any of those puts a file somewhere the release is not.
    """
    if not name or name.startswith(("/", "\\")) or ":" in name:
        return False
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    return bool(parts) and ".." not in parts


def retag_inner_folder(root: Path, wanted: str) -> tuple[str, str] | None:
    """Rename a lone top-level folder that still carries the UC branding.

    Returns (old, new) or None. `wanted` is the stem the archive itself is
    getting, so the folder inside and the archive around it end up agreeing.

    Deliberately timid. Only one top-level directory is considered, and only
    if stripping UC actually changes its name -- so a release that unpacks to
    several folders, or whose single folder is the game's own (Data, bin,
    Touhou Luna Nights with no branding on it), comes out untouched. Renaming
    a folder the game did not name after its packer would be a surprise in a
    different direction.
    """
    try:
        children = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return None
    if len(children) != 1:
        return None
    folder = children[0]
    cleaned = strip_uc(folder.name)
    if cleaned == folder.name:
        return None
    target = root / wanted
    if target.exists():
        return None
    try:
        folder.replace(target)
    except OSError as exc:
        warn(f"could not rename {folder.name}: {exc}")
        return None
    return (folder.name, target.name)


def add_files(root: Path, entries: list[str], dry_run: bool = False,
              standard: bool = False) -> list[str]:
    """Copy files and whole folders in, honouring `NAME=path`.

    A folder source is copied recursively, so pointing at a prepared
    `redist/` brings the lot rather than an empty shell.

    A source that is not there stops the run, and this used to have a
    `required=False` that made the standard set a warning instead. That is how
    a release came out with no runtime installers and a success message: these
    paths belong to one machine, and a folder that has moved was reported and
    skipped. Not wanting the standard set is now expressed by not listing it --
    `"default_add": []` -- rather than by a folder being missing, and those two
    are not the same thing. `standard` only changes how the refusal reads.
    """
    placed: list[str] = []
    for entry in entries:
        name, source = split_extra(entry)
        if not source.exists():
            hint = ""
            if "=" in entry:
                hint = ("  (if that was meant as NAME=path, the part after the "
                        "'=' does not exist)")
            if standard:
                hint += ("\n   This is part of every release. Point it "
                         "somewhere that exists with UC_REDIST or UC_README, "
                         "or set \"default_add\": [] if you do not want it.")
            raise SystemExit(f"{'standard file' if standard else '--add'}: "
                             f"not found: {source}{hint}")
        target = root / name if name else root / source.name
        if dry_run:
            placed.append(target.relative_to(root).as_posix())
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Clear whatever is in the way first, and clear it correctly for
            # what it is. rmtree on a file raises, which turned "add this
            # folder" into "WinError 267: the directory name is invalid"; and
            # copy2 onto an existing directory copies *into* it, which quietly
            # produced steam_api64.dll/steam_api64.dll and left the directory it
            # was meant to replace still sitting there.
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
            if source.is_dir():
                shutil.copytree(source, target)
            else:
                shutil.copy2(source, target)
        except OSError as exc:
            raise SystemExit(f"--add: could not copy {source}: {exc}") from exc

        placed.append(target.relative_to(root).as_posix())
    return placed


# ------------------------------------------------------------------- profiles
def default_profile_dir() -> Path:
    """`uc-archiver/profiles/`, next to this script.

    Not relative to the working directory: the same catalogue should find the
    same profile whether it is run from the tool folder, from a container's
    /work, or from a shell somewhere else entirely.
    """
    return Path(__file__).resolve().parent / "profiles"


def profile_path(catalogue: Catalogue, base: Path) -> Path:
    """One profile per catalogue, named after it.

    The catalogue is the unit a source is configured by, so that is the unit
    the settings hang off: set the remove and add lists once for a source and
    every later run of that source reuses them.

    Named `<slug>.profile.json` rather than `<slug>.json`, so a profile is
    never mistaken for the catalogue it belongs to.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", catalogue.name).strip("-").lower() or "default"
    return base / f"{slug}.profile.json"


# What goes into every release unless a profile says otherwise: the runtime
# installers as a folder (the people who get the release need them, and a
# folder of installers is what the folder is for), and the readme.
#
# Absolute paths, so they are only true on the machine that set them up. That
# is why these are added as `required=False`: a Desktop that has moved should
# cost a warning, not stop every run. An explicit --add still fails loudly,
# because that one was asked for by name.
# Always stripped from a release. A .url is a browser bookmark, and the one
# in these archives points at whoever packed it -- union-crax.xyz and the
# like. It is not part of the game, it is an advert for them, and it has no
# business shipping in a release that carries somebody else's name on it.
# Kept separate from the profile's `remove` for the same reason `default_add`
# is: a pattern the user chose is theirs, this is not.
DEFAULT_REMOVE = ["*.url"]

DEFAULT_ADD = [
    r"C:\Users\Mfree\OneDrive\Desktop\~Common Redist",
    r"C:\Users\Mfree\OneDrive\Documents\zakuro-tool\ReadME.txt",
]

# What the two above are called, and where they can be redirected to.
#
# Both are absolute paths on one machine, and both are baked into the image by
# `COPY profiles/`. On a server neither exists, and a missing standard file used
# to be a warning, so a release came out with no runtime installers and a
# success message. Setting these points the same two things somewhere that does
# exist -- a mount, a synced folder -- without editing the profile.
REDIST_NAME = "~Common Redist"
README_NAME = "ReadME.txt"


def standard_add(configured: list[str]) -> list[str]:
    """The standard set, with the two machine-specific paths redirected.

    Matched by what an entry is *called* rather than by its position, so
    reordering the list cannot point the redist at the readme.
    """
    redist = os.environ.get("UC_REDIST", "").strip()
    readme = os.environ.get("UC_README", "").strip()
    out = []
    for entry in configured:
        tail = re.split(r"[\\/]", entry.rstrip("\\/"))[-1]
        if tail == REDIST_NAME and redist:
            entry = redist
        elif tail == README_NAME and readme:
            entry = readme
        out.append(entry)
    return out


@dataclass
class Profile:
    remove: list[str] = field(default_factory=list)
    add: list[str] = field(default_factory=list)
    # The standard set, added to every release. Separate from `add` because it
    # is optional: these paths are absolute and only true on one machine, so
    # one that has moved is a warning rather than a failure.
    default_add: list[str] = field(default_factory=lambda: list(DEFAULT_ADD))
    default_remove: list[str] = field(default_factory=lambda: list(DEFAULT_REMOVE))
    level: str = "normal"
    archiver: str = ""
    output_dir: str = ""
    keep_download: bool = False
    work_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "remove": list(self.remove),
            "add": list(self.add),
            "default_add": list(self.default_add),
            "default_remove": list(self.default_remove),
            "level": self.level,
            "archiver": self.archiver,
            "output_dir": self.output_dir,
            "keep_download": self.keep_download,
            "work_dir": self.work_dir,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        return cls(
            remove=[str(x) for x in (data.get("remove") or [])],
            add=[str(x) for x in (data.get("add") or [])],
            default_add=([str(x) for x in data["default_add"]]
                         if "default_add" in data else list(DEFAULT_ADD)),
            default_remove=([str(x) for x in data["default_remove"]]
                            if "default_remove" in data else list(DEFAULT_REMOVE)),
            level=str(data.get("level") or "normal"),
            archiver=str(data.get("archiver") or ""),
            output_dir=str(data.get("output_dir") or ""),
            keep_download=bool(data.get("keep_download")),
            work_dir=str(data.get("work_dir") or ""),
        )


def load_profile(path: Path) -> Profile:
    try:
        return Profile.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return Profile()


def save_profile(path: Path, profile: Profile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile.to_dict(), indent=2) + "\n",
                    encoding="utf-8", newline="\n")


# ----------------------------------------------------------------------- name
TAG = "[Zakuro]"

# The catalogue these come from brands its archives with a trailing "UC" --
# "Hollow Knight - UC.7z". That is somebody else's mark on a file that is
# about to carry ours, so it comes off and the Zakuro tag goes on in its
# place. Matched on a word boundary at the end of the name, so a game whose
# own title ends in those letters is left alone.
_UC_TAIL = re.compile(r"[\s\-_–—]*\buc\b[\s\-_–—]*$", re.IGNORECASE)


def strip_uc(name: str) -> str:
    """`'Hollow Knight - UC' -> 'Hollow Knight'`. Falls back to the input."""
    return _UC_TAIL.sub("", name).strip() or name


def zakuro_name(stem: str, ext: str) -> str:
    """`Game [Zakuro].rar`, from any of the shapes a source name arrives in.

    Takes off the UC branding, takes off a tag that is already there so a
    re-run does not produce `Game [Zakuro] [Zakuro].rar`, and puts ours on.
    """
    clean = re.sub(r"\s*\[Zakuro\]\s*", " ", stem, flags=re.IGNORECASE)
    clean = strip_uc(clean).strip()
    return f"{clean} {TAG}{ext}"


# ------------------------------------------------------------------------ main
def pick_entry(cat: Catalogue, args) -> Entry:
    """Work out which row of the catalogue is wanted.

    An index wins, then an exact title, then a substring. An exact title is
    tried before a substring so a catalogue that holds both "Hades" and
    "Hades II" does the obvious thing when you name one exactly.
    """
    if args.pick:
        found = cat.by_index(args.pick)
        if found is None:
            raise SystemExit(f"no entry numbered {args.pick} (1-{len(cat.entries)})")
        return found
    needle = args.match
    if not needle:
        raise SystemExit("say which one: --pick N, or --match TEXT, or --list")
    exact = [e for e in cat.entries if e.title.lower() == needle.lower()]
    if len(exact) == 1:
        return exact[0]
    hits = cat.find(needle)
    if not hits:
        raise SystemExit(f"nothing in {cat.name} matches {needle!r}")
    if len(hits) > 1 and not args.first:
        print_catalogue(cat, hits)
        raise SystemExit(
            f"{len(hits)} match; pass --pick N for one, or --first to take the top"
        )
    return hits[0]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="uc-archiver",
        description="Fetch a file from a vikingfile catalogue, strip and add files, "
                    "repack it with the [Zakuro] tag.",
    )
    p.add_argument("catalogue", nargs="?", help="the catalogue .json")
    g = p.add_argument_group("web interface")
    g.add_argument("--serve", action="store_true",
                   help="run the web interface instead of a one-off job")
    g.add_argument("--host", default=os.environ.get("UC_HOST", "127.0.0.1"),
                   help="address for --serve (default: loopback only; anything "
                        "else needs UC_TOKEN set)")
    g.add_argument("--port", type=int, default=8073, help="port for --serve")
    p.add_argument("--pick", metavar="N", help="entry number, as listed by --list")
    p.add_argument("--match", metavar="TEXT", help="pick by title, exactly or in part")
    p.add_argument("--first", action="store_true",
                   help="with several matches, take the first instead of stopping")
    p.add_argument("--list", action="store_true", dest="do_list",
                   help="list the catalogue and exit")
    p.add_argument("--find", metavar="TEXT",
                   help="list only the rows whose title contains TEXT")
    p.add_argument("--check", action="store_true",
                   help="ask the host about every entry and report which are gone")

    g = p.add_argument_group("what to change")
    g.add_argument("--remove", action="append", metavar="PATTERN", default=None,
                   help="delete what matches, relative to the game folder "
                        "(repeatable; a bare name takes a whole folder)")
    g.add_argument("--add", action="append", metavar="[NAME=]PATH", default=None,
                   help="copy a file or folder in, optionally renamed "
                        "(repeatable)")

    g = p.add_argument_group("where things live")
    g.add_argument("--work-dir", help="where to download and unpack (default: a temp folder)")
    g.add_argument("--output-dir", help="where the finished archive goes (default: next to the work dir)")
    g.add_argument("--force", action="store_true",
                   help="replace an output archive that already exists")
    g.add_argument("--keep-download", action="store_true",
                   help="keep the downloaded archive after repacking")

    g = p.add_argument_group("how it is built")
    g.add_argument("--level", choices=LEVELS, default=None, help="compression level")
    g.add_argument("--archiver", metavar="PATH", help="rar.exe / rar / 7z to use")
    g.add_argument("--no-test", action="store_true", help="skip the archive test after packing")
    g.add_argument("--timeout", type=int, default=90, help="per-request timeout in seconds")
    g.add_argument("--headed", action="store_true",
                   help="show the browser, to watch the Cloudflare challenge clear")

    g = p.add_argument_group("profile")
    g.add_argument("--profile-dir",
                   help="where profiles are kept (default: the profiles folder "
                        "next to this script)")
    g.add_argument("--save-profile", action="store_true",
                   help="remember these settings against this catalogue")
    g.add_argument("--no-profile", action="store_true",
                   help="ignore a saved profile for this run")
    g.add_argument("--show-profile", action="store_true",
                   help="print the saved profile for this catalogue and exit")
    g.add_argument("--dry-run", action="store_true",
                   help="say what would happen, download nothing")
    return p


def resolve_settings(args, cat: Catalogue, base: Path) -> tuple[Profile, Path]:
    """Saved profile for this catalogue, with the command line on top.

    Anything given on the command line wins; anything not given comes from the
    profile, which is the point of having one -- a source is set up once and
    every later run of it repeats without the flags.
    """
    path = profile_path(cat, base)
    profile = Profile() if args.no_profile else load_profile(path)
    # A profile saved before this existed carries the standard set implicitly.
    if profile.default_add is DEFAULT_ADD:
        profile.default_add = list(DEFAULT_ADD)
    if profile.default_remove is DEFAULT_REMOVE:
        profile.default_remove = list(DEFAULT_REMOVE)
    if args.remove is not None:
        profile.remove = list(args.remove)
    if args.add is not None:
        profile.add = list(args.add)
    if args.level:
        profile.level = args.level
    if args.archiver:
        profile.archiver = args.archiver
    if args.output_dir:
        profile.output_dir = args.output_dir
    if args.work_dir:
        profile.work_dir = args.work_dir
    if args.keep_download:
        profile.keep_download = True
    return profile, path


def check_catalogue(cat: Catalogue, viking: Viking) -> int:
    """Report which entries the host still has, and which have gone.

    Worth having as its own command. A catalogue is a list somebody wrote down
    some time ago and links rot: the 260-entry UnionCrax list has 27 dead
    links in it, and the only way to find out which is to ask. It also surfaces
    where the catalogue's own size disagrees with the host, which is how you
    can tell a re-upload from the original.
    """
    step(f"checking {len(cat.entries)} entries against the host")
    found = viking.check_many([e.hash for e in cat.entries])
    alive = [e for e in cat.entries if e.hash in found]
    dead = [e for e in cat.entries if e.hash not in found]
    say(f"   present : {len(alive)}")
    say(f"   gone    : {len(dead)}")
    if dead:
        say("\n   gone (the host does not have these any more):")
        for e in dead:
            say(f"     {e.index:>4}  {e.title}")
    mismatched = []
    for e in alive:
        said = e.file_size.strip()
        real = int(found[e.hash].get("size") or 0)
        if said and real:
            try:
                gib = float("".join(c for c in said if c.isdigit() or c == "."))
                unit = said.split()[-1].lower() if " " in said else said[-2:].lower()
                factor = {"b": 1 / 2**30, "kb": 1 / 2**20, "mb": 1 / 2**10}.get(unit, 1)
                if abs(gib * factor - real / 2**30) > 0.05 * max(real / 2**30, 1):
                    mismatched.append((e, said, real))
            except ValueError:
                pass
    if mismatched:
        say(f"\n   size differs from the catalogue for {len(mismatched)} "
            f"(probably re-uploaded):")
        for e, said, real in mismatched[:10]:
            say(f"     {e.index:>4}  {e.title[:38]:40} catalogue {said:>9}  "
                f"host {human(real)}")
    return 0 if alive else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.serve:
        import webapp
        webapp.serve(host=args.host, port=args.port,
                     token=os.environ.get("UC_TOKEN", ""),
                     catalogue=args.catalogue,
                     work_dir=args.work_dir, out_dir=args.output_dir)
        return 0
    if not args.catalogue:
        build_parser().print_help()
        return 2

    cat = load_catalogue(args.catalogue)
    if args.find:
        hits = cat.find(args.find)
        if not hits:
            raise SystemExit(f"nothing matches {args.find!r}")
        print_catalogue(cat, hits)
        return 0
    if args.do_list:
        print_catalogue(cat)
        return 0

    if args.check:
        return check_catalogue(cat, Viking(timeout=args.timeout))

    base = Path(args.profile_dir).expanduser() if args.profile_dir else default_profile_dir()
    profile, prof_path = resolve_settings(args, cat, base)
    if args.show_profile:
        say(f"{prof_path}")
        say(json.dumps(profile.to_dict(), indent=2))
        return 0

    # Saved before anything else happens, so `--dry-run --save-profile` is how
    # a source is set up: record the remove and add lists, see what it would
    # do, and download nothing. Saving after the work would mean the one run
    # you most want to rehearse is the one that cannot record itself.
    if args.save_profile:
        save_profile(prof_path, profile)
        say(f"profile: {prof_path}")

    entry = pick_entry(cat, args)
    say(f"{cat.name} -> {entry.title}")
    say(f"   {entry.download_url}")
    if entry.file_size:
        say(f"   catalogue says {entry.file_size}")

    archiver = Archiver(profile.archiver or args.archiver)
    say(f"   archiver: {archiver.label}")

    work_base = Path(profile.work_dir).expanduser() if profile.work_dir else Path(tempfile.gettempdir())
    work_base.mkdir(parents=True, exist_ok=True)
    out_dir = Path(profile.output_dir).expanduser() if profile.output_dir else work_base
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = re.sub(r'[<>:"/\\|?*]+', "_", entry.title).strip() or entry.hash
    ext = ".rar" if archiver.kind == "rar" else ".7z"

    if args.dry_run:
        step("dry run")
        say(f"   would wait for  {entry.hash}")
        # The extension cannot be known here and is not the archiver's. The
        # real name comes from what the host calls the file, and that is only
        # known once the page has been resolved -- which is the step a dry run
        # exists to avoid. So it says so rather than naming a file that will
        # not be there: it used to print `<stem>.download.rar` and produce
        # `<stem>.download.7z`, and the extension is what decides which tool
        # opens it.
        say(f"   would download to  {work_base / (stem + '.download')}"
            f"<the host's extension, once the page is resolved>")
        say(f"   would unpack     {stem}/")
        for pattern in profile.remove:
            say(f"   would remove     {pattern}")
        for item in list(profile.add) + list(profile.default_add):
            say(f"   would add        {item}")
        say(f"   would write      {out_dir / zakuro_name(stem, ext)}")
        return 0

    # -- is there already one of these? ---------------------------------
    # Asked on the name the host uses, which is only known after the page has
    # been resolved -- so this sits below that rather than here. It still
    # happens before the download, which is the part that matters: these run
    # to 155 GB, and finding out at the end means fetching the whole thing to
    # throw it away. Asked again just before the move, in case something else
    # wrote there meanwhile.
    #
    # The early check on the catalogue name stays as a cheap first guard, since
    # a re-run usually produces the same name as last time.
    provisional = out_dir / zakuro_name(stem, ext)
    if provisional.exists() and not args.force:
        raise SystemExit(
            f"{provisional.name} is already there. Pass --force to replace it, "
            f"or --output-dir somewhere else to keep both."
        )

    # -- wait for it ----------------------------------------------------
    step("waiting for the file to be available")
    info = Viking(timeout=args.timeout).wait_until_ready(entry.hash)
    remote_name = str(info.get("name") or "")
    remote_size = int(info.get("size") or 0)
    if info:
        say(f"   ready: {remote_name}  {human(remote_size)}")
    else:
        warn("the host never confirmed it; trying the download anyway")
        remote_size = 0

    # -- resolve the link ------------------------------------------------
    # The API above says the file is there and what it is called, but it
    # hands back no link. The link lives on the share page and appears only
    # after that page's Cloudflare challenge is solved, so this is where the
    # browser is needed. The page's figures win over the API's where they
    # differ: it is the thing the download will actually be measured against.
    step("resolving the download link")
    try:
        share = resolve_share(entry.hash, headed=args.headed)
    except ShareUnavailable as exc:
        raise SystemExit(f"could not resolve a download link: {exc}")
    say(f"   {share.name}  {human(share.size)}")
    # Only mention it when the difference is real. These two routinely differ
    # by a handful of bytes, and a warning that says 3.1 MB and 3.1 MB is worse
    # than no warning at all.
    if remote_size and share.size and abs(remote_size - share.size) > remote_size * 0.01:
        warn(f"the API said {human(remote_size)} ({remote_size} bytes) and the "
             f"page says {human(share.size)} ({share.size} bytes); going with the page")
    if share.name:
        remote_name = share.name
    if share.size:
        remote_size = share.size

    # -- name it ---------------------------------------------------------
    # The host's own filename, with the UC branding off, rather than the
    # catalogue's title. The host calls it "Hollow Knight - UC.7z" and the
    # catalogue calls it "Hollow Knight (V1.5.12620)"; the first describes
    # the archive and the second carries a build id that means nothing to
    # whoever unpacks it. "Hollow Knight [Zakuro].rar" comes out of the first.
    # Only the last path component is ever used, so a host that answers with
    # a full path cannot steer the name out of the output directory.
    host_stem = Path(str(remote_name).replace("\\", "/")).stem
    host_stem = re.sub(r'[<>:"/\\|?*]+', "_", host_stem).strip()
    out_stem = host_stem or strip_uc(stem)
    archive = out_dir / zakuro_name(out_stem, ext)
    say(f"   {archive.name}")
    if archive.exists() and not args.force:
        raise SystemExit(
            f"{archive.name} is already there. Pass --force to replace it, or "
            f"--output-dir somewhere else to keep both."
        )

    # -- download -------------------------------------------------------
    step("downloading")
    # Name the download after what the host calls it, not what the catalogue
    # guessed. The extension is the point: these are mostly .7z, and a .rar
    # built from a guessed name would be unpacked by the wrong tool first.
    safe_remote = Path(remote_name.replace("\\", "/")).name or f"{stem}.bin"
    remote_ext = Path(safe_remote).suffix or ext
    download_path = work_base / f"{stem}.download{remote_ext}"
    need = remote_size or 0
    # Room claimed for this run and given back when it ends, whichever way it
    # ends. The download, the unpacked tree and the finished archive all have to
    # fit at once -- a 3.1 MB .7z in this catalogue held 73.7 MB.
    #
    # A context manager rather than a helper function on purpose: this part of
    # main is long, and splitting it out meant passing a dozen names across by
    # hand. Two of them were missed, and a dry run cannot see either, because it
    # returns before here.
    with _RoomClaim(work_base, need, stem):
        import jobs
        job = jobs.current()
        # Held for the whole run, not just the fetch: two runs of one entry
        # deadlock on the shared partial, and a hang with no error is a bad
        # thing to hand someone.
        try:
            with _RunLock(download_path, safe_remote):
                got = download(share.download_url, download_path, expect=need,
                               timeout=args.timeout, referer=share.page_url,
                               progress=jobs.bind_progress(job) if job else None,
                               cancel=job.stop_requested if job else None)
        except AlreadyRunning as exc:
            raise SystemExit(str(exc))
        say(f"   {human(got)} in {download_path.name}")

    if Path(safe_remote).stem.lower() != Path(download_path).stem.lower() and "." in safe_remote:
        say(f"   the host calls it {safe_remote}")

    # -- extract --------------------------------------------------------
    step("unpacking")
    unpack = work_base / stem
    if unpack.exists():
        shutil.rmtree(unpack)
    unpack.mkdir(parents=True)
    # Now the archive can be asked what it turns into, which nothing could have
    # told us from the catalogue. Checked before unpacking rather than after,
    # because running out of disk halfway leaves a partial folder that the next
    # step would then repack as if it were the whole game.
    expand = archiver.unpacked_size(download_path)
    if expand:
        free = free_bytes(work_base)
        # What other runs in this process have claimed is not free to this one,
        # so the room left is the volume minus their claims. Reading the volume
        # on its own is the bug this fixes: ten runs each seeing the same number
        # all agree there is room, and then the volume fills.
        room = None if free is None else free - _room_others_hold()
        # The download, the unpacked tree and the finished archive all have to
        # fit at once, plus slack: the repack is written to the same volume and
        # a .rar of a .7z is not reliably smaller.
        want = got + expand + int(expand * 0.05)
        say(f"   expands to {human(expand)}")
        if room is None:
            warn(f"could not read the free space on {work_base}, so the room "
                 f"check is being skipped -- these need about {human(want)}")
        elif room < want:
            raise SystemExit(
                f"not enough room to unpack: {human(want)} needed in {work_base} "
                f"for the download, the unpacked files and the finished archive, "
                f"{human(room)} free. Point --work-dir at a bigger disk."
            )
    unsafe = [n for n in archiver.list_names(download_path) if is_unsafe_entry(n)]
    screen_entries(unsafe, "archive")
    archiver.extract(download_path, unpack)
    say(f"   unpacked into {unpack}")

    # -- name the folder inside ------------------------------------------
    # These archives wrap the game in a folder named after whoever packed it:
    # "Touhou Luna Nights - UC/". Renaming the archive was only half of it --
    # unpacked, the release still carried somebody else's mark, and that is
    # the name people see first.
    #
    # Done here, immediately after unpacking and before anything is added,
    # because that is the only point where the game's own folder is alone.
    # Later there is ~Common Redist beside it, a second top-level directory,
    # and a check for "exactly one" would then decline to touch either.
    #
    # Narrow on purpose: one top-level directory, and only when stripping UC
    # actually changes its name. A game whose own folder is called "Data" or
    # "bin", or an archive that unpacks to several folders, is left alone.
    # archive.stem, not out_stem: out_stem is the host's name with "- UC"
    # still on it, so it is the very folder being renamed and the rename
    # would always decline. The UC comes off in zakuro_name, above.
    retag = retag_inner_folder(unpack, archive.stem)
    if retag:
        say(f"   folder inside: {retag[0]} -> {retag[1]}")

    # -- edit -----------------------------------------------------------
    # The patterns the profile chose, plus the standard sweep. A .url is a
    # browser bookmark -- in these archives it points at whoever packed it,
    # union-crax.xyz and the like -- and it is not part of the game.
    strip = list(profile.remove)
    seen = {_norm(p) for p in strip}
    for pattern in profile.default_remove:
        if _norm(pattern) not in seen:
            strip.append(pattern)
            seen.add(_norm(pattern))
    if strip:
        step("removing")
        for rel in remove_matches(unpack, strip):
            say(f"   - {rel}")

    # --add is what the command line or the profile asked for; the standard
    # set goes in on top of it, skipping anything already covered so the same
    # file is not copied twice.
    asked = list(profile.add)
    already = {split_extra(a)[1] for a in asked}
    standard = [e for e in standard_add(profile.default_add)
                if split_extra(e)[1] not in already]
    if asked or standard:
        step("adding")
        for rel in add_files(unpack, asked):
            say(f"   + {rel}")
        # Required, so a standard file that is configured but not there stops
        # the run. It used to be a warning, on the grounds that these paths
        # belong to one machine and that folder moves -- which is true, and
        # which is exactly how a release ships with no runtime installers and
        # reports success. `default_add: []` is the way to not want them, and
        # an empty list never gets here, so the two are not confused.
        for rel in add_files(unpack, standard, standard=True):
            say(f"   + {rel}")

    # -- repack ---------------------------------------------------------
    step("repacking")
    out_dir.mkdir(parents=True, exist_ok=True)
    # Asked again here, having been asked before the download. Cheap, and it
    # is the last point at which refusing costs nothing.
    if archive.exists():
        if not args.force:
            raise SystemExit(
                f"{archive.name} appeared while this was running. Pass --force "
                f"to replace it."
            )
        warn(f"{archive.name} exists, replacing it")
        archive.unlink()
    # Written beside the output and moved into place at the end, so a pack that
    # fails or is interrupted cannot leave a half-written archive under the
    # name of a finished one.
    staging = archive.with_name(archive.name + ".partial")
    if staging.exists():
        staging.unlink()
    archiver.pack(unpack, staging, level=profile.level, test=not args.no_test)
    say(f"   {staging.name}  {human(staging.stat().st_size)}")
    if not args.no_test:
        say("   verified")

    # -- tidy -----------------------------------------------------------
    # The two things that went into the archive, gone now that it is written:
    # the download it came from and the unpacked copy of it. Together they are
    # roughly twice the size of the finished archive and neither is wanted
    # afterwards; the download in particular is the thing that fills a disk
    # over a run through a catalogue.
    #
    # The move happens first, so the archive is only taken away once there is
    # a good one where it belongs.
    staging.replace(archive)
    say(f"   {archive.name}  {human(archive.stat().st_size)}")
    if not profile.keep_download:
        try:
            download_path.unlink()
        except OSError:
            warn(f"could not remove {download_path.name}; it is still there")
    if unpack.exists():
        shutil.rmtree(unpack, ignore_errors=True)
        if unpack.exists():
            warn(f"could not remove the unpacked folder {unpack.name}; "
                 f"it is still there")

    say(f"\ndone: {archive}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
