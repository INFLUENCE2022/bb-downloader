# bb-downloader

Bulk-download every course on your BlackBoard account, archived by academic term and
mirrored into the same folder hierarchy BlackBoard uses.

Built for BlackBoard Learn **Classic / Original** (not Ultra). Reuses an already
logged-in browser session, so it works with SSO / ADFS logins and needs no API keys.

> 中文说明见 [README.zh-CN.md](README.zh-CN.md)。

## What it does

- Enumerates **all** courses visible to your account, across all terms
- Walks each course's full content tree via BlackBoard's REST API
- Mirrors the tree on disk: `<year><term>/<course>/<area>/<folder>/<file>`
- Downloads lectures, slides, notebooks, datasets, archives — everything
- Downloads assignment attachments and writes a per-course assignment index
- Records external links (Zoom, etc.) without trying to download them
- Is resumable: a per-course status file skips finished courses on re-run

## Requirements

- Python 3.9+
- Microsoft Edge (or Chromium) installed
- A BlackBoard account you can log into in a browser

Dependencies are pinned in `requirements.txt`. Note `DrissionPage==4.2.0b20` is a **beta
on purpose** — the crawler uses 4.2+ APIs and no stable 4.2+ release exists on PyPI, so
`pip install "DrissionPage>=4.2"` fails.

```bash
pip install -r requirements.txt
```

## Configure

```bash
cp configs/paths.example.json configs/paths.json
```

Then edit `configs/paths.json`:

| Key | Meaning |
|---|---|
| `base_url` | Your institution's BlackBoard URL, e.g. `https://bb.your-university.edu` |
| `browser_path` | Path to the Edge/Chromium executable |
| `profile_path` | A **dedicated** browser profile dir (holds login state) |
| `port` | Local CDP port; anything free |
| `download_root` | Where term folders go, relative to the project root |
| `login_timeout` | Seconds to wait for you to finish logging in |

`profile_path` must **not** point at your everyday browser profile — the tool kills
processes holding it on startup.

## Usage

```bash
python bb_crawler.py --dry-run           # list courses + term mapping, download nothing
python bb_crawler.py                     # download everything (skips finished courses)
python bb_crawler.py --course _12345_1   # one course only (repeatable)
python bb_crawler.py --force             # wipe that course's folder and re-download
                                         # (keeps a `homework/` subfolder if you have one)
python bb_crawler.py --update            # incremental: fetch only new/changed content
python bb_crawler.py --update --dry-run  # preview what --update would fetch
python bb_crawler.py --clean-profile     # trim the browser profile (keeps the session)
```

**Always start with `--dry-run`.** It shows which folder each course would land in
without downloading anything — the fastest way to confirm your term mapping is right.

The first run opens a browser window and asks you to log in once. The session is then
persisted in `profile_path`, so later runs are unattended.

## Incremental updates

`--update` re-walks every course and downloads only what is new or changed. It is
designed to be run unattended on a schedule (see below).

**How it decides.** Every content item in the BlackBoard tree carries a `modified`
timestamp, and *that timestamp does move when a course is edited* — including when an
attachment is added to or replaced on an existing item. The crawler keeps a snapshot of
`item_id → modified` in `.content_snapshot.json` and acts on the difference:

| Situation | Action |
|---|---|
| Item id not in the snapshot | new → download |
| `modified` changed | changed → re-download |
| Item's parent path changed | moved → re-download into the new location |
| Otherwise | skip — **the item's page is never fetched**, which is where most of the time saving comes from |

**Discipline that keeps it honest:**

- An item is written to the snapshot **only after every one of its attachments downloaded
  successfully**. A failure is left out on purpose, so next week retries it instead of
  treating it as "seen, unchanged" and losing it forever.
- If the content tree walk hits an error partway, the snapshot is **not** updated at all
  for that course — an incomplete walk must not look like "these items disappeared".
- The snapshot is merged, never replaced, so an item the instructor temporarily hides
  keeps its record and is re-checked when it reappears.
- Files whose names only the browser knows are downloaded to a temp dir first and then
  moved onto the target name, so re-downloading a changed file **overwrites** it instead
  of leaving a `_1` copy next to a stale original.

**Known limitation.** `modified` tracks *edits to content items*. If someone overwrites a
file directly in the course file store under the same name without touching the item, the
timestamp does not move and the change is not detected. Detecting that would require
re-fetching every attachment every run (the REST API exposes no size or checksum for a
content file), which is the cost this mode exists to avoid.

**First run after enabling.** If a course was already downloaded but has no snapshot yet,
the crawler *seeds* the snapshot without downloading anything — but it still fetches
anything modified since that course was last crawled. So the first `--update` is cheap,
and it will not re-download an existing archive.

## Running it on a schedule

`scripts/run_weekly.bat` runs `--update`, appends to `logs/update.log`, and propagates a
meaningful exit code:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | unexpected error |
| 2 | needs a human to log in (session expired) |
| 3 | something was found but not retrieved — it was **not** recorded, next run retries |
| 4 | another instance is already running |

Register it on Windows (Saturday and Sunday at 20:00):

```
schtasks /create /tn BB_WeeklyUpdate ^
         /tr "<仓库路径>\scripts\run_weekly.bat" ^
         /sc weekly /d SAT,SUN /st 20:00 /f
```

Two things to know about the default task settings:

- The task runs as `InteractiveToken`, i.e. **only while you are logged in**. It will not
  fire if the machine is off or asleep, and there is no catch-up. Running twice a week
  plus the idempotent incremental logic is what covers that.
- Set "If the task is already running, do not start a new instance". The crawler also
  takes a `.crawl.lock` for the same reason — two instances would kill each other's
  browser (startup kills every `msedge` holding that profile).

Reports go to `reports/update_YYYY-MM-DD.md` + `reports/latest-update.md`, and a report
is written on **every** run — including the ones that end at exit code 2/1. A report that
is absent or stale means the task never started, not that nothing changed. Each entry is
one of:

| Mark | Meaning |
|---|---|
| 新增 / 已修改 / 位置变更 | fetched and written to disk |
| 补访 | re-visited to backfill a missing on-disk record (see `--update` notes) |
| ⚠️ 本次没拿到 | found but not retrieved; **not** recorded, so the next run retries |
| ❓ 需人工确认 | an item with no downloadable attachment and no body text |

The ⚠️ rows are the ones that matter: they are the only case where the crawler knows it
is missing something.

## Output layout

```
<download_root>/
├── 2025年下学期/                       # term folder (name comes from your term mapping)
│   └── CS101_Introduction to Programming (2026 Fall)/
│       ├── _作业清单.md                # assignment index for this course
│       ├── homework/                   # your own work (never downloaded; see --force)
│       │   └── Assignment1.docx
│       ├── Assessment/
│       │   └── assignment1.ipynb       # assignment attachments
│       └── Materials/
│           ├── _外链.md                # external links, recorded not downloaded
│           └── week 1/lectures/Lecture_1.pdf
├── .crawl_status.json                  # resume state (gitignored)
├── .content_snapshot.json              # incremental-update baseline (gitignored)
├── .crawl.lock                         # single-instance lock (gitignored)
├── reports/
│   ├── update_2026-01-01.md            # per-run incremental report
│   └── latest-update.md                # shortcut to the newest one
├── configs/paths.json                  # your config (gitignored)
└── .edge_profile/                      # browser profile (gitignored)
```

Marker files are named in Chinese (this started as a personal tool); they are plain
Markdown and safe to ignore or rename:

| File | Contents |
|---|---|
| `_作业清单.md` | Per-course assignment index: title, path, attachment count |
| `_未识别条目.md` | Content types the crawler did not handle — **check this if it appears** |
| `_外链.md` | External URLs, one per line |
| `<name>_作业要求.md` | Assignment instructions, written only when the page has real text |

## Term mapping

Courses are grouped into folders by their BlackBoard term. The mapping is resolved in
this order:

1. `configs/term_map.json` → `by_term_id` (exact `termId` match)
2. `configs/term_map.json` → `by_term_name` (exact term-name match)
3. Built-in auto rule for term names shaped `YY + T + 0 [+ PG|UG]`:
   - `T=1` → `<YY>年下学期` (fall)
   - `T=2` → `<YY+1>年上学期` (spring)
   - `T=5` → `<YY+1>年夏季学期` (summer)
4. Otherwise → `未分类/` (uncategorized) — check the dry-run output if you see this

**The auto rule encodes one institution's naming scheme and probably does not match
yours.** If `--dry-run` shows courses landing in `未分类/`, fill in
`configs/term_map.json` manually — that always wins.

## How it works

The interesting parts (and the traps) are documented in
[docs/blackboard-structure.md](docs/blackboard-structure.md):

- How to enumerate courses and walk the content tree over the REST API
- Why the course menu is not a reliable index of content areas
- How to recover real file download URLs (they are not in the API — they have to be
  regex-extracted from an HTML wrapper page)
- Why filenames must come from the browser, not the API
- The in-browser `fetch` + poll pattern used to call the API from the page context

## Known limitations

- **BlackBoard Classic only.** Ultra uses a different DOM and API surface.
- **Term auto-mapping is institution-specific** — see above.
- **Rotating terms need config.** A new academic year means new term IDs; if
  `--dry-run` shows `未分类/`, add the new mapping.
- **Full mode (without `--update`) is not incremental.** Re-running a finished course is
  skipped entirely; use `--update` for incremental, or
  `--force` to re-fetch it (which deletes and re-downloads the whole course folder).
  A `homework/` subfolder is the one exception: it is left alone, on the assumption
  that anything you put in there is yours and not something the crawler downloaded.
  If you keep your own files in a course folder under a different name, add that name
  to `KEEP_ON_FORCE` near the top of `bb_crawler.py` before running `--force`.
- **HTTP rate limits are your problem.** There is a small delay between downloads, but
  no adaptive backoff. Don't run this against instances you don't have permission to use.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `缺少配置文件` / missing config | `cp configs/paths.example.json configs/paths.json` and fill it in |
| Stuck at the login prompt | Log in in the opened browser window; raise `login_timeout` if slow |
| Courses land in `未分类/` | Fill in `configs/term_map.json` |
| `_未识别条目.md` is non-empty | A content type the crawler doesn't handle — see the type table in the structure doc |
| Garbled non-ASCII output | The script forces UTF-8 stdout; check your terminal encoding |
| Port already in use | The script kills processes holding that profile and waits for the port |

## License

[MIT](LICENSE)
