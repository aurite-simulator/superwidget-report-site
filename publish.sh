#!/usr/bin/env bash
# Publish sim_data/reports/index.html to the gh-pages branch of this
# repo, served via GitHub Pages. Idempotent: if the file hasn't changed
# since the last publish, no commit is made.
#
# Workflow:
#   bash run.sh                                          # sim runs
#   venv/bin/python3 agents/report_site/agent.py         # optional manual rebuild
#   bash agents/report_site/publish.sh                   # push to gh-pages
#
# One-time setup: in the repo on GitHub, enable Settings → Pages →
# Source "Deploy from a branch", Branch "gh-pages", Folder "/".
# After that, the site lives at:
#   https://aurite-simulator.github.io/superwidget-report-site/

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INDEX_HTML="$SCRIPT_DIR/../../sim_data/reports/index.html"
SITE_URL="https://aurite-simulator.github.io/superwidget-report-site/"

if [ ! -f "$INDEX_HTML" ]; then
    echo "[publish] index.html not found at $INDEX_HTML" >&2
    echo "[publish] Run the agent first:" >&2
    echo "[publish]   venv/bin/python3 agents/report_site/agent.py" >&2
    exit 1
fi

cd "$SCRIPT_DIR"

# Pick up the remote gh-pages branch if it exists (no-op if it doesn't).
git fetch origin gh-pages 2>/dev/null || true

WORKTREE="$(mktemp -d)"
cleanup() {
    cd "$SCRIPT_DIR"
    git worktree remove --force "$WORKTREE" 2>/dev/null || rm -rf "$WORKTREE"
}
trap cleanup EXIT

if git show-ref --quiet refs/remotes/origin/gh-pages; then
    # gh-pages exists remotely — check it out (creates or resets local branch)
    git worktree add -B gh-pages "$WORKTREE" origin/gh-pages
elif git show-ref --quiet refs/heads/gh-pages; then
    # local-only gh-pages (rare)
    git worktree add "$WORKTREE" gh-pages
else
    # First publish ever — create an orphan branch with no history
    git worktree add --orphan -b gh-pages "$WORKTREE"
    (cd "$WORKTREE" && git rm -rf . >/dev/null 2>&1 || true)
fi

cp "$INDEX_HTML" "$WORKTREE/index.html"

cd "$WORKTREE"
git add index.html
if git diff --cached --quiet; then
    echo "[publish] index.html unchanged since last publish — nothing to push."
    echo "[publish] Site URL: $SITE_URL"
else
    git commit -m "Publish $(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
    git push -u origin gh-pages
    echo
    echo "[publish] Published — site URL:"
    echo "  $SITE_URL"
    echo
    echo "[publish] First time? Enable GitHub Pages in the repo settings:"
    echo "  Settings → Pages → Source: Deploy from a branch"
    echo "                    Branch: gh-pages, Folder: /"
fi
