# report_site — Monitor Specification

## Nomenclature

This simulation framework decomposes work into five plugin slots, each living in its own directory under the framework root and (typically) its own git repository:

- **SOI** (system of interest) — the focal system being modeled. Lives in `soi/<name>/`. Typically a business or organization with employees, departments, and internal processes.
- **Environment** — external actors that drive inputs into the SOI. Lives in `environment/<name>/`. Generates leads, customer responses, market conditions, or any other stimulus the SOI would receive from the outside world. Sim workers only — no employees, no payroll.
- **Monitors** — read-only time-series capture. Each monitor samples sim state at intervals and writes timestamped snapshots to its own output store under `sim_data/`. Lives in `monitors/<name>/`. Runs in-process via a singleton monitor worker the framework auto-injects when monitors are present. Purpose: preserve trajectory information (how state *changed* over time) that current-state databases lose.
- **Agents** — cron-triggered standalone processes spawned as subprocesses on a sim-time schedule. Lives in `agents/<name>/`. Can be read-only (e.g. report generators with LLM narrative) or read+write (decision-makers, async worker replacements). Heavy work is fine — subprocesses don't stall the sim tick. Agents often consume monitor outputs alongside current state for their analyses.
- **Utilities** — shared Python helpers importable across slots. Lives in `utilities/`. Generic functions (e.g. notes generators, scoring algorithms) that any module can call.

### What is a monitor

A **monitor** is a read-only, framework-integrated observer of simulation state. Two typical patterns:

1. **Time-series capture** — sample sim state at intervals to preserve trajectory information that point-in-time databases lose. Example: querying `inventory_database` at end-of-sim tells you what inventory IS at that moment. It does not tell you how inventory changed hour by hour. A monitor that samples `inventory_database` every sim-hour and writes timestamped rows to its own output store gives you the trajectory.

2. **LLM-narrative reports or dashboards** — periodic markdown/HTML outputs that summarize current state for human readers. Heavier per fire but acceptable for infrequent schedules (e.g. daily, weekly).

**The read-only invariant.** A monitor does NOT mutate SOI or environment state. It reads from those databases freely; it writes only to its *own* output store (a monitor-owned SQLite database, markdown file, or HTML file under `sim_data/`). The constraint is that monitor output is never consumed as a sim input — removing a monitor leaves the simulation's state evolution identical. **This is what defines a monitor.**

**Sequential dispatch.** Monitors run **in-process** via a singleton monitor worker the framework auto-injects when any `monitors/<name>/monitor.py` exists. Multiple monitors firing on the same tick run sequentially, in registration order. The wall-clock cost of a tick is the sum of all monitors firing on it — so design schedules to avoid same-tick clusters of heavy monitors, OR accept the stall on infrequent firings.

**Monitor vs agent — when to pick which:**
- Anything that observes-only (reports, dashboards, time-series capture) → **monitor**
- Anything that takes action affecting sim state (decision-makers, async worker replacements, executive forecasters) → **agent** (read+write, runs as subprocess so heavy work doesn't block ticks)

For technical conventions on how monitors hook into the framework, see `FRAMEWORK_CONTEXT.md` "Monitors" section.

## 1. Overview

`report_site` renders a single-page, self-contained HTML dashboard that summarizes the whole simulation in one browsable file. It builds a tabbed "control room" view with: a Summary tab carrying headline metric cards (cash with trend, lifetime revenue/COGS/debt/inventory/AR/AP, pay periods settled, total gross payroll, lifetime cost-per-firing and utilization, customers in pipeline with trend) and Chart.js graphs of customer-pipeline counts over time, order value by stage, the cash/savings/debt trajectory, other finance flows, payroll by pay period, and worker efficiency by pay period; collapsible browsable tabs that render every pipeline, finance, payroll, and efficiency markdown report produced by the sibling report monitors; and a Worker Activity tab with a per-worker rollup (firings, ✓/∅ work counts, active vs off-shift hours) drilling down per task. Chart data comes partly from databases the monitor reads directly and partly from the structured `SNAPSHOT` JSON embedded in the pipeline reports. It is scheduled to fire the hour after the 23:00 markdown reports so it picks up the freshly written files.

## 2. Schedule

`SCHEDULE = "0 0 * * *"` — daily at 00:00 (midnight) sim-time. The minute field is parsed but ignored (the sim clock advances in whole hours). Midnight is deliberately the hour **after** the 23:00 markdown report monitors (`finance_report`, `pipeline_report`, `payroll_report`, `efficiency_report`), so this run aggregates the markdown those monitors just wrote rather than yesterday's.

## 3. Inputs

Per fire the monitor reads:

- `superwidget.finance_database` (`finance` table) — the full time series (`date_time`, `cash_assets`, `savings`, `debt`, `inventory`, `accounts_receivable`, `accounts_payable`, `cogs`, `revenue`, `total_labor`, `in_process` ordered by `date_time`) for the metric cards and the cash and finance-flow charts.
- `superwidget.payroll_database` (`payroll_summary` table) — per-pay-period sums of gross/federal tax/state tax/health/union/net for the payroll chart and metrics.
- `superwidget.task_log_database` (`task_log` table) — the per-worker/per-task activity rollup (firings, `work_count`, `no_work_count`, `duration_hours`, excluding `phased_end` rows) for the Worker Activity tab, and the productive/bounce firing counts re-joined against payroll period windows for the efficiency chart.
- Pipeline data — derived from the `SNAPSHOT` JSON embedded in the pipeline reports (customer status counts and `orders_value` by status) rather than re-queried from the pipeline databases.
- The markdown reports written by the sibling monitors, read from `sim_data/reports/`: `finance_report_*.md`, `pipeline_report_*.md` (including their embedded `SNAPSHOT` comment), `payroll_*.md`, and `efficiency_*.md`. Each is converted to HTML for its collapsible browse view.

`framework/run.log` is referenced in code comments but only as context; it is not parsed into the dashboard.

## 4. Output store

This is a dashboard monitor, not a time-series DB sampler — it does **not** write a per-monitor `monitor_report_site.db` SQLite store. It writes a single self-contained HTML file (overwritten in full each fire), pulling Chart.js from a CDN at view time:

- `sim_data/reports/index.html`

The monitor creates `sim_data/reports/` on first fire.

## 5. Sample shape

Rather than DB rows, each fire (re)writes one HTML file with this structure:

- A sticky top nav with tabs: **Summary**, **Pipeline Reports (n)**, **Finance Reports (n)**, **Payroll Reports (n)**, **Efficiency Reports (n)**, **Worker Activity** (counts reflect how many reports of each kind were found).
- **Summary** tab — a grid of metric cards (each a label, value, and optional up/down/flat delta), an optional executive-summary narrative block, and six `<canvas>` Chart.js graphs: customer pipeline over time (line), order value by stage (stacked bar), cash position over time (line), other finance flows (line), payroll by pay period (stacked bar), and worker efficiency by pay period (dual-axis line: cost/firing and utilization %). Chart series are emitted into the page as inline JSON.
- **Pipeline / Finance / Payroll / Efficiency** tabs — each renders its set of reports as `<details>`/`<summary>` collapsibles (label from the report's sim time or pay-period range; body is the report markdown converted to HTML, with the `SNAPSHOT` comment stripped).
- **Worker Activity** tab — one collapsible per worker summarizing total firings, ✓/∅ counts, and active vs off-shift hours, expanding to a per-task table (task, firings, ✓, ∅, hours).

## 6. Downstream consumers

`index.html` is a terminal human-facing artifact — opened in a browser (or published via the module's `publish.sh`). Nothing in the sim consumes it.

**Monitor-output dependency (note for future authors):** unusually for a monitor, `report_site` consumes the *outputs of other monitors*. It reads the markdown reports written by the sibling monitors `finance_report`, `pipeline_report`, `payroll_report`, and `efficiency_report` (from `sim_data/reports/`), and it relies on the `SNAPSHOT` JSON that `pipeline_report` embeds in its files for the pipeline charts. This is why its schedule (00:00) is placed one hour after those monitors' 23:00 schedule. The dependency is one-directional and read-only — it does not feed back into sim state, preserving the monitor read-only invariant — but it does mean that changing the report filenames, header formats, or the embedded `SNAPSHOT` shape in those sibling monitors will break this monitor's parsing.

## 7. Runtime Config Parameters

None.

## 8. Cost

Cheaper than the LLM-narrative report monitors: the bulk of the work is SQLite reads, markdown-to-HTML conversion, and HTML string assembly — sub-second to a couple of seconds depending on how many reports have accumulated. It fires alone at the 00:00 tick (no sibling monitors share that hour), so there is no same-tick clustering.

Honest note on the LLM: the code path for the Summary tab's executive-summary narrative *does* make one Claude Haiku call (`max_tokens=1200`) when `ANTHROPIC_API_KEY` is set, adding a few seconds; if the key is absent the narrative block is simply omitted and the rest of the dashboard renders unchanged. So the dashboard itself is the cheap, no-LLM core, with an optional LLM-written summary blurb layered on when a key is available.
