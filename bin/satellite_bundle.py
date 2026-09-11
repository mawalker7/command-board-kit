#!/usr/bin/env python3
"""satellite_bundle.py: bundle this machine's local context for the hub (no LLM, no secrets).

Writes data/satellite/<hostname>/bundle.md and sessions.json in the command-board repo:
sessions (live + recent, with first prompts), memory indexes + recently updated memory files,
recent markdown deliverables in ~/Downloads, and unpushed work in the known repos.
"""

import glob
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_local as cl  # noqa: E402

REPOS_FILE = Path(__file__).resolve().parent.parent / "config" / "repos.json"   # {"repos": ["~/Documents/GitHub/x", ...]}


def unpushed(repo_paths):
    out = []
    for r in repo_paths:
        p = Path(os.path.expanduser(r))
        if not (p / ".git").exists() and not (p / ".git").is_file():
            continue
        rc, branch, _ = cl.sh(["git", "-C", str(p), "rev-parse", "--abbrev-ref", "HEAD"], timeout=20)
        rc2, status, _ = cl.sh(["git", "-C", str(p), "status", "--porcelain"], timeout=20)
        rc3, ahead, _ = cl.sh(["git", "-C", str(p), "log", "--oneline", "@{u}..HEAD"], timeout=20)
        dirty = len(status.strip().splitlines()) if rc2 == 0 else "?"
        ahead_n = len(ahead.strip().splitlines()) if rc3 == 0 else "no upstream"
        out.append(f"- {p.name}: branch {branch.strip() or '?'}, {dirty} dirty file(s), unpushed commits: {ahead_n}")
    return "\n".join(out) or "(no known repos on this machine)"


def main(argv=None):
    repo = Path(argv[0]).resolve() if argv else Path(__file__).resolve().parent.parent
    host = socket.gethostname()
    out_dir = repo / "data" / "satellite" / host
    out_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    sessions = cl.collect_sessions(now)
    (out_dir / "sessions.json").write_text(json.dumps(sessions, indent=1))
    parts = [f"# Satellite bundle: {host} at {datetime.fromtimestamp(now).isoformat(timespec='minutes')}", ""]
    parts.append("## Claude Code sessions on this machine")
    for s in sessions:
        parts.append(f"- [{s['status']}] {s['name']} · {s['project']} · last activity {s['last_activity']} · first prompt: {s['first_prompt'][:140]}")
    if not sessions:
        parts.append("- (none live or active in the last 24 h)")
    repos = json.loads(REPOS_FILE.read_text()).get("repos", []) if REPOS_FILE.exists() else []
    parts += ["", "## Unpushed work", unpushed(repos), "", "## Recent deliverables in ~/Downloads", cl.collect_downloads(now), "",
              "## Memory", cl.collect_memory(now)]
    text = "\n".join(parts)
    if len(text) > 60_000:
        text = text[:60_000] + "\n\n(bundle truncated at 60 KB)"
    (out_dir / "bundle.md").write_text(text)
    print(f"bundle written: {out_dir / 'bundle.md'} ({len(text)} chars, {len(sessions)} sessions)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
