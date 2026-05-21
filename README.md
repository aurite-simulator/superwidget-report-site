# superwidget-report-site

Single-file HTML "control room" viewer for the [superwidget](https://github.com/aurite-simulator/superwidget-model) simulation. Aggregates every markdown report (finance, pipeline, payroll), worker activity rolled up from the task event log, and Chart.js graphs of pipeline + finance + payroll metrics over sim time into one self-contained `index.html` page.

Five tabs:

- **Summary** — Chart.js graphs of customer pipeline over time, order value by stage, cash position, finance flows, and payroll by pay period.
- **Pipeline Reports** — each daily pipeline report as a collapsible block.
- **Finance Reports** — same, for the daily finance reports.
- **Payroll Reports** — same, for the per-period payroll reports.
- **Worker Activity** — per-worker rollup from `task_log_database`: total firings, ✓ vs ∅ counts, active vs off-shift hours, and a per-task breakdown.

## Install

Cloned alongside the model and other agents into a single framework instance:

```bash
git clone https://github.com/aurite-simulator/framework framework
cd framework

git clone https://github.com/aurite-simulator/superwidget-model            models/superwidget
git clone https://github.com/aurite-simulator/superwidget-utilities        utilities
git clone https://github.com/aurite-simulator/superwidget-finance-report   agents/finance_report
git clone https://github.com/aurite-simulator/superwidget-pipeline-report  agents/pipeline_report
git clone https://github.com/aurite-simulator/superwidget-report-site      agents/report_site

bash setup.sh
```

## How it runs

The framework's cron worker fires the agent automatically based on the entry in `models/superwidget/crontab`. The default schedule rebuilds the page once per sim-day at midnight (the hour after the daily pipeline/finance reports are written):

```
0 0 * * *    $MODEL_DIR/../../venv/bin/python3 $MODEL_DIR/../../agents/report_site/agent.py
```

You can also run it manually anytime — output is idempotent:

```bash
cd <framework root>
venv/bin/python3 agents/report_site/agent.py
```

## Output

Writes `<framework>/model_data/reports/index.html`. Open in any browser:

```
file:///<absolute-path>/model_data/reports/index.html
```

Chart.js is loaded from CDN (needs internet on first view; browser-cached after).

## Configuration

Reads from the environment:

| Variable | Required? | Used for |
|---|---|---|
| `MODEL_DIR` | Set by cron | Locates `model_data/` two levels up |
| `DATA_DIR` | Yes (if no `MODEL_DIR`) | Locates `model_data/` for manual runs |

No API key needed — the agent is purely a renderer.

The agent is **read-only** with respect to simulation state; it only writes `index.html`.
