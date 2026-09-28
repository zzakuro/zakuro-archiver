# uc-archiver

Fetch a file from a vikingfile.com share, change what is inside the archive, and
repack it with a `[Zakuro]` tag on the name.

Standalone on purpose. It shares no code with `zakuro-tool`, because it is a
different job with a different risk profile: this one downloads multi-gigabyte
archives from the internet and unpacks whatever is inside them.

## The flow

```
catalogue  ->  wait  ->  resolve  ->  download  ->  extract  ->  edit  ->  repack
```

| Step | What happens |
| --- | --- |
| **catalogue** | a `.json` listing entries, each with a title and a vikingfile link. One catalogue is one profile, so the remove/add lists are saved against it and reused |
| **wait** | `check-file` is polled until the host answers with a real name and byte count. A file still being prepared is waited out rather than failed on |
| **resolve** | the share page is driven by a real browser until its Cloudflare challenge clears, and the download link is read out of it. This is the step the API cannot do |
| **download** | resumable, with a progress line and a free-space check first — these run to 155 GB |
| **extract** | through WinRAR where it is installed, 7-Zip otherwise. Entries are checked for paths that climb out of the destination before anything is written |
| **edit** | remove patterns, add files |
| **repack** | with the `[Zakuro]` tag, then tested with `rar t` / `7z t` |

## Why there is a browser in here

`check-file` will tell you a file exists, what it is called and how big it is.
It will not tell you where to download it from. That link is on the share page
and it appears only after that page's own Cloudflare Turnstile resolves: the
widget calls `cloudflareCallback`, which POSTs `cf-turnstile-response=<token>`
to the page's own URL, gets `{"link": "..."}` back, and only then does
`#download-link` get an href.

So a plain HTTP GET of the page returns an anchor that is still `hidden` with
no `href` — which looks exactly like the challenge having failed. The page has
to be driven by a real browser and read *after* that POST lands; waiting for
network idle is what makes the difference.

`scrapling`'s `StealthySession` does the solving. Its adaptive parser is used
rather than a regex on purpose: the host has already moved domain once
(`vikingfile.com` → `vik1ngfile.site`) and the download path has changed shape
before.

## The output name

The host brands its archives with a trailing `UC` — `Hollow Knight - UC.7z`.
That is somebody else's mark on a file that is about to carry ours, so it comes
off and the tag goes on in its place:

| The host calls it | You get |
| --- | --- |
| `Hollow Knight - UC.7z` | `Hollow Knight [Zakuro].rar` |
| `Touhou Reiiden ... - UC.7z` | `Touhou Reiiden ... [Zakuro].rar` |
| `Hollow Knight [Zakuro].rar` | `Hollow Knight [Zakuro].rar` (not doubled) |
| `Lacuna.7z` | `Lacuna [Zakuro].rar` — a title ending in those letters is left alone |

The host's own filename is used, not the catalogue's title, because the host
describes the archive and the catalogue carries a build id:
`Hollow Knight (V1.5.12620)` in the catalogue, `Hollow Knight - UC.7z` on the
host. Only the last path component is ever read, so a host answering with a
full path cannot steer the name out of the output directory.

## The folder inside

Renaming the archive was only half of it. These archives wrap the game in a
folder named after whoever packed it, so unpacked, the release still carried
somebody else's mark — and that is the name people see first.

```
Touhou Luna Nights - UC/touhou_luna_nights.exe   ->  Touhou Luna Nights [Zakuro]/…
```

The folder inside now takes the same name as the archive around it. Narrow on
purpose:

- only when the archive unpacks to **one** top-level folder — several folders
  means the game's own layout, not a wrapper
- only when stripping `UC` actually changes the name, so a game whose own
  folder is called `Data` or `bin` is left alone
- never over the top of something that already has that name

It runs immediately after unpacking, before anything is added. That is the
only point where the game's folder is alone: a moment later `~Common Redist`
is beside it, there are two top-level directories, and a check for "exactly
one" would decline to touch either.

## What gets swept out

`.url` files go, always. They are browser bookmarks and the one in these
archives points at whoever packed it — `union-crax.xyz`:

```
- UnionCrax.url
```

It is not part of the game, it is an advert for them, and it has no business
shipping in a release carrying somebody else's name on it. It is a
`default_remove` like the standard additions are, so a profile can put it back
with `{"default_remove": []}`.

## What --remove and --add match

**`--remove` takes every match, at any depth.** One name, all of them:

```bash
python uc_archiver.py catalogue.json --pick 243 --remove Online
```

```
- Online/launcher.dat          (top level)
- bin/Online/                  (nested)
- a/b/c/Online/deep.bin        (deeply nested)
```

A pattern is matched against the path relative to the game folder, against
the bare name, and against each part of the path in turn, so all of these work
and none of them needs a wildcard:

| Written | Takes |
| --- | --- |
| `Online` | every `Online` folder or file, at any depth, and everything inside a matching folder |
| `*.dll` | every `.dll` at any depth |
| `Redist` | the `Redist` folder and its whole tree |
| `T98\` or `T98/` | the same, with a trailing separator |

Matching ignores case **on every platform**. That is deliberate: `fnmatch`
alone folds case through `os.path.normcase`, which lowercases on Windows and
does nothing on Linux, so a profile written on Windows would have removed
`online` and `ONLINE` there and only `Online` in the container — the same
release, two different answers.

**`--add` copies whole folders, and can put them anywhere inside the game.**

```bash
--add "D:/patches/steam_api64.dll"            # keeps its name, lands at the root
--add "bin/redist=D:/patches/redist"         # a folder, into bin/redist/
--add "bin\x64\steam_api64.dll=D:/p/..."   # backslashes work the same
```

A folder source is copied recursively, so pointing at a prepared `redist/`
brings the lot. Anything that would climb out of the game folder — `..`, a
leading `/`, a `C:` drive — is refused, because a target is a place inside
the tree and not anywhere on the disk.

Replacing works in both directions and says what it did: a file over a file
replaces it, a file over a folder replaces the folder, and a folder over
either replaces it. Adding a file onto a folder used to land *inside* it
(`steam_api64.dll/steam_api64.dll`) and leave the folder there, which is the
kind of thing that only shows up as a game that will not start.

## Do not trust the catalogue's sizes

It is out by a lot, and it is not a systematic relationship:

```
Touhou Rei'iden      catalogue   8 MB     host   3.1 MB   contents  73.7 MB
Among Us            catalogue  992 MB    host  593.1 MB
AColony              catalogue 2.47 GB   host  605.7 MB
Age of Empires       catalogue 12.05 GB  host   9.2 GB
```

The Touhou entry settles it: the catalogue claims 8 MB for a file holding
73.7 MB. So the figure is neither the packed size nor the unpacked one, and it
is wrong in both directions rather than consistently off by a ratio.

Every size check therefore uses what the **page** says, falling back to the
API, and never the catalogue. Two things follow from that:

- **The page rounds to three significant figures**, so its figure is
  approximate. Nothing compares against it exactly: a download is measured
  against the server's own `Content-Length` where there is one, and against
  the page with half a percent of slack where there is not. Comparing a
  finished download to a rounded number exactly reports complete files as
  short.
- **The archive is asked what it expands to** before anything is unpacked,
  because a 3.1 MB `.7z` in this catalogue held 73.7 MB, and the free-space
  check has to allow for the download, the unpacked tree and the finished
  archive all at once.

## Install

```bash
pip install -r requirements.txt
scrapling install          # the browser it drives; this is the big one
```

`scrapling install` is not optional. Without it the fetchers import but cannot
launch anything, and the failure shows up as a missing link rather than as a
missing browser.

## Use

```bash
# what is in the catalogue
python uc_archiver.py catalogue.json --list

# what does the host actually have, for a few entries
python uc_archiver.py catalogue.json --check

# the whole pipeline on one entry
python uc_archiver.py catalogue.json --pick 243

# by title, several at once
python uc_archiver.py catalogue.json --match "Touhou"

# rehearse it: resolve the link, download nothing
python uc_archiver.py catalogue.json --pick 243 --dry-run
```

Editing is driven by a profile, saved per catalogue:

```json
{
  "remove": ["Online"],
  "add": ["D:/patches/steam_api64.dll"]
}
```

**Every release also gets the standard set**, without needing a profile:

```
~Common Redist/     the runtime installers, as a folder
ReadME.txt          the readme
```

They are `default_add` on the profile rather than `add`, and are kept out of
the profile file's `add` list so they are the same on every run. Two reasons
they are separate:

- **The redist has to stay a folder.** It is added as a source folder, so it
  arrives as `~Common Redist/` with all ten installers in it and its own
  name, tilde and all — which is what the people receiving the release need,
  and what makes the `~` sort to the top of the folder rather than being
  buried among the game's own directories.
- **A missing one is a warning, not a failure.** These are absolute paths on
  one machine. If that Desktop folder moves, you get
  `standard file not there, skipped: ...` and the release is still built. An
  explicit `--add` still fails loudly, because that one you asked for by
  name.

Put a `default_add` in the profile to change or clear the set:

```json
{ "default_add": ["D:/my/redist"] }
```

```bash
python uc_archiver.py catalogue.json --remove "Online" --save-profile
python uc_archiver.py catalogue.json --pick 243 --show-profile
```

`--headed` shows the browser, which is the thing to reach for when a challenge
will not clear on its own — you can watch it waiting.

## Docker

```bash
docker build -t uc-archiver .
docker run --rm -v "$(pwd)/out:/work/out" uc-archiver catalogue.json --pick 243
```

Two things the image deliberately does not have:

**WinRAR / `rar`.** Creating a `.rar` needs the `rar` binary, which is
proprietary and not something to bake into an image. 7-Zip is installed and
will read the `.7z` shares and write `.7z` output. For `.rar` output, mount a
`rar` in:

```bash
docker run --rm -v /path/to/rar:/usr/local/bin/rar:ro ...
```

or bake one in with `--build-arg RAR_FROM=/path/to/rar`.

**Your browser.** scrapling drives its own patched Firefox, fetched at build
time. That is the browser that clears the challenge.

`/work` is a volume, because these archives do not belong in a container's
writable layer.

## Tests

```bash
python selftest.py
```

163 checks, none of which touch the network or need an archiver. The store is
faked at the urlopen boundary, the share page at the session boundary, and the
download at the same, so the parsing is tested — including the case where the
page gives no link, which has to be a clear error rather than a silently wrong
download.

Three of them exist because the bug they cover was invisible otherwise:

- the resolver is compiled with `SyntaxWarning` promoted to an error, because
  a `return` inside a `finally` runs perfectly well and only misbehaves once
  something has already gone wrong;
- the download checks assert that a complete file passes against an estimate
  that rounds **up**, which is the case an exact comparison fails;
- free space is checked for returning "unknown" rather than zero, since zero
  read as "no room" to one caller and "could not tell" to another;
- the edit checks perform a real add rather than a dry run, which is the only
  reason the one above was caught: an indentation slip had put the entire copy
  body inside the `if dry_run:` block, so every add reported the file it had
  placed, copied nothing, and the suite stayed green through it.

## What it will not do

It refuses, rather than trying to be clever:

- an entry that climbs out of the destination with `..`, or names an absolute
  path or a Windows drive-relative one like `C:evil`
- a symlink or device node inside an archive
- more files or more expanded bytes than the budget allows, checked as it goes
  rather than after the fact
- a download whose size does not match what the page said — the partial is kept
  so a resume can finish it, but nothing is unpacked
