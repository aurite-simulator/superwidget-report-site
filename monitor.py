"""report_site monitor.

Builds a single-file HTML "control room" viewer at sim_data/reports/index.html
that aggregates every markdown report (finance, pipeline, payroll, efficiency),
worker activity rolled up from task_log_database, and Chart.js graphs of
pipeline + finance + payroll + efficiency metrics over sim time.

Fires daily at 00:00 sim-time via the framework's monitor worker (the hour
after the daily report-generating monitors at 23:00, so this run picks up
the freshly-written markdown).

Reads:
  sim_data/reports/finance_report_*.md
  sim_data/reports/pipeline_report_*.md   (including embedded SNAPSHOT JSON)
  sim_data/reports/payroll_*.md
  sim_data/reports/efficiency_*.md
  sim_data/finance_database.db            (time series of finance state)
  sim_data/payroll_database.db            (pay period rollups)
  sim_data/task_log_database.db           (per-worker per-task firings)
  framework/run.log                         (full text log — referenced only)

Writes:
  sim_data/reports/index.html
"""

import html as html_module
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import markdown

import db


SCHEDULE = "0 0 * * *"   # daily at 00:00 sim-time


# ─── Path resolution ─────────────────────────────────────────────────────

DATA_DIR = Path(db.DATA_DIR)
FRAMEWORK_DIR = DATA_DIR.parent
REPORTS_DIR = DATA_DIR / "reports"
RUN_LOG = FRAMEWORK_DIR / "run.log"
OUTPUT = REPORTS_DIR / "index.html"


# ─── Report parsing ──────────────────────────────────────────────────────

_SIM_TIME_RX = re.compile(r"sim time[\s—-]*([0-9T:+\-]{10,})")
_PERIOD_HEADER_RX = re.compile(
    r"Pay Period\s+([0-9\-]{10})\s+to\s+([0-9\-]{10})", re.IGNORECASE
)
_SNAPSHOT_RX = re.compile(r"<!--\s*SNAPSHOT:\s*(\{.*?\})\s*-->", re.DOTALL)


def _md_to_html(text):
    text = _SNAPSHOT_RX.sub("", text)
    return markdown.markdown(text, extensions=["tables", "fenced_code"])


def _extract_sim_time(text):
    m = _SIM_TIME_RX.search(text)
    return m.group(1) if m else ""


def _extract_period_range(text):
    m = _PERIOD_HEADER_RX.search(text)
    return (m.group(1), m.group(2)) if m else ("", "")


def _extract_snapshot(text):
    m = _SNAPSHOT_RX.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _read_reports(glob_pattern):
    if not REPORTS_DIR.exists():
        return []
    items = []
    for f in sorted(REPORTS_DIR.glob(glob_pattern)):
        raw = f.read_text(encoding="utf-8", errors="replace")
        items.append({"filename": f.name, "raw": raw, "html": _md_to_html(raw)})
    return items


def load_finance_reports():
    items = _read_reports("finance_report_*.md")
    for it in items:
        it["label"] = _extract_sim_time(it["raw"]) or it["filename"]
    items.sort(key=lambda x: x["label"])
    return items


def load_pipeline_reports():
    items = _read_reports("pipeline_report_*.md")
    for it in items:
        it["label"] = _extract_sim_time(it["raw"]) or it["filename"]
        it["snapshot"] = _extract_snapshot(it["raw"])
    items.sort(key=lambda x: x["label"])
    return items


def load_payroll_reports():
    items = _read_reports("payroll_*.md")
    for it in items:
        start, end = _extract_period_range(it["raw"])
        it["label"] = f"{start} → {end}" if start else it["filename"]
    items.sort(key=lambda x: x["filename"])
    return items


def load_efficiency_reports():
    items = _read_reports("efficiency_*.md")
    for it in items:
        start, end = _extract_period_range(it["raw"])
        it["label"] = f"{start} → {end}" if start else it["filename"]
    items.sort(key=lambda x: x["filename"])
    return items


# ─── Database series ─────────────────────────────────────────────────────

def _connect(name):
    path = DATA_DIR / name
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def load_finance_series():
    conn = _connect("finance_database.db")
    if conn is None:
        return []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT date_time, cash_assets, savings, debt, inventory, "
            "       accounts_receivable, accounts_payable, cogs, revenue, "
            "       total_labor, in_process "
            "FROM finance ORDER BY date_time"
        )]
    finally:
        conn.close()
    return rows


def load_payroll_series():
    conn = _connect("payroll_database.db")
    if conn is None:
        return []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT pay_period_id, "
            "       SUM(gross_pay)       AS gross, "
            "       SUM(federal_tax)     AS fed_tax, "
            "       SUM(state_tax)       AS state_tax, "
            "       SUM(health_benefits) AS health, "
            "       SUM(union_fees)      AS union_fees, "
            "       SUM(net_pay)         AS net "
            "FROM payroll_summary GROUP BY pay_period_id ORDER BY pay_period_id"
        )]
    finally:
        conn.close()
    return rows


def load_efficiency_series():
    """One row per pay_period_id with total gross pay + productive firings
    across all workers in that period's date window. cost_per_firing and
    utilization are computed in Python after joining the two databases.
    """
    pay_conn = _connect("payroll_database.db")
    log_conn = _connect("task_log_database.db")
    if pay_conn is None or log_conn is None:
        if pay_conn: pay_conn.close()
        if log_conn: log_conn.close()
        return []
    try:
        periods = [dict(r) for r in pay_conn.execute(
            "SELECT pay_period_id, "
            "       MIN(period_start) AS period_start, "
            "       MAX(period_end)   AS period_end, "
            "       SUM(gross_pay)    AS gross "
            "FROM payroll_summary "
            "GROUP BY pay_period_id ORDER BY pay_period_id"
        )]
        out = []
        for p in periods:
            row = log_conn.execute(
                "SELECT COALESCE(SUM(work_count), 0)    AS productive, "
                "       COALESCE(SUM(no_work_count), 0) AS bounce "
                "FROM task_log WHERE sim_date BETWEEN ? AND ?",
                (p["period_start"], p["period_end"]),
            ).fetchone()
            productive = int(row["productive"] or 0)
            bounce = int(row["bounce"] or 0)
            total = productive + bounce
            out.append({
                "pay_period_id": p["pay_period_id"],
                "gross": float(p["gross"] or 0),
                "productive": productive,
                "bounce": bounce,
                "cost_per_firing": (float(p["gross"] or 0) / productive) if productive > 0 else None,
                "utilization": (productive / total) if total > 0 else None,
            })
        return out
    finally:
        pay_conn.close()
        log_conn.close()


def load_worker_activity():
    conn = _connect("task_log_database.db")
    if conn is None:
        return []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT worker_id, task_name, "
            "       COUNT(*)                       AS firings, "
            "       COALESCE(SUM(work_count), 0)   AS work, "
            "       COALESCE(SUM(no_work_count),0) AS no_work, "
            "       COALESCE(SUM(duration_hours),0) AS hours "
            "FROM task_log WHERE shape != 'phased_end' "
            "GROUP BY worker_id, task_name ORDER BY worker_id, firings DESC"
        )]
    finally:
        conn.close()
    return rows


def _summary_metrics(finance_series, payroll_series, efficiency_series,
                     pipeline_series):
    """Headline numbers covering the whole sim run so far. Returns a dict
    of label → (value, delta_or_None). delta is +/- prefix-formatted string,
    or None when no delta is meaningful.
    """
    metrics = {}

    if finance_series:
        first = finance_series[0]
        last = finance_series[-1]
        cash_delta = last["cash_assets"] - first["cash_assets"]
        metrics["Cash"] = (
            f"${last['cash_assets']:,.0f}",
            f"{'+' if cash_delta >= 0 else ''}${cash_delta:,.0f}",
        )
        metrics["Revenue (lifetime)"] = (f"${last['revenue']:,.0f}", None)
        metrics["COGS (lifetime)"]    = (f"${last['cogs']:,.0f}", None)
        metrics["Debt"]               = (f"${last['debt']:,.0f}", None)
        metrics["Inventory"]          = (f"${last['inventory']:,.0f}", None)
        metrics["Accounts receivable"] = (f"${last['accounts_receivable']:,.0f}", None)
        metrics["Accounts payable"]    = (f"${last['accounts_payable']:,.0f}", None)

    if payroll_series:
        total_gross = sum(r["gross"] or 0 for r in payroll_series)
        metrics["Pay periods settled"] = (str(len(payroll_series)), None)
        metrics["Total gross payroll"] = (f"${total_gross:,.0f}", None)

    if efficiency_series:
        lifetime_productive = sum(r["productive"] for r in efficiency_series)
        lifetime_bounce     = sum(r["bounce"]     for r in efficiency_series)
        lifetime_gross      = sum(r["gross"]      for r in efficiency_series)
        total_attempts = lifetime_productive + lifetime_bounce
        if lifetime_productive > 0:
            metrics["Cost / productive firing"] = (
                f"${lifetime_gross / lifetime_productive:,.2f}", None)
        if total_attempts > 0:
            metrics["Utilization (lifetime)"] = (
                f"{100 * lifetime_productive / total_attempts:.1f}%", None)

    # Customer pipeline — latest snapshot + delta from first snapshot.
    customer_rows = pipeline_series.get("customers") or []
    if customer_rows:
        sim_times = []
        for r in customer_rows:
            if r["sim_time"] not in sim_times:
                sim_times.append(r["sim_time"])
        first_t, last_t = sim_times[0], sim_times[-1]
        latest_total = sum(r["count"] for r in customer_rows if r["sim_time"] == last_t)
        first_total  = sum(r["count"] for r in customer_rows if r["sim_time"] == first_t)
        delta = latest_total - first_total
        metrics["Customers in pipeline"] = (
            f"{latest_total:,}",
            f"{'+' if delta >= 0 else ''}{delta:,}",
        )

    return metrics


def _render_metrics_block(metrics):
    if not metrics:
        return "<p class='meta'>No data yet — run a simulation first.</p>"
    cards = []
    for label, (value, delta) in metrics.items():
        delta_html = ""
        if delta is not None:
            cls = "up" if delta.startswith("+") else ("down" if delta.startswith("-") else "flat")
            delta_html = f"<span class='metric-delta {cls}'>{html_module.escape(delta)}</span>"
        cards.append(
            f"<div class='metric-card'>"
            f"<div class='metric-label'>{html_module.escape(label)}</div>"
            f"<div class='metric-value'>{html_module.escape(value)}</div>"
            f"{delta_html}</div>"
        )
    return "<div class='metrics-grid'>" + "".join(cards) + "</div>"


def _summary_narrative(metrics, finance_series, payroll_series,
                       efficiency_series):
    """Ask Haiku for a 2-4 paragraph overview. Returns rendered Markdown
    HTML, or an empty string if anthropic / API key isn't available.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""
    try:
        from anthropic import Anthropic
    except ImportError:
        return ""

    metric_lines = "\n".join(
        f"- {label}: {value}" + (f" ({delta})" if delta else "")
        for label, (value, delta) in metrics.items()
    )
    timespan = ""
    if finance_series:
        timespan = (f"Finance snapshots from {finance_series[0]['date_time']} "
                    f"to {finance_series[-1]['date_time']}.")

    cost_lifetime_lines = []
    for r in efficiency_series:
        cpf = f"${r['cost_per_firing']:,.2f}" if r["cost_per_firing"] is not None else "n/a"
        util = f"{r['utilization']*100:.0f}%" if r["utilization"] is not None else "n/a"
        cost_lifetime_lines.append(
            f"- {r['pay_period_id']}: gross ${r['gross']:,.0f}, "
            f"productive {r['productive']}, bounce {r['bounce']}, "
            f"cost/firing {cpf}, util {util}"
        )
    cost_lifetime = "\n".join(cost_lifetime_lines) or "(no settled periods yet)"

    prompt = (
        "You are writing the executive summary at the top of an internal "
        "operating dashboard for a small widget-manufacturing simulation. "
        "Given the headline metrics (covering the whole simulation run so "
        "far) and the per-pay-period efficiency breakdown, write 2-4 short "
        "paragraphs in Markdown covering:\n"
        "  - overall trajectory: cash, revenue, and pipeline movement\n"
        "  - operational efficiency trend across pay periods (is cost/firing "
        "improving? Is utilization rising or falling?)\n"
        "  - one or two observations about where the business stands and "
        "what to watch next\n\n"
        f"{timespan}\n\n"
        "Headline metrics:\n"
        f"{metric_lines}\n\n"
        "Per-pay-period efficiency:\n"
        f"{cost_lifetime}\n"
    )
    client = Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1200,
        messages=[{"role": "user", "content": prompt}],
    )
    return _md_to_html(response.content[0].text.strip())


def derive_pipeline_series(pipeline_reports):
    series_customers = []
    series_orders_value = []
    for r in pipeline_reports:
        snap = r.get("snapshot") or {}
        sim_time = r["label"]
        for status, count in (snap.get("customers") or {}).items():
            series_customers.append({"sim_time": sim_time, "status": status, "count": count})
        for entry in (snap.get("orders_value") or []):
            series_orders_value.append({
                "sim_time": sim_time,
                "status": entry.get("status"),
                "count": entry.get("count"),
                "value": entry.get("value"),
            })
    return {"customers": series_customers, "orders_value": series_orders_value}


# ─── HTML assembly ───────────────────────────────────────────────────────

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Superwidget Simulation Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; color: #222; }}
  nav {{ background: #2c3e50; position: sticky; top: 0; z-index: 10; }}
  nav button {{ background: none; color: #ecf0f1; border: none;
                padding: 14px 22px; cursor: pointer; font-size: 15px;
                border-bottom: 3px solid transparent; }}
  nav button:hover {{ background: #34495e; }}
  nav button.active {{ background: #1a252f; border-bottom-color: #3498db; }}
  .tab-content {{ display: none; padding: 24px 32px; max-width: 1280px;
                  margin: 0 auto; }}
  .tab-content.active {{ display: block; }}
  h2 {{ color: #2c3e50; margin-top: 28px; border-bottom: 1px solid #eee;
        padding-bottom: 6px; }}
  details {{ background: #fafafa; border: 1px solid #ddd; border-radius: 4px;
             padding: 6px 14px; margin: 6px 0; }}
  details[open] {{ background: #fff; padding-bottom: 14px; }}
  summary {{ cursor: pointer; font-weight: 600; padding: 6px 0; }}
  table {{ border-collapse: collapse; margin: 12px 0; }}
  th, td {{ border: 1px solid #ddd; padding: 4px 10px; text-align: left; }}
  th {{ background: #f4f6f8; }}
  .chart-wrap {{ position: relative; height: 380px; margin: 16px 0; }}
  .meta {{ color: #666; font-size: 13px; }}
  details.worker > summary {{ font-family: "SF Mono", Consolas, monospace; font-size: 13px; }}
  table.activity {{ width: 100%; max-width: 720px; }}
  table.activity td:nth-child(n+2) {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
                   gap: 10px; margin: 12px 0 20px; }}
  .metric-card {{ background: #f4f6f8; border: 1px solid #e1e4e8; border-radius: 6px;
                  padding: 12px 14px; }}
  .metric-label {{ font-size: 12px; color: #555; text-transform: uppercase;
                   letter-spacing: 0.4px; }}
  .metric-value {{ font-size: 22px; font-weight: 600; margin-top: 4px;
                   font-variant-numeric: tabular-nums; color: #2c3e50; }}
  .metric-delta {{ font-size: 13px; font-variant-numeric: tabular-nums;
                   margin-top: 2px; display: inline-block; }}
  .metric-delta.up {{ color: #27ae60; }}
  .metric-delta.down {{ color: #c0392b; }}
  .metric-delta.flat {{ color: #7f8c8d; }}
  .narrative {{ background: #fafbfc; border-left: 3px solid #3498db;
                padding: 8px 16px; margin: 6px 0 24px; }}
  .narrative p {{ margin: 8px 0; line-height: 1.55; }}
</style>
</head>
<body>
<nav>
  <button data-tab="summary" class="active">Summary</button>
  <button data-tab="pipeline">Pipeline Reports ({pipeline_count})</button>
  <button data-tab="finance">Finance Reports ({finance_count})</button>
  <button data-tab="payroll">Payroll Reports ({payroll_count})</button>
  <button data-tab="efficiency">Efficiency Reports ({efficiency_count})</button>
  <button data-tab="activity">Worker Activity</button>
</nav>

<div id="summary" class="tab-content active">
  <h2>Overview</h2>
  {summary_metrics_html}
  {summary_narrative_html}

  <h2>Customer pipeline over time</h2>
  <p class="meta">From pipeline_report SNAPSHOT JSON. One line per customer status.</p>
  <div class="chart-wrap"><canvas id="chart-customers"></canvas></div>

  <h2>Order value by stage</h2>
  <p class="meta">From orders_value in pipeline snapshots (quantity × price by status).</p>
  <div class="chart-wrap"><canvas id="chart-orders-value"></canvas></div>

  <h2>Cash position over time</h2>
  <p class="meta">From finance_database — one row per update_books firing.</p>
  <div class="chart-wrap"><canvas id="chart-finance-cash"></canvas></div>

  <h2>Other finance flows</h2>
  <div class="chart-wrap"><canvas id="chart-finance-flow"></canvas></div>

  <h2>Payroll by pay period</h2>
  <p class="meta">Stacked: gross = net + federal tax + state tax + benefits.</p>
  <div class="chart-wrap"><canvas id="chart-payroll"></canvas></div>

  <h2>Worker efficiency by pay period</h2>
  <p class="meta">Cost per productive firing (gross pay ÷ task_log.work_count) and overall utilization (productive ÷ productive + bounce) across the period window.</p>
  <div class="chart-wrap"><canvas id="chart-efficiency"></canvas></div>
</div>

<div id="pipeline" class="tab-content">
  <h2>Pipeline reports ({pipeline_count})</h2>
  {pipeline_html}
</div>

<div id="finance" class="tab-content">
  <h2>Finance reports ({finance_count})</h2>
  {finance_html}
</div>

<div id="payroll" class="tab-content">
  <h2>Payroll reports ({payroll_count})</h2>
  {payroll_html}
</div>

<div id="efficiency" class="tab-content">
  <h2>Efficiency reports ({efficiency_count})</h2>
  {efficiency_html}
</div>

<div id="activity" class="tab-content">
  <h2>Worker activity</h2>
  <p class="meta">Per-worker rollup from <code>task_log_database</code>. Click a worker to expand the per-task breakdown. <strong>firings</strong> = number of dispatch sessions. <strong>✓ / ∅</strong> = number of sub-task iterations that did / did not produce useful work. For sub-hour tasks (e.g. 10-minute create_lead) one firing dispatches multiple iterations, so the ✓/∅ totals can exceed the firing count. Phased tasks are counted once per task instance (the start row), with full declared duration attributed.</p>
  {activity_html}
</div>

<script>
const TABS = document.querySelectorAll('nav button');
const PANELS = document.querySelectorAll('.tab-content');
TABS.forEach(b => b.addEventListener('click', () => {{
  TABS.forEach(x => x.classList.remove('active'));
  PANELS.forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  document.getElementById(b.dataset.tab).classList.add('active');
}}));

const customerSeries = {customer_series_json};
const ordersValueSeries = {orders_value_series_json};
const financeSeries = {finance_series_json};
const payrollSeries = {payroll_series_json};
const efficiencySeries = {efficiency_series_json};

function pivotByStatus(rows) {{
  const byStatus = {{}};
  const labels = [];
  for (const r of rows) {{
    if (!labels.includes(r.sim_time)) labels.push(r.sim_time);
    if (!byStatus[r.status]) byStatus[r.status] = {{}};
    byStatus[r.status][r.sim_time] = r.count;
  }}
  return {{ labels, byStatus }};
}}

const PALETTE = ['#3498db','#e74c3c','#2ecc71','#f39c12','#9b59b6',
                 '#1abc9c','#34495e','#e67e22','#7f8c8d','#16a085',
                 '#c0392b','#27ae60','#2980b9','#8e44ad','#d35400'];
const color = i => PALETTE[i % PALETTE.length];

(function() {{
  const {{ labels, byStatus }} = pivotByStatus(customerSeries);
  const datasets = Object.entries(byStatus).map(([status, vals], i) => ({{
    label: status,
    data: labels.map(t => vals[t] ?? 0),
    borderColor: color(i), backgroundColor: color(i),
    tension: 0.25, fill: false,
  }}));
  new Chart(document.getElementById('chart-customers'), {{
    type: 'line', data: {{ labels, datasets }},
    options: {{ responsive: true, maintainAspectRatio: false,
                interaction: {{ mode: 'index' }} }},
  }});
}})();

(function() {{
  const labels = [...new Set(ordersValueSeries.map(r => r.sim_time))];
  const statuses = [...new Set(ordersValueSeries.map(r => r.status))];
  const datasets = statuses.map((status, i) => ({{
    label: status,
    data: labels.map(t => {{
      const m = ordersValueSeries.find(r => r.sim_time === t && r.status === status);
      return m ? m.value : 0;
    }}),
    backgroundColor: color(i),
  }}));
  new Chart(document.getElementById('chart-orders-value'), {{
    type: 'bar', data: {{ labels, datasets }},
    options: {{ responsive: true, maintainAspectRatio: false,
                scales: {{ x: {{ stacked: true }},
                          y: {{ stacked: true,
                                ticks: {{ callback: v => '$' + Number(v).toLocaleString() }} }} }} }},
  }});
}})();

(function() {{
  const labels = financeSeries.map(r => r.date_time);
  const fields = [['cash_assets','#3498db'],['savings','#2ecc71'],['debt','#e74c3c']];
  const datasets = fields.map(([f, c]) => ({{
    label: f, data: financeSeries.map(r => r[f]),
    borderColor: c, backgroundColor: c, tension: 0.25, fill: false,
  }}));
  new Chart(document.getElementById('chart-finance-cash'), {{
    type: 'line', data: {{ labels, datasets }},
    options: {{ responsive: true, maintainAspectRatio: false,
                scales: {{ y: {{ ticks: {{ callback: v => '$' + Number(v).toLocaleString() }} }} }} }},
  }});
}})();

(function() {{
  const labels = financeSeries.map(r => r.date_time);
  const fields = [['revenue','#27ae60'],['cogs','#c0392b'],
                  ['accounts_receivable','#3498db'],['accounts_payable','#e67e22'],
                  ['total_labor','#9b59b6'],['in_process','#7f8c8d']];
  const datasets = fields.map(([f, c]) => ({{
    label: f, data: financeSeries.map(r => r[f]),
    borderColor: c, backgroundColor: c, tension: 0.25, fill: false,
  }}));
  new Chart(document.getElementById('chart-finance-flow'), {{
    type: 'line', data: {{ labels, datasets }},
    options: {{ responsive: true, maintainAspectRatio: false,
                scales: {{ y: {{ ticks: {{ callback: v => '$' + Number(v).toLocaleString() }} }} }} }},
  }});
}})();

(function() {{
  const labels = payrollSeries.map(r => r.pay_period_id);
  const stacks = [['net','#2ecc71'],['fed_tax','#e74c3c'],['state_tax','#e67e22'],
                  ['health','#9b59b6'],['union_fees','#7f8c8d']];
  const datasets = stacks.map(([f, c]) => ({{
    label: f, data: payrollSeries.map(r => r[f] || 0),
    backgroundColor: c,
  }}));
  new Chart(document.getElementById('chart-payroll'), {{
    type: 'bar', data: {{ labels, datasets }},
    options: {{ responsive: true, maintainAspectRatio: false,
                scales: {{ x: {{ stacked: true }},
                          y: {{ stacked: true,
                                ticks: {{ callback: v => '$' + Number(v).toLocaleString() }} }} }} }},
  }});
}})();

(function() {{
  const labels = efficiencySeries.map(r => r.pay_period_id);
  const cpf  = efficiencySeries.map(r => r.cost_per_firing);
  const util = efficiencySeries.map(r => r.utilization === null ? null : r.utilization * 100);
  new Chart(document.getElementById('chart-efficiency'), {{
    type: 'line',
    data: {{
      labels,
      datasets: [
        {{ label: '$ / productive firing', data: cpf,
           borderColor: '#9b59b6', backgroundColor: '#9b59b6',
           yAxisID: 'y',  tension: 0.25, fill: false, spanGaps: true }},
        {{ label: 'utilization %', data: util,
           borderColor: '#1abc9c', backgroundColor: '#1abc9c',
           yAxisID: 'y1', tension: 0.25, fill: false, spanGaps: true }},
      ],
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      interaction: {{ mode: 'index' }},
      scales: {{
        y:  {{ position: 'left',  ticks: {{ callback: v => '$' + Number(v).toLocaleString() }} }},
        y1: {{ position: 'right', min: 0, max: 100,
               ticks: {{ callback: v => v + '%' }},
               grid: {{ drawOnChartArea: false }} }},
      }},
    }},
  }});
}})();
</script>
</body>
</html>
"""


def _render_report_list(items):
    if not items:
        return "<p class='meta'>No reports written yet.</p>"
    parts = []
    for it in items:
        label = html_module.escape(it["label"])
        parts.append(f"<details><summary>{label}</summary>\n{it['html']}\n</details>")
    return "\n".join(parts)


def _render_worker_activity(rows):
    if not rows:
        return "<p class='meta'>task_log is empty — no activity to summarize yet. Run a simulation first.</p>"
    by_worker = {}
    for r in rows:
        by_worker.setdefault(r["worker_id"], []).append(r)
    parts = []
    for wid in sorted(by_worker.keys()):
        entries = by_worker[wid]
        total_firings = sum(e["firings"] for e in entries)
        total_work = sum(e["work"] for e in entries)
        total_no_work = sum(e["no_work"] for e in entries)
        total_hours = sum(e["hours"] for e in entries)
        off_shift_hours = sum(e["hours"] for e in entries if e["task_name"] == "(off_shift)")
        active_hours = total_hours - off_shift_hours
        summary = (
            f"{wid} — {total_firings} firings (✓{total_work} / ∅{total_no_work}) "
            f"— {active_hours:.0f}h active, {off_shift_hours:.0f}h off-shift"
        )
        tbody = []
        for e in sorted(entries, key=lambda x: -x["firings"]):
            tbody.append(
                f"<tr><td>{html_module.escape(e['task_name'])}</td>"
                f"<td>{e['firings']}</td>"
                f"<td>{e['work']}</td>"
                f"<td>{e['no_work']}</td>"
                f"<td>{e['hours']:.0f}</td></tr>"
            )
        table = (
            "<table class='activity'><thead><tr><th>task</th><th>firings</th>"
            "<th>✓</th><th>∅</th><th>hours</th></tr></thead><tbody>"
            + "".join(tbody) + "</tbody></table>"
        )
        parts.append(
            f"<details class='worker'><summary>{html_module.escape(summary)}</summary>"
            f"{table}</details>"
        )
    return "\n".join(parts)


def build_html():
    pipeline_reports = load_pipeline_reports()
    finance_reports = load_finance_reports()
    payroll_reports = load_payroll_reports()
    efficiency_reports = load_efficiency_reports()
    pipeline_series = derive_pipeline_series(pipeline_reports)
    finance_series = load_finance_series()
    payroll_series = load_payroll_series()
    efficiency_series = load_efficiency_series()

    metrics = _summary_metrics(
        finance_series, payroll_series, efficiency_series, pipeline_series,
    )
    narrative_html = _summary_narrative(
        metrics, finance_series, payroll_series, efficiency_series,
    )
    summary_narrative_block = (
        f"<div class='narrative'>{narrative_html}</div>" if narrative_html else ""
    )

    return _HTML_TEMPLATE.format(
        pipeline_count=len(pipeline_reports),
        finance_count=len(finance_reports),
        payroll_count=len(payroll_reports),
        efficiency_count=len(efficiency_reports),
        pipeline_html=_render_report_list(pipeline_reports),
        finance_html=_render_report_list(finance_reports),
        payroll_html=_render_report_list(payroll_reports),
        efficiency_html=_render_report_list(efficiency_reports),
        activity_html=_render_worker_activity(load_worker_activity()),
        summary_metrics_html=_render_metrics_block(metrics),
        summary_narrative_html=summary_narrative_block,
        customer_series_json=json.dumps(pipeline_series["customers"]),
        orders_value_series_json=json.dumps(pipeline_series["orders_value"]),
        finance_series_json=json.dumps(finance_series),
        payroll_series_json=json.dumps(payroll_series),
        efficiency_series_json=json.dumps(efficiency_series),
    )


def run(sim_time: datetime) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build_html(), encoding="utf-8")
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"[report_site] wrote {OUTPUT}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    run(datetime.now())
