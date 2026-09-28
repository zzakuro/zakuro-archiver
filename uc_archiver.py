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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

API = "https://vikingfile.com/api"
FILE_URL = "https://vikingfile.com/f/{}"
# The host writes sizes in decimal (3.14 MB) but the values are powers of 1024
# in practice, so the scale is matched the way the site means it rather than
# the way SI would. A wrong constant here would fail every size check.
_UNIT_SCALE = {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3,
               "TB": 1024 ** 4, "KIB": 1024, "MIB": 1024 ** 2,
               "GIB": 1024 ** 3, "TIB": 1024 ** 4}
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
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"  ! {msg}", flush=True)


def step(msg: str) -> None:
    print(f"\n== {msg}", flush=True)


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def free_bytes(path: Path) -> int:
    try:
        path.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(path).free
    except OSError:
        return 0


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
                  timeout: int = 90_000) -> Share:
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
    Waiting for network idle is what makes the difference.

    Scrapling's StealthySession does the solving and re-locates the selectors
    if the site moves them, which matters here: the host has already changed
    domain once (vikingfile.com -> vik1ngfile.site) and the download path has
    changed shape before.
    """
    problem = scrapling_problem()
    if problem:
        raise ShareUnavailable(problem)

    page_url = FILE_URL.format(url_or_hash) if not url_or_hash.startswith("http") \
        else url_or_hash
    from scrapling.fetchers import StealthySession

    # A fresh session each time. Holding one open across many shares is
    # faster, but the host issues a challenge per share and a stale clearance
    # is a confusing failure much later on.
    #
    # scrapling logs "No Cloudflare challenge found" at ERROR when a page needs
    # no challenge -- which happens once a clearance is held, and is the
    # normal case on a second run. Left alone it reads like this tool failing,
    # so its logger is lifted out of the way for the duration.
    import logging
    noisy = logging.getLogger("scrapling")
    previous = noisy.level
    noisy.setLevel(logging.CRITICAL)
    try:
        with StealthySession(headless=not headed, solve_cloudflare=True,
                             network_idle=True) as session:
            page = session.fetch(page_url, timeout=timeout)
    finally:
        noisy.setLevel(previous)

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
            raise ShareUnavailable(
                "the page gave no download link. The Cloudflare challenge may "
                "not have cleared, or the site changed shape -- try again, or "
                "run with --headed to watch it happen."
            )
        if href.startswith("/"):
            href = "https://vikingfile.com" + href

        size = 0
        try:
            size = int(float(re.sub(r"[^0-9.]", "", size_text) or 0) * _UNIT_SCALE.get(
                (re.sub(r"[^A-Za-z]", "", size_text) or "B").upper(), 1))
        except ValueError:
            size = 0
        return Share(page_url=page_url, name=name or "download.bin",
                     size=size, download_url=href)


# ------------------------------------------------------------------ download
def download(url: str, dest: Path, expect: int = 0, timeout: int = 60,
             referer: str = "") -> int:
    """Fetch `url` to `dest`, resuming a partial file if there is one.

    Returns the byte count on disk. Raises on a short read, because an archive
    that stops halfway is the one failure that wastes the most time: it only
    shows up when the extractor cannot read it, long after the download.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.is_file() else 0
    headers = {"User-Agent": USER_AGENT}
    if referer:
        # The download endpoint is served by the same host as the share
        # page and expects to be coming from it.
        headers["Referer"] = referer
    if have and expect and have < expect:
        say(f"   resuming at {human(have)} of {human(expect)}")
        headers["Range"] = f"bytes={have}-"
    elif have:
        # A partial file that is not shorter than the whole thing is no use.
        have = 0
        dest.unlink(missing_ok=True)

    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:
            say("   the server says the file is already complete")
            return have
        raise SystemExit(f"download failed: HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SystemExit(f"download failed: {exc}") from exc

    mode = "ab" if (have and resp.status == 206) else "wb"
    if mode == "wb":
        have = 0
    total = expect or (int(resp.headers.get("Content-Length") or 0) + have)
    shown = 0
    started = time.time()
    last = 0.0
    try:
        with dest.open(mode) as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                have += len(chunk)
                now = time.time()
                if now - last > 1.0:
                    last = now
                    rate = have / max(now - started, 0.001)
                    pct = f"{have * 100 / total:5.1f}%" if total else "  ?  "
                    print(f"\r   {pct}  {human(have)}  {human(rate)}/s   ",
                          end="", flush=True)
                    shown = have
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        print()
        raise SystemExit(f"download interrupted at {human(shown)}: {exc}") from exc
    finally:
        # A stand-in in the tests has no close(); a real response always does.
        close = getattr(resp, "close", None)
        if callable(close):
            close()
    print()
    if total and have < total:
        raise SystemExit(
            f"download is short: got {human(have)} of {human(total)} "
            f"(re-run to resume)"
        )
    return have


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


def remove_matches(root: Path, patterns: list[str], dry_run: bool = False) -> list[str]:
    """Delete what the patterns name, deepest first.

    A directory match takes its contents with it, so the children are not
    listed separately afterwards. Patterns are matched against the path
    relative to the root, with `/` separators, and also against each path
    component so `logs` takes `logs/old.log` without asking for a wildcard.
    """
    removed: list[str] = []
    victims: list[Path] = []
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not path.exists():
            continue
        rel = path.relative_to(root).as_posix()
        name = path.name
        for pattern in patterns:
            if not pattern:
                continue
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
    """
    if "=" in entry:
        name, _, raw = entry.partition("=")
        name, raw = name.strip(), raw.strip()
        if name and raw and not re.search(r"[/\\:]", name) and Path(raw).exists():
            return name, Path(raw).expanduser()
    return None, Path(entry).expanduser()


def add_files(root: Path, entries: list[str], dry_run: bool = False) -> list[str]:
    placed: list[str] = []
    for entry in entries:
        name, source = split_extra(entry)
        if not source.exists():
            hint = ""
            if "=" in entry and not source.exists():
                hint = ("  (if that was meant as NAME=path, the part after the "
                        "'=' does not exist)")
            raise SystemExit(f"--add: not found: {source}{hint}")
        target = root / name if name else root / source.name
        if dry_run:
            placed.append(target.relative_to(root).as_posix())
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if source.is_dir():
                if target.exists():
                    shutil.rmtree(target)
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


@dataclass
class Profile:
    remove: list[str] = field(default_factory=list)
    add: list[str] = field(default_factory=list)
    level: str = "normal"
    archiver: str = ""
    output_dir: str = ""
    keep_download: bool = False
    work_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "remove": list(self.remove),
            "add": list(self.add),
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
def zakuro_name(stem: str, ext: str) -> str:
    """`Game [Zakuro].rar`, and not `Game [Zakuro] [Zakuro].rar` on a re-run."""
    clean = re.sub(r"\s*\[Zakuro\]\s*", " ", stem, flags=re.IGNORECASE).strip()
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
        say(f"   would download to  {work_base / (stem + '.download' + ext)}")
        say(f"   would unpack     {stem}/")
        for pattern in profile.remove:
            say(f"   would remove     {pattern}")
        for item in profile.add:
            say(f"   would add        {item}")
        say(f"   would write      {out_dir / zakuro_name(stem, ext)}")
        return 0

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

    # -- download -------------------------------------------------------
    step("downloading")
    # Name the download after what the host calls it, not what the catalogue
    # guessed. The extension is the point: these are mostly .7z, and a .rar
    # built from a guessed name would be unpacked by the wrong tool first.
    safe_remote = Path(remote_name.replace("\\", "/")).name or f"{stem}.bin"
    remote_ext = Path(safe_remote).suffix or ext
    download_path = work_base / f"{stem}.download{remote_ext}"
    need = remote_size or 0
    if need:
        have = free_bytes(work_base)
        if have and have < need * 1.05:
            raise SystemExit(
                f"not enough room: {human(need)} needed in {work_base}, "
                f"{human(have)} free"
            )
    got = download(share.download_url, download_path, expect=need,
                   timeout=args.timeout, referer=share.page_url)
    say(f"   {human(got)} in {download_path.name}")
    if Path(safe_remote).stem.lower() != Path(download_path).stem.lower() and "." in safe_remote:
        say(f"   the host calls it {safe_remote}")

    # -- extract --------------------------------------------------------
    step("unpacking")
    unpack = work_base / stem
    if unpack.exists():
        shutil.rmtree(unpack)
    unpack.mkdir(parents=True)
    unsafe = [n for n in archiver.list_names(download_path) if is_unsafe_entry(n)]
    screen_entries(unsafe, "archive")
    archiver.extract(download_path, unpack)
    say(f"   unpacked into {unpack}")

    # -- edit -----------------------------------------------------------
    if profile.remove:
        step("removing")
        for rel in remove_matches(unpack, profile.remove):
            say(f"   - {rel}")
    if profile.add:
        step("adding")
        for rel in add_files(unpack, profile.add):
            say(f"   + {rel}")

    # -- repack ---------------------------------------------------------
    step("repacking")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / zakuro_name(stem, ext)
    if archive.exists():
        warn(f"{archive.name} exists, replacing it")
        archive.unlink()
    archiver.pack(unpack, archive, level=profile.level, test=not args.no_test)
    say(f"   {archive}  {human(archive.stat().st_size)}")
    if not args.no_test:
        say("   verified")

    # -- tidy -----------------------------------------------------------
    shutil.rmtree(unpack, ignore_errors=True)
    if not profile.keep_download:
        try:
            download_path.unlink()
            say(f"   removed {download_path.name}")
        except OSError:
            pass

    say(f"\ndone: {archive}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
