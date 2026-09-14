"""
dashboard/app.py — Streamlit run-history dashboard for SignOff Copilot.

Reads directly from the SQLite data store — no API server needed.
Provides a run history table, per-design trend charts, and a link
to each run's HTML report.

Usage:
    streamlit run dashboard/app.py
  or
    signoff-copilot dashboard
"""

from __future__ import annotations

import sys
from pathlib import Path

# ── Make the src package importable without installation ──────────────────────
_repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_repo_root / "src"))

import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
import pandas as pd
from signoff_copilot.datastore import DataStore

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="SignOff Copilot Dashboard",
    page_icon="🔬",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — database + filters
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🔬 SignOff Copilot")
    st.markdown("---")

    db_path = st.text_input(
        "Database path",
        value=str(_repo_root / "runs" / "signoff.db"),
        help="Path to the SQLite database file.",
    )

    if not Path(db_path).exists():
        st.warning(f"Database not found:\n`{db_path}`\n\nRun `signoff-copilot run` first.")
        st.stop()

    store   = DataStore(Path(db_path))
    designs = store.designs()

    if not designs:
        st.info("No runs in the database yet.\nRun `signoff-copilot run --design picorv32` to get started.")
        st.stop()

    selected_design = st.selectbox("Design", ["(all)"] + designs)
    limit = st.slider("Max rows", min_value=10, max_value=200, value=50, step=10)

    st.markdown("---")
    st.caption("Data refreshes on page reload.")

# ─────────────────────────────────────────────────────────────────────────────
# Load data
# ─────────────────────────────────────────────────────────────────────────────

design_filter = selected_design if selected_design != "(all)" else None
runs = store.list_runs(design=design_filter, limit=limit)

if not runs:
    st.info("No runs match the current filter.")
    st.stop()

df = pd.DataFrame(runs)

# ─────────────────────────────────────────────────────────────────────────────
# Header
# ─────────────────────────────────────────────────────────────────────────────

st.title("🔬 SignOff Copilot — Run History Dashboard")

total    = len(df)
n_pass   = (df["status"] == "PASS").sum()
n_fail   = (df["status"] == "FAIL").sum()
pass_pct = 100 * n_pass / total if total else 0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total runs",   total)
col2.metric("Pass",         n_pass,  delta=f"{pass_pct:.0f}%")
col3.metric("Fail",         n_fail)
col4.metric("Pass rate",    f"{pass_pct:.0f}%")

st.markdown("---")

# ─────────────────────────────────────────────────────────────────────────────
# Run history table
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("📋 Run History")

display_cols = [
    "run_id", "design", "status", "wns_ns", "tns_ns",
    "num_timing_violations", "num_drc_violations",
    "cell_count", "utilization_pct", "runtime_sec", "timestamp",
]
df_display = df[[c for c in display_cols if c in df.columns]].copy()

# Colour the status column
def _colour_status(val: str) -> str:
    if val == "PASS":
        return "background-color: #d1fae5; color: #065f46; font-weight: bold;"
    return "background-color: #fee2e2; color: #991b1b; font-weight: bold;"

styled = df_display.style.applymap(_colour_status, subset=["status"])
st.dataframe(styled, use_container_width=True, hide_index=True)

# ─────────────────────────────────────────────────────────────────────────────
# Trend charts
# ─────────────────────────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("📈 Timing Trends")

if "timestamp" in df.columns:
    df_sorted = df.sort_values("timestamp")

    tab_wns, tab_tns, tab_cells, tab_violations = st.tabs(["WNS", "TNS", "Cell Count", "Violations"])

    with tab_wns:
        if df_sorted["wns_ns"].notna().any():
            fig = px.line(
                df_sorted,
                x="timestamp", y="wns_ns",
                color="design" if "design" in df_sorted.columns else None,
                markers=True,
                title="Worst Negative Slack (WNS) over Time",
                labels={"wns_ns": "WNS (ns)", "timestamp": "Run Timestamp"},
            )
            fig.add_hline(y=0, line_dash="dash", line_color="red",
                         annotation_text="Slack boundary (0 ns)")
            fig.update_layout(hovermode="x unified")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No WNS data available.")

    with tab_tns:
        if df_sorted["tns_ns"].notna().any():
            fig = px.line(
                df_sorted,
                x="timestamp", y="tns_ns",
                color="design" if "design" in df_sorted.columns else None,
                markers=True,
                title="Total Negative Slack (TNS) over Time",
                labels={"tns_ns": "TNS (ns)", "timestamp": "Run Timestamp"},
            )
            fig.add_hline(y=0, line_dash="dash", line_color="red")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No TNS data available.")

    with tab_cells:
        if df_sorted["cell_count"].notna().any():
            fig = px.bar(
                df_sorted,
                x="timestamp", y="cell_count",
                color="design" if "design" in df_sorted.columns else None,
                title="Cell Count per Run",
                labels={"cell_count": "Cell Count", "timestamp": "Run Timestamp"},
            )
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("No cell count data.")

    with tab_violations:
        if df_sorted["num_timing_violations"].notna().any():
            fig = px.bar(
                df_sorted,
                x="timestamp", y="num_timing_violations",
                color="status",
                color_discrete_map={"PASS": "#10b981", "FAIL": "#ef4444"},
                title="Timing Violations per Run",
                labels={"num_timing_violations": "Violations", "timestamp": "Run Timestamp"},
            )
            st.plotly_chart(fig, use_container_width=True)

# ─────────────────────────────────────────────────────────────────────────────
# Run detail expander
# ─────────────────────────────────────────────────────────────────────────────

st.markdown("---")
st.subheader("🔎 Run Detail")

run_ids = [r["run_id"] for r in runs]
selected_run_id = st.selectbox("Select a run", run_ids)

if selected_run_id:
    row = store.get_run(selected_run_id)
    if row:
        col_left, col_right = st.columns(2)

        with col_left:
            st.markdown("**Metrics**")
            st.json({k: v for k, v in row.items()
                     if k not in ("llm_summary", "metric_verdicts_json", "report_html_path")})

        with col_right:
            if row.get("llm_summary"):
                st.markdown("**AI Summary**")
                st.info(row["llm_summary"])

            report_path = row.get("report_html_path")
            if report_path and Path(report_path).exists():
                st.markdown("**Report**")
                with open(report_path, encoding="utf-8") as fh:
                    html_content = fh.read()
                st.download_button(
                    label="⬇️ Download HTML Report",
                    data=html_content,
                    file_name=f"report_{selected_run_id}.html",
                    mime="text/html",
                )
                with st.expander("Preview Report (inline)"):
                    st.components.v1.html(html_content, height=600, scrolling=True)
