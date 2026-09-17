#!/usr/bin/env python3
"""
Import the file-based posts into EmDash, correctly this time.

Three things the CLI's Markdown converter does not handle, found by inspecting
the stored blocks after the first import:

  * GFM tables          -> 41 rows leaked into the post as plain paragraphs
  * thematic breaks     -> 13 literal "---" paragraphs
  * *single-asterisk*   -> 15 literal asterisks (only _underscore_ is supported)

Breaks are dropped, asterisk emphasis is rewritten to underscores, and tables
are converted to real Portable Text `table` blocks. Because the CLI cannot
express a table, tables are swapped for placeholders before the create call and
spliced back into the raw Portable Text afterwards via the REST API.
"""

import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path("/tmp/website/src/content/blog")
API = "http://localhost:4321/_emdash/api"
COOKIES = Path("/tmp/emcookies.txt")
SLUGS = ["welcome", "kubernetes-at-home", "meo-outage-day"]
# No underscores: the Markdown converter reads _TABLE_ as emphasis and
# strips them, so the token would not survive the round trip.
PLACEHOLDER = "@@EMDASHTABLE%d@@"


# ---------------------------------------------------------------- transport

def cookie_header():
    jar = []
    for line in COOKIES.read_text().splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            jar.append(f"{parts[5]}={parts[6]}")
    if not jar:
        raise SystemExit("no cookies parsed")
    return "; ".join(jar)


COOKIE = cookie_header()


def api(method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json",
                 "X-EmDash-Request": "1", "Cookie": COOKIE})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=180).read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def cli(*args):
    out = subprocess.run(["npx", "emdash", *args], capture_output=True, text=True,
                         cwd=Path.home() / "work/website", timeout=900)
    i = out.stdout.find("{")
    if i < 0:
        raise SystemExit(f"CLI gave no JSON:\n{out.stdout}\n{out.stderr}")
    return json.loads(out.stdout[i:])


# ---------------------------------------------------------------- markdown

def split_fences(md):
    """Yield (is_code, text) so transforms never touch fenced blocks."""
    parts = re.split(r"(```.*?```)", md, flags=re.S)
    for part in parts:
        yield part.startswith("```"), part


def normalise(md):
    """Drop thematic breaks and rewrite *em* to _em_, outside code fences."""
    out, breaks, ems = [], 0, 0
    for is_code, part in split_fences(md):
        if is_code:
            out.append(part)
            continue
        part, n = re.subn(r"^---[ \t]*$\n?", "", part, flags=re.M)
        breaks += n
        part, n = re.subn(r"(?<![\*\w])\*([^*\n]+?)\*(?!\*)", r"_\1_", part)
        ems += n
        out.append(part)
    return "".join(out), breaks, ems


TABLE_RE = re.compile(
    r"^[ \t]*\|.+\|[ \t]*\n[ \t]*\|[ \t:\-|]+\|[ \t]*\n(?:[ \t]*\|.*\|[ \t]*\n?)+",
    re.M)


def extract_tables(md):
    """Replace each GFM table with a placeholder paragraph; return the raw tables."""
    tables, chunks, idx = [], [], 0
    for is_code, part in split_fences(md):
        if is_code:
            chunks.append(part)
            continue

        def swap(m):
            nonlocal idx
            tables.append(m.group(0))
            token = PLACEHOLDER % idx
            idx += 1
            return f"\n{token}\n\n"

        chunks.append(TABLE_RE.sub(swap, part))
    return "".join(chunks), tables


# ------------------------------------------------------------ inline spans

INLINE_RE = re.compile(r"(`[^`]+`|\*\*[^*]+\*\*|_[^_]+_|\[[^\]]+\]\([^)]+\))")


def spans(text, key):
    """Parse a cell's inline Markdown into Portable Text spans with marks."""
    out = []
    for piece in INLINE_RE.split(text):
        if not piece:
            continue
        marks, body = [], piece
        if piece.startswith("`") and piece.endswith("`"):
            marks, body = ["code"], piece[1:-1]
        elif piece.startswith("**") and piece.endswith("**"):
            marks, body = ["strong"], piece[2:-2]
        elif piece.startswith("_") and piece.endswith("_"):
            marks, body = ["em"], piece[1:-1]
        elif piece.startswith("["):
            body = piece[1:piece.index("]")]
        out.append({"_type": "span", "_key": f"{key}s{len(out)}",
                    "text": body, "marks": marks})
    return out or [{"_type": "span", "_key": f"{key}s0", "text": "", "marks": []}]


ALIGN = {(True, True): "center", (False, True): "right", (True, False): "left"}


def to_table_block(raw, key):
    lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    grid = [[c.strip() for c in l.strip().strip("|").split("|")] for l in lines]
    header, sep, body = grid[0], grid[1], grid[2:]

    aligns = []
    for spec in sep:
        aligns.append(ALIGN.get((spec.startswith(":"), spec.endswith(":"))))

    rows = []
    for r, cells in enumerate([header, *body]):
        built = []
        for c, cell in enumerate(cells):
            k = f"{key}r{r}c{c}"
            entry = {"_type": "tableCell", "_key": k,
                     "content": spans(cell, k), "markDefs": []}
            if r == 0:
                entry["isHeader"] = True
            if c < len(aligns) and aligns[c]:
                entry["textAlign"] = aligns[c]
            built.append(entry)
        rows.append({"_type": "tableRow", "_key": f"{key}r{r}", "cells": built})

    return {"_type": "table", "_key": key, "hasHeaderRow": True,
            "rows": rows, "markDefs": []}


# ------------------------------------------------------------------- main

def frontmatter(path):
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    meta = {}
    for line in m.group(1).split("\n"):
        if ": " in line:
            k, v = line.split(": ", 1)
            meta[k.strip()] = v.strip()
    meta["tags"] = [t.strip() for t in meta.get("tags", "[]").strip("[]").split(",") if t.strip()]
    return meta, m.group(2).lstrip()


def upload_images(folder, body):
    images = folder / "images"
    if not images.is_dir():
        return body, 0
    count = 0
    for ref in sorted(set(re.findall(r"!\[[^\]]*\]\((images/[^)]+)\)", body))):
        local = folder / ref
        if not local.exists():
            continue
        item = cli("media", "upload", str(local))
        item = item.get("item", item)
        url = item.get("url") or f"/_emdash/api/media/file/{item.get('id')}"
        body = body.replace(f"]({ref})", f"]({url})")
        count += 1
    return body, count


def term_id(taxonomy, slug):
    got = api("GET", f"/taxonomies/{taxonomy}/terms?limit=200")
    for t in (got.get("data") or {}).get("terms", []):
        if t.get("slug") == slug:
            return t["id"]
    res = api("POST", f"/taxonomies/{taxonomy}/terms",
              {"slug": slug, "label": slug.replace("-", " ").capitalize()})
    if res.get("success"):
        return res["data"]["term"]["id"]
    got = api("GET", f"/taxonomies/{taxonomy}/terms?limit=200")
    for t in (got.get("data") or {}).get("terms", []):
        if t.get("slug") == slug:
            return t["id"]
    return None


def main():
    for name in SLUGS:
        folder = SRC / name
        meta, body = frontmatter(folder / "index.md")
        slug = meta.get("slug", name)
        print(f"\n  {slug}")

        body, imgs = upload_images(folder, body)
        body, breaks, ems = normalise(body)
        body, tables = extract_tables(body)
        print(f"      images={imgs}  breaks_dropped={breaks}  em_fixed={ems}  tables={len(tables)}")

        for it in ((api("GET", "/content/posts?limit=100").get("data") or {}).get("items", [])):
            if it.get("slug") == slug:
                api("DELETE", f"/content/posts/{it['id']}?overrideLock=true")
                api("DELETE", f"/content/posts/{it['id']}/permanent?overrideLock=true")

        tmp = Path(f"/tmp/em2-{slug}.json")
        tmp.write_text(json.dumps({"title": meta["title"],
                                   "excerpt": meta.get("description", ""),
                                   "content": body}))
        created = cli("content", "create", "posts", "--file", str(tmp), "--slug", slug)
        cid = created["id"]

        # Splice the real table blocks in over their placeholders.
        got = api("GET", f"/content/posts/{cid}")
        item = got["data"]["item"]
        blocks = item["data"]["content"]
        rebuilt, swapped = [], 0
        for b in blocks:
            text = "".join(s.get("text", "") for s in b.get("children", [])).strip()
            m = re.fullmatch(r"@@EMDASHTABLE(\d+)@@", text)
            if m:
                rebuilt.append(to_table_block(tables[int(m.group(1))], f"tbl{m.group(1)}"))
                swapped += 1
            else:
                rebuilt.append(b)

        payload = dict(item["data"])
        payload["content"] = rebuilt
        res = api("PUT", f"/content/posts/{cid}",
                  {"_rev": got["data"]["_rev"], "data": payload,
                   "publishedAt": f"{meta['date']}T09:00:00.000Z"})
        ok = res.get("success")
        print(f"      tables spliced={swapped}  date+content saved={'ok' if ok else json.dumps(res.get('error'))[:140]}")

        # A raw REST PUT writes a revision but does not promote it to the
        # content table, which is what the live loader reads. The CLI
        # auto-publishes; this path has to do it explicitly.
        api("POST", f"/content/posts/{cid}/publish?overrideLock=true", {})

        ids = [i for i in (term_id("tag", t) for t in meta["tags"]) if i]
        if ids:
            r = api("POST", f"/content/posts/{cid}/terms/tag", {"termIds": ids})
            print(f"      tags applied={len((r.get('data') or {}).get('terms', []))}")

    print("\n  === final ===")
    for it in ((api("GET", "/content/posts?limit=50").get("data") or {}).get("items", [])):
        print(f"  {it['slug']:<22} {it['status']:<10} {it.get('publishedAt')}")


if __name__ == "__main__":
    main()
