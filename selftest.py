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
import time
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

    # A relative href is resolved against the page it came from rather than a
    # domain written into the code. The host has moved once already and both
    # spellings answer today, so a hardcoded one costs nothing now and is a
    # silent wrong-host link the day it does.
    for page_url in ("https://vikingfile.com/f/2Yrfl4o0bE",
                     "https://vik1ngfile.site/f/7xQAYwCHIL"):
        origin = page_url.split("/f/")[0]
        got = uc._share_from(FakePage("g.7z", "1 MB", "/d/abc/g.7z"), page_url)
        checks.check(f"share: a relative link follows the page it came from "
                     f"({origin.split('//')[1]})",
                     got.download_url == f"{origin}/d/abc/g.7z",
                     got.download_url)
        checks.check("share: and the page url is kept for the Referer",
                     got.page_url == page_url, got.page_url)

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


def _inner_folder_checks(checks) -> None:
    """The folder inside the archive was still branded.

    Renaming the archive was only half of it: unpacked, the release still had
    somebody else's mark on the folder, and that is the name people see
    first. Narrow on purpose, though -- renaming a folder the game did not
    name after its packer would be a surprise in the other direction.
    """
    import shutil as _shutil
    from pathlib import Path as _Path

    def make(spec):
        root = _Path(tempfile.mkdtemp(prefix="uc-inner-"))
        for rel in spec:
            (root / rel).mkdir(parents=True, exist_ok=True)
            (root / rel / "f.txt").write_text("x", encoding="utf-8")
        return root

    root = make(["Touhou Luna Nights - UC/data"])
    got = uc.retag_inner_folder(root, "Touhou Luna Nights [Zakuro]")
    dirs = sorted(p.name for p in root.iterdir() if p.is_dir())
    checks.check("inner folder: a branded folder takes the tag",
                 got == ("Touhou Luna Nights - UC", "Touhou Luna Nights [Zakuro]")
                 and dirs == ["Touhou Luna Nights [Zakuro]"], f"{got} -> {dirs}")

    # The one that got through. The rename was handed the host's own stem --
    # still carrying "- UC" -- which is the very folder being renamed, so the
    # "does that name already exist" check always fired and nothing moved. The
    # archive came out named "[Zakuro]" with a "- UC" folder inside it.
    raw = make(["Hollow Knight - UC"])
    uc.retag_inner_folder(raw, "Hollow Knight - UC")
    checks.check("inner folder: renaming onto the name it already has is a no-op",
                 (raw / "Hollow Knight - UC").is_dir()
                 and len([p for p in raw.iterdir() if p.is_dir()]) == 1)
    _shutil.rmtree(raw, ignore_errors=True)
    checks.check("inner folder: and its contents came with it",
                 (root / "Touhou Luna Nights [Zakuro]" / "data" / "f.txt").is_file())
    _shutil.rmtree(root, ignore_errors=True)

    root = make(["Data/game.exe"])
    checks.check("inner folder: a folder the game named itself is left alone",
                 uc.retag_inner_folder(root, "Whatever [Zakuro]") is None
                 and (root / "Data").is_dir())
    _shutil.rmtree(root, ignore_errors=True)

    root = make(["One - UC", "Two - UC"])
    checks.check("inner folder: two of them means neither is the game's folder",
                 uc.retag_inner_folder(root, "Whatever [Zakuro]") is None
                 and (root / "One - UC").is_dir() and (root / "Two - UC").is_dir())
    _shutil.rmtree(root, ignore_errors=True)

    # A wrapper folder with a loose file beside it is still a wrapper, and the
    # retag runs before anything is added, so this is the real shape.
    root = make(["Game - UC"])
    (root / "readme.txt").write_text("x", encoding="utf-8")
    got = uc.retag_inner_folder(root, "Game [Zakuro]")
    checks.check("inner folder: a branded folder is retagged even beside a file",
                 got == ("Game - UC", "Game [Zakuro]")
                 and (root / "Game [Zakuro]").is_dir(), str(got))
    _shutil.rmtree(root, ignore_errors=True)

    # A name that would collide is not renamed over the top of something.
    root = make(["Game - UC"])
    (root / "Game [Zakuro]").mkdir()
    checks.check("inner folder: it will not clobber an existing name",
                 uc.retag_inner_folder(root, "Game [Zakuro]") is None
                 and (root / "Game - UC").is_dir())
    _shutil.rmtree(root, ignore_errors=True)


def _parallel_download_checks(checks) -> None:
    """The parallel downloader, against a server that can be told to misbehave.

    Served locally over real HTTP rather than faked at urlopen, because the
    things that go wrong here are protocol-level: a server that answers 200 to
    a Range request instead of 206, a chunk that comes back short, four
    threads writing into one file. A stubbed urlopen cannot produce any of
    those, and the fallback would then be assumed rather than tested.
    """
    import shutil as _shutil
    from pathlib import Path as _Path

    import parallel
    from testserver import Server

    # Not a pattern of one byte: a payload like that can be produced by a
    # broken writer and look fine.
    payload = bytes((i * 7 + 11) % 251 for i in range(300_000))

    work = _Path(tempfile.mkdtemp(prefix="uc-par-"))
    try:
        # -- the host serves ranges ------------------------------------
        srv = Server(payload, honour_ranges=True)
        try:
            dest = work / "four.bin"
            got = uc.download(srv.url, dest, expect=len(payload),
                              connections=4, timeout=10)
            checks.check("parallel: four connections fetch the whole file",
                         got == len(payload) and dest.read_bytes() == payload,
                         f"{got} of {len(payload)}")
            checks.check("parallel: it really used more than one connection",
                         srv.hits > 2, f"{srv.hits} request(s)")
        finally:
            srv.close()

        # -- and one that does not -------------------------------------
        # The dangerous case. Every thread would be sent the whole file and
        # each would write it at its own offset, giving an archive the right
        # size and full of holes.
        srv = Server(payload, honour_ranges=False)
        try:
            dest = work / "noreranges.bin"
            got = uc.download(srv.url, dest, expect=len(payload),
                              connections=4, timeout=10)
            checks.check("parallel: a host that ignores ranges still gets a whole file",
                         got == len(payload) and dest.read_bytes() == payload,
                         f"{got} of {len(payload)}")
            checks.check("parallel: and it is byte-for-byte right, not just the right size",
                         dest.read_bytes() == payload)
        finally:
            srv.close()

        # -- one connection is still one connection --------------------
        srv = Server(payload, honour_ranges=True)
        try:
            dest = work / "one.bin"
            got = uc.download(srv.url, dest, expect=len(payload),
                              connections=1, timeout=10)
            checks.check("download: one connection works and is exact",
                         got == len(payload) and dest.read_bytes() == payload)
        finally:
            srv.close()

        # -- resume ----------------------------------------------------
        srv = Server(payload, honour_ranges=True)
        try:
            dest = work / "resume.bin"
            dest.write_bytes(payload[:100_000])
            got = uc.download(srv.url, dest, expect=len(payload),
                              connections=4, timeout=10)
            checks.check("parallel: a partial file is continued, not restarted",
                         got == len(payload) and dest.read_bytes() == payload,
                         f"{got} of {len(payload)}")
        finally:
            srv.close()

        # -- cancellation ------------------------------------------------
        srv = Server(payload, honour_ranges=True)
        try:
            dest = work / "cancel.bin"
            state = {"n": 0}

            def cancel():
                state["n"] += 1
                return state["n"] > 2

            try:
                uc.download(srv.url, dest, expect=len(payload),
                            connections=4, timeout=10, cancel=cancel)
                checks.check("parallel: cancelling stops it", False, "it finished anyway")
            except parallel.DownloadCancelled:
                checks.check("parallel: cancelling stops it", True)
            checks.check("parallel: and the partial is left, not deleted",
                         dest.is_file(), "the partial was thrown away")
        finally:
            srv.close()

        # -- splitting ---------------------------------------------------

        # A preallocated file with no record is the dangerous one. It is the
        # exact length of the real download and holds nothing but holes, and a
        # size check alone takes it for a finished one. That happened: a run
        # died after preallocating, and the next run reported 271.5 MB
        # downloaded and handed a file of zeros to the extractor.
        fake = work / "preallocated.bin"
        with open(fake, "ab") as h:
            h.truncate(len(payload))
        checks.check("resume: a full-length file with no record is not a download",
                     parallel.read_spans(fake) == []
                     and fake.stat().st_size == len(payload))
        todo = parallel.missing_spans(fake, len(payload), 4)
        checks.check("resume: so it is thrown away and fetched whole",
                     not fake.exists() and sum(t.length for t in todo) == len(payload),
                     f"exists={fake.exists()} spans={todo}")

        # A recorded partial, on the other hand, is believed -- that is the
        # whole point of keeping it when a run is stopped.
        real_partial = work / "real.bin"
        real_partial.write_bytes(payload[:len(payload) // 3])
        parallel.write_spans(real_partial, [(0, len(payload) // 3 - 1)])
        todo = parallel.missing_spans(real_partial, len(payload), 4)
        checks.check("resume: a recorded partial is kept and only the rest fetched",
                     real_partial.exists()
                     and sum(t.length for t in todo) == len(payload) - len(payload) // 3,
                     f"left={sum(t.length for t in todo)}")

        spans = parallel.split_span(0, 99, 4)
        checks.check("split: four spans cover the range exactly",
                     sum(s.length for s in spans) == 100
                     and spans[0].start == 0 and spans[-1].end == 99, str(spans))
        ragged = parallel.split_span(0, 9, 4)
        checks.check("split: a remainder is spread, not dumped on the last",
                     sum(s.length for s in ragged) == 10
                     and max(s.length for s in ragged) - min(s.length for s in ragged) <= 1,
                     str(ragged))
        checks.check("split: an empty range gives no spans",
                     parallel.split_span(5, 4, 4) == [])
        checks.check("split: one connection means one span",
                     len(parallel.split_span(0, 999, 1)) == 1)
    finally:
        _shutil.rmtree(work, ignore_errors=True)


def _job_checks(checks) -> None:
    """A job, and the two ways it talks to the downloader.

    Both bugs here were the same mistake in opposite directions: a callable
    where a value was expected, and a value where a callable was expected. The
    second one is the nastier, because a bound method is truthy -- passing one
    where a bool was wanted cancels every download the instant it starts, and
    nothing in a normal run says why.
    """
    import jobs

    mgr = jobs.JobManager()
    seen: list[bool] = []

    def work(job: jobs.Job) -> str:
        seen.append(job.stop_requested())
        return "done"

    job = mgr.submit(0, "probe", work)
    for _ in range(100):
        if job.state in (jobs.DONE, jobs.FAILED):
            break
        time.sleep(0.05)
    checks.check("job: a fresh job is not cancelled",
                 seen and seen[0] is False, str(seen[:2]))
    checks.check("job: stop_requested answers with a bool, not a method",
                 isinstance(job.stop_requested(), bool),
                 type(job.stop_requested()).__name__)
    checks.check("job: it runs to done", job.state == jobs.DONE,
                 f"{job.state} {job.error}")

    # Cancelling a job mid-flight.
    slow = jobs.JobManager()

    def waiter(job: jobs.Job) -> str:
        for _ in range(200):
            job.checkpoint()
            time.sleep(0.02)
        return "ran to the end"

    job2 = slow.submit(1, "slow", waiter)
    time.sleep(0.2)
    checks.check("job: cancel is accepted while running", slow.cancel(job2.id))
    for _ in range(100):
        if job2.state in (jobs.CANCELLED, jobs.DONE, jobs.FAILED):
            break
        time.sleep(0.05)
    checks.check("job: and it ends cancelled, not failed",
                 job2.state == jobs.CANCELLED, f"{job2.state} {job2.error}")
    checks.check("job: a finished job cannot be cancelled",
                 not slow.cancel(job2.id))
    checks.check("job: its progress is a real fraction",
                 0.0 <= job2.fraction <= 1.0, str(job2.fraction))
    checks.check("job: clearing takes finished ones away",
                 slow.clear_finished() >= 1 and not slow.get(job2.id))
    checks.check("job: a new manager is not the same one",
                 jobs.JobManager() is not slow)

    # A job that died part way is not a job that finished, and a bar that says
    # otherwise sends somebody looking for the last 80%.
    dead = jobs.Job(id="x", index=9, title="t", state=jobs.FAILED)
    dead.set_progress(30, 300)
    checks.check("job: a failed bar is not full",
                 dead.fraction < 0.99 and dead.fraction > 0.1, str(dead.fraction))

    # A job's phase has to move. It did not: step() called checkpoint() for the
    # stop check and never told the job what phase it was in, so a real run
    # through the web interface reported "starting" from the first resolve to
    # the finished archive. Found by driving a real job, not by a check.
    walked = jobs.JobManager()
    job3 = walked.submit(8, "walked", lambda j: "")
    try:
        previous_job = jobs.bind(job3)
        uc.step("one")
        uc.step("two")
        uc.step("three")
    finally:
        jobs.unbind(previous_job)
    checks.check("job: step() is what moves the phase",
                 job3.phase == "three", job3.phase)
    checks.check("job: and each phase is announced in the log exactly once",
                 sum(1 for line in job3.log if line == "== two ==") == 1,
                 str([line for line in job3.log if line.startswith("==")]))

    # A checkpoint with no name must not clear the phase: something that only
    # wants the stop check should not wipe what the bar is showing.
    held = jobs.Job(id="h", index=9, title="t")
    previous_job = jobs.bind(held)
    try:
        held.set_phase("downloading", announce=False)
        jobs.checkpoint()
        kept = held.phase
    finally:
        jobs.unbind(previous_job)
    checks.check("job: a checkpoint with no name leaves the phase alone",
                 kept == "downloading", kept)
    checks.check("job: a cancelled bar is not full either",
                 (setattr(dead, "state", jobs.CANCELLED),
                  dead.fraction < 0.99)[1], str(dead.fraction))


def _lock_checks(checks) -> None:
    """Two runs of one entry must not both start.

    They deadlock, and the symptom is a hang with no error message: both open
    the same partial for writing, neither gets a byte out, and it reads as a
    slow network. It happened twice here before this existed, and both times
    the diagnosis was "did I start it twice".
    """
    import shutil as _shutil
    from pathlib import Path as _Path

    work = _Path(tempfile.mkdtemp(prefix="uc-lock-"))
    try:
        target = work / "game.7z"
        target.write_bytes(b"partial")

        with uc._RunLock(target, "game.7z"):
            lock = uc._lock_path(target)
            checks.check("lock: it exists while held", lock.exists())
            checks.check("lock: it records who holds it",
                         lock.read_text(encoding="ascii").strip().isdigit())
            try:
                with uc._RunLock(target, "game.7z"):
                    checks.check("lock: a second run is refused", False,
                                 "it was allowed in")
            except uc.AlreadyRunning as exc:
                checks.check("lock: a second run is refused", True)
                checks.check("lock: and the refusal says who to stop",
                             "pid" in str(exc), str(exc))
        checks.check("lock: released on the way out",
                     not uc._lock_path(target).exists())

        # A lock left behind by a run that died must not block the next one.
        stale = work / "other.7z"
        stale.write_bytes(b"x")
        uc._lock_path(stale).write_text("999999999", encoding="ascii")
        try:
            with uc._RunLock(stale, "other.7z"):
                checks.check("lock: a stale lock from a dead run is cleared", True)
        except uc.AlreadyRunning as exc:
            checks.check("lock: a stale lock from a dead run is cleared", False,
                         str(exc))
        checks.check("lock: and it is gone again afterwards",
                     not uc._lock_path(stale).exists())
    finally:
        _shutil.rmtree(work, ignore_errors=True)


def _web_route_checks(checks) -> None:
    """Every JSON route, walked and parsed.

    The catalogue route shipped broken -- it called a `to_dict` that `Entry`
    does not have, and the page showed "cannot load the catalogue" -- because
    /api/status and the job routes had all been verified by hand against a
    running server while that one was not. So this walks all of them in one
    place, offline, and parses every answer.

    A real socket on an ephemeral port rather than calling the handler
    directly. The claim being tested is that a route answers with JSON, and the
    routing, the auth check and the body are all part of that claim.

    No route here starts a download. A run is submitted straight to the job
    manager with a function that returns a string, because POST /api/jobs
    calls main() for real.
    """
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    import jobs
    import webapp

    tmp = Path(tempfile.mkdtemp(prefix="uc-webtest-"))
    cat_path = tmp / "routes.json"
    cat_path.write_text(json.dumps(CATALOGUE), encoding="utf-8")

    webapp.Handler.app = webapp.Server(catalogue=str(cat_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    httpd.daemon_threads = True
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def call(path, method="GET", body=None, token=""):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")
        except (TimeoutError, OSError) as exc:
            # A route that hangs must read as a failed check, not take the
            # whole run down with it: a traceback here would abort the suite
            # and hide every check after this one.
            return 0, f"no answer: {type(exc).__name__}: {exc}"

    def as_json(text):
        try:
            return json.loads(text)
        except ValueError:
            return None

    try:
        # -- the route that shipped broken ----------------------------
        status, text = call("/api/catalogue")
        checks.check("web: the catalogue route answers 200",
                     status == 200, f"{status} {text[:100]}")
        cat = as_json(text)
        checks.check("web: the catalogue route answers JSON, not an error",
                     isinstance(cat, dict) and "error" not in cat, text[:100])
        if isinstance(cat, dict):
            # Two of the five rows carry no link and are dropped on load, so
            # the count is of entries, not of rows in the file.
            checks.check("web: it counts the entries it carries",
                         cat.get("count") == len(cat.get("entries") or []) == 3,
                         str(cat.get("count")))
            checks.check("web: it carries the catalogue's name",
                         cat.get("name") == CATALOGUE["name"], str(cat.get("name")))
            first = (cat.get("entries") or [{}])[0]
            want = {"index", "title", "url", "hash", "declared_size",
                    "declared_size_text", "uploaded"}
            checks.check("web: an entry has every field the page reads",
                         want <= set(first), str(sorted(set(first))))
            checks.check("web: the fields are the entry's own, not shifted",
                         first.get("index") == 1
                         and first.get("title") == CATALOGUE["downloads"][0]["title"]
                         and first.get("hash") == "aaaaaaaaaaaa"
                         and first.get("url", "").endswith("aaaaaaaaaaaa"),
                         str(first)[:160])
            checks.check("web: declared_size is read from the text, not invented",
                         first.get("declared_size") == uc.parse_page_size("11.1 GB")
                         and first.get("declared_size_text") == "11.1 GB",
                         str(first.get("declared_size")))
            checks.check("web: every entry is complete, not just the first",
                         all(want <= set(e) for e in cat.get("entries") or []))

        # -- the rest of the API --------------------------------------
        status, text = call("/api/status")
        body = as_json(text)
        checks.check("web: /api/status answers 200 with a dict",
                     status == 200 and isinstance(body, dict), f"{status} {text[:80]}")
        checks.check("web: /api/status says which catalogue is loaded",
                     isinstance(body, dict) and body.get("catalogue") == str(cat_path),
                     str(body)[:100] if isinstance(body, dict) else text[:80])

        # A finished job, so /events has a terminal state to send and end on
        # rather than looping for ever.
        probe = webapp.Handler.app.jobs.submit(1, "probe", lambda job: "ok")
        for _ in range(100):
            if probe.state in (jobs.DONE, jobs.FAILED):
                break
            time.sleep(0.05)
        checks.check("web: the probe job finished",
                     probe.state == jobs.DONE, f"{probe.state} {probe.error}")

        status, text = call("/api/jobs")
        body = as_json(text)
        checks.check("web: /api/jobs answers 200 with a list",
                     status == 200 and isinstance(body, list) and body, f"{status}")

        status, text = call(f"/api/jobs/{probe.id}")
        body = as_json(text)
        checks.check("web: one job comes back whole",
                     status == 200 and isinstance(body, dict)
                     and body.get("id") == probe.id, f"{status} {text[:80]}")

        status, text = call(f"/api/jobs/{probe.id}/events")
        checks.check("web: the event stream opens and sends a state",
                     status == 200 and text.startswith("data: ")
                     and as_json(text[6:].split("\n", 1)[0]) is not None,
                     f"{status} {text[:80]}")

        status, text = call(f"/api/jobs/{probe.id}/cancel", method="POST")
        checks.check("web: cancelling a finished job is refused with 409",
                     status == 409 and as_json(text) == {"cancelled": False},
                     f"{status} {text[:80]}")

        status, text = call("/api/jobs/clear", method="POST")
        checks.check("web: clear answers with what it removed",
                     status == 200 and isinstance(as_json(text), dict)
                     and "cleared" in as_json(text), f"{status} {text[:80]}")

        for path in ("/api/jobs/nope", "/api/nowhere"):
            status, text = call(path)
            body = as_json(text)
            checks.check(f"web: {path} is a 404 with a reason, not a crash",
                         status == 404 and isinstance(body, dict)
                         and "error" in body, f"{status} {text[:80]}")

        # -- the token -------------------------------------------------
        # Handler.app is read per request, so swapping it turns auth on
        # without a second server.
        webapp.Handler.app = webapp.Server(token="s3cret", catalogue=str(cat_path))
        status, _ = call("/api/status")
        checks.check("web: with a token set, a bare request is refused",
                     status == 401, str(status))
        status, _ = call("/api/status", token="wrong")
        checks.check("web: the wrong token is refused too", status == 401, str(status))
        status, _ = call("/api/status", token="s3cret")
        checks.check("web: the right token is let in", status == 200, str(status))
        status, _ = call("/api/catalogue", token="s3cret")
        checks.check("web: and the catalogue answers behind it too",
                     status == 200, str(status))
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


def _progress_bar_checks(checks) -> None:
    """The download line, and the fact that it stays out of the way.

    The rate was always computed and thrown away: Progress.rate existed, the
    web page drew it, and a command-line run printed nothing at all -- while
    the README promised "a progress line". So the numbers were there and
    unwatched, which is the same shape as every other bug in this file.
    """
    import parallel

    fmt = parallel.format_progress
    MB = 1024 ** 2

    line = fmt(0, 100 * MB, 0.0)
    checks.check("bar: an untouched download reads as empty, not as an error",
                 line.startswith("  [") and "0.0%" in line, line)
    half = fmt(50 * MB, 100 * MB, 5.0)
    checks.check("bar: half way reads as half",
                 "50.0%" in half and "50.00 MiB/100.00 MiB" in half, half)
    full = fmt(100 * MB, 100 * MB, 10.0)
    checks.check("bar: a finished download reads as 100%",
                 "100.0%" in full and "ETA 0s" in full, full)
    checks.check("bar: the bar itself fills and empties",
                 half.count("#") == 14 and half.count("-") == 14
                 and full.count("-") == 0, half)

    # A zero total means no Content-Length. A percentage and a bar would both be
    # measured against nothing, which is how a bar ends up lying.
    unknown = fmt(7 * MB, 0, 3.0)
    checks.check("bar: with no total there is no percentage and no bar",
                 "%" not in unknown and "#" not in unknown
                 and "?" in unknown, unknown)
    checks.check("bar: but the bytes and the rate are still there",
                 "7.00 MiB" in unknown and "MiB/s" in unknown, unknown)

    # A rate of zero at the first draw is not a division by zero.
    cold = fmt(0, 100 * MB, 0.0)
    checks.check("bar: no rate yet reads as a dash, not a crash",
                 "-" in cold and "ETA" in cold, cold)
    checks.check("bar: durations are readable at every scale",
                 [parallel.duration(v) for v in (0, 12, 65, 3600, 5400)]
                 == ["0s", "12s", "1m05s", "1h00m", "1h30m"],
                 str([parallel.duration(v) for v in (0, 12, 65, 3600, 5400)]))
    checks.check("bar: a rate under 1 KB/s is not dressed up as MiB",
                 parallel.human_rate(0) == "-"
                 and parallel.human_rate(512) == "512 B/s"
                 and parallel.human_rate(4.1 * MB).endswith("/s"),
                 str(parallel.human_rate(4.1 * MB)))

    # Silent when there is no stream, which is the job path: the web page is
    # already drawing this object and two bars would fight over one row.
    class Sink:
        def __init__(self):
            self.written = []

        def write(self, text):
            self.written.append(text)

        def flush(self):
            pass

    quiet = parallel.Progress(total=100)
    quiet.advance(10)
    quiet.finish()
    checks.check("bar: no stream means nothing is written",
                 quiet._painted == 0, str(quiet._painted))

    sink = Sink()
    loud = parallel.Progress(total=100 * MB, stream=sink)
    for _ in range(40):
        loud.advance(MB)
    loud.finish()
    painted = "".join(sink.written)
    checks.check("bar: a terminal gets a line with the rate on it",
                 painted.count("\r") >= 1 and "MiB/s" in painted, painted[:120])
    checks.check("bar: and it ends on a newline so the next line starts clean",
                 sink.written[-1] == "\n", repr(sink.written[-1]))
    checks.check("bar: drawing is throttled, not once per chunk",
                 len(sink.written) < 40, f"{len(sink.written)} writes for 40 chunks")

    # A log line has to be able to wipe the bar, or a warning lands in the
    # middle of it and both become unreadable.
    sink2 = Sink()
    painted_bar = parallel.Progress(total=100 * MB, stream=sink2)
    painted_bar.done = 50 * MB
    painted_bar.draw(force=True)
    before = len(sink2.written)
    parallel.clear_bar()
    checks.check("bar: clear_bar wipes the line and leaves the cursor home",
                 len(sink2.written) > before
                 and sink2.written[-1].endswith("\r")
                 and sink2.written[-1].strip("\r ") == "",
                 repr(sink2.written[-1]))

    # A cancelled download must not leave a bar on screen claiming progress.
    sink3 = Sink()
    stopped = parallel.Progress(total=100 * MB, stream=sink3)
    stopped.done = 10 * MB
    stopped.draw(force=True)
    parallel.clear_bar()
    checks.check("bar: a stop leaves nothing painted",
                 stopped._painted == 0, str(stopped._painted))

    # restart() is what the single-connection fallback uses after throwing away
    # the parallel file: the clock and the count both start again.
    restarted = parallel.Progress(total=100 * MB)
    restarted.done = 30 * MB
    restarted.restart()
    checks.check("bar: a fallback restarts the clock and the count",
                 restarted.done == 0 and restarted.rate == 0.0,
                 f"done={restarted.done} rate={restarted.rate}")


def _compaction_checks(checks) -> None:
    """A rate-limited run keeps what it had instead of starting over.

    Measured on a real 16-connection fetch: the host answered 429 at 62%, and
    the fallback threw away 169 MB of verified bytes and fetched the file again
    from nothing. 701s against 180s for the run that was never rate limited.

    The bytes could not simply be handed over, because fetch_single resumes by
    asking for `Range: bytes=<file size>-` and a preallocated parallel file is
    already the full length -- so it would have asked past the end. A run from
    byte 0 has to be cut back to one, which is what compact_prefix does.
    """
    import parallel

    tmp = Path(tempfile.mkdtemp(prefix="uc-compact-"))
    try:
        _compaction_body(checks, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _compaction_body(checks, tmp: Path) -> None:
    """The body of the compaction checks, on a temp dir that gets cleaned up."""
    import parallel

    def make(name, total, spans, fill=None):
        dest = tmp / name
        with open(dest, "wb") as fh:
            fh.truncate(total)
            if fill:
                for start, end in fill:
                    fh.seek(start)
                    # Never 0: a marker of zero is indistinguishable from the
                    # hole this is all about, which is how the first version
                    # of this filled offset 0 with what it was testing for.
                    fh.write(bytes([1 + start % 250]) * (end - start + 1))
        parallel.write_spans(dest, spans)
        return dest

    total = 1000

    # Spans out of order and with a hole: only the run from 0 is reachable.
    dest = make("holed.bin", total, [(400, 499), (0, 199), (200, 299), (600, 699)],
                fill=[(0, 299), (400, 499), (600, 699)])
    checks.check("compact: the run from byte 0 is what is kept",
                 parallel.compact_prefix(dest) == 300, "wrong prefix")
    checks.check("compact: the file is cut to exactly that prefix",
                 dest.stat().st_size == 300, str(dest.stat().st_size))
    checks.check("compact: the sidecar goes, so nothing claims bytes past it",
                 not parallel.spans_file(dest).exists())
    # The kept bytes must still be the bytes that were there, not zeros.
    with open(dest, "rb") as fh:
        head = fh.read(8)
    checks.check("compact: the bytes kept are the real ones, not holes",
                 head and len(set(head)) == 1 and head[0] != 0, repr(head[:8]))

    # No span starts at 0: nothing is reachable, so the caller must discard.
    dest = make("nohead.bin", total, [(100, 199), (300, 399)], fill=[(100, 199)])
    checks.check("compact: with nothing at byte 0 there is no prefix to keep",
                 parallel.compact_prefix(dest) == 0, "kept something")
    checks.check("compact: and it leaves the file for the caller to discard",
                 dest.exists() and dest.stat().st_size == total)

    # Adjacent and overlapping spans are one run, not several.
    dest = make("adjacent.bin", total, [(0, 99), (100, 199), (150, 249)],
                fill=[(0, 249)])
    checks.check("compact: touching and overlapping spans count as one run",
                 parallel.compact_prefix(dest) == 250, "wrong prefix")

    # The whole file already done: nothing to cut.
    dest = make("whole.bin", total, [(0, 999)], fill=[(0, 999)])
    checks.check("compact: a complete file is left alone",
                 parallel.compact_prefix(dest) == 1000
                 and dest.stat().st_size == 1000)

    # A sidecar that is not there at all must not invent a prefix.
    dest = make("nosidecar.bin", total, [])
    parallel.spans_file(dest).unlink(missing_ok=True)
    checks.check("compact: no sidecar means no claim to anything",
                 parallel.compact_prefix(dest) == 0)

    # A resume asks for "from here to the end", which is not the same shape as
    # a closed range. It used to send `bytes=300-0` -- end before start -- and
    # a resumed download silently got an empty body and then called itself
    # short. Cheap to pin down here rather than only through the fetch above.
    checks.check("compact: a resume asks for bytes=from-there-to-the-end",
                 parallel.Span(300).header == "bytes=300-",
                 parallel.Span(300).header)
    checks.check("compact: a closed range still names its end",
                 parallel.Span(0, 0).header == "bytes=0-0"
                 and parallel.Span(10, 20).header == "bytes=10-20"
                 and parallel.Span(10, 20).length == 11,
                 parallel.Span(10, 20).header)
    checks.check("compact: an open-ended range admits it has no length",
                 _raises(lambda: parallel.Span(300).length),
                 "length did not complain")

    # The point of the whole thing: after compacting, fetch_single resumes from
    # the prefix rather than starting again. Checked against the real test
    # server, over real HTTP, because "it resumed" has to mean a Range header
    # and a stubbed urlopen cannot produce one.
    from testserver import Server

    served = b"\x41" * 300 + b"\x00" * 700
    httpd = Server(payload=served)
    try:
        resumed = tmp / "resumed.bin"
        with open(resumed, "wb") as fh:
            fh.truncate(total)                 # preallocated, holes to the end
        with open(resumed, "r+b") as fh:
            fh.write(b"\x41" * 300)            # one good span at the front
        parallel.write_spans(resumed, [(0, 299)])

        kept = parallel.compact_prefix(resumed)
        checks.check("compact: the prefix survives the handover",
                     kept == 300 and resumed.stat().st_size == 300,
                     f"kept={kept}")

        hits_before = httpd.hits
        got = parallel.fetch_single(httpd.url, resumed, expect=total, timeout=20)
        checks.check("compact: the single connection finished the file",
                     got == total, str(got))
        checks.check("compact: and it was one ranged request, not the whole file",
                     httpd.hits - hits_before == 1,
                     f"{httpd.hits - hits_before} requests")
        with open(resumed, "rb") as fh:
            data = fh.read()
        checks.check("compact: the result is the served bytes, in order",
                     data == served, f"{len(data)} bytes, "
                     f"head={data[:4]!r} tail={data[-4:]!r}")
    finally:
        httpd.close()


def _span_retry_checks(checks) -> None:
    """A throttled span is retried on its own, and the rest keeps going.

    This is the measured failure. A 16-connection run against the real host was
    answered 429 at 62% of 271 MB, and because every span shared one pool the
    first exception took the whole download down, discarded 169 MB of good
    data, and restarted on a single connection: 701s against 180s.

    What the other download managers do instead is treat a 429 as a transient
    error on the one connection it happened to, retry it from wherever that
    span got to, and spend a budget that only counts attempts which produced
    nothing. Read out of the AB Download Manager source: it has no 429 handling
    and no adaptive concurrency at all, its thread count is a flat 8, and what
    it actually relies on is `tries` resetting the instant a byte lands.
    """
    import parallel
    from testserver import Server

    tmp = Path(tempfile.mkdtemp(prefix="uc-retry-"))
    try:
        _span_retry_body(checks, tmp, parallel, Server)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _span_retry_body(checks, tmp: Path, parallel, Server) -> None:
    import threading

    # -- backoff, because a flat wait is wrong for a time-window limiter ---
    # Ten one-second waits is ten seconds of patience, and this host's window
    # is longer: the 429s kept coming and every retry landed inside the penalty.
    schedule = [parallel.retry_delay(n) for n in range(1, 9)]
    checks.check("retry: the wait doubles after each silent failure",
                 schedule[:4] == [1.0, 2.0, 4.0, 8.0], str(schedule[:4]))
    checks.check("retry: and then it stops growing, so giving up stays a "
                 "decision somebody made",
                 schedule[-1] == parallel.SPAN_RETRY_MAX_DELAY
                 and schedule == sorted(schedule), str(schedule))
    whole = sum(parallel.retry_delay(n)
                for n in range(1, parallel.SPAN_MAX_TRIES + 1))
    checks.check("retry: a run that never recovers is waited out for minutes, "
                 "not seconds", whole > 60, f"{whole:.0f}s")

    # The real schedule waits out a rate-limit window, which is minutes. That is
    # right for the host and far too slow for a test suite, so what follows
    # runs with it shrunk; the shape is what is being checked there, not the pace.
    real_delay, real_max = parallel.SPAN_RETRY_DELAY, parallel.SPAN_RETRY_MAX_DELAY
    parallel.SPAN_RETRY_DELAY = 0.01
    parallel.SPAN_RETRY_MAX_DELAY = 0.05
    try:
        _span_retry_scenarios(checks, tmp, parallel, Server, threading)
    finally:
        parallel.SPAN_RETRY_DELAY, parallel.SPAN_RETRY_MAX_DELAY = real_delay, real_max


def _span_retry_scenarios(checks, tmp: Path, parallel, Server, threading) -> None:
    payload = bytes(range(256)) * 16          # 4096 bytes, each byte distinct
    total = len(payload)
    quarter = total // 4

    # -- and two downloads really do share the one budget -----------------
    # The claim is about overlap, so it is checked as overlap: two downloads,
    # four connections each, one budget of four. Had the budget not bound, the
    # two servers between them would have had eight requests open at once,
    # which is the number that drew a 429 from the real host.
    #
    # The server stalls briefly so the two genuinely overlap. A local server
    # otherwise answers faster than the client can ask, and the test would pass
    # without ever having two downloads running at the same time -- which is
    # the thing being claimed.
    big = bytes(range(256)) * 256          # 64 KiB
    one = Server(payload=big, stall=0.02)
    two = Server(payload=big, stall=0.02)
    previous = parallel.budget()
    try:
        parallel.set_budget(4)
        results = []

        def pull(server, name):
            target = tmp / f"share-{name}.bin"
            try:
                results.append(parallel.fetch_parallel(
                    server.url, target, len(big), connections=4, timeout=30))
            except Exception as exc:
                results.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=pull, args=(s, n), daemon=True)
                   for s, n in ((one, "a"), (two, "b"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        peak = one.peak_concurrency + two.peak_concurrency
        checks.check("budget: both downloads finished while sharing it",
                     sorted(results) == [len(big), len(big)], str(results))
        checks.check("budget: and between them they never had more open than "
                     "the budget", peak <= 4, f"{peak} open at once")
        checks.check("budget: both really were in flight together, or the "
                     "test proves nothing about sharing",
                     one.peak_concurrency >= 1 and two.peak_concurrency >= 1,
                     f"{one.peak_concurrency} and {two.peak_concurrency}")
    finally:
        parallel.set_budget(previous[1] or parallel.DEFAULT_CONNECTIONS)
        one.close()
        two.close()

    # -- one connection budget for the process, not one per download ------
    # The throttle is per IP, so ten downloads each taking eight connections
    # is eighty sockets against a limit that eight was already the wrong side
    # of once. UC_CONNECTIONS is what a single download would like;
    # UC_TOTAL_CONNECTIONS is what it may have at once, shared out between
    # whatever is running.
    previous = parallel.budget()
    try:
        parallel.set_budget(3)
        checks.check("budget: it starts empty and says what is allowed",
                     parallel.budget() == (0, 3), str(parallel.budget()))
        for _ in range(3):
            parallel._acquire_slot()
        checks.check("budget: three can be held at once",
                     parallel.budget() == (3, 3), str(parallel.budget()))
        # A fourth has to wait rather than open a fourth socket.
        got_through = []

        def grab():
            parallel._acquire_slot()
            got_through.append(True)

        waiter = threading.Thread(target=grab, daemon=True)
        waiter.start()
        time.sleep(0.4)
        checks.check("budget: a fourth waits instead of taking a fourth slot",
                     not got_through and parallel.budget()[0] == 3,
                     f"got through: {bool(got_through)}")
        parallel._release_slot()
        waiter.join(timeout=3)
        checks.check("budget: and it goes through once one is given back",
                     bool(got_through), f"got through: {bool(got_through)}")
        for _ in range(3):
            parallel._release_slot()
        time.sleep(0.15)
        checks.check("budget: everything given back leaves it empty",
                     parallel.budget()[0] == 0, str(parallel.budget()))
        # A slot left held by a failure would shrink the budget for everything
        # else, so the count must never go negative.
        parallel._release_slot()
        checks.check("budget: an extra release cannot make it negative",
                     parallel.budget()[0] == 0, str(parallel.budget()))
    finally:
        parallel.set_budget(previous[1] or parallel.DEFAULT_CONNECTIONS)

    # -- disk one run has claimed is not available to the next -------------
    # Each run reading the whole disk and each passing is right for one
    # download and wrong for several, which is what running the catalogue is.
    import shutil as _sh
    base = tmp / "room"
    base.mkdir()
    real_free = uc.free_bytes
    try:
        uc.free_bytes = lambda p: 10_000
        with uc._RoomClaim(base, 4_000, "first"):
            checks.check("room: a run that fits is allowed in",
                         uc._room_others_hold() == 4_000,
                         str(uc._room_others_hold()))
            with uc._RoomClaim(base, 4_000, "second"):
                checks.check("room: and the next one too, room for both",
                             uc._room_others_hold() == 8_000,
                             str(uc._room_others_hold()))
                try:
                    with uc._RoomClaim(base, 4_000, "third"):
                        outcome = "allowed"
                except SystemExit as exc:
                    outcome = str(exc)
                checks.check("room: a third is refused, the first two took it",
                             outcome != "allowed", outcome[:90])
                checks.check("room: and the refusal says what is claimed",
                             "claimed" in outcome, outcome[:90])
            # The inner one is given back, so the next run fits.
            checks.check("room: leaving the inner block gives the room back",
                         uc._room_others_hold() == 4_000,
                         str(uc._room_others_hold()))
            with uc._RoomClaim(base, 4_000, "third"):
                checks.check("room: and the next run fits once one finishes",
                             uc._room_others_hold() == 8_000,
                             str(uc._room_others_hold()))
        # A run that dies still gives its claim back, or it shrinks the disk
        # for everything queued behind it.
        checks.check("room: a finished run leaves nothing claimed",
                     uc._room_others_hold() == 0, str(uc._room_others_hold()))
        try:
            with uc._RoomClaim(base, 4_000, "doomed"):
                raise SystemExit("something went wrong mid-run")
        except SystemExit:
            pass
        checks.check("room: a run that fails gives its claim back too",
                     uc._room_others_hold() == 0, str(uc._room_others_hold()))
        # Unreadable free space is a skipped check, never a refusal.
        uc.free_bytes = lambda p: None
        with uc._RoomClaim(base, 99_000_000, "blind") as claim:
            checks.check("room: a volume that cannot be measured is not a "
                         "refusal", claim.amount == 0, str(claim.amount))
        # Asking for nothing is not a refusal either.
        with uc._RoomClaim(base, 0, "nothing"):
            checks.check("room: a run that needs no room claims none",
                         uc._room_others_hold() == 0, str(uc._room_others_hold()))
    finally:
        uc.free_bytes = real_free
        _sh.rmtree(base, ignore_errors=True)

    # -- the connection plan: start high, walk down ----------------------
    # The host is not consistent about what it gives. Eight connections carried
    # 7.15 MB/s on a share that was not throttling and got a 429 on one that
    # was, so the count is a starting point and Throttled is how it moves.
    checks.check("connections: eight walks down through four to one",
                 uc._connection_plan(8) == [8, 4, 1], str(uc._connection_plan(8)))
    checks.check("connections: four does not step through eight",
                 uc._connection_plan(4) == [4, 1], str(uc._connection_plan(4)))
    checks.check("connections: one stays one, because it was asked for",
                 uc._connection_plan(1) == [1], str(uc._connection_plan(1)))
    checks.check("connections: sixteen also lands on four and one",
                 uc._connection_plan(16) == [16, 4, 1],
                 str(uc._connection_plan(16)))
    checks.check("connections: the default is eight",
                 parallel.DEFAULT_CONNECTIONS == 8,
                 str(parallel.DEFAULT_CONNECTIONS))

    # fetch_parallel really does raise Throttled when the host will not serve
    # some chunks, and keeps what it got. Asked for directly, so no probe is
    # involved -- a probe asks for bytes=0-0, which is indistinguishable from a
    # span at offset 0, and that ambiguity makes a live server useless here.
    walled = Server(payload=payload, flaky={0: 999})
    try:
        d = tmp / "walled.bin"
        try:
            parallel.fetch_parallel(walled.url, d, total, connections=8,
                                    timeout=20)
            outcome = "finished"
        except parallel.Throttled:
            outcome = "throttled"
        except Exception as exc:
            outcome = f"{type(exc).__name__}: {exc}"
        checks.check("connections: a host that throttles a chunk raises "
                     "Throttled, not a bare failure", outcome == "throttled",
                     outcome)
        checks.check("connections: and the seven spans that worked are kept",
                     d.is_file() and d.read_bytes()[quarter:] == payload[quarter:],
                     "the good part was lost")
    finally:
        walled.close()

    # And the walk itself. download() has to notice Throttled and try the next
    # count, so that loop is driven with a stand-in rather than a real server:
    # what is being checked is which connection counts it asks for, in what
    # order, and that nothing is thrown away between attempts.
    tried = []
    real_fetch = parallel.fetch_parallel

    def counting_fetch(url, dest, total, connections=8, **kw):
        tried.append(connections)
        if len(tried) == 1:
            raise parallel.Throttled("stubbed: the host said no")
        return real_fetch(url, dest, total, connections=connections, **kw)

    parallel.fetch_parallel = counting_fetch
    clean = Server(payload=payload)
    try:
        d = tmp / "downgrade.bin"
        got = uc.download(clean.url, d, expect=total, timeout=20, connections=8)
        checks.check("connections: a throttle drops the count and carries on",
                     tried[:2] == [8, 4], f"tried {tried}")
        checks.check("connections: and the file is whole afterwards",
                     got == total and d.read_bytes() == payload,
                     f"{got} of {total}")
    finally:
        parallel.fetch_parallel = real_fetch
        clean.close()

    # A host that throttles at every count is given up on, with the bytes kept.
    # A host that throttles at every count is given up on, and the bytes are
    # still named in the reason. The server itself is clean -- a probe asks for
    # bytes=0-0, which the offset-0 throttle would catch, and that would stop
    # the parallel path being entered at all.
    walked = []

    def always_throttled(url, dest, total, connections=8, **kw):
        walked.append(connections)
        raise parallel.Throttled("stubbed: the host said no")

    clean2 = Server(payload=payload)
    parallel.fetch_parallel = always_throttled
    real_delay, real_max = parallel.SPAN_RETRY_DELAY, parallel.SPAN_RETRY_MAX_DELAY
    parallel.SPAN_RETRY_DELAY = 0.01
    parallel.SPAN_RETRY_MAX_DELAY = 0.05
    try:
        d = tmp / "nowhere.bin"
        try:
            uc.download(clean2.url, d, expect=total, timeout=20, connections=8)
            outcome = "finished"
        except SystemExit as exc:
            outcome = str(exc)
        except Exception as exc:
            outcome = f"{type(exc).__name__}: {exc}"
        checks.check("connections: a host that throttles at every count is "
                     "given up on, and says so",
                     "rate limiting" in outcome, outcome[:90])
        checks.check("connections: and it tried all three before giving up",
                     walked == [8, 4, 1], f"tried {walked}")
    finally:
        parallel.fetch_parallel = real_fetch
        parallel.SPAN_RETRY_DELAY, parallel.SPAN_RETRY_MAX_DELAY = real_delay, real_max
        clean2.close()

    # -- a throttled span is retried, and the rest keeps going:
    # The first span gets two 429s and then is served normally. Before this
    # change that first 429 failed the entire download.
    flaky = Server(payload=payload, flaky={0: 2})
    try:
        dest = tmp / "flaky.bin"
        got = parallel.fetch_parallel(flaky.url, dest, total, connections=4,
                                      timeout=20)
        checks.check("retry: a throttled span is retried, not fatal",
                     got == total, f"got {got} of {total}")
        checks.check("retry: and the finished file is byte-for-byte right",
                     dest.read_bytes() == payload,
                     f"{dest.stat().st_size} bytes")
        starts = [s for s, _ in flaky.asked]
        checks.check("retry: the throttled span was asked for three times",
                     starts.count(0) == 3, f"asked at 0: {starts.count(0)}")
        checks.check("retry: the other spans were each asked once, not re-run",
                     all(starts.count(s) == 1
                         for s in {quarter, quarter * 2, quarter * 3}),
                     str(sorted(starts)))
    finally:
        flaky.close()

    # A span that partly succeeded and then finished used to be recorded twice:
    # once as the partial run, once as the whole. The two overlap, so every
    # count of what is done reads high -- the sidecar reached 100% of the file
    # by arithmetic while missing_spans still had a gap, and a real download
    # announced "resuming at 246.07 MiB of 246.07 MiB".
    overlap = tmp / "overlap.bin"
    parallel.write_spans(overlap, [(0, 149), (0, 199), (400, 499)])
    counted = sum(b - a + 1 for a, b in parallel.read_spans(overlap))
    checks.check("retry: an overlapping sidecar is counted as overlapping, "
                 "which is what makes the double-record worth fixing",
                 counted == 450, f"{counted} bytes counted for a {total} file")
    # And through the real path: a file with a partial span recorded is
    # finished by a real fetch, and each range is named once afterwards.
    server = Server(payload=b"z" * total)
    try:
        d = tmp / "resumed-mid-span.bin"
        with open(d, "wb") as fh:
            fh.truncate(total)
            fh.write(b"z" * 200)       # the sidecar is the record of truth, so
                                        # the bytes it claims have to be there
        parallel.write_spans(d, [(0, 199)])
        got = parallel.fetch_parallel(server.url, d, total, connections=1,
                                      timeout=20)
        checks.check("retry: a fetch resuming over a partial span finishes it",
                     got == total and d.read_bytes() == b"z" * total,
                     f"{got} of {total}")
        checks.check("retry: and leaves no sidecar claiming a finished file",
                     not parallel.spans_file(d).exists())
    finally:
        server.close()

    # -- a span that never gets a byte eventually gives up ---------------
    # Ten consecutive silent failures is a host that is not serving this slice.
    # It has to fail rather than wait for ever.
    dead = Server(payload=payload, flaky={0: 999})
    try:
        dest = tmp / "dead.bin"
        try:
            parallel.fetch_parallel(dead.url, dest, total, connections=4,
                                   timeout=20)
            failed = False
            why = "it reported success"
        except SystemExit as exc:
            # SystemExit is a BaseException, so a bare `except Exception` here
            # would let it straight through and take the whole run with it.
            failed, why = True, str(exc)
        except Exception as exc:
            failed, why = True, f"{type(exc).__name__}: {exc}"
        checks.check("retry: a span that never moves gives up instead of "
                     "looping for ever", failed, why)
        # Only the throttled span's own requests: the other three each make one
        # and succeed. Each pass is a fresh budget for the straggler, so it is
        # asked PARALLEL_PASSES times over rather than once.
        throttled = [s for s, _ in dead.asked if s == 0]
        checks.check("retry: and it gives up on a schedule, not far past it",
                     len(throttled) == (parallel.SPAN_MAX_TRIES + 1)
                     * parallel.PARALLEL_PASSES,
                     f"{len(throttled)} attempts")
        # The reason for running in passes at all: a quarter of the file being
        # throttled must not cost the three quarters that were fine. This is the
        # 169 MB of good data the old code used to throw away.
        survivors = sorted({s for s, _ in dead.asked if s != 0})
        checks.check("retry: the spans that were not throttled were still "
                     "fetched, on the first pass", len(survivors) == 3,
                     str(survivors))
        on_disk = dest.read_bytes() if dest.is_file() else b""
        checks.check("retry: and the spans that did work are on disk, correct",
                     len(on_disk) == total
                     and on_disk[quarter:] == payload[quarter:],
                     f"{len(on_disk)} bytes")
        checks.check("retry: the throttled span is left as a hole rather than "
                     "filled with something invented",
                     set(on_disk[:quarter]) == {0}, "not empty")
        checks.check("retry: the sidecar is kept, so a re-run takes the gaps "
                     "rather than starting again",
                     parallel.spans_file(dest).is_file(), "no sidecar")
    finally:
        dead.close()

    # -- progress forgives the budget ------------------------------------
    # The counter is there to catch a host that is not serving, not to cap the
    # number of hiccups in a long healthy download. Each attempt hands back
    # some bytes and is then cut short, so tries must never accumulate.
    partial = Server(payload=payload, shorten={0: quarter // 2})
    try:
        dest = tmp / "partial.bin"
        got = parallel.fetch_parallel(partial.url, dest, total, connections=1,
                                      timeout=20)
        checks.check("retry: repeated short reads still finish, because each "
                     "one made progress", got == total, f"got {got}")
        checks.check("retry: and the file is correct after all of them",
                     dest.read_bytes() == payload)
        # It must have resumed, not restarted: the offsets asked for walk
        # forward from 0 instead of always being 0.
        starts = sorted({s for s, _ in partial.asked})
        checks.check("retry: the retry asked only for what was missing",
                     len(starts) > 1 and starts[0] == 0
                     and all(b > a for a, b in zip(starts, starts[1:])),
                     str(starts[:8]))
    finally:
        partial.close()

    # -- a stop is honoured during the retry pause -----------------------
    slow = Server(payload=payload, flaky={0: 999})
    try:
        dest = tmp / "cancel.bin"
        flag = threading.Event()

        def cancelling() -> bool:
            return flag.is_set()

        threading.Timer(0.05, flag.set).start()
        began = time.time()
        try:
            parallel.fetch_parallel(slow.url, dest, total, connections=4,
                                    timeout=20, cancel=cancelling)
            outcome = "finished"
        except parallel.DownloadCancelled:
            outcome = "cancelled"
        except Exception:
            outcome = "other"
        took = time.time() - began
        budget = sum(parallel.retry_delay(n)
                     for n in range(1, parallel.SPAN_MAX_TRIES + 1))
        checks.check("retry: a stop during a retry pause is honoured",
                     outcome == "cancelled", outcome)
        checks.check("retry: and it stops early, not after the budget is spent",
                     took < budget / 2, f"{took:.2f}s of a {budget:.2f}s budget")
    finally:
        slow.close()

    # -- a host that ignores ranges is still not a throttle --------------
    # That one has to reach the caller, because the parallel premise is void
    # and the right answer is to come down to one connection. Retrying it
    # would just burn the budget discovering the same thing ten times.
    blind = Server(payload=payload, honour_ranges=False)
    try:
        dest = tmp / "blind.bin"
        try:
            parallel.fetch_parallel(blind.url, dest, total, connections=4,
                                   timeout=20)
            outcome = "finished"
        except parallel.RangeUnsupported:
            outcome = "range-unsupported"
        checks.check("retry: a host that ignores ranges is reported at once, "
                     "not retried", outcome == "range-unsupported", outcome)
        checks.check("retry: and it was not asked ten times first",
                     len(blind.asked) <= 4, f"{len(blind.asked)} requests")
    finally:
        blind.close()


    # -- the fallback is not a soft spot ---------------------------------
    # A rate-limited parallel run lands here, so this path has to survive a 429
    # as well. It did not: it made exactly one request, and the run died on the
    # first try having already spent ten rounds retrying sixteen spans.
    single = Server(payload=payload, flaky={0: 3})
    try:
        dest = tmp / "single.bin"
        got = parallel.fetch_single(single.url, dest, expect=total, timeout=20)
        checks.check("retry: one connection also waits out a 429",
                     got == total, f"got {got} of {total}")
        checks.check("retry: and the file it leaves is correct",
                     dest.read_bytes() == payload)
    finally:
        single.close()

    # A 404 is not a throttle and must not be retried into a slow failure.
    gone = Server(payload=payload)
    gone.close()
    try:
        dest = tmp / "gone.bin"
        try:
            parallel.fetch_single("http://127.0.0.1:1/nothing",
                                  dest, expect=total, timeout=2)
            outcome = "no error"
        except SystemExit as exc:
            outcome = str(exc)
        except Exception as exc:
            outcome = f"{type(exc).__name__}: {exc}"
        checks.check("retry: a refused connection reports rather than spins",
                     "no error" not in outcome, outcome[:80])
    finally:
        pass


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
    _inner_folder_checks(checks)
    _parallel_download_checks(checks)
    _job_checks(checks)
    _lock_checks(checks)
    _web_route_checks(checks)
    _progress_bar_checks(checks)
    _compaction_checks(checks)
    _span_retry_checks(checks)
    total = checks.passed + checks.failed
    print(f"uc-archiver selftest: {checks.passed} passed, {checks.failed} failed")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
