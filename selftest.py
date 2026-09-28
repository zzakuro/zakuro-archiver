"""Offline checks for uc-archiver.

Nothing here touches the network and nothing needs an archiver: the store is
faked at the urlopen boundary and the archive work is exercised against a real
zip built with the stdlib. What is being tested is the part that is easy to get
wrong and expensive to get wrong -- picking the right row out of a catalogue,
refusing an archive that writes outside its folder, editing what it says it
edits, and naming the output once rather than twice.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import uc_archiver as uc  # noqa: E402


class Checks:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
        else:
            self.failed += 1
            print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""), file=sys.stderr)

    def note(self, msg: str) -> None:
        print(f"  ..    {msg}", file=sys.stderr)


CATALOGUE = {
    "name": "TestSource",
    "downloads": [
        {"title": "Hades (b1)", "uris": ["https://vikingfile.com/f/aaaaaaaaaaaa"],
         "uploadDate": "2024-01-01T00:00:00.000Z", "fileSize": "11.1 GB"},
        {"title": "Hades II (b2)", "uris": ["https://vikingfile.com/f/bbbbbbbbbbbb"],
         "fileSize": ""},
        {"title": "Hollow Knight (b3)", "uris": ["https://vikingfile.com/f/cccccccccccc"],
         "fileSize": "5 GB"},
        {"title": "No Link (b4)", "uris": []},
        {"title": "Bad Row"},
    ],
}


def run(checks: Checks) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="uc-selftest-"))
    try:
        _catalogue(checks, tmp)
        _hashes(checks)
        _selection(checks, tmp)
        _safety(checks)
        _editing(checks, tmp)
        _naming(checks)
        _profiles(checks, tmp)
        _store(checks, tmp)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ catalogue
def _catalogue(checks: Checks, tmp: Path) -> None:
    path = tmp / "cat.json"
    path.write_text(json.dumps(CATALOGUE), encoding="utf-8")
    cat = uc.load_catalogue(path)
    checks.check("catalogue: name is read", cat.name == "TestSource", cat.name)
    # Two rows are unusable: one has no link, one is not even an object. Both
    # are dropped rather than becoming entries that fail later.
    checks.check("catalogue: rows with no usable link are dropped",
                 len(cat.entries) == 3, str(len(cat.entries)))
    checks.check("catalogue: numbers are 1-based and contiguous",
                 [e.index for e in cat.entries] == [1, 2, 3],
                 str([e.index for e in cat.entries]))
    checks.check("catalogue: an empty file size is kept, not invented",
                 cat.entries[1].file_size == "", cat.entries[1].file_size)
    checks.check("catalogue: a bare downloads list also loads", True)
    bare = tmp / "bare.json"
    bare.write_text(json.dumps(CATALOGUE["downloads"]), encoding="utf-8")
    checks.check("catalogue: a bare list works too",
                 len(uc.load_catalogue(bare).entries) == 3)
    for bad, label in (("not json at all", "not json"),
                       ('{"name": "x"}', "no downloads list")):
        p = tmp / "bad.json"
        p.write_text(bad, encoding="utf-8")
        try:
            uc.load_catalogue(p)
            checks.check(f"catalogue: {label} is refused", False)
        except SystemExit:
            checks.check(f"catalogue: {label} is refused", True)


# ---------------------------------------------------------------------- hashes
def _hashes(checks: Checks) -> None:
    good = "https://vikingfile.com/f/oBr9xRdY11"
    checks.check("hash: a full link yields the hash", uc.hash_from_uri(good) == "oBr9xRdY11")
    checks.check("hash: a bare hash is accepted",
                 uc.hash_from_uri("oBr9xRdY11") == "oBr9xRdY11")
    checks.check("hash: a trailing slash is tolerated",
                 uc.hash_from_uri(good + "/") == "oBr9xRdY11")
    checks.check("hash: nothing yields nothing", uc.hash_from_uri("") == "")
    checks.check("hash: a page link is not mistaken for a hash",
                 uc.hash_from_uri("https://vikingfile.com/f/") == "")


# ------------------------------------------------------------------- selection
def _selection(checks: Checks, tmp: Path) -> None:
    path = tmp / "cat.json"
    path.write_text(json.dumps(CATALOGUE), encoding="utf-8")
    cat = uc.load_catalogue(path)

    def pick(*argv: str):
        return uc.pick_entry(cat, uc.build_parser().parse_args(["x", *argv]))

    checks.check("pick: by number", pick("--pick", "2").title.startswith("Hades II"))
    checks.check("pick: an exact title beats a substring",
                 pick("--match", "Hades (b1)").title == "Hades (b1)")
    checks.check("pick: a substring still finds it",
                 pick("--match", "hollow", "--first").title.startswith("Hollow"))
    checks.check("pick: a number past the end is refused",
                 _fails(pick, "--pick", "99"))
    checks.check("pick: no match is refused", _fails(pick, "--match", "zzzz"))
    checks.check("pick: nothing said is refused", _fails(pick))


def _fails(fn, *argv) -> bool:
    try:
        fn(*argv)
        return False
    except SystemExit:
        return True


# --------------------------------------------------------------------- safety
def _safety(checks: Checks) -> None:
    """An archive from the internet is untrusted input.

    These are the names that would write outside the destination, and the ones
    that look similar and are fine. Getting this wrong means unpacking
    something over the system, so each hostile one is named explicitly.
    """
    unsafe = [
        "/etc/passwd",
        "C:/Windows/System32/evil.dll",
        "C:\\Windows\\evil.dll",
        "../../outside.txt",
        "game/../../outside.txt",
        "..\\..\\outside.txt",
        "//server/share/evil.dll",
        # A bare .. on its own resolves to the parent of the destination, so
        # it is refused. `dir/..` is the harmless case and is not.
        "..",
    ]
    for name in unsafe:
        checks.check(f"safety: refuses {name!r}", uc.is_unsafe_entry(name))

    safe = [
        "game.exe",
        "data/maps/one.txt",
        "data/maps/..hidden",
        "a..b/c.txt",
        "..leading-dots/file.txt",
        ".",
        "",
        "日本語/ファイル.txt",
        " spaced name .txt",
    ]
    for name in safe:
        checks.check(f"safety: allows {name!r}", not uc.is_unsafe_entry(name))
    # `dir/..` resolves back to the folder it came from, so it stays inside.
    # The path is normalised rather than trusted: an entry that only looks
    # harmless because of what is around it is how a traversal gets through.
    checks.check("safety: a trailing .. is normalised, not trusted",
                 not uc.is_unsafe_entry("dir/.."))
    checks.check("safety: a leading .. is still refused",
                 uc.is_unsafe_entry("../x.txt"))

    try:
        uc.screen_entries(["game.exe", "../../evil.dll"], "archive")
        checks.check("safety: a bad listing stops the extract", False)
    except SystemExit as exc:
        checks.check("safety: a bad listing stops the extract",
                     "refusing" in str(exc).lower(), str(exc)[:60])

    checks.check("safety: a clean listing passes",
                 len(uc.screen_entries(["a.txt", "b/c.dll"], "archive")) == 2)


# -------------------------------------------------------------------- editing
def _editing(checks: Checks, tmp: Path) -> None:
    root = tmp / "Game"
    (root / "Online").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "Data").mkdir()
    (root / "game.exe").write_text("exe", encoding="utf-8")
    (root / "readme.txt").write_text("readme", encoding="utf-8")
    (root / "Data" / "table.pak").write_text("pak", encoding="utf-8")
    (root / "Online" / "news.html").write_text("news", encoding="utf-8")
    (root / "logs" / "old.log").write_text("log", encoding="utf-8")

    removed = uc.remove_matches(root, ["*.txt", "Online", "logs"])
    checks.check("edit: a wildcard takes the file", "readme.txt" in removed, str(removed))
    checks.check("edit: a bare name takes the whole folder",
                 "Online" in removed, str(removed))
    checks.check("edit: the folder's contents go with it",
                 not (root / "Online" / "news.html").exists())
    checks.check("edit: another bare folder goes too", "logs" in removed, str(removed))
    checks.check("edit: what was not named is still there",
                 (root / "game.exe").is_file() and (root / "Data" / "table.pak").is_file())
    checks.check("edit: a pattern matching nothing is not an error",
                 uc.remove_matches(root, ["nothing-like-this"]) == [])

    # A pattern is matched against the relative path too, so `Data/table.pak`
    # works as well as the bare file name.
    got = uc.remove_matches(root, ["Data/table.pak"])
    checks.check("edit: a relative path works as a pattern",
                 got == ["Data/table.pak"], str(got))

    # --add, with and without a rename
    payload = tmp / "payload.txt"
    payload.write_text("mine", encoding="utf-8")
    folder = tmp / "extras"
    folder.mkdir()
    (folder / "note.md").write_text("note", encoding="utf-8")
    placed = uc.add_files(root, [str(payload), f"ReadMe={payload}", str(folder)])
    checks.check("edit: a bare path keeps its name", "payload.txt" in placed, str(placed))
    checks.check("edit: NAME=path renames on the way in", "ReadMe" in placed, str(placed))
    checks.check("edit: the renamed file has the new name and the right bytes",
                 (root / "ReadMe").is_file() and (root / "ReadMe").read_text(encoding="utf-8") == "mine")
    checks.check("edit: a folder is copied whole",
                 (root / folder.name / "note.md").is_file())
    checks.check("edit: a missing --add is an error, not a silent skip",
                 _fails(uc.add_files, root, [str(tmp / "nope.txt")]))
    # `=` only means a rename when the right-hand side is a file that is
    # really there, so a path with an `=` in a directory name is left alone.
    eqdir = tmp / "my=games"
    eqdir.mkdir(exist_ok=True)
    inside = eqdir / "thing.exe"
    inside.write_text("x", encoding="utf-8")
    checks.check("edit: '=' in a directory name is not a rename",
                 uc.split_extra(str(inside)) == (None, inside),
                 str(uc.split_extra(str(inside))))
    checks.check("edit: a real NAME=path still renames",
                 uc.split_extra(f"New Name={payload}") == ("New Name", payload),
                 str(uc.split_extra(f"New Name={payload}")))
    checks.check("edit: a rename onto a missing file is not a rename",
                 uc.split_extra("Name=nothing-here.txt")[0] is None)


# --------------------------------------------------------------------- naming
def _naming(checks: Checks) -> None:
    checks.check("name: the tag goes on once",
                 uc.zakuro_name("Hades", ".rar") == "Hades [Zakuro].rar",
                 uc.zakuro_name("Hades", ".rar"))
    checks.check("name: a re-run does not double it",
                 uc.zakuro_name("Hades [Zakuro]", ".rar") == "Hades [Zakuro].rar",
                 uc.zakuro_name("Hades [Zakuro]", ".rar"))
    checks.check("name: the old spelling is normalised too",
                 uc.zakuro_name("Hades [ZAKURO]", ".rar") == "Hades [Zakuro].rar",
                 uc.zakuro_name("Hades [ZAKURO]", ".rar"))
    checks.check("name: a version suffix survives",
                 uc.zakuro_name("Hollow Knight (V1.5.12620)", ".rar")
                 == "Hollow Knight (V1.5.12620) [Zakuro].rar")
    checks.check("name: a 7z build still gets tagged",
                 uc.zakuro_name("Hades", ".7z") == "Hades [Zakuro].7z")


# -------------------------------------------------------------------- profiles
def _profiles(checks: Checks, tmp: Path) -> None:
    path = tmp / "cat.json"
    path.write_text(json.dumps(CATALOGUE), encoding="utf-8")
    cat = uc.load_catalogue(path)
    base = tmp / "profiles"

    target = uc.profile_path(cat, base)
    checks.check("profile: one per catalogue", target.name == "testsource.profile.json",
                 target.name)
    checks.check("profile: named apart from the catalogue",
                 target.suffix == ".json" and "profile" in target.name)

    uc.save_profile(target, uc.Profile(remove=["*.txt"], add=["a=b"], level="max"))
    again = uc.load_profile(target)
    checks.check("profile: what was saved comes back",
                 again.remove == ["*.txt"] and again.add == ["a=b"] and again.level == "max",
                 str(again.to_dict()))
    checks.check("profile: a missing profile is an empty one, not a crash",
                 uc.load_profile(base / "nothing.json").remove == [])

    # The command line wins; anything not given comes from the profile.
    args = uc.build_parser().parse_args(
        ["x", "--remove", "Online", "--add", "z=z", "--level", "store"])
    got, _ = uc.resolve_settings(args, cat, base)
    checks.check("profile: the command line overrides the profile",
                 got.remove == ["Online"] and got.add == ["z=z"] and got.level == "store",
                 str(got.to_dict()))

    args = uc.build_parser().parse_args(["x", "--remove", "Online"])
    got, _ = uc.resolve_settings(args, cat, base)
    checks.check("profile: anything not given is reused",
                 got.level == "max" and got.remove == ["Online"], str(got.to_dict()))

    args = uc.build_parser().parse_args(["x", "--no-profile", "--level", "high"])
    got, _ = uc.resolve_settings(args, cat, base)
    checks.check("profile: --no-profile ignores what was saved",
                 got.level == "high" and got.remove == [], str(got.to_dict()))

    # Two catalogues must not share a profile.
    other = tmp / "other.json"
    other.write_text(json.dumps({"name": "Other Source", "downloads": CATALOGUE["downloads"]}),
                     encoding="utf-8")
    other_path = uc.profile_path(uc.load_catalogue(other), base)
    checks.check("profile: a different catalogue gets a different profile",
                 other_path != target, f"{other_path.name} vs {target.name}")


# ---------------------------------------------------------------------- store
def _store(checks: Checks, tmp: Path) -> None:
    """The waiting and downloading, with the host faked out.

    The catalogue's own fileSize is a human string and is empty on seven of the
    real entries, so the size the host reports is the only trustworthy one and
    the download is checked against it.
    """
    calls: list[dict] = []

    class Resp:
        def __init__(self, body: bytes, status: int = 200,
                     headers: dict | None = None) -> None:
            self._body, self.status = body, status
            self.headers = headers or {}

        def read(self, n: int = -1) -> bytes:
            if self._body and n != 1:
                out, self._body = self._body, b""
                return out
            return self._body

        def __enter__(self) -> "Resp":
            return self

        def __exit__(self, *_exc: object) -> bool:
            return False

    payload = b"x" * 5000

    def fake_urlopen(request, *_a, **_kw):
        url = getattr(request, "full_url", "")
        if "check-file" in url:
            calls.append({"n": 1})
            body = json.dumps([{"exist": True, "hash": "abc",
                                "name": "Game - UC.7z", "size": len(payload)}])
            return Resp(body.encode())
        return Resp(payload, headers={"Content-Length": str(len(payload))})

    import urllib.request as _u

    real = _u.urlopen
    _u.urlopen = fake_urlopen
    try:
        viking = uc.Viking(timeout=5)
        row = viking.wait_until_ready("abc", tries=3, pause=0)
        checks.check("store: a present file is reported ready", bool(row), str(row))
        checks.check("store: the host's real name is used, not the catalogue's",
                     row.get("name") == "Game - UC.7z", str(row.get("name")))
        checks.check("store: the host's real size is used",
                     row.get("size") == len(payload), str(row.get("size")))
        checks.check("store: a file that is there does not retry",
                     len(calls) == 1, f"{len(calls)} calls")

        calls.clear()

        def not_there(request, *_a, **_kw):
            if "check-file" in getattr(request, "full_url", ""):
                calls.append({"n": 1})
                return Resp(json.dumps([{"exist": False, "hash": "zz"}]).encode())
            return Resp(payload, headers={"Content-Length": str(len(payload))})

        _u.urlopen = not_there
        row = uc.Viking(timeout=5).wait_until_ready("zz", tries=3, pause=0)
        checks.check("store: a file that never turns up gives up", row == {})
        checks.check("store: and it really did retry", len(calls) == 3, f"{len(calls)} calls")

        # batch: the endpoint takes 100 hashes, so 260 is three requests
        calls.clear()
        many = [f"h{i:04d}" for i in range(260)]

        def batched(request, *_a, **_kw):
            import urllib.parse as up
            # The hashes go in the POST body, not the query string.
            body = request.data.decode() if request.data else ""
            got = up.parse_qs(body).get("hash[]", [])
            calls.append({"n": len(got)})
            rows = [{"exist": h != "h0007", "hash": h} for h in got]
            return Resp(json.dumps(rows).encode())

        _u.urlopen = batched
        found = uc.Viking(timeout=5).check_many(many)
        checks.check("store: 260 hashes cost 3 requests", len(calls) == 3, f"{len(calls)}")
        checks.check("store: every chunk is at most 100",
                     all(c["n"] <= 100 for c in calls), str(calls))
        checks.check("store: the present ones come back", len(found) == 259, str(len(found)))

        # download
        _u.urlopen = fake_urlopen
        dest = tmp / "got.bin"
        got_bytes = uc.download("https://vikingfile.com/f/abc", dest,
                                expect=len(payload), timeout=5)
        checks.check("store: a whole file lands at the right size",
                     got_bytes == len(payload) and dest.stat().st_size == len(payload),
                     f"{got_bytes} vs {len(payload)}")
    finally:
        _u.urlopen = real



# ---- the share page resolver -------------------------------------------
# The page scrape is the one part that cannot be checked without a browser and
# a live challenge, so what is checked here is everything around it: the size
# arithmetic, the filename fallback, and the refusal when there is no link.

def _size_checks(checks) -> None:
    """The unit table, against the values the site actually writes."""
    cases = [
        ("3.14 MB", 3 * 1024 ** 2 + 140 * 1024),
        ("1.61 GB", int(1.61 * 1024 ** 3)),
        ("992 MB", 992 * 1024 ** 2),
        ("605.7 MB", 605 * 1024 ** 2 + 700 * 1024),
        ("53.3 GB", int(53.3 * 1024 ** 3)),
        ("343 MB", 343 * 1024 ** 2),
    ]
    for text, want in cases:
        got = int(float(uc._UNIT_SCALE.get(
            "".join(c for c in text if c.isalpha()).upper(), 1))
            * float("".join(c for c in text if c.isdigit() or c == ".")))
        checks.check(f"share size: {text} is read in 1024s",
                     abs(got - want) <= want * 0.02, f"got {got}, want about {want}")


def _resolver_checks(checks) -> None:
    """A fake page, so the parsing can be tested without a browser."""
    from unittest import mock

    class FakeResults(list):
        def get(self, default=None):
            return self[0] if self else default

    class FakePage:
        url = "https://vik1ngfile.site/f/xITbBs4Q6l"

        def __init__(self, name, size, href, title="x - UC.7z"):
            self._name, self._size, self._href, self._title = name, size, href, title

        def css(self, selector):
            table = {
                "#filename::text": self._name,
                "#size::text": self._size,
                "#download-link::attr(href)": self._href,
                "title::text": self._title,
                "#file-information p::text": "",
            }
            value = table.get(selector)
            if value is None:
                return FakeResults([])
            # A ::attr() selector yields the attribute's value, so .get() is
            # the string itself. Faking an element here would not match what
            # scrapling actually returns.
            return FakeResults([value])

    def resolve_with(page, ref="xITbBs4Q6l"):
        class FakeSession:
            def __init__(self, **_kw):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def fetch(self, url, timeout=None):
                return page
        import scrapling.fetchers as real
        with mock.patch.object(real, "StealthySession", FakeSession):
            return uc.resolve_share(ref)

    good = FakePage("Some Game - UC.7z", "3.14 MB",
                    "https://vikingfile.com/d/abc/Some%20Game.7z")
    share = resolve_with(good)
    checks.check("share: the name comes off the page",
                 share.name == "Some Game - UC.7z", share.name)
    checks.check("share: the size comes off the page, not the catalogue",
                 abs(share.size - 3.14 * 1024 ** 2) < 1024 ** 2, str(share.size))
    checks.check("share: the link is the /d/ one, not the page",
                 "/d/abc/" in share.download_url, share.download_url)

    moved = FakePage("", "", "https://vikingfile.com/d/abc/x.7z",
                     title="Some Game - UC.7z")
    s2 = resolve_with(moved)
    checks.check("share: a moved id falls back to the title",
                 s2.name == "Some Game", s2.name)

    try:
        resolve_with(FakePage("n", "1 MB", ""))
        checks.check("share: no link is a clear error, not a bad download",
                     False, "it returned instead of raising")
    except uc.ShareUnavailable as exc:
        checks.check("share: no link is a clear error, not a bad download",
                     "no download link" in str(exc), str(exc))

    problem = uc.scrapling_problem()
    checks.check("share: a missing scrapling is reported as an install step",
                 problem is None or "pip install" in problem, str(problem))


def main() -> int:
    checks = Checks()
    run(checks)
    _size_checks(checks)
    _resolver_checks(checks)
    total = checks.passed + checks.failed
    print(f"uc-archiver selftest: {checks.passed} passed, {checks.failed} failed")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
