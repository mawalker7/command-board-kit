#!/usr/bin/env python3
"""collect_local.py: deterministic context collector for the command-board hub run (no LLM).

Writes small, bounded files into <repo>/context/ so the summarizing model reads instead of explores:
  run.json            run stamp: now, am|pm, hub host, previous run time
  sessions.json       live + recently active Claude Code sessions on this machine (name, cwd, first prompt)
  memory.md           every project memory index + memory files touched in the last 3 days
  github.md           open PRs / review requests / mentions / assigned issues via gh (best effort)
  briefs.md           newest brief from ~/Downloads/briefs (any other scheduled report you run) + its handled.md
  downloads.md        markdown deliverables in ~/Downloads touched in the last 3 days (head only)
  satellite.md        bundles pushed by the other machine (data/satellite/<host>/bundle.md)
  watchers.json       slack_watch / msgraph_watch --all --consumer board --json (or not_configured)
  previous-board.json newest snapshot from data/snapshots
  manifest.json       what was written, sizes, per-input status (feeds the board's run_health)
"""

import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HOME = Path.home()
CLAUDE = HOME / ".claude"
DAYS3 = 3 * 86400
CAP_FILE = 6_000
CAP_HEAD = 2_000


def sh(cmd, timeout=60, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def strip_tags(text):
    """Drop leading <tag>…</tag> blocks (ide context, system reminders) and return the human prompt."""
    text = re.sub(r"<(\w[\w-]*)[^>]*>.*?</\1>", " ", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip()


def first_prompt(jsonl):
    try:
        with open(jsonl, errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") != "user":
                    continue
                c = (d.get("message") or {}).get("content")
                if isinstance(c, list):
                    txt = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
                else:
                    txt = str(c or "")
                txt = strip_tags(txt)
                if txt and not txt.startswith("/"):
                    return txt[:200]
    except OSError:
        pass
    return ""


def project_name(encoded_dir, cwd=""):
    """~/.claude/projects encodes the cwd with '/' as '-'; recover the repo folder name.

    Prefer the live session's cwd; otherwise match the longest known folder under ~/Documents/GitHub
    that the encoded name ends with; fall back to the last '-' segment.
    """
    if cwd:
        return Path(cwd).name
    name = Path(encoded_dir).name
    known = sorted((Path(p).name for p in glob.glob(str(HOME / "Documents" / "GitHub" / "*"))), key=len, reverse=True)
    for k in known:
        if name.endswith("-" + k) or name == k:
            return k
    return name.rsplit("-", 1)[-1]


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def collect_sessions(now):
    host = socket.gethostname()
    live = {}
    for f in glob.glob(str(CLAUDE / "sessions" / "*.json")):
        try:
            d = json.loads(Path(f).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not pid_alive(d.get("pid")):
            continue
        live[d.get("sessionId")] = d
    out = []
    for proj_dir in glob.glob(str(CLAUDE / "projects" / "*")):
        for jsonl in glob.glob(os.path.join(proj_dir, "*.jsonl")):
            sid = Path(jsonl).stem
            project = project_name(proj_dir, live.get(sid, {}).get("cwd", ""))
            mtime = os.path.getmtime(jsonl)
            is_live = sid in live
            if not is_live and now - mtime > 86400:
                continue
            meta = live.get(sid, {})
            out.append({
                "machine": host,
                "session_id": sid,
                "name": meta.get("name") or f"{project}-{sid[:4]}",
                "name_source": meta.get("nameSource", "derived"),
                "status": "live" if is_live else "recent",
                "project": project,
                "cwd": meta.get("cwd", ""),
                "started_at": datetime.fromtimestamp(meta["startedAt"] / 1000).isoformat(timespec="minutes") if meta.get("startedAt") else "",
                "last_activity": datetime.fromtimestamp(mtime).isoformat(timespec="minutes"),
                "first_prompt": first_prompt(jsonl),
            })
    out.sort(key=lambda s: (s["status"] != "live", s["last_activity"]), reverse=False)
    return out


def collect_memory(now):
    parts = []
    for mem in sorted(glob.glob(str(CLAUDE / "projects" / "*" / "memory"))):
        project = project_name(Path(mem).parent)
        idx = Path(mem) / "MEMORY.md"
        parts.append(f"# Memory index: {project}\n")
        parts.append(idx.read_text()[:12_000] if idx.exists() else "(no MEMORY.md)")
        fresh = [p for p in glob.glob(os.path.join(mem, "*.md")) if not p.endswith("MEMORY.md") and now - os.path.getmtime(p) < DAYS3]
        for p in sorted(fresh, key=os.path.getmtime, reverse=True)[:12]:
            parts.append(f"\n## Recently updated memory: {Path(p).name} ({datetime.fromtimestamp(os.path.getmtime(p)).date()})\n")
            parts.append(Path(p).read_text()[:CAP_FILE])
        parts.append("\n")
    return "\n".join(parts)


JQ = r'.[] | "\(.repository.nameWithOwner)#\(.number) \(.title) (updated \(.updatedAt[:10])) \(.url)"'


def collect_github():
    out, status = [], "ok"
    queries = [
        ("Open PRs I authored", ["gh", "search", "prs", "--author", "@me", "--state", "open", "--limit", "30",
                                 "--json", "repository,number,title,updatedAt,url", "--jq", JQ]),
        ("Review requested from me", ["gh", "search", "prs", "--review-requested", "@me", "--state", "open", "--limit", "30",
                                      "--json", "repository,number,title,updatedAt,url", "--jq", JQ]),
        ("Open PRs mentioning me", ["gh", "search", "prs", "--mentions", "@me", "--state", "open", "--limit", "30",
                                    "--json", "repository,number,title,updatedAt,url", "--jq", JQ]),
        ("Open issues assigned to me", ["gh", "search", "issues", "--assignee", "@me", "--state", "open", "--limit", "30",
                                        "--json", "repository,number,title,updatedAt,url", "--jq", JQ]),
    ]
    for label, cmd in queries:
        rc, so, se = sh(cmd, timeout=90)
        out.append(f"## {label}")
        if rc != 0:
            status = "partial"
            out.append(f"(gh failed: {se.strip()[:200]})")
        else:
            out.append(so.strip() or "(none)")
        out.append("")
    return "\n".join(out), status


def collect_briefs():
    d = Path(os.environ.get("BOARD_BRIEFS_DIR", HOME / "Downloads" / "briefs"))
    parts = []
    briefs = sorted(glob.glob(str(d / "*-am.md")) + glob.glob(str(d / "*-pm.md")), key=os.path.getmtime, reverse=True)
    if briefs:
        parts.append(f"# Latest brief: {Path(briefs[0]).name}\n")
        parts.append(Path(briefs[0]).read_text()[:9_000])
    handled = d / "handled.md"
    if handled.exists():
        parts.append("\n# briefs handled.md\n")
        parts.append(handled.read_text()[:3_000])
    return "\n".join(parts) or "(no briefs found)"


def collect_downloads(now):
    parts = []
    files = [p for p in glob.glob(str(HOME / "Downloads" / "*.md")) if now - os.path.getmtime(p) < DAYS3]
    for p in sorted(files, key=os.path.getmtime, reverse=True)[:20]:
        parts.append(f"## {Path(p).name} (modified {datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec='minutes')})")
        parts.append(Path(p).read_text(errors="replace")[:CAP_HEAD])
        parts.append("")
    return "\n".join(parts) or "(no markdown deliverables in ~/Downloads in the last 3 days)"


def collect_satellite(repo):
    parts = []
    rc, so, se = sh(["git", "-C", str(repo), "pull", "--ff-only", "-q"], timeout=60)
    status = "ok" if rc == 0 else "pull_failed"
    for b in sorted(glob.glob(str(repo / "data" / "satellite" / "*" / "bundle.md"))):
        host = Path(b).parent.name
        age_h = (time.time() - os.path.getmtime(b)) / 3600
        parts.append(f"# Satellite bundle from {host} (file age {age_h:.1f} h)\n")
        parts.append(Path(b).read_text()[:40_000])
    if not parts:
        parts.append("(no satellite bundles yet)")
        status = status if status != "ok" else "none"
    return "\n".join(parts), status


def collect_watchers(repo):
    result = {}
    for name, cfg_dir, script in [("slack", HOME / ".config" / "slack-watch", repo / "watchers" / "slack_watch.py"),
                                  ("msgraph", HOME / ".config" / "msgraph-watch", repo / "watchers" / "msgraph_watch.py")]:
        if not (cfg_dir / "sources.json").exists() or not script.exists():
            result[name] = {"status": "not_configured", "items": [], "errors": []}
            continue
        rc, so, se = sh([sys.executable, str(script), "--all", "--consumer", "board", "--json"], timeout=120)
        try:
            data = json.loads(so) if so.strip() else {"items": [], "errors": [se.strip()]}
        except json.JSONDecodeError:
            data = {"items": [], "errors": [f"unparseable output: {so[:200]} {se[:200]}"]}
        data["status"] = "ok" if rc == 0 else "failed"
        if se.strip() and rc == 0:
            data.setdefault("errors", []).append(se.strip()[:300])
        result[name] = data
    return result


def main(argv=None):
    repo = Path(argv[0]).resolve() if argv else Path(__file__).resolve().parent.parent
    ctx = repo / "context"
    ctx.mkdir(exist_ok=True)
    now = time.time()
    state_dir = repo / ".state"
    state_dir.mkdir(exist_ok=True)
    last = state_dir / "last-run.json"
    prev = json.loads(last.read_text()) if last.exists() else {}
    run = {"now": datetime.fromtimestamp(now).isoformat(timespec="minutes"), "run": "am" if datetime.fromtimestamp(now).hour < 12 else "pm",
           "hub": socket.gethostname(), "previous_run": prev.get("now"), "previous_run_kind": prev.get("run")}
    manifest = {"written_at": run["now"], "inputs": {}}

    def put(name, text, status="ok", detail=""):
        (ctx / name).write_text(text)
        manifest["inputs"][name] = {"status": status, "bytes": len(text.encode()), "detail": detail}

    put("run.json", json.dumps(run, indent=1))
    sessions = collect_sessions(now)
    put("sessions.json", json.dumps(sessions, indent=1), detail=f"{sum(1 for s in sessions if s['status']=='live')} live")
    put("memory.md", collect_memory(now))
    gh_text, gh_status = collect_github()
    put("github.md", gh_text, gh_status)
    put("briefs.md", collect_briefs())
    put("downloads.md", collect_downloads(now))
    sat_text, sat_status = collect_satellite(repo)
    put("satellite.md", sat_text, sat_status)
    rc, so, se = sh([sys.executable, str(repo / "bin" / "ics_events.py"), "--hours", "48"], timeout=90)
    try:
        ics = json.loads(so) if so.strip() else {"events": [], "errors": [se.strip()[:200]], "status": "failed"}
    except json.JSONDecodeError:
        ics = {"events": [], "errors": [f"unparseable: {so[:120]}"], "status": "failed"}
    put("ics-calendar.json", json.dumps(ics, indent=1, ensure_ascii=False), ics.get("status", "failed"), f"{len(ics.get('events', []))} events")
    watchers = collect_watchers(repo)
    put("watchers.json", json.dumps(watchers, indent=1), detail=", ".join(f"{k}={v['status']}" for k, v in watchers.items()))
    snaps = sorted(glob.glob(str(repo / "data" / "snapshots" / "*.json")), key=os.path.getmtime)
    put("previous-board.json", Path(snaps[-1]).read_text() if snaps else "{}", "ok" if snaps else "none")
    (ctx / "manifest.json").write_text(json.dumps(manifest, indent=1))
    last.write_text(json.dumps(run))
    for k, v in manifest["inputs"].items():
        print(f"{k:<22} {v['status']:<15} {v['bytes']:>7} B  {v['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
