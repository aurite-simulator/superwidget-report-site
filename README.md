# superwidget-report-site

A single-file HTML "control room" viewer for the [superwidget](https://github.com/aurite-simulator/superwidget-soi) simulation. After each sim-day (or whenever you ask), it walks every output of the run and renders one self-contained `index.html` you can open locally or publish to GitHub Pages.

## What you get

The page has six tabs:

| Tab | Contents |
|---|---|
| **Summary** | Chart.js graphs over sim time — customer pipeline counts by status, order value by stage, cash position (cash / savings / debt), other finance flows (revenue, COGS, AR, AP, total_labor, in_process), and payroll by pay period (stacked: net + fed tax + state tax + benefits). |
| **Pipeline Reports** | One collapsible `<details>` per daily pipeline report. Click to expand the full rendered markdown — status tables, file-queue depths, per-salesperson load, stuck-items watch, narrative. |
| **Finance Reports** | Same shape, for the daily finance reports. |
| **Payroll Reports** | Same shape, for the per-period payroll reports. |
| **Efficiency Reports** | Same shape, for the per-period efficiency reports (cost-per-productive-firing per worker). |
| **Worker Activity** | Per-worker rollup from `task_log_database` — total firings, ✓ vs ∅ split, hours on-shift vs off-shift, and a per-task breakdown. |

The HTML is fully self-contained (one file, ~200 KB). Chart.js is loaded from CDN at view time.

## Quick start

Assuming you already have a `superwidget` simulation set up (see the [SOI README](https://github.com/aurite-simulator/superwidget-soi)):

```bash
# Clone this monitor into the framework's monitors/ directory:
git clone https://github.com/aurite-simulator/superwidget-report-site monitors/report_site

# Install dependencies (markdown package):
bash setup.sh
# or just:  venv/bin/pip install -r agents/report_site/requirements.txt

# Run a simulation:
bash run.sh

# Rebuild the viewer on demand (cron also rebuilds it at every sim-midnight):
venv/bin/python3 agents/report_site/agent.py

# Open the resulting file:
xdg-open sim_data/reports/index.html      # Linux
open sim_data/reports/index.html          # macOS
```

That's the full local workflow. To make the page accessible to others, see "Publish to GitHub Pages" below.

## When the page updates

The SOI's `crontab` includes an entry that fires this agent at sim-midnight every day, right after the daily pipeline and finance reports are written:

```
0 0 * * *    $MODULE_DIR/../../venv/bin/python3 $MODULE_DIR/../../agents/report_site/agent.py
```

So during a long sim run, the page refreshes itself daily. You can also rebuild on demand anytime — the agent is idempotent (every run replaces `index.html` from current state of the databases and report files).

The page is **local only** until you publish it. The cron firing writes `sim_data/reports/index.html` but does not push to GitHub Pages — that step is manual.

## Output location

```
<framework>/sim_data/reports/index.html
```

Open in any browser via `file://`. The sim_data directory is wiped by `setup_redis.py` on each fresh sim init, so the file is regenerated from scratch on the first cron firing (or your first manual `agent.py` invocation) after init.

## Publish to GitHub Pages

`publish.sh` pushes the current `index.html` to this repo's `gh-pages` branch. GitHub Pages serves it at:

```
https://aurite-simulator.github.io/superwidget-report-site/
```

### One-time setup

1. Run a sim and rebuild the viewer at least once so `sim_data/reports/index.html` exists.
2. Run `bash agents/report_site/publish.sh`. It creates the `gh-pages` orphan branch and pushes the initial commit.
3. On GitHub: go to the [Pages settings](https://github.com/aurite-simulator/superwidget-report-site/settings/pages):
   - Source: **Deploy from a branch**
   - Branch: **`gh-pages`**
   - Folder: **`/`**
   - Save.
4. Within ~1 minute the site goes live at the URL above.

### Ongoing workflow

```bash
bash run.sh                                # sim runs, agent rebuilds index.html at midnight
venv/bin/python3 agents/report_site/agent.py   # optional manual rebuild
bash agents/report_site/publish.sh             # push to gh-pages → live site
```

`publish.sh` is idempotent: if `index.html` is byte-identical to what's already on `gh-pages`, no commit is created and nothing is pushed.

### How `publish.sh` works (briefly)

Uses `git worktree add` to check out the `gh-pages` branch into a temporary directory without disturbing your main checkout. Copies `index.html` in, commits, pushes, cleans up. The `gh-pages` branch is an orphan (no shared history with `main`) since it only contains the published file; one commit per publish builds a history of past dashboards.

### Privacy

Public repo + GitHub Pages = anyone with the URL can view the page. The dashboard embeds:

- Synthetic simulation data (faker-generated names, addresses, etc.)
- Dollar figures (gross/net pay totals, transaction amounts, finance metrics)
- Per-salesperson load summaries (real-looking employee names tied to customer counts)
- LLM-generated narrative sections from `finance_report` and `pipeline_report` (Haiku writes these — they can occasionally include speculation about company performance)

For a public framework demo that's fine. If you'd rather keep the data private:

- Make the repo private (GitHub Pro required for private Pages), or
- Skip `publish.sh` entirely and just view `index.html` locally via `file://`.

## Configuration

The agent reads from environment variables (no `runtime_config.toml` of its own):

| Variable | Required? | Set by | Used for |
|---|---|---|---|
| `MODULE_DIR` | Yes (one of) | Cron worker | Locates `sim_data/` two parents up |
| `DATA_DIR` | Yes (one of) | You | Locates `sim_data/` for manual runs |

The cron-launch path automatically sets `MODULE_DIR`. For manual invocation, the agent finds the right path relative to its own location, so you don't usually need to set anything — just run it from the framework root.

No `ANTHROPIC_API_KEY` needed — this agent doesn't call any LLM. It only reads SQLite files and renders markdown.

## Dependencies

```
markdown>=3.5    # renders the source .md report bodies to HTML
```

Chart.js is loaded from `https://cdn.jsdelivr.net/npm/chart.js` at view time (no Python dep, no offline support unless you've cached it via prior page visits).

## Troubleshooting

**`index.html` not found when running publish.sh** — Run the agent first to generate it: `venv/bin/python3 agents/report_site/agent.py`.

**`task_log empty — no activity to summarize yet`** in the Worker Activity tab — Either the sim hasn't started yet, or you're looking at the page generated before `setup_redis.py` reseeded. Re-run the agent after the sim has produced some task firings.

**Charts are empty on Summary tab** — The pipeline charts come from `<!-- SNAPSHOT: {...} -->` JSON embedded in pipeline_report markdown files. If `pipeline_report` hasn't fired yet (it runs at 23:00 sim-time daily), there's nothing to chart. The finance charts come from `finance_database`, which `update_books` populates every sim hour, so those should show data quickly.

**`publish.sh` fails with `git worktree add --orphan` error** — `--orphan` requires git 2.38+. Update git, or manually push to `gh-pages` the first time using a temporary clone.

**Page renders but graphs don't show** — Check the browser console; usually it's a CDN problem (no internet, or Chart.js failed to load). The dashboard is otherwise functional — text reports and worker activity still render server-side from the embedded HTML.

## Source data — what gets read

```
sim_data/reports/finance_report_*.md         # rendered into Finance Reports tab
sim_data/reports/pipeline_report_*.md        # rendered + snapshot extracted for charts
sim_data/reports/payroll_*.md                # rendered into Payroll Reports tab
sim_data/finance_database.db                 # time series for cash/AR/AP/etc. charts
sim_data/payroll_database.db                 # per-period totals for payroll chart
sim_data/task_log_database.db                # per-worker per-task rollup
```

The agent is **read-only** with respect to simulation state — it never writes to any simulation database or Redis key. It only writes `index.html`.

## How this fits with the other agents

| Agent | What it produces | When |
|---|---|---|
| `finance_report` | One `finance_report_<wall-time>.md` markdown file | Daily at 23:00 sim-time |
| `pipeline_report` | One `pipeline_report_<wall-time>.md` (+ embedded SNAPSHOT) | Daily at 23:00 sim-time |
| **`report_site`** | One `index.html` aggregating everything above | Daily at 00:00 sim-time (the hour after the others) |

The ordering matters — `report_site` rebuilds at midnight so it picks up whatever the 23:00 cron jobs just wrote. Each agent is independent and can be skipped without breaking the others.
