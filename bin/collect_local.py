#!/usr/bin/env python3
"""collect_local.py: deterministic context collector for the command-board hub run (no LLM).

Writes small, bounded files into <repo>/context/ so the summarizing model reads instead of explores:
  run.json            run stamp: now, am|pm, hub host, previous run time
  sessions.json       live + recently active Claude Code sessions on this machine (name, cwd, first prompt)
  memory.md           every project memory index + memory files touched in the last 3 days
  github.md           open PRs / review requests / mentions / assigned issues via gh (best effort)
  briefs.md           newest brief from ~/.local/share/briefs (any other scheduled report you run) + its handled.md
  downloads.md        markdown deliverables in ~/Downloads touched in the last 3 days (head only)
  satellite.md        bundles pushed by the other machine (data/satellite/<host>/bundle.md)
  watchers.json       slack_watch / msgraph_watch --all --consumer board --json (or not_configured)
  previous-board.json newest snapshot from data/snapshots
  manifest.json       what was written, sizes, per-input status (feeds the board's run_health)
"""

import faulthandler
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
DAYS_MEM = 2 * 86400   # memory files count as fresh for 2 days (token budget)
CAP_FILE = 4_000
CAP_INDEX = 8_000
MAX_FRESH = 8
CAP_HEAD = 2_000


def sh(cmd, timeout=60, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


PROTECTED = ("Documents", "Desktop", "Downloads")


def readable(path):
    """On scheduled (launchd) runs, refuse paths that resolve into macOS-protected folders: a read there can block
    forever on a privacy prompt that cannot be shown. Symlinks are the usual trap (e.g. a memory folder that
    points into a repo under ~/Documents)."""
    if not os.environ.get("BOARD_SCHEDULED"):
        return True
    try:
        real = Path(path).resolve()
    except OSError:
        return False
    return not any(part in PROTECTED for part in real.parts)


def strip_tags(text):
    """Drop leading <tag>…</tag> blocks (ide context, system reminders) and return the human prompt."""
    text = re.sub(r"<(\w[\w-]*)[^>]*>.*?</\1>", " ", text, flags=re.S)
    return re.sub(r"\s+", " ", text).strip()


def first_prompt(jsonl):
    if not readable(jsonl):
        return ""
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

    Prefer the live session's cwd. Otherwise derive from the encoded name alone: the segment after the
    last known code-folder marker. Never lists ~/Documents: under launchd that read blocks on a consent
    prompt that cannot be shown (macOS TCC).
    """
    if cwd:
        return Path(cwd).name
    name = Path(encoded_dir).name
    for marker in ("-GitHub-", "-Projects-", "-src-", "-code-", "-Documents-", "-share-"):
        if marker in name:
            return name.split(marker, 1)[1]
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


def mirror_memory(repo, cfg, now, parts):
    """Read git-tracked memory folders through a sparse clone under <repo>/mirrors/<project>.

    Needed when a project's memory folder under ~/.claude is a symlink into a macOS-protected folder
    (unreadable on scheduled runs). Returns the set of project names covered. Never prompts for
    credentials (GIT_TERMINAL_PROMPT=0) and every git call has a timeout.
    """
    covered = set()
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    for m in cfg.get("memory_mirrors", []):
        project, url, sub = m.get("project"), m.get("repo"), m.get("path", ".claude/memory")
        if not (project and url):
            continue
        dest = repo / "mirrors" / project
        try:
            if not (dest / ".git").exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                r = subprocess.run(["git", "clone", "-q", "--depth", "1", "--filter=blob:none", "--sparse", url, str(dest)],
                                   capture_output=True, text=True, timeout=120, env=env)
                if r.returncode != 0:
                    parts.append(f"# Memory index: {project}\n(mirror clone failed: {r.stderr.strip()[:160]})\n")
                    continue
                subprocess.run(["git", "-C", str(dest), "sparse-checkout", "set", sub], capture_output=True, text=True, timeout=60, env=env)
            r = subprocess.run(["git", "-C", str(dest), "pull", "-q", "--ff-only"], capture_output=True, text=True, timeout=90, env=env)
            note = "" if r.returncode == 0 else f" (mirror pull failed, showing last mirrored state: {r.stderr.strip()[:120]})"
        except (OSError, subprocess.TimeoutExpired) as e:
            parts.append(f"# Memory index: {project}\n(mirror unavailable: {type(e).__name__})\n")
            continue
        mem = dest / sub
        idx = mem / "MEMORY.md"
        parts.append(f"# Memory index: {project} (git mirror{note})\n")
        parts.append(idx.read_text()[:CAP_INDEX] if idx.exists() else "(no MEMORY.md in mirror)")
        fresh = [q for q in glob.glob(str(mem / "*.md")) if not q.endswith("MEMORY.md") and now - os.path.getmtime(q) < DAYS_MEM]
        for q in sorted(fresh, key=os.path.getmtime, reverse=True)[:MAX_FRESH]:
            parts.append(f"\n## Recently updated memory: {Path(q).name} ({datetime.fromtimestamp(os.path.getmtime(q)).date()})\n")
            parts.append(Path(q).read_text()[:CAP_FILE])
        parts.append("\n")
        covered.add(project)
    return covered


def collect_memory(now, repo=None, cfg=None):
    parts = []
    covered = mirror_memory(repo, cfg, now, parts) if (repo is not None and cfg) else set()
    for mem in sorted(glob.glob(str(CLAUDE / "projects" / "*" / "memory"))):
        project = project_name(Path(mem).parent)
        if project in covered:
            continue  # served by the git mirror above
        if not readable(mem):
            parts.append(f"# Memory index: {project}\n(not read on scheduled runs: folder resolves into a macOS-protected location)\n")
            continue
        idx = Path(mem) / "MEMORY.md"
        parts.append(f"# Memory index: {project}\n")
        parts.append(idx.read_text()[:CAP_INDEX] if idx.exists() else "(no MEMORY.md)")
        fresh = [p for p in glob.glob(os.path.join(mem, "*.md")) if not p.endswith("MEMORY.md") and now - os.path.getmtime(p) < DAYS_MEM and readable(p)]
        for p in sorted(fresh, key=os.path.getmtime, reverse=True)[:MAX_FRESH]:
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
    d = Path(os.environ.get("BOARD_BRIEFS_DIR", HOME / ".local" / "share" / "briefs"))   # not under ~/Downloads: launchd cannot read it
    parts = []
    try:
        briefs = sorted(glob.glob(str(d / "*-am.md")) + glob.glob(str(d / "*-pm.md")), key=os.path.getmtime, reverse=True)
    except OSError:
        return "(briefs folder not readable in this context)"
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
    if os.environ.get("BOARD_SCHEDULED"):
        # Under launchd a read of ~/Downloads can BLOCK on a consent prompt that never appears (macOS TCC), not just fail.
        return "(~/Downloads scan skipped: BOARD_SCHEDULED is set on launchd runs; deliverables are read on terminal runs)"
    try:
        files = [p for p in glob.glob(str(HOME / "Downloads" / "*.md")) if now - os.path.getmtime(p) < DAYS3]
    except OSError as e:  # launchd agents cannot read ~/Downloads on macOS (TCC)
        return f"(~/Downloads not readable in this context: {e.__class__.__name__}; deliverables are read only when run from a terminal)"
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
    # Watchdog: a blocked syscall (e.g. a macOS privacy prompt that cannot be shown under launchd) would hang forever;
    # after 240 s dump the exact Python line to stderr and exit non-zero so the run fails fast with evidence.
    faulthandler.dump_traceback_later(240, exit=True, file=sys.stderr)
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

    def step(name):
        print(f"collector: {name} ...", file=sys.stderr, flush=True)

    def put(name, text, status="ok", detail=""):
        (ctx / name).write_text(text)
        manifest["inputs"][name] = {"status": status, "bytes": len(text.encode()), "detail": detail}

    put("run.json", json.dumps(run, indent=1))
    step("sessions"); sessions = collect_sessions(now)
    put("sessions.json", json.dumps(sessions, indent=1), detail=f"{sum(1 for s in sessions if s['status']=='live')} live")
    cfg_path = repo / "config" / "lines.json"
    cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    step("memory"); put("memory.md", collect_memory(now, repo, cfg))
    step("github"); gh_text, gh_status = collect_github()
    put("github.md", gh_text, gh_status)
    step("briefs"); put("briefs.md", collect_briefs())
    step("downloads"); dl = collect_downloads(now)
    put("downloads.md", dl, "blocked" if dl.startswith("(~/Downloads") else "ok")
    step("satellite"); sat_text, sat_status = collect_satellite(repo)
    put("satellite.md", sat_text, sat_status)
    step("ics"); rc, so, se = sh([sys.executable, str(repo / "bin" / "ics_events.py"), "--hours", "48"], timeout=90)
    try:
        ics = json.loads(so) if so.strip() else {"events": [], "errors": [se.strip()[:200]], "status": "failed"}
    except json.JSONDecodeError:
        ics = {"events": [], "errors": [f"unparseable: {so[:120]}"], "status": "failed"}
    put("ics-calendar.json", json.dumps(ics, indent=1, ensure_ascii=False), ics.get("status", "failed"), f"{len(ics.get('events', []))} events")
    step("watchers"); watchers = collect_watchers(repo)
    put("watchers.json", json.dumps(watchers, indent=1), detail=", ".join(f"{k}={v['status']}" for k, v in watchers.items()))
    snaps = sorted(glob.glob(str(repo / "data" / "snapshots" / "*.json")), key=os.path.getmtime)
    put("previous-board.json", Path(snaps[-1]).read_text() if snaps else "{}", "ok" if snaps else "none")
    # One concatenated file: the summarizer reads it in a single turn (each extra turn re-reads the growing context).
    order = ["run.json", "sessions.json", "memory.md", "github.md", "briefs.md", "downloads.md", "satellite.md", "ics-calendar.json", "watchers.json", "previous-board.json"]
    bundle = []
    for name in order:
        f = ctx / name
        if f.exists():
            bundle.append(f"\n\n===== {name} =====\n")
            bundle.append(f.read_text())
    (ctx / "bundle.md").write_text("".join(bundle))
    manifest["inputs"]["bundle.md"] = {"status": "ok", "bytes": sum(len(b.encode()) for b in bundle), "detail": "all inputs concatenated"}
    (ctx / "manifest.json").write_text(json.dumps(manifest, indent=1))
    last.write_text(json.dumps(run))
    for k, v in manifest["inputs"].items():
        print(f"{k:<22} {v['status']:<15} {v['bytes']:>7} B  {v['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
