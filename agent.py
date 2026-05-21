"""report_site agent.

Builds a single-file HTML "control room" viewer at model_data/reports/index.html
that aggregates every markdown report (finance, pipeline, payroll), worker
activity rolled up from task_log_database, and Chart.js graphs of pipeline
+ finance + payroll metrics over sim time.

Triggered by the framework's cron worker on a schedule defined in the model's
crontab. Can also be invoked manually after a simulation finishes.

Reads:
  model_data/reports/finance_report_*.md
  model_data/reports/pipeline_report_*.md   (including embedded SNAPSHOT JSON)
  model_data/reports/payroll_*.md
  model_data/finance_database.db            (time series of finance state)
  model_data/payroll_database.db            (pay period rollups)
  model_data/task_log_database.db           (per-worker per-task firings)
  framework/run.log                         (full text log — referenced only)

Writes:
  model_data/reports/index.html
"""

import html as html_module
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

try:
    import markdown
except ImportError:
    sys.exit(
        "[report_site] missing dependency: `markdown` — run "
        "`venv/bin/pip install -r agents/report_site/requirements.txt`"
    )


# ─── Path resolution ─────────────────────────────────────────────────────

# When fired by the cron worker, $MODEL_DIR points at <framework>/models/<name>/.
# model_data/ is two parents up from that; the agent itself lives at
# <framework>/agents/report_site/.
_MODEL_DIR = os.environ.get("MODEL_DIR")
if _MODEL_DIR:
    DATA_DIR = Path(os.path.normpath(os.path.join(_MODEL_DIR, "..", "..", "model_data")))
else:
    _DD = os.environ.get("DATA_DIR")
    if not _DD:
        sys.exit("[report_site] DATA_DIR (or MODEL_DIR) must be set")
    DATA_DIR = Path(_DD)

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
</style>
</head>
<body>
<nav>
  <button data-tab="summary" class="active">Summary</button>
  <button data-tab="pipeline">Pipeline Reports ({pipeline_count})</button>
  <button data-tab="finance">Finance Reports ({finance_count})</button>
  <button data-tab="payroll">Payroll Reports ({payroll_count})</button>
  <button data-tab="activity">Worker Activity</button>
</nav>

<div id="summary" class="tab-content active">
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
    pipeline_series = derive_pipeline_series(pipeline_reports)

    return _HTML_TEMPLATE.format(
        pipeline_count=len(pipeline_reports),
        finance_count=len(finance_reports),
        payroll_count=len(payroll_reports),
        pipeline_html=_render_report_list(pipeline_reports),
        finance_html=_render_report_list(finance_reports),
        payroll_html=_render_report_list(payroll_reports),
        activity_html=_render_worker_activity(load_worker_activity()),
        customer_series_json=json.dumps(pipeline_series["customers"]),
        orders_value_series_json=json.dumps(pipeline_series["orders_value"]),
        finance_series_json=json.dumps(load_finance_series()),
        payroll_series_json=json.dumps(load_payroll_series()),
    )


def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build_html(), encoding="utf-8")
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"[report_site] wrote {OUTPUT}  ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
