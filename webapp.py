"""An HTTP front end: start a run, watch it, stop it.

Standard library only, so the container needs nothing installed for this and
the image stays the same size. `ThreadingHTTPServer` rather than a framework
because the whole API is seven routes and one page, and a dependency would
outweigh the code it replaced.

Progress is pushed over SSE where the browser supports it, and every route
also answers plain JSON, so a script can drive this without a browser at all.

Bound to loopback unless told otherwise. The thing it can do -- start
downloads and write archives to disk -- is not something to put on a network
by accident, and the token below is a second lock on the same door.
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jobs
import uc_archiver as uc

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DEFAULT_PORT = 8073
# Long enough for a catalogue scan, short enough that a stopped connection is
# noticed rather than left hanging a thread.
SSE_KEEPALIVE = 15.0


class Server:
    """The pieces the request handler needs, in one place to be testable."""

    def __init__(self, token: str = "", catalogue: str | None = None,
                 work_dir: str | None = None, out_dir: str | None = None) -> None:
        self.token = token
        self.catalogue = catalogue
        self.work_dir = work_dir
        self.out_dir = out_dir
        self.jobs = jobs.JobManager()

    # -- the catalogue -------------------------------------------------

    def load(self) -> uc.Catalogue:
        """Read the catalogue, turning the tool's own errors into messages.

        The tool signals anything the user should read by raising SystemExit,
        which is BaseException and not Exception -- so an ordinary
        `except Exception` lets it past, and out through the request handler
        with no response written at all. The connection just closes and the
        browser sees nothing. Hence the explicit catch.
        """
        if not self.catalogue:
            raise FileNotFoundError("no catalogue given; pass one with --catalogue")
        try:
            return uc.load_catalogue(self.catalogue)
        except SystemExit as exc:
            raise FileNotFoundError(str(exc)) from None

    # -- starting a run ------------------------------------------------

    def start(self, index: int, options: dict | None = None) -> jobs.Job:
        options = options or {}
        cat = self.load()
        entry = next((e for e in cat.entries if e.index == index), None)
        if entry is None:
            raise KeyError(f"no entry numbered {index}")
        return self.jobs.submit(entry.index, entry.title,
                                self._work(cat, entry, options))

    def _work(self, cat: uc.Catalogue, entry: uc.Entry, options: dict):
        """Build the argv for one entry and run the ordinary pipeline.

        The command line and this both go through main(), so there is one
        pipeline rather than two that drift apart. The arguments are
        synthesised rather than reimplemented, which keeps the profile
        resolution, the selection flags and the safety defaults exactly as
        they are on the command line.
        """

        def run(job: jobs.Job) -> str:
            argv = [self.catalogue, "--pick", str(entry.index)]
            if self.work_dir:
                argv += ["--work-dir", self.work_dir]
            if self.out_dir:
                argv += ["--output-dir", self.out_dir]
            for flag, key in (("level", "level"), ("archiver", "archiver")):
                if options.get(key):
                    argv += [f"--{flag}", str(options[key])]
            if options.get("force"):
                argv.append("--force")
            if options.get("dry_run"):
                argv.append("--dry-run")
            job.set_phase("starting")
            code = uc.main(argv)
            return "" if code == 0 else f"exited {code}"

        return run


class Handler(BaseHTTPRequestHandler):
    server_version = "uc-archiver"
    app: Server = None            # set by serve()

    # -- plumbing ------------------------------------------------------

    def log_message(self, *_args):
        pass                       # the job log is the log

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data, indent=2).encode("utf-8"),
                   "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _authorised(self) -> bool:
        token = self.app.token
        if not token:
            return True
        given = self.headers.get("Authorization", "")
        if given.startswith("Bearer "):
            given = given[7:]
        if given != token:
            # Deliberately the same shape as a browser's own basic-auth
            # failure, so a stale tab gets a prompt rather than a blank page.
            self._send(401, b"a token is needed: send Authorization: Bearer <token>",
                       "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Bearer realm="uc-archiver"'})
            return False
        return True

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routes --------------------------------------------------------

    def do_GET(self):
        if not self._authorised():
            return
        route = urllib.parse.urlparse(self.path)
        path = route.path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/status":
            return self._json(self._status())
        if path == "/api/catalogue":
            return self._json(self._catalogue())
        if path == "/api/jobs":
            return self._json([j.to_dict() for j in self.app.jobs.all()])
        if path.startswith("/api/jobs/"):
            rest = path[len("/api/jobs/"):]
            if rest.endswith("/events"):
                return self._events(rest[:-len("/events")])
            return self._job(rest)
        self._json({"error": "no such route"}, 404)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        if not self._authorised():
            return
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/jobs":
            body = self._body()
            try:
                job = self.app.start(int(body.get("index", -1)),
                                     body.get("options") or {})
            except KeyError as exc:
                return self._json({"error": str(exc)}, 400)
            except SystemExit as exc:
                # The tool's own "stop with a message" path. BaseException, so
                # an `except Exception` would miss it and the client would get
                # a closed socket instead of the reason.
                return self._json({"error": str(exc)}, 400)
            except Exception as exc:                       # noqa: BLE001
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return self._json(job.to_dict(), 202)
        if path.startswith("/api/jobs/") and path.endswith("/cancel"):
            job_id = path[len("/api/jobs/"):-len("/cancel")]
            ok = self.app.jobs.cancel(job_id)
            return self._json({"cancelled": ok}, 202 if ok else 409)
        if path == "/api/jobs/clear":
            return self._json({"cleared": self.app.jobs.clear_finished()})
        self._json({"error": "no such route"}, 404)

    # -- handlers ------------------------------------------------------

    def _status(self):
        return {
            "catalogue": self.app.catalogue,
            "work_dir": self.app.work_dir,
            "out_dir": self.app.out_dir,
            "auth": bool(self.app.token),
            "connections": uc._default_connections(),
            "surge_host": os.environ.get("SURGE_HOST", ""),
            "active": len(self.app.jobs.active()),
            "scrapling": uc.scrapling_problem(),
        }

    def _catalogue(self):
        """The entries, or a reason there are none.

        The whole body is inside the try on purpose. Anything that escapes a
        request handler does not produce an error page -- the connection is
        closed and the browser reports a network failure, which is why "cannot
        load the catalogue" says nothing about what went wrong. A handler that
        always answers something is worth a great deal more than one that is
        usually right.
        """
        try:
            cat = self.app.load()
            return {
                "name": cat.name,
                "count": len(cat.entries),
                # Built here rather than by a method on Entry. Entry is the
                # tool's own model and the CLI prints it through
                # print_catalogue, not through a dict -- so a to_dict would be
                # a second, web-only view of the same row, and the one route
                # that had it was the one that shipped broken.
                "entries": [
                    {
                        "index": e.index,
                        "title": e.title,
                        "url": e.uri,
                        "hash": e.hash,
                        "declared_size": uc.parse_page_size(e.file_size),
                        "declared_size_text": e.file_size,
                        "uploaded": e.upload_date,
                    }
                    for e in cat.entries
                ],
            }
        except (Exception, SystemExit) as exc:            # noqa: BLE001
            return {"error": str(exc) or type(exc).__name__}

    def _job(self, job_id: str):
        job = self.app.jobs.get(job_id)
        if job is None:
            return self._json({"error": "no such job"}, 404)
        return self._json(job.to_dict())

    def _events(self, job_id: str):
        """Server-sent events for one job.

        Sends the state on connect and then only when it changes, so an idle
        job costs nothing and a progress bar updates without polling.
        """
        job = self.app.jobs.get(job_id)
        if job is None:
            return self._json({"error": "no such job"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last = None
        try:
            while True:
                state = job.to_dict()
                if state != last:
                    self.wfile.write(f"data: {json.dumps(state)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    last = state
                if state["state"] in (jobs.DONE, jobs.FAILED, jobs.CANCELLED):
                    # Ending the stream has to end the socket with it. The
                    # Connection header above says keep-alive but is only
                    # advisory: close_connection is set from the *request*, and
                    # an HTTP/1.1 request without "Connection: close" leaves it
                    # False. So returning here used to leave the socket open
                    # with the client still waiting for the stream to finish.
                    # A browser's EventSource does not notice -- it reconnects
                    # on its own -- but any script reading to the end hangs
                    # until it times out, which is the client the plain JSON
                    # routes are for.
                    self.close_connection = True
                    return
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return                        # the browser went away; fine

    def _static(self, name: str):
        # Resolved and checked inside STATIC: a name from the URL must not be
        # able to walk out of it.
        target = (STATIC / name).resolve()
        try:
            target.relative_to(STATIC.resolve())
        except ValueError:
            return self._send(403, b"no", "text/plain")
        if not target.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})


def serve(host: str = "127.0.0.1", port: int = DEFAULT_PORT, token: str = "",
          catalogue: str | None = None, work_dir: str | None = None,
          out_dir: str | None = None) -> None:
    Handler.app = Server(token=token, catalogue=catalogue,
                         work_dir=work_dir, out_dir=out_dir)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    where = f"http://{host}:{httpd.server_address[1]}/"
    print(f"uc-archiver on {where}", flush=True)
    if not token:
        print("  no token set, so anything that can reach this address can "
              "start runs", flush=True)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  bound to {host}, which is every interface -- set a token if "
              f"this is not a machine only you can reach", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", flush=True)
    finally:
        httpd.server_close()
