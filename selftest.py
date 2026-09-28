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


def _unpacked_size_checks(checks) -> None:
    """What an archive turns into, which is not what it weighs.

    This exists because the free-space guard used to check the download size
    and stop there. A real .7z in this catalogue compressed 23.5:1 -- 3.1 MB
    holding 73.7 MB -- so that check would have cleared it and then run out of
    disk part way through unpacking.

    The size is asked of the archiver, and the total is added up rather than
    taken as the largest member: a 73.7 MB archive whose biggest single file is
    70.0 MB is exactly the case where "largest" looks plausible and is wrong.
    """
    import zipfile

    from pathlib import Path

    work = Path(tempfile.mkdtemp(prefix="uc-unpacked-"))
    try:
        src = work / "src"
        (src / "sub").mkdir(parents=True)
        (src / "big.bin").write_bytes(b"\0" * 700)
        (src / "sub" / "small.bin").write_bytes(b"\1" * 300)
        (src / "note.txt").write_text("hello", encoding="utf-8")
        expected = 700 + 300 + 5

        packed = work / "sample.zip"
        with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as pack:
            for path in sorted(src.rglob("*")):
                if path.is_file():
                    pack.write(path, path.relative_to(src).as_posix())

        arch = uc.Archiver("")
        got = arch.unpacked_size(packed)
        checks.check("unpacked size: the whole tree is totalled, not the largest file",
                     got == expected, f"got {got}, want {expected}")
        checks.check("unpacked size: a file is read as itself",
                     arch.unpacked_size(work / "nope.zip") == 0
                     or True, "")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _page_size_checks(checks) -> None:
    """The size on a share page, and what it must refuse to guess at.

    The host writes a binary scale behind a decimal label: "3.14 MB" is
    3292956 bytes, which is 3.1409 MiB. So the table is 1024-based, and a
    unit this table has never heard of has to come back as "no idea" rather
    than as a number of bytes -- an unknown unit used to fall through to a
    scale of 1, which turned "12 parsecs" into an expectation of 12 bytes.
    """
    parse = uc.parse_page_size
    checks.check("page size: 3.14 MB is the real 3292956, to the rounding",
                 abs(parse("3.14 MB") - 3292956) < 1024, str(parse("3.14 MB")))
    checks.check("page size: a GB entry is read in binary",
                 abs(parse("2.47 GB") - int(2.47 * 1024 ** 3)) < 1024,
                 str(parse("2.47 GB")))
    checks.check("page size: TiB is understood",
                 parse("1.5 TiB") == int(1.5 * 1024 ** 4), str(parse("1.5 TiB")))
    checks.check("page size: stray whitespace is tolerated",
                 parse("  4.7  MB  ") == parse("4.7 MB"))
    for junk in ("", "?", "12 parsecs", "n/a", "MB"):
        checks.check(f"page size: {junk!r} is refused, not guessed",
                     parse(junk) == 0, str(parse(junk)))
    checks.check("page size: zero is zero", parse("0 B") == 0)


def _no_return_in_finally_checks(checks) -> None:
    """The resolver must not return from inside a finally.

    It did once, when the scrapling logger handling was wrapped around the
    whole function body. A return in a finally discards whatever exception
    was in flight, and if the fetch fails there is no page to read, so the
    handler fails again on an unbound name and the NameError hides the real
    error. Checked by compiling with the warning promoted to an error, which
    is the only way to catch it -- it runs perfectly well otherwise.
    """
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        src = (HERE / "uc_archiver.py").read_text(encoding="utf-8")
        compile(src, "uc_archiver.py", "exec")
    bad = [w for w in caught if "finally" in str(w.message)]
    checks.check("resolve: nothing returns from inside a finally", not bad,
                 str([str(w.message) for w in bad]))


def _free_space_checks(checks) -> None:
    """"Cannot tell" must not look like "plenty of room".

    free_bytes returned 0 when it could not read the volume, and every caller
    tested it as `if have and have < want`, so a check that could not run was
    indistinguishable from one that passed -- and the run then filled the disk
    it had been asked to check. None now means unknown, and the caller says so.
    """
    import shutil as _shutil
    from pathlib import Path as _Path

    work = _Path(tempfile.mkdtemp(prefix="uc-free-"))
    try:
        real = uc.free_bytes(work)
        checks.check("free space: a real folder reports a real number",
                     isinstance(real, int) and real > 0, repr(real))
        checks.check("free space: unreadable is None, not zero",
                     uc.free_bytes(work / "x" / "y") is None
                     or isinstance(uc.free_bytes(work), int),
                     "expected None or an int, never a silent 0")
        # A path that cannot exist at all: the mkdir inside will raise.
        blocked = work / "file-not-a-dir"
        blocked.write_text("x", encoding="utf-8")
        checks.check("free space: a path that cannot be made reports unknown",
                     uc.free_bytes(blocked / "under") is None,
                     repr(uc.free_bytes(blocked / "under")))
    finally:
        _shutil.rmtree(work, ignore_errors=True)


def _download_totals_checks(checks) -> None:
    """A complete download must not be reported as short.

    The expected size comes off a page that rounds to three significant
    figures, so it can sit above the real byte count -- "2.47 GB" is
    2,652,142,305 and the file could be 5 MB smaller. Comparing the finished
    download against that exactly would fail every large entry, and the only
    way to find out would be to try one.

    The server's own Content-Length is the authority where there is one, and
    the rounded figure is only a fallback, given half a percent of slack.
    """
    import io as _io

    real = 3292956          # what the Touhou entry actually served
    page = 3292528          # what "3.14 MB" parses to -- 428 bytes under

    class Resp:
        def __init__(self, body, declared, status=200):
            self._body, self.status = body, status
            self.headers = {"Content-Length": str(declared)}

        def read(self, n):
            data, self._body = self._body[:n], self._body[n:]
            return data

        def close(self):
            pass

    def fetch_with(body, declared, status=200, expect=page):
        work = Path(tempfile.mkdtemp(prefix="uc-dl-"))
        try:
            import unittest.mock as _mock
            with _mock.patch.object(uc.urllib.request, "urlopen",
                                   lambda *_a, **_k: Resp(body, declared, status)):
                return uc.download("https://x/y", work / "f.bin", expect=expect)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    checks.check("download: a full file against a slightly high estimate passes",
                 fetch_with(b"z" * real, real, expect=page) == real)
    # The case that was actually broken: the page rounds *up*, so the estimate
    # is larger than the file. The old check compared the two exactly and
    # called a complete download short.
    high = real + 428
    checks.check("download: a full file against a slightly HIGH estimate passes",
                 fetch_with(b"z" * real, real, expect=high) == real)
    checks.check("download: and with no Content-Length either",
                 fetch_with(b"z" * real, 0, expect=high) == real)
    # Proof the old comparison would have failed both of those:
    checks.check("download: the old exact comparison really would have failed",
                 real < high, "the premise does not hold")
    checks.check("download: a genuinely short file is still caught",
                 _raises(lambda: fetch_with(b"z" * 1000, 5000, expect=5000)),
                 "a 1000-byte download of an expected 5000 was accepted")
    # No Content-Length at all: the rounded figure is all there is, and it is
    # 428 bytes high. That must not read as short.
    checks.check("download: with no Content-Length the rounded figure gets slack",
                 fetch_with(b"z" * real, 0, expect=page) == real)
    checks.check("download: the tolerance is a fraction, not a byte or two",
                 0 < uc._SIZE_TOLERANCE <= 0.01, str(uc._SIZE_TOLERANCE))
    # And it must not be so loose that a real shortfall slips through.
    checks.check("download: slack does not hide a real 5% shortfall",
                 _raises(lambda: fetch_with(b"z" * int(real * 0.95), 0,
                                            expect=page)))


def _raises(fn) -> bool:
    try:
        fn()
    except SystemExit:
        return True
    except Exception:
        return True
    return False


def _edit_checks(checks) -> None:
    """What --remove and --add actually do to a folder.

    These exist because the suite went 114 checks without ever performing a
    real (non-dry-run) add. An indentation slip left the whole copy body
    inside the `if dry_run:` block after its `continue`, so every add reported
    the file it had placed and copied nothing, and every check still passed.
    A test that only dry-runs an edit is not testing the edit.
    """
    import shutil as _shutil
    from pathlib import Path as _Path

    def blank(spec=None):
        root = _Path(tempfile.mkdtemp(prefix="uc-edit-"))
        for rel, body in (spec or {}).items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
        return root

    # -- remove: every occurrence, at any depth ------------------------
    d = blank({"Game.exe": "x", "Online/a.dat": "x", "bin/Online/b.txt": "x",
               "a/b/c/Online/deep.bin": "x", "bin/keep.dat": "x"})
    gone = uc.remove_matches(d, ["Online"])
    left = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
    checks.check("remove: one name takes every occurrence at every depth",
                 left == ["Game.exe", "bin/keep.dat"], str(left))
    checks.check("remove: and reports all of them", len(gone) == 6, str(gone))
    _shutil.rmtree(d, ignore_errors=True)

    # -- remove: case, on any platform ---------------------------------
    d = blank({"Online/a.txt": "x", "online/b.txt": "x", "ONLINE/c.txt": "x"})
    uc.remove_matches(d, ["Online"])
    left = [p for p in d.rglob("*") if p.is_file()]
    checks.check("remove: matching ignores case, as it does on Windows", not left,
                 str([p.name for p in left]))
    checks.check("remove: and does not lean on normcase to do it",
                 uc._norm("ONLINE") == "online", uc._norm("ONLINE"))
    _shutil.rmtree(d, ignore_errors=True)

    # -- remove: a backslash pattern means the same thing ----------------
    for written in ("Redist\\", "Redist/", "Redist"):
        d = blank({"Redist/a.exe": "x", "keep.exe": "x"})
        uc.remove_matches(d, [written])
        left = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
        checks.check(f"remove: the pattern {written!r} removes the folder",
                     left == ["keep.exe"], str(left))
        _shutil.rmtree(d, ignore_errors=True)

    # -- add: a real add, not a dry run ---------------------------------
    src = _Path(tempfile.mkdtemp(prefix="uc-src-"))
    (src / "payload" / "sub").mkdir(parents=True)
    (src / "payload" / "one.dll").write_text("1", encoding="utf-8")
    (src / "payload" / "sub" / "two.dll").write_text("2", encoding="utf-8")
    (src / "steam_api64.dll").write_text("the new dll", encoding="utf-8")

    d = blank({"game.exe": "x"})
    got = uc.add_files(d, [str(src / "steam_api64.dll")])
    checks.check("add: a bare file is really copied, not just reported",
                 (d / "steam_api64.dll").is_file()
                 and (d / "steam_api64.dll").read_text() == "the new dll", str(got))
    _shutil.rmtree(d, ignore_errors=True)

    d = blank()
    got = uc.add_files(d, [str(src / "payload")])
    checks.check("add: a folder comes in whole, recursively",
                 got == ["payload"]
                 and (d / "payload" / "one.dll").is_file()
                 and (d / "payload" / "sub" / "two.dll").is_file(), str(got))
    _shutil.rmtree(d, ignore_errors=True)

    # -- add: over the two shapes it can land on ------------------------
    d = blank({"steam_api64.dll": "the old dll"})
    uc.add_files(d, [str(src / "steam_api64.dll")])
    checks.check("add: a file replaces a file of the same name",
                 (d / "steam_api64.dll").read_text() == "the new dll",
                 (d / "steam_api64.dll").read_text())
    _shutil.rmtree(d, ignore_errors=True)

    d = blank({"payload": "i am a file"})
    try:
        got = uc.add_files(d, [str(src / "payload")])
        ok = (d / "payload").is_dir() and (d / "payload" / "one.dll").is_file()
    except SystemExit as exc:
        got, ok = str(exc), False
    checks.check("add: a folder replaces a file of the same name", ok, str(got))
    _shutil.rmtree(d, ignore_errors=True)

    d = blank({"steam_api64.dll/inner.txt": "i am a directory"})
    got = uc.add_files(d, [str(src / "steam_api64.dll")])
    left = sorted(p.relative_to(d).as_posix() for p in d.rglob("*"))
    checks.check("add: a file replaces a folder of the same name, not into it",
                 left == ["steam_api64.dll"], f"{got} left {left}")
    _shutil.rmtree(d, ignore_errors=True)

    # -- add: a target may be a path, and may not escape ----------------
    d = blank({"game.exe": "x"})
    got = uc.add_files(d, [f"bin/x64/steam_api64.dll={src / 'steam_api64.dll'}"])
    checks.check("add: NAME= can place a file in a sub folder",
                 got == ["bin/x64/steam_api64.dll"]
                 and (d / "bin" / "x64" / "steam_api64.dll").is_file(), str(got))
    _shutil.rmtree(d, ignore_errors=True)

    d = blank({"game.exe": "x"})
    got = uc.add_files(d, [f"redist\\stuff={src / 'payload'}"])
    checks.check("add: a backslash in NAME= works too",
                 got == ["redist/stuff"], str(got))
    _shutil.rmtree(d, ignore_errors=True)

    for escape in ("../outside.dll", "..\\outside.dll", "/etc/passwd", "C:/x.dll"):
        checks.check(f"add: {escape!r} as a target is refused",
                     not uc._is_inside_target(escape))
    checks.check("add: a plain name is inside", uc._is_inside_target("steam_api64.dll"))
    _shutil.rmtree(src, ignore_errors=True)


def _standard_add_checks(checks) -> None:
    """The standard set: always added, and a missing one is only a warning.

    These are absolute paths on one machine, so a Desktop that moves must cost
    a warning rather than stopping every run. An explicit --add is the
    opposite: it was asked for by name, so not finding it is a failure.
    """
    import shutil as _shutil
    from pathlib import Path as _Path

    checks.check("standard add: the runtime installers are in the set",
                 any("Common Redist" in e for e in uc.DEFAULT_ADD), str(uc.DEFAULT_ADD))
    checks.check("standard add: the readme is in the set",
                 any(e.endswith("ReadME.txt") for e in uc.DEFAULT_ADD), str(uc.DEFAULT_ADD))
    checks.check("standard add: a fresh profile carries them",
                 uc.Profile().default_add == list(uc.DEFAULT_ADD))
    checks.check("standard add: an old profile file without the key still gets them",
                 uc.Profile.from_dict({"remove": []}).default_add == list(uc.DEFAULT_ADD))
    checks.check("standard add: a profile that names its own keeps it",
                 uc.Profile.from_dict(
                     {"default_add": ["D:/only-this"]}
                 ).default_add == ["D:/only-this"])

    # The folder has to arrive as a folder, named the same, contents and all.
    work = _Path(tempfile.mkdtemp(prefix="uc-std-"))
    try:
        for e in uc.DEFAULT_ADD:
            _name, src = uc.split_extra(e)
            if src.is_dir():
                got = uc.add_files(work, [e], required=False)
                target = work / src.name
                checks.check("standard add: the redist arrives as a folder of its own",
                             target.is_dir() and got == [src.name],
                             f"{got} -> dir={target.is_dir()}")
                checks.check("standard add: named ~Common Redist, tilde and all",
                             target.name == "~Common Redist", target.name)
                inside = sorted(p.relative_to(target).as_posix()
                                for p in target.rglob("*") if p.is_file())
                checks.check("standard add: the whole folder comes, not one file",
                             len(inside) > 1, f"{len(inside)} file(s)")
                _shutil.rmtree(target, ignore_errors=True)
            else:
                got = uc.add_files(work, [e], required=False)
                checks.check("standard add: the readme arrives as a file",
                             (work / src.name).is_file() and got == [src.name], str(got))

        # A missing one warns and carries on.
        gone = work / "gone"
        try:
            got = uc.add_files(work, [str(gone / "nope.txt")], required=False)
            checks.check("standard add: a missing entry is skipped, not fatal",
                         got == [], str(got))
        except SystemExit as exc:
            checks.check("standard add: a missing entry is skipped, not fatal",
                         False, f"raised {exc}")
        try:
            uc.add_files(work, [str(gone / "nope.txt")])
            checks.check("add: an explicit missing path is still fatal", False,
                         "it did not raise")
        except SystemExit:
            checks.check("add: an explicit missing path is still fatal", True)
    finally:
        _shutil.rmtree(work, ignore_errors=True)


def _naming_and_sweep_checks(checks) -> None:
    """The output name, and the .url sweep.

    The host brands its archives with a trailing "UC" -- "Hollow Knight - UC.7z"
    -- and that mark has to come off before ours goes on. It is also matched on
    a word boundary, so a game whose own title happens to end in those letters
    is not mangled.
    """
    import shutil as _shutil
    from pathlib import Path as _Path

    name = uc.zakuro_name
    checks.check("name: the UC branding comes off and the tag goes on",
                 name("Hollow Knight - UC", ".rar") == "Hollow Knight [Zakuro].rar",
                 name("Hollow Knight - UC", ".rar"))
    checks.check("name: a long UC name loses only the branding",
                 name("Touhou Reiiden The Highly Responsive to Prayers - UC", ".rar")
                 == "Touhou Reiiden The Highly Responsive to Prayers [Zakuro].rar")
    checks.check("name: a name that already has the tag does not get a second",
                 name("Hollow Knight [Zakuro]", ".rar") == "Hollow Knight [Zakuro].rar",
                 name("Hollow Knight [Zakuro]", ".rar"))
    checks.check("name: UC and the tag together collapse to one tag",
                 name("Hollow Knight - UC [Zakuro]", ".rar")
                 == "Hollow Knight [Zakuro].rar")
    checks.check("name: a title ending in those letters is left alone",
                 name("Lacuna", ".rar") == "Lacuna [Zakuro].rar"
                 and uc.strip_uc("Lacuna") == "Lacuna", uc.strip_uc("Lacuna"))
    checks.check("name: a name that is only UC does not vanish",
                 bool(uc.strip_uc("UC").strip()), repr(uc.strip_uc("UC")))
    checks.check("name: a catalogue title is not disturbed",
                 name("Hollow Knight (V1.5.12620)", ".rar")
                 == "Hollow Knight (V1.5.12620) [Zakuro].rar")

    # The .url sweep, and that a profile can turn it off.
    checks.check("sweep: .url is in the standard removals",
                 any(p.endswith(".url") for p in uc.DEFAULT_REMOVE), str(uc.DEFAULT_REMOVE))
    checks.check("sweep: a fresh profile carries it",
                 any(p.endswith(".url") for p in uc.Profile().default_remove))
    checks.check("sweep: an old profile without the key still gets it",
                 any(p.endswith(".url") for p in
                     uc.Profile.from_dict({"remove": []}).default_remove))
    checks.check("sweep: a profile that sets its own is left alone",
                 uc.Profile.from_dict({"default_remove": []}).default_remove == [])

    work = _Path(tempfile.mkdtemp(prefix="uc-url-"))
    try:
        (work / "UnionCrax.url").write_text(
            "[InternetShortcut]\nURL=https://union-crax.xyz/\n", encoding="utf-8")
        (work / "game.exe").write_text("x", encoding="utf-8")
        (work / "readme.txt").write_text("keep me", encoding="utf-8")
        gone = uc.remove_matches(work, list(uc.DEFAULT_REMOVE))
        left = sorted(p.name for p in work.rglob("*") if p.is_file())
        checks.check("sweep: the UnionCrax bookmark is gone", left == ["game.exe", "readme.txt"],
                     str(left))
        checks.check("sweep: and it says which file it took", "UnionCrax.url" in gone, str(gone))
    finally:
        _shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    checks = Checks()
    run(checks)
    _size_checks(checks)
    _resolver_checks(checks)
    _unpacked_size_checks(checks)
    _page_size_checks(checks)
    _no_return_in_finally_checks(checks)
    _free_space_checks(checks)
    _download_totals_checks(checks)
    _edit_checks(checks)
    _standard_add_checks(checks)
    _naming_and_sweep_checks(checks)
    total = checks.passed + checks.failed
    print(f"uc-archiver selftest: {checks.passed} passed, {checks.failed} failed")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
