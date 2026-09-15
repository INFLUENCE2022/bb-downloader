# BlackBoard Learn (Classic) 结构与抓取指南

This document records how BlackBoard Learn **Classic / Original** (as opposed to Ultra)
exposes courses and files, the practical traps, and how to re-run the reconnaissance
against your own institution's instance.

> Findings here were verified empirically against one real BB Classic deployment.
> Other deployments share the same product but may differ in version, theme, or local
> configuration — treat everything below as *likely correct, verify before relying on it*.

## 0. Is your instance Classic or Ultra?

The course record carries it:

```
GET /learn/api/public/v1/users/me/courses?expand=term,course
→ results[].course.ultraStatus   # "Classic" | "Ultra"
```

This tool targets **Classic**. Ultra uses a completely different DOM and API surface.

## 1. Enumerating courses

```
GET /learn/api/public/v1/users/me/courses?expand=term,course&limit=200[&offset=N]
```

- Works with **plain session cookies** — no OAuth token, no CSRF header required.
- Returns `courseId`, `course.name`, `course.termId`, `course.ultraStatus`,
  `course.externalAccessUrl`, `courseRoleId`.
- **`expand=term` does not populate a term object.** Get term names separately:

```
GET /learn/api/public/v1/terms?limit=200
GET /learn/api/public/v1/terms/{termId}
```

## 2. Walking the course content tree

```
GET /learn/api/public/v1/courses/{courseId}/contents?limit=200                    # top level
GET /learn/api/public/v1/courses/{courseId}/contents/{contentId}/children?limit=200
```

Each item: `id`, `title`, `hasChildren`, `availability.available`, `contentHandler`,
and sometimes a `links[rel=alternate]` pointing at a HTML view.

### Content handler types observed

| `contentHandler.id` | Meaning | Handling |
|---|---|---|
| `resource/x-bb-folder` | Folder | recurse into `/children` |
| `resource/x-bb-file` | A file (has `contentHandler.file.fileName`) | download |
| `resource/x-bb-document` | Document-style item | download attachments |
| `resource/x-bb-assignment` | Assignment (has `gradeColumnId`) | download attachments |
| `resource/x-bb-externallink` | External URL | record only, not downloadable |

Other types exist in the product (tests, discussions, blogs, lesson plans…) that were
not present in the instance we surveyed. The crawler deliberately does **not** silently
drop unknown types — it writes them to `_未识别条目.md` in the course folder so nothing
disappears unnoticed. If you hit one, add a branch and consult the type table above.

### ⚠ Trap 1 — the course menu does not list every content area

In the surveyed instance every course carried the same three top-level areas,
but the left-hand course menu only linked to two of them — one area had no menu
link at all.
**A DOM-driven crawl that follows the course menu silently loses an entire area.**

Always enumerate via the REST tree. Do not trust the menu.

## 3. Resolving the actual file download URL

This is the single most awkward part of BB Classic.

What does **not** work:

- `GET /learn/api/public/v1/courses/{cid}/contents/{id}/file` → **404** (does not exist)
- `GET /webapps/blackboard/execute/content/file?cmd=view&content_id=…` → returns an
  HTML wrapper page (~50 KB), not the file
- `links[rel=alternate]` → `displayIndividualContent?…` → also an HTML wrapper page

What works — fetch the wrapper page and pull the real URL out of its HTML with a regex:

```
GET /webapps/blackboard/execute/displayIndividualContent?course_id={cid}&content_id={contentId}
```

then match:

```
/bbcswebdav/pid-<pid>-dt-content-rid-<rid>_1/xid-<rid>_1
```

**Verify before trusting:** the numeric `pid` equals the numeric part of the content id
(`_123456_1` → `pid-123456`). Keep only matches where they agree — a wrapper page can
reference other items, and you do not want to download the wrong attachment.

`rid` is not derivable from anything else; it only exists in the page.

## 4. Assignments

`resource/x-bb-assignment` items carry a `gradeColumnId` but, in the surveyed instance,
**no inline instructions** — the assignment page body contained only the title plus
JavaScript noise. The actual assignment material was always an attachment.

So the crawler downloads assignment attachments and additionally writes:
- a per-course `_作业清单.md` index (title, path, attachment count)
- `<assignment>_作业要求.md` **only if** the page body exceeds a length threshold
  (i.e. some assignments do have instructions — handle both cases)

## 5. Talking to the REST API from inside the browser

The crawler reuses the logged-in browser session rather than re-implementing auth.
Two constraints shape the implementation:

- `DrissionPage`'s `run_js` does **not** await promises, so you cannot `return` a fetch result.
- Synchronous `XMLHttpRequest` is **disabled** on the main thread in current Chromium:
  `Failed to load …` on `.send()`.

The working pattern is fire-and-forget plus polling:

```javascript
window.__res = undefined;
fetch(url, {credentials: "include"})
  .then(r => r.text().then(t => { window.__res = {status: r.status, text: t}; }))
  .catch(e => { window.__res = {status: -1, text: String(e)}; });
```

then poll `run_js("return window.__res === undefined ? null : window.__res")` until it
is set (see `api_fetch()` in `bb_crawler.py`).

## 6. File naming

Use the **browser's** saved filename, not the REST `title`.

- BB sends `Content-Disposition`, and the browser saves the real filename.
- REST only supplies `contentHandler.file.fileName` for `resource/x-bb-file`.
  `document` and `assignment` items have no such field.
- Renaming `document` items by their REST `title` **destroys** the real names —
  the title is a label (e.g. `notes`), while the attachments are real files
  (e.g. `Lecture 5.1 Course Notes.ipynb`, several of them under one item).

## 7. Re-running reconnaissance on your own instance

With the tool's browser profile logged in, call the endpoints in §1–§2 and inspect.
The cheapest entry point is the "My Institution"/"My Courses" tab, whose URL looks like:

```
https://<host>/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_X_Y
```

Then confirm, for **your** instance:

1. `ultraStatus` is `Classic` (§0)
2. Which content handler types actually appear — walk a course and tally them
3. Whether the download-URL regex in §3 still matches (BB version changes could alter it)
4. What your term names look like, and fill in `configs/term_map.json`

Run `python bb_crawler.py --dry-run` first — it prints the course → term mapping without
downloading anything, which is the fastest way to see whether the term mapping is right.
