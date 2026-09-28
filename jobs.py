"""Jobs: a run of one catalogue entry, made observable and stoppable.

The pipeline in uc_archiver is not rebuilt here. It is *listened* to. Every
phase of it already announces itself with `step()` and narrates itself with
`say()`, and the downloader already reports bytes, so a job is those three
things collected:

  - the phase name, from `step()`, which also gives the natural place to
    check for cancellation. Every phase begins with one, so a stop requested
    during a two-hour download is honoured at the next boundary rather than
    after the whole job.
  - the log, from `say()` and `warn()`.
  - bytes, speed and ETA, from the downloader's own progress object.

That means the console and a browser are reading the same model rather than
one of them being a translation of the other, and a bug fixed in the pipeline
shows up in the web UI for free.

A job runs on its own thread. The active job is kept in a thread-local, so a
second job started alongside the first cannot have its log lines land in the
wrong one.
"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

# How much log a job keeps. A long download redraws a progress line
# constantly, and an unbounded log is a memory leak with a progress bar on it.
MAX_LOG_LINES = 500


class JobCancelled(Exception):
    """Raised at a phase boundary when the job has been asked to stop."""


@dataclass
class Job:
    id: str
    index: int
    title: str
    state: str = QUEUED
    phase: str = ""
    done: int = 0
    total: int = 0
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    log: list[str] = field(default_factory=list)
    result: str = ""
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- progress ------------------------------------------------------

    @property
    def seconds(self) -> float:
        if not self.started:
            return 0.0
        return (self.finished or time.time()) - self.started

    @property
    def rate(self) -> float:
        return self.done / max(self.seconds, 0.001)

    @property
    def eta(self) -> float | None:
        if self.state in (DONE, FAILED, CANCELLED):
            return 0.0
        left = self.total - self.done
        if left <= 0:
            return 0.0
        return left / self.rate if self.rate > 0 else None

    @property
    def fraction(self) -> float:
        """How far along, 0..1, for the bar.

        Bytes alone would sit at nothing for the whole resolve phase and then
        jump, so the phases before the download are given a small share each.
        The weights are guesses about what is slow, and being wrong only makes
        the bar slightly wrong -- it never makes it stuck.

        A failed job is held just short of full. One that died at 17% is not
        done, and a bar that says otherwise is a lie that costs somebody an
        afternoon -- but stopping just short of 1.0 is the clearest signal
        there is that this one will not be finishing.
        """
        if self.state == DONE:
            return 1.0
        if self.state == CANCELLED:
            return min(0.99, self.done / self.total) if self.total else 0.0
        if self.total and self.done:
            # Most of the bar is the download, with a little for everything else.
            reach = min(0.92, 0.08 + 0.84 * (self.done / self.total))
        else:
            reach = {"queued": 0.0, "running": 0.04}.get(self.state, 0.0)
        if self.state == FAILED:
            reach = min(0.99, reach)
        return reach

    # -- control -------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def stop_requested(self) -> bool:
        """Has a stop been asked for? A callable for the downloader.

        It is handed to something that re-asks between chunks, so passing the
        `cancelled` property would be passing a value and the first call would
        fail with "'bool' object is not callable" -- at the start of every
        download. Note the call on the last line: returning the bound method
        instead of its result looks identical in a test and is not, because a
        bound method is truthy, so the downloader is cancelled the instant it
        begins.
        """
        return self._cancel.is_set()

    def checkpoint(self) -> None:
        if self._cancel.is_set():
            raise JobCancelled()

    # -- reporting -----------------------------------------------------

    def set_phase(self, name: str) -> None:
        with self._lock:
            self.phase = name
        self.append(f"== {name} ==")

    def append(self, line: str) -> None:
        with self._lock:
            self.log.append(line)
            if len(self.log) > MAX_LOG_LINES:
                del self.log[: len(self.log) // 2]

    def set_progress(self, done: int, total: int) -> None:
        with self._lock:
            self.done = done
            if total:
                self.total = total

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "index": self.index,
                "title": self.title,
                "state": self.state,
                "phase": self.phase,
                "done": self.done,
                "total": self.total,
                "rate": round(self.rate, 1),
                "eta": round(self.eta, 1) if self.eta is not None else None,
                "fraction": round(self.fraction, 4),
                "seconds": round(self.seconds, 1),
                "error": self.error,
                "result": self.result,
                "log": list(self.log[-80:]),
                "cancellable": self.state in (QUEUED, RUNNING),
            }


class JobManager:
    """Holds the jobs and runs them, one thread each."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()

    def submit(self, index: int, title: str,
               work: Callable[[Job], str]) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], index=index, title=title)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        thread = threading.Thread(target=self._run, args=(job, work), daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, work: Callable[[Job], str]) -> None:
        job.state = RUNNING
        job.started = time.time()
        token = bind(job)
        try:
            job.result = work(job) or ""
            job.state = CANCELLED if job.cancelled else DONE
        except JobCancelled:
            job.state = CANCELLED
            job.append("stopped")
        except _downloader_cancelled():
            # The downloader has its own stop exception, raised from a worker
            # thread rather than from a phase boundary. It is the same event,
            # so it has to land the same way -- as a stop, not a failure.
            job.state = CANCELLED
            job.append("stopped during the download")
        except BaseException as exc:                      # noqa: BLE001
            job.state = FAILED
            job.error = f"{type(exc).__name__}: {exc}"
            job.append(job.error)
            # Kept in the log, not just in the message: the traceback is what
            # makes a failure debuggable and a one-liner rarely is.
            for line in traceback.format_exc().splitlines()[-8:]:
                job.append("  " + line)
        finally:
            job.finished = time.time()
            unbind(token)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in self._order if i in self._jobs]

    def active(self) -> list[Job]:
        return [j for j in self.all() if j.state in (QUEUED, RUNNING)]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.state not in (QUEUED, RUNNING):
            return False
        job.cancel()
        return True

    def clear_finished(self) -> int:
        with self._lock:
            gone = [i for i in self._order
                    if self._jobs[i].state in (DONE, FAILED, CANCELLED)]
            for i in gone:
                self._jobs.pop(i, None)
            self._order = [i for i in self._order if i not in gone]
        return len(gone)


# --------------------------------------------------------------- the binding
#
# A thread-local, not a global. Two jobs at once is the normal case for this
# tool, and a plain module global would send the second job's log lines to the
# first.

_local = threading.local()


def bind(job: Job) -> Job:
    previous = getattr(_local, "job", None)
    _local.job = job
    return previous


def unbind(previous: Job | None) -> None:
    _local.job = previous


def current() -> Job | None:
    return getattr(_local, "job", None)


def report(line: str) -> None:
    """Called by the pipeline for every line it prints."""
    job = current()
    if job is not None:
        job.append(line)


def checkpoint() -> None:
    """Called at every phase boundary, to honour a stop request."""
    job = current()
    if job is not None:
        job.checkpoint()


def _downloader_cancelled():
    """parallel's stop exception, or a stand-in if it cannot be imported.

    Only ever used in an `except` clause, so a plain class works: a class in
    an except tuple is matched with isinstance, not called.
    """
    try:
        import parallel
        return parallel.DownloadCancelled
    except Exception:                                    # noqa: BLE001
        return JobCancelled


def bind_progress(job: Job) -> "Progress | None":
    """A progress object wired to `job`, for the downloader.

    Imported here rather than at the top because parallel has no idea jobs
    exist, and this is the only place the two meet.
    """
    import parallel

    bar = parallel.Progress()

    class _Wired:
        """Forwards the downloader's numbers onto the job.

        Delegating rather than subclassing, so anything the downloader gains
        later is picked up without touching this. `total` is a real property
        rather than a class attribute: a class attribute would be found before
        __getattr__ is ever asked, so setting the total would land on the
        wrapper and the bar would show 0 against 0 forever.
        """

        def __init__(self, inner):
            self._inner = inner

        @property
        def total(self) -> int:
            return self._inner.total

        @total.setter
        def total(self, value: int) -> None:
            self._inner.total = value

        def advance(self, n: int) -> None:
            self._inner.advance(n)
            job.set_progress(self._inner.done, self._inner.total)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    return _Wired(bar)
