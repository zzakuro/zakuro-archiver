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
from typing import Any

CHUNK_READ = 1 << 20
DEFAULT_CONNECTIONS = 4

# How many times one span may fail *without producing a byte* before the
# download is failed. Progress forgives the count, so a slow but healthy span
# that hiccups now and then never runs out of budget. Ten consecutive silent
# failures means the host is not serving this slice at all, and waiting longer
# only wastes the run.
SPAN_MAX_TRIES = 10

# Waiting between those tries, doubling each time up to the cap. A flat delay
# is what a first implementation does and it is wrong for a host that rate
# limits over a time window rather than a request count: ten one-second waits
# is ten seconds of patience, and this host's window is longer than that, so
# every retry landed inside the penalty and the run died still inside it.
# Measured against vikingfile.com -- a 429 at 16 connections retried on a flat
# 1s never got back in.
SPAN_RETRY_DELAY = 1.0
SPAN_RETRY_MAX_DELAY = 30.0


def retry_delay(tries: int) -> float:
    """How long to wait before attempt `tries`+1. Doubling, then capped.

    Capped rather than unbounded because the alternative -- doubling forever --
    turns a host that is down for an hour into a run that sits there for an
    hour. Thirty seconds is long enough to outlast a rate-limit window and
    short enough that giving up is still a decision somebody made.
    """
    return min(SPAN_RETRY_DELAY * (2 ** max(0, tries - 1)), SPAN_RETRY_MAX_DELAY)
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

# The progress line is redrawn at most this often. Every worker thread calls
# advance() on every megabyte it reads, so without a throttle four threads on a
# fast link would spend more time writing the bar than moving the file.
DRAW_INTERVAL = 0.1

# Which Progress is currently holding the terminal's bottom line, so a log line
# can wipe it before printing rather than landing in the middle of it. One at a
# time: two downloads in one terminal is two bars fighting over the same row.
_drawing: "Progress | None" = None
_draw_guard = threading.Lock()


def clear_bar() -> None:
    """Erase the progress line, if one is on screen.

    Called by the pipeline before it prints anything. The next chunk redraws,
    so this is only about the line that is already there -- without it a
    warning lands mid-bar and both become unreadable.
    """
    global _drawing
    with _draw_guard:
        bar, _drawing = _drawing, None
    if bar is None or bar.stream is None or not bar._painted:
        return
    bar.stream.write("\r" + " " * (bar._painted + 1) + "\r")
    bar.stream.flush()
    bar._painted = 0


def human_rate(per_second: float) -> str:
    """A speed a person can read, or a dash when there is not one yet."""
    if per_second <= 0:
        return "-"
    if per_second < 1024:
        return f"{per_second:.0f} B/s"
    return f"{human(int(per_second))}/s"


def duration(seconds: float) -> str:
    """Seconds as 12s, 3m04s or 1h20m. Never a decimal."""
    total = int(max(seconds, 0))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def format_progress(done: int, total: int, seconds: float, width: int = 28) -> str:
    """The download line, as a string. Pure, so it can be checked without a tty.

    A total of zero means the server never sent a Content-Length. Then there is
    no bar and no percentage, because both would be measured against nothing --
    which is the same reason the archive is asked what it expands to rather
    than having it guessed.
    """
    elapsed = max(seconds, 0.001)
    rate = done / elapsed
    if total > 0:
        frac = min(max(done / total, 0.0), 1.0)
        filled = int(frac * width)
        bar = "#" * filled + "-" * (width - filled)
        head = f"  [{bar}] {frac * 100:5.1f}%  {human(done)}/{human(total)}"
        eta = f"ETA {duration((total - done) / rate)}" if rate > 0 else "ETA --"
    else:
        head = f"  [{'?' * 1}{' ' * (width - 1)}]   ?  {human(done)}"
        eta = ""
    return f"{head}  {human_rate(rate):>13}" + (f"  {eta}" if eta else "")


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
    """What a caller can show while a download runs.

    `stream` is None for a caller that has somewhere else to show this -- the
    job, which the web page reads. The CLI passes stdout and gets a live line.
    The numbers are the same either way: the bar reads this object, it is not a
    second opinion on it.
    """

    done: int = 0
    total: int = 0
    started: float = field(default_factory=time.time)
    stream: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _drawn: float = field(default=0.0, repr=False)
    _painted: int = field(default=0, repr=False)

    def advance(self, n: int) -> None:
        with self._lock:
            self.done += n
            self.draw()

    def draw(self, force: bool = False) -> None:
        """Repaint the line, unless it was painted a moment ago."""
        if self.stream is None:
            return
        now = time.time()
        if not force and now - self._drawn < DRAW_INTERVAL:
            return
        self._drawn = now
        line = format_progress(self.done, self.total, now - self.started)
        self.stream.write("\r" + line)
        self.stream.flush()
        self._painted = len(line)
        global _drawing
        with _draw_guard:
            _drawing = self

    def restart(self) -> None:
        """Zero the clock, for a fetch that starts over on another connection."""
        with self._lock:
            self.started = time.time()
            self.done = 0
            self._drawn = 0.0

    def finish(self) -> None:
        """Leave the last state on screen, on a line of its own.

        Without the newline the next thing printed continues the bar, and a
        finished download reads as a truncated one.
        """
        if self.stream is None:
            return
        with self._lock:
            self.draw(force=True)
            if self._painted:
                self.stream.write("\n")
                self.stream.flush()
                # Now in scrollback rather than on the row a later line would
                # land on. Left marked painted, the clear_bar below would wipe
                # the final state of a download that had just succeeded.
                self._painted = 0
        clear_bar()

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
    """A byte range, `start` to `end` inclusive.

    An `end` of None means "to the end of the file", which is what a resume
    asks for. Span could not say it before, because it has no way to name the
    end of a file whose length is not yet known -- so a resume passed
    `Span(resume_from, 0)` and the header came out `bytes=300-0`, end before
    start. A server is entitled to 416 that, and the test server answered it
    with an empty 206, so a resumed download silently got nothing and then
    reported itself short.
    """

    start: int
    end: int | None = None

    @property
    def length(self) -> int:
        if self.end is None:
            raise ValueError("an open-ended range has no length")
        return self.end - self.start + 1

    @property
    def header(self) -> str:
        if self.end is None:
            return f"bytes={self.start}-"
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


def compact_prefix(dest: Path) -> int:
    """Keep the valid run from byte 0, drop the rest, and say how much that was.

    The parallel fetch writes each span at its own offset, so a run that is
    part way through has real bytes scattered across a preallocated file with
    holes between them. `fetch_single` resumes by asking for `Range: bytes=N-`
    where N is the *file size* -- which here is the whole file, so it cannot
    take over: it would ask for a range past the end and call the download
    finished. So until now the only safe handover was to throw the file away.

    Throwing it away is expensive. Observed on a real 16-connection run: the
    host answered 429 after 169 MiB of good data, the fallback dropped all of
    it, and the whole file was fetched again on one connection -- 701s against
    180s for the four-connection run that never tripped the limit.

    No copying is needed. A contiguous run starting at 0 is already sitting
    where fetch_single expects a prefix to be, so this only has to find where
    that run ends and truncate there. Anything past it is a hole or a span that
    never finished, and both go.

    Returns the prefix length, or 0 when there is nothing usable at the front --
    in which case the caller should discard, as before.
    """
    done = sorted(read_spans(dest))
    if not done or done[0][0] != 0:
        return 0
    end = done[0][1]
    for start, stop in done[1:]:
        if start <= end + 1:              # contiguous, or overlapping
            end = max(end, stop)
        else:
            break                         # a hole: nothing past here is reachable
    keep = end + 1
    try:
        with open(dest, "r+b") as handle:
            handle.truncate(keep)
    except OSError:
        return 0
    spans_file(dest).unlink(missing_ok=True)
    return keep


def _sleep_or_cancel(seconds: float, stop: threading.Event, cancel=None) -> None:
    """Wait out a retry pause, but give up the moment a stop is asked for.

    Polls the caller's `cancel` itself rather than only waiting on `stop`,
    because `stop` is set by workers that are inside a read loop. When the
    other spans have already finished -- which is exactly the situation a retry
    pause tends to happen in, since a throttled span is often the only one left
    -- nothing is left to notice a stop, and the run would sit out the whole
    delay. Measured: a cancel asked for at 0.3s went unheeded for 10.3s.
    """
    deadline = time.time() + seconds
    while True:
        if stop.is_set() or (cancel is not None and cancel()):
            stop.set()
            raise DownloadCancelled()
        left = deadline - time.time()
        if left <= 0:
            return
        stop.wait(min(left, 0.1))


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
                progress: Progress, cancel, handle,
                start_at: int = 0, tally: list[int] | None = None) -> int:
    """Write part of one span into `dest` at its own offset. Returns bytes written.

    `start_at` is how far into the span to begin, so a retry asks only for what
    is still missing instead of the whole slice again. `tally` is filled in even
    when this raises, because the caller needs to know whether the attempt got
    any bytes at all -- that is what decides whether a failure counts against
    the retry budget or is forgiven.

    The handle is this thread's own, opened by the caller, so nothing here
    seeks on a shared file pointer.
    """
    written = 0
    begin = span.start + start_at
    if tally is not None:
        tally[0] = 0
    try:
        with _open(url, Span(begin, span.end) if start_at else span,
                   referer, timeout) as resp:
            status = getattr(resp, "status", 200) or 200
            if status != 206:
                # The host sent the whole file rather than the slice asked for.
                # Every other thread is about to do the same into its own offset.
                raise RangeUnsupported(f"server answered {status} to a Range request")
            handle.seek(begin)
            while True:
                if cancel is not None and cancel():
                    raise DownloadCancelled()
                block = resp.read(CHUNK_READ)
                if not block:
                    break
                handle.write(block)
                written += len(block)
                if tally is not None:
                    tally[0] = written
                if progress is not None:
                    progress.advance(len(block))
    finally:
        if tally is not None:
            tally[0] = written
    if written != span.length - start_at:
        raise OSError(
            f"chunk {span.header} came back short: {written} of "
            f"{span.length - start_at} bytes"
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
        # Nothing left to fetch. The bar has counted nothing this run, so it
        # has to be told the file is whole -- otherwise it closes on 0% after
        # a resume that had, in fact, just finished the job.
        size = dest.stat().st_size if dest.is_file() else 0
        progress.total = total
        progress.done = size
        return size

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
        """Fetch one span, retrying it alone rather than failing the download.

        A rate-limited host does not stop the whole file, it stops the
        connections it has decided to stop -- and that is the shape of the
        failure this used to get wrong. One 429 out of sixteen spans used to
        propagate out of pool.map, take the download down with it, and force a
        restart on a single connection: measured at 62% of a 271 MB file, 701s
        against 180s for the run that was never rate limited.

        So each span carries its own budget, spends it only on attempts that
        produced nothing, resumes from wherever it got to, and keeps the other
        spans running while it waits. The counter is progress-gated rather than
        a lifetime count, because a span that is moving is not a span that is
        failing -- a fixed budget of, say, 5 would kill a slow but healthy
        download that hiccuped five times over twenty minutes.

        Bytes already written are recorded in the sidecar as they are confirmed,
        so a later run resumes them rather than asking again.
        """
        have = 0                      # bytes of this span already on disk
        tries = 0                     # attempts that produced nothing, in a row
        while True:
            if stop.is_set():
                raise DownloadCancelled()
            tally: list[int] = [0]
            try:
                with open(dest, "r+b") as handle:   # this thread's own handle
                    wrote = _fetch_span(url, dest, span, referer, timeout,
                                        progress, share_cancel, handle,
                                        start_at=have, tally=tally)
                have += wrote
                with lock:
                    done_spans.append((span.start, span.end))
                    write_spans(dest, done_spans)
                return span.length
            except DownloadCancelled:
                raise
            except RangeUnsupported:
                # Not a throttle: the host ignored the Range, so the whole
                # parallel premise is void and the caller has to know that.
                raise
            except (OSError, urllib.error.URLError) as exc:
                have += tally[0]
                if tally[0]:
                    tries = 0        # it moved, so this is not a failure yet
                    with lock:
                        if have < span.length:
                            done_spans.append((span.start, span.start + have - 1))
                            write_spans(dest, done_spans)
                else:
                    tries += 1
                if tries > SPAN_MAX_TRIES:
                    raise
                report(f"   {span.header} interrupted ({exc}); "
                       f"retry {tries} of {SPAN_MAX_TRIES} at byte "
                       f"{span.start + have} in {retry_delay(tries):.0f}s")
                _sleep_or_cancel(retry_delay(tries), stop, cancel)

    with ThreadPoolExecutor(max_workers=len(todo)) as pool:
        for _ in pool.map(worker, todo):
            pass
    # Nothing above discards the file on a failure, and that is the point.
    #
    # Every span in the sidecar is a verified 206 read that returned its full
    # length -- including when the failure is a Range answered with 200, since
    # that thread raises before it writes anything -- so none of it is suspect.
    # Discarding used to throw away 169 MB of good data when a 16-connection
    # run was rate limited at 62%, and then fetch the whole file again on one
    # connection: 701s against 180s.
    #
    # Leaving a preallocated file full of holes is safe precisely because the
    # sidecar goes with it. The next run asks the sidecar rather than the size,
    # which is the whole reason the sidecar exists; removing both is what makes
    # a file that only its length could be mistaken for. A stop and a failure
    # are the same case here, so both just propagate.
    spans_file(dest).unlink(missing_ok=True)
    return dest.stat().st_size


def _discard(dest: Path) -> None:
    """Throw away a partial file that must not be believed or resumed."""
    for path in (dest, spans_file(dest)):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _throttled(exc: BaseException) -> bool:
    """Is this the host pushing back rather than something being wrong?"""
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code == 429 or 500 <= code < 600
    # No status: a dropped connection or a read timeout. Worth one more go.
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def fetch_single(url: str, dest: Path, expect: int = 0, timeout: int = 60,
                 referer: str = "", cancel=None, progress: Progress | None = None,
                 size_tolerance: float = 0.0) -> int:
    """One connection, with resume and retry. The fallback, and the only path
    that works when the host will not do ranges.

    Retries because this is also where a rate-limited parallel run lands. It
    used to make exactly one request: the 429 that had just stopped sixteen
    spans arrived here too, and the whole run died on the first try having
    already spent ten rounds of retrying. A 429 is the host saying "not now",
    so it waits and asks again, from wherever it got to.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    have = dest.stat().st_size if dest.is_file() else 0
    if have and expect and have < expect:
        report(f"   resuming at {human(have)} of {human(expect)}")
    elif have:
        have = 0
        dest.unlink(missing_ok=True)

    stop = threading.Event()
    tries = 0
    total = 0
    exact = False
    while True:
        if cancel is not None and cancel():
            raise DownloadCancelled()
        base = have
        tally = [0]
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        if referer:
            headers["Referer"] = referer
        if base:
            headers["Range"] = f"bytes={base}-"
        try:
            resp = _open(url, Span(base) if base else None, referer, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and have:
                if expect and have < expect * (1 - size_tolerance):
                    raise SystemExit(
                        f"the server says the file is complete but only "
                        f"{human(have)} is here against {human(expect)} "
                        f"expected; delete the partial and start again"
                    ) from exc
                report("   the server says the file is already complete")
                if progress is not None:
                    progress.done = have
                return have
            if not _throttled(exc):
                raise SystemExit(
                    f"download failed: HTTP {exc.code} {exc.reason}") from exc
            last = exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            if not _throttled(exc):
                raise SystemExit(f"download failed: {exc}") from exc
            last = exc
        else:
            declared = int(resp.headers.get("Content-Length") or 0)
            if declared:
                # A byte count. For a 206 it is the remainder, so whatever is
                # already on disk is added back to it.
                total = declared + (base if resp.status == 206 else 0)
                exact = True
            else:
                # Nothing from the server, so the rounded figure on the page is
                # all there is -- and it is approximate, hence the slack below.
                total = expect
                exact = False
            if progress is not None:
                progress.total = total or (progress.total or 0)
                progress.done = base
            try:
                with open(dest, "ab" if base and resp.status == 206 else "wb") as out:
                    while True:
                        if cancel is not None and cancel():
                            raise DownloadCancelled()
                        block = resp.read(CHUNK_READ)
                        if not block:
                            break
                        out.write(block)
                        have += len(block)
                        tally[0] += len(block)
                        if progress is not None:
                            progress.advance(len(block))
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last = exc
            else:
                last = None
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
            if last is None:
                break

        if tally[0]:
            tries = 0            # it moved, so this is not a failure yet
        else:
            tries += 1
        if tries > SPAN_MAX_TRIES:
            raise SystemExit(f"download failed: {last}")
        report(f"   interrupted at {human(have)} ({last}); retry {tries} of "
               f"{SPAN_MAX_TRIES} in {retry_delay(tries):.0f}s")
        _sleep_or_cancel(retry_delay(tries), stop, cancel)

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
