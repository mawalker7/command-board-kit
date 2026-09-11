#!/usr/bin/env python3
"""board_sanitize.py: validate the hub's board.json and emit the doc-safe twin plus board.md.

The delivery channel is a Google Doc read back through the Drive connector, whose text export
backslash-escapes markdown punctuation and doubles backslashes. That transform is reversible
(the page strips one backslash before any character), but the model also has to copy the JSON
verbatim into the upload call, so we keep the payload boring: single line, no double quotes or
backslashes inside strings, capped lengths, canonical line order, computed counts.

Usage: board_sanitize.py board.json --lines config/lines.json --out-json board.doc.json --out-md board.md
Exit 0 on success, 3 on validation failure (message on stderr).
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

STATES = ("needs_you", "waiting", "moving", "parked")
STATE_LABEL = {"needs_you": "Needs you", "waiting": "Waiting on them", "moving": "Moving", "parked": "Parked"}
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_TITLE, MAX_SYNOPSIS, MAX_ITEMS_PER_LINE, MAX_BYTES = 120, 420, 12, 60_000


class ValidationError(Exception):
    pass


def clean_str(s, limit=None):
    if s is None:
        return ""
    s = str(s).replace('"', "'").replace("\\", "/")
    s = re.sub(r"\s+", " ", s).strip()
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def sanitize(board, lines_cfg):
    if not isinstance(board, dict) or board.get("schema") != 1:
        raise ValidationError("schema must be 1")
    if board.get("run") not in ("am", "pm"):
        raise ValidationError("run must be 'am' or 'pm'")
    try:
        datetime.fromisoformat(str(board.get("generated_at", "")).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError("generated_at must be ISO 8601")
    canonical = [(l["id"], l["name"]) for l in lines_cfg["lines"]]
    by_id = {l.get("id"): l for l in board.get("lines", []) if isinstance(l, dict)}
    unknown = set(by_id) - {i for i, _ in canonical}
    if unknown:
        raise ValidationError(f"unknown line ids: {sorted(unknown)}")
    seen_ids = set()
    out_lines = []
    for lid, name in canonical:
        src = by_id.get(lid, {})
        items = []
        for it in src.get("items", []) or []:
            iid = str(it.get("id", ""))
            if not ID_RE.match(iid):
                raise ValidationError(f"bad item id {iid!r} in line {lid}")
            if iid in seen_ids:
                raise ValidationError(f"duplicate item id {iid!r}")
            seen_ids.add(iid)
            state = it.get("state")
            if state not in STATES:
                raise ValidationError(f"item {iid}: state must be one of {STATES}")
            due = it.get("due")
            if due not in (None, "") and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(due)):
                raise ValidationError(f"item {iid}: due must be YYYY-MM-DD or null")
            sessions = []
            for s in it.get("sessions", []) or []:
                if isinstance(s, str):
                    sessions.append({"name": clean_str(s, 80), "machine": "", "first_prompt": ""})
                elif isinstance(s, dict):
                    sessions.append({"name": clean_str(s.get("name"), 80), "machine": clean_str(s.get("machine"), 40),
                                     "first_prompt": clean_str(s.get("first_prompt"), 140)})
            items.append({
                "id": iid,
                "state": state,
                "title": clean_str(it.get("title"), MAX_TITLE),
                "synopsis": clean_str(it.get("synopsis"), MAX_SYNOPSIS),
                "source_url": clean_str(it.get("source_url")) or None,
                "source_kind": clean_str(it.get("source_kind"), 20) or "note",
                "owner": clean_str(it.get("owner"), 60) or "you",
                "due": due or None,
                "updated": clean_str(it.get("updated"), 32),
                "sessions": sessions,
            })
        order = {s: i for i, s in enumerate(STATES)}
        items.sort(key=lambda i: (order[i["state"]], i["due"] or "9999", i["title"]))
        if len(items) > MAX_ITEMS_PER_LINE:
            keep = [i for i in items if i["state"] != "parked"][:MAX_ITEMS_PER_LINE]
            items = keep + [i for i in items if i["state"] == "parked"][: MAX_ITEMS_PER_LINE - len(keep)]
        counts = {s: sum(1 for i in items if i["state"] == s) for s in STATES}
        out_lines.append({"id": lid, "name": name, "headline": clean_str(src.get("headline"), 140), "counts": counts, "items": items})
    health = []
    for h in board.get("run_health", []) or []:
        if isinstance(h, dict):
            health.append({"input": clean_str(h.get("input"), 40), "status": clean_str(h.get("status"), 16) or "unknown",
                           "detail": clean_str(h.get("detail"), 160)})
    out = {"schema": 1, "generated_at": clean_str(board["generated_at"], 32), "run": board["run"],
           "hub": clean_str(board.get("hub"), 40), "handled_applied": [clean_str(x, 60) for x in board.get("handled_applied", []) or []],
           "lines": out_lines, "run_health": health}
    text = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    if "\n" in text:
        raise ValidationError("output must be a single line")
    if len(text.encode()) > MAX_BYTES:
        raise ValidationError(f"board too large: {len(text.encode())} bytes > {MAX_BYTES}")
    return out, text


def to_markdown(out):
    md = [f"# Command Board — {out['generated_at']} ({out['run'].upper()})", ""]
    total = sum(l["counts"]["needs_you"] for l in out["lines"])
    md.append(f"Needs you across all lines: {total}")
    md.append("")
    for l in out["lines"]:
        c = l["counts"]
        md.append(f"## {l['name']}  ·  needs you {c['needs_you']} · waiting {c['waiting']} · moving {c['moving']} · parked {c['parked']}")
        if l["headline"]:
            md.append(f"_{l['headline']}_")
        md.append("")
        if not l["items"]:
            md.append("- (nothing tracked)")
        for i in l["items"]:
            due = f" · due {i['due']}" if i["due"] else ""
            md.append(f"- **[{STATE_LABEL[i['state']]}] {i['title']}**{due} · owner: {i['owner']}")
            md.append(f"  {i['synopsis']}")
            if i["source_url"]:
                md.append(f"  Source: {i['source_url']}")
            if i["sessions"]:
                md.append("  Sessions: " + "; ".join(f"{s['name']}" + (f" ({s['machine']})" if s['machine'] else "") for s in i["sessions"]))
        md.append("")
    if out["run_health"]:
        md.append("## Run health")
        for h in out["run_health"]:
            md.append(f"- {h['input']}: {h['status']}" + (f" — {h['detail']}" if h["detail"] else ""))
        md.append("")
    return "\n".join(md)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--lines", required=True)
    ap.add_argument("--out-json", required=True)
    ap.add_argument("--out-md", required=True)
    args = ap.parse_args(argv)
    try:
        board = json.loads(Path(args.board).read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"ERROR: cannot read board: {e}", file=sys.stderr)
        return 3
    lines_cfg = json.loads(Path(args.lines).read_text())
    try:
        out, text = sanitize(board, lines_cfg)
    except ValidationError as e:
        print(f"ERROR: board.json invalid: {e}", file=sys.stderr)
        return 3
    Path(args.out_json).write_text(text)
    Path(args.out_md).write_text(to_markdown(out))
    n_items = sum(len(l["items"]) for l in out["lines"])
    print(f"ok: {n_items} items across {len(out['lines'])} lines, {len(text.encode())} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
