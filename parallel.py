"""Fetching a file, in parallel where the host allows it.

The share host throttles *per connection*, which is why its own page offers
"Download with 4 parallel threads". One stream out of it runs at a fraction
of what the link can carry, so this asks for several and writes each chunk at
its own offset in the same file.

Everything here is arranged so the fast path can fail and nothing is left
half-written:

- **Ranges are checked before they are used.** A server that answers 200 to a
  Range request is sending the whole file regardless, and four threads each
  believing they own the whole file produce a file the right size and full of
  holes. That answer is detected on the first chunk and every thread stops;
  the caller falls back to a single connection.
- **Each chunk is verified against the range it asked for.** A short read
  fails that chunk rather than leaving a gap that only shows up when the
  archive will not open.
- **One file handle per thread.** Seeking and writing on a shared handle from
  several threads interleaves; separate handles do not.
- **The file is sized up front** where the total is known, so a chunk is
  never writing past the end into a file another chunk has not reached.

Resume is the same machinery: a partial file is a prefix, so the work left is
the one range from where it stops to the end, and that is what gets split.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

CHUNK_READ = 1 << 20
DEFAULT_CONNECTIONS = 4
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)


def _say(_message: str) -> None:
    """Overwritten by the caller.

    This module deliberately knows nothing about the tool it serves: the
    pipeline logs, and importing that from here would be a circle. The caller
    sets `report` and the messages start appearing.
    """


report = _say


def human(count: int) -> str:
    """A size in something a person can read. Duplicated on purpose: this
    module does not import the tool it serves, and six lines is cheaper than
    a cycle."""
    step = 1024.0
    value = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < step or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= step
    return f"{value:.2f} TiB"


class DownloadCancelled(Exception):
    """Raised when the caller asked to stop."""


class RangeUnsupported(Exception):
    """The server ignored a Range request and sent the whole file."""


@dataclass
class Progress:
    """What a caller can show while a download runs."""

    done: int = 0
    total: int = 0
    started: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def advance(self, n: int) -> None:
        with self._lock:
            self.done += n

    @property
    def seconds(self) -> float:
        return max(time.time() - self.started, 0.001)

    @property
    def rate(self) -> float:
        return self.done / self.seconds

    @property
    def eta(self) -> float | None:
        left = self.total - self.done
        if left <= 0 or self.rate <= 0:
            return 0.0 if left <= 0 else None
        return left / self.rate


@dataclass(frozen=True)
class Span:
    """A half-open byte range, `start` to `end` inclusive."""

    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def header(self) -> str:
        return f"bytes={self.start}-{self.end}"


def split_span(start: int, end: int, parts: int) -> list[Span]:
    """Cut `start..end` into at most `parts` spans, none of them empty.

    The tail is spread one byte at a time rather than given all to the last
    span, so a remainder of 3 over 4 parts does not leave one thread with
    three times the work of the others.
    """
    if end < start or parts < 1:
        return []
    total = end - start + 1
    if parts == 1 or total <= parts:
        return [Span(start, start + total - 1)]
    base, extra = divmod(total, parts)
    spans: list[Span] = []
    at = start
    for i in range(parts):
        size = base + (1 if i < extra else 0)
        if size <= 0:
            break
        spans.append(Span(at, at + size - 1))
        at += size
    return spans


def spans_file(dest: Path) -> Path:
    """The sidecar that says which spans of `dest` really hold bytes.

    A partial file cannot be trusted on its size alone. Preallocating makes it
    the full length before a single byte arrives, so a run that dies after
    preallocating leaves a file that is exactly the right size and entirely
    holes -- and a later run that checks only the size calls that a finished
    download. It happened here: 284 MB of zeros, reported as 271.5 MB
    downloaded, and the archive would not open.

    So the spans that were genuinely written are recorded here, and resume
    asks this rather than the file size.
    """
    return dest.with_name(dest.name + ".spans")


def read_spans(dest: Path) -> list[tuple[int, int]]:
    path = spans_file(dest)
    try:
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            a, _, b = line.partition(" ")
            if a.isdigit() and b.isdigit():
                out.append((int(a), int(b)))
        return out
    except (OSError, ValueError):
        return []


def write_spans(dest: Path, spans: list[tuple[int, int]]) -> None:
    try:
        spans_file(dest).write_text(
            "".join(f"{a} {b}\n" for a, b in sorted(spans)), encoding="utf-8")
    except OSError:
        pass


def missing_spans(dest: Path, total: int, wanted: int) -> list[Span]:
    """What still has to be fetched, given what the sidecar says is there."""
    have = sorted(read_spans(dest))
    if not have:
        # No record at all. Either a fresh file, or one whose sidecar was lost
        # -- and a full-length file with no record is the preallocated case
        # that is not a download, so it is thrown away rather than believed.
        if dest.is_file() and dest.stat().st_size >= total > 0:
            try:
                dest.unlink()
            except OSError:
                pass
        return [Span(0, total - 1)] if wanted <= 1 else split_span(0, total - 1, wanted)
    covered = sum(b - a + 1 for a, b in have)
    todo: list[Span] = []
    at = 0
    for a, b in have:
        if a > at:
            todo.append(Span(at, a - 1))
        at = max(at, b + 1)
    if at < total:
        todo.append(Span(at, total - 1))
    report(f"   resuming at {human(covered)} of {human(total)}")
    return todo


def _open(url: str, span: Span | None, referer: str, timeout: int):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    if span is not None:
        headers["Range"] = span.header
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request, timeout=timeout)


def probe(url: str, referer: str = "", timeout: int = 30) -> tuple[bool, int]:
    """Does this host serve ranges, and how big is the file?

    Asks for a single byte. A 206 means ranges work and the Content-Range
    header carries the true total, which is better than the size on the page
    because that one is rounded. A 200 means ranges are ignored.
    """
    try:
        with _open(url, Span(0, 0), referer, timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            total = 0
            rng = resp.headers.get("Content-Range") or ""
            if "/" in rng:
                tail = rng.rsplit("/", 1)[-1].strip()
                if tail.isdigit():
                    total = int(tail)
            if not total:
                declared = resp.headers.get("Content-Length") or ""
                if declared.isdigit():
                    total = int(declared)
            return status == 206, total
    except (urllib.error.URLError, urllib.error.HTTPError, OSError):
        return False, 0


def _fetch_span(url: str, dest: Path, span: Span, referer: str, timeout: int,
                progress: Progress, cancel, handle) -> int:
    """Write one span into `dest` at its own offset. Returns bytes written.

    The handle is this thread's own, opened by the caller, so nothing here
    seeks on a shared file pointer.
    """
    written = 0
    with _open(url, span, referer, timeout) as resp:
        status = getattr(resp, "status", 200) or 200
        if status != 206:
            # The host sent the whole file rather than the slice asked for.
            # Every other thread is about to do the same into its own offset.
            raise RangeUnsupported(f"server answered {status} to a Range request")
        handle.seek(span.start)
        while True:
            if cancel is not None and cancel():
                raise DownloadCancelled()
            block = resp.read(CHUNK_READ)
            if not block:
                break
            handle.write(block)
            written += len(block)
            if progress is not None:
                progress.advance(len(block))
    if written != span.length:
        raise OSError(
            f"chunk {span.header} came back short: {written} of {span.length} bytes"
        )
    return written


def fetch_parallel(url: str, dest: Path, total: int, connections: int = DEFAULT_CONNECTIONS,
                   timeout: int = 60, referer: str = "", cancel=None,
                   progress: Progress | None = None) -> int:
    """Fetch `url` into `dest` with several connections. Returns the size.

    `total` is the whole file. What is already there is taken from the sidecar
    rather than the file size, so a run that died after preallocating is not
    mistaken for a finished download.
    """
    if progress is None:
        progress = Progress(total=total)
    else:
        # A caller that brought its own progress (a job) still has to learn the
        # total, or the bar measures against nothing.
        progress.total = total

    done_spans = list(read_spans(dest))
    todo = missing_spans(dest, total, connections)
    if not todo:
        return dest.stat().st_size if dest.is_file() else 0

    # Sized up front so no thread extends the file past the end while another
    # is still filling in earlier bytes.
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "ab") as handle:
        handle.truncate(total)

    stop = threading.Event()
    lock = threading.Lock()

    def share_cancel() -> bool:
        if cancel is not None and cancel():
            stop.set()
        return stop.is_set()

    def worker(span: Span) -> int:
        if stop.is_set():
            raise DownloadCancelled()
        with open(dest, "r+b") as handle:      # this thread's own handle
            written = _fetch_span(url, dest, span, referer, timeout,
                                  progress, share_cancel, handle)
        # Recorded only once the span is complete, so the sidecar never claims
        # bytes that are not there.
        with lock:
            done_spans.append((span.start, span.end))
            write_spans(dest, done_spans)
        return written

    try:
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            for _ in pool.map(worker, todo):
                pass
    except DownloadCancelled:
        # A stop is not a failure. What was fetched is real and the sidecar
        # says which parts, so it is kept and a later run carries on from
        # here rather than starting the file again.
        raise
    except RangeUnsupported:
        # The offsets it wrote mean nothing, so the preallocated file has to go
        # rather than be handed on as a download.
        _discard(dest)
        raise
    except BaseException:
        # A real failure. The file is the right length and full of holes, and
        # a size check alone would take it for a finished download next time.
        _discard(dest)
        raise
    spans_file(dest).unlink(missing_ok=True)
    return dest.stat().st_size


def _discard(dest: Path) -> None:
    """Throw away a partial file that must not be believed or resumed."""
    for path in (dest, spans_file(dest)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def fetch_single(url: str, dest: Path, expect: int = 0, timeout: int = 60,
                 referer: str = "", cancel=None, progress: Progress | None = None,
                 size_tolerance: float = 0.0) -> int:
    """One connection, with resume. The fallback, and the only path that works
    when the host will not do ranges."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.is_file() else 0
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    if have and expect and have < expect:
        report(f"   resuming at {human(have)} of {human(expect)}")
        headers["Range"] = f"bytes={have}-"
    elif have:
        have = 0
        dest.unlink(missing_ok=True)
    resume_from = have

    try:
        resp = _open(url, Span(resume_from, 0) if resume_from else None,
                     referer, timeout) if resume_from else \
            urllib.request.urlopen(
                urllib.request.Request(url, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and have:
            if expect and have < expect * (1 - size_tolerance):
                raise SystemExit(
                    f"the server says the file is complete but only "
                    f"{human(have)} is here against {human(expect)} expected; "
                    f"delete the partial and start again"
                ) from exc
            report("   the server says the file is already complete")
            return have
        raise SystemExit(f"download failed: HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SystemExit(f"download failed: {exc}") from exc

    declared = int(resp.headers.get("Content-Length") or 0)
    if declared:
        # A byte count. For a 206 it is the remainder, so whatever is already
        # on disk is added back to it.
        total = declared + (resume_from if resp.status == 206 else 0)
        exact = True
    else:
        # Nothing from the server, so the rounded figure on the page is all
        # there is -- and it is approximate, hence the slack below.
        total = expect
        exact = False
    if progress is not None:
        progress.total = total or (progress.total or 0)
        progress.done = resume_from
    try:
        with open(dest, "ab" if resume_from and resp.status == 206 else "wb") as out:
            while True:
                if cancel is not None and cancel():
                    raise DownloadCancelled()
                block = resp.read(CHUNK_READ)
                if not block:
                    break
                out.write(block)
                have += len(block)
                if progress is not None:
                    progress.advance(len(block))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise SystemExit(f"download interrupted at {human(have)}: {exc}") from exc
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()

    # A file that stopped halfway is the failure that costs the most: it only
    # shows up when the extractor cannot read it, long after the download.
    if total:
        slack = 0 if exact else int(total * size_tolerance)
        if have + slack < total:
            raise SystemExit(
                f"download is short: got {human(have)} of {human(total)} "
                f"(re-run to resume)"
            )
    return have
