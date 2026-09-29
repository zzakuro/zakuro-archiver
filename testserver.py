"""A local HTTP server for testing the downloader, in both of its moods.

`http.server` is enough and it is in the standard library, so this runs in
the test suite and in the container without anything installed.

The switch that matters is `honour_ranges`. A host that ignores a Range
header and sends the whole file is the dangerous case: four threads each
believing they own the whole file produce something the right size and full
of holes. The server can be told to do exactly that, so the fallback is
tested rather than assumed.
"""

import http.server
import threading


class _Handler(http.server.BaseHTTPRequestHandler):
    payload = b""
    honour_ranges = True
    hits = 0
    lock = threading.Lock()
    # Range start offset -> how many more times to answer it badly. This is
    # what makes a throttling host reproducible: the real one rate limits some
    # connections and leaves the rest alone, and that is exactly the case the
    # per-span retry exists to survive.
    flaky = {}
    # Range start offset -> bytes to withhold from the end of that response, so
    # the span arrives short and the retry has to resume from a real offset
    # rather than from the start of the slice.
    shorten = {}
    asked = []

    def log_message(self, *_args):
        pass

    def _flaky(self, start):
        cls = type(self)
        with cls.lock:
            left = cls.flaky.get(start, 0)
            if left:
                cls.flaky[start] = left - 1
        return left

    def do_GET(self):
        cls = type(self)
        with cls.lock:
            cls.hits += 1
        data = cls.payload
        rng = self.headers.get("Range")
        if not rng or not cls.honour_ranges or not rng.startswith("bytes="):
            # The whole file, 200, as a server that ignores ranges would.
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        try:
            start_s, _, end_s = rng[len("bytes="):].partition("-")
            start = int(start_s)
            end = int(end_s) if end_s else len(data) - 1
        except ValueError:
            self.send_error(416)
            return
        with cls.lock:
            cls.asked.append((start, end))
        if start >= len(data):
            self.send_error(416)
            return
        end = min(end, len(data) - 1)

        if self._flaky(start):
            # 429, the way the share host answers when pushed too hard. No
            # body, and crucially no bytes written by the client.
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.end_headers()
            return

        chunk = data[start:end + 1]
        with cls.lock:
            drop = cls.shorten.get(start, 0)
        if drop:
            chunk = chunk[:max(0, len(chunk) - drop)]
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(len(chunk)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(chunk)


class Server:
    """Serve one payload on localhost for as long as the context lives."""

    def __init__(self, payload: bytes, honour_ranges: bool = True,
                 flaky: dict | None = None, shorten: dict | None = None):
        handler = type("H", (_Handler,), {
            "payload": payload, "honour_ranges": honour_ranges, "hits": 0,
            "flaky": dict(flaky or {}), "shorten": dict(shorten or {}),
            "asked": []})
        self.handler = handler
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/file.bin"

    @property
    def hits(self) -> int:
        return self.handler.hits

    @property
    def asked(self) -> list:
        with self.handler.lock:
            return list(self.handler.asked)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
