"""
cli.py — Click-based entry point for SignOff Copilot.

Commands
--------
  signoff-copilot run        — Run the full EDA flow
  signoff-copilot report     — View a past run's report
  signoff-copilot list       — List recent runs
  signoff-copilot ask        — NL-to-TCL translator (bonus module)
  signoff-copilot dashboard  — Launch the Streamlit dashboard
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

import click
import yaml

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _project_root() -> Path:
    """Return the project root directory (parent of src/ or CWD fallback)."""
    # Walk up looking for pyproject.toml
    candidate = Path(__file__).resolve()
    for parent in candidate.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=level,
        stream=sys.stderr,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Root group
# ─────────────────────────────────────────────────────────────────────────────

@click.group()
@click.version_option(package_name="signoff-copilot")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Enable debug logging.")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """SignOff Copilot — AI-assisted RTL-to-timing EDA signoff automation."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"]      = verbose
    ctx.obj["project_root"] = _project_root()
    _setup_logging(verbose)


# ─────────────────────────────────────────────────────────────────────────────
# signoff-copilot run
# ─────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--design",         "-d", required=True,  help="Design name (must match designs/<name>/).")
@click.option("--clock-period",   "-c", default=4.0,    show_default=True, type=float, help="Target clock period in nanoseconds.")
@click.option("--pnr",            is_flag=True, default=False, help="Enable OpenROAD place-and-route stage.")
@click.option("--no-llm",         is_flag=True, default=False, help="Skip LLM summary (useful offline).")
@click.option("--no-notify",      is_flag=True, default=False, help="Skip Slack/email notifications.")
@click.option("--open-report",    is_flag=True, default=False, help="Open the HTML report in a browser after the run.")
@click.option("--db",             default=None,  type=click.Path(), help="Path to SQLite database (default: runs/signoff.db).")
@click.pass_context
def run(
    ctx: click.Context,
    design: str,
    clock_period: float,
    pnr: bool,
    no_llm: bool,
    no_notify: bool,
    open_report: bool,
    db: str | None,
) -> None:
    """Run the full EDA flow and generate a signoff report."""
    from .orchestrator     import Orchestrator
    from .parser           import parse_run
    from .rules_engine     import RulesEngine
    from .report_generator import ReportGenerator
    from .llm_summary      import generate_summary
    from .datastore        import DataStore
    from .notifier         import notify_run_complete

    root = ctx.obj["project_root"]

    click.echo(f"🚀  SignOff Copilot starting run for design '{design}' @ {clock_period} ns ...")

    # ── 1. Orchestrate ────────────────────────────────────────────────────────
    orchestrator = Orchestrator(project_root=root)
    orch_result  = orchestrator.run(
        design=design,
        clock_period_ns=clock_period,
        enable_pnr=pnr,
    )
    _print_stage_summary(orch_result.stages)

    # ── 2. Parse ──────────────────────────────────────────────────────────────
    click.echo("📊  Parsing EDA logs ...")
    metrics = parse_run(
        run_id=orch_result.run_id,
        design=design,
        clock_period_ns=clock_period,
        runtime_sec=orch_result.total_runtime_sec,
        timestamp=orch_result.timestamp,
        synth_log_path=orch_result.synth_log_path,
        timing_rpt_path=orch_result.timing_rpt_path,
        drc_rpt_path=orch_result.drc_rpt_path,
    )

    # ── 3. Rules engine ───────────────────────────────────────────────────────
    click.echo("⚖️   Evaluating thresholds ...")
    engine  = RulesEngine(thresholds_path=root / "config" / "thresholds.yaml")
    verdict = engine.evaluate(metrics)
    metrics.status = verdict.overall_status

    badge = click.style("PASS", fg="green", bold=True) if verdict.overall_status == "PASS" \
            else click.style("FAIL", fg="red", bold=True)
    click.echo(f"\n  Verdict: {badge}")
    for line in verdict.summary_lines():
        click.echo(line)

    # ── 4. LLM summary ───────────────────────────────────────────────────────
    llm_summary = ""
    if not no_llm:
        click.echo("\n🤖  Generating AI summary ...")
        llm_summary = generate_summary(metrics, verdict)
        click.echo(f"\n  {llm_summary}")

    # ── 5. Report ─────────────────────────────────────────────────────────────
    click.echo("\n📄  Generating HTML report ...")
    generator = ReportGenerator(templates_dir=root / "src" / "signoff_copilot" / "templates")
    report_path = generator.render(
        metrics=metrics,
        verdict=verdict,
        llm_summary=llm_summary,
        output_dir=orch_result.run_dir,
    )
    click.echo(f"  Report written to: {report_path}")

    # ── 6. Data store ─────────────────────────────────────────────────────────
    db_path = Path(db) if db else (root / "runs" / "signoff.db")
    store   = DataStore(db_path=db_path)
    store.insert_run(
        metrics=metrics,
        verdict=verdict,
        llm_summary=llm_summary,
        report_html_path=str(report_path),
    )
    click.echo(f"  Run saved to database: {db_path}")

    # ── 7. Notify ─────────────────────────────────────────────────────────────
    if not no_notify:
        results = notify_run_complete(metrics, verdict, llm_summary, report_path)
        for backend, ok in results.items():
            click.echo(f"  Notification ({backend}): {'sent ✓' if ok else 'failed ✗'}")

    # ── 8. Open browser ──────────────────────────────────────────────────────
    if open_report:
        webbrowser.open(f"file://{report_path.resolve()}")

    click.echo(f"\n✅  Done in {orch_result.total_runtime_sec:.1f} s — run ID: {orch_result.run_id}")
    sys.exit(0 if verdict.overall_status == "PASS" else 1)


# ─────────────────────────────────────────────────────────────────────────────
# signoff-copilot list
# ─────────────────────────────────────────────────────────────────────────────

@main.command("list")
@click.option("--design", "-d", default=None, help="Filter by design name.")
@click.option("--limit",  "-n", default=20, show_default=True, type=int, help="Max rows to show.")
@click.option("--db", default=None, type=click.Path(), help="Path to SQLite database.")
@click.pass_context
def list_runs(ctx: click.Context, design: str | None, limit: int, db: str | None) -> None:
    """List recent signoff runs."""
    from .datastore import DataStore

    root    = ctx.obj["project_root"]
    db_path = Path(db) if db else (root / "runs" / "signoff.db")
    store   = DataStore(db_path=db_path)
    rows    = store.list_runs(design=design, limit=limit)

    if not rows:
        click.echo("No runs found.")
        return

    # Header
    click.echo(f"\n{'Run ID':<22} {'Design':<12} {'Status':<6} {'WNS (ns)':<10} "
               f"{'TNS (ns)':<10} {'Cells':<8} {'Runtime':<8}")
    click.echo("-" * 80)

    for r in rows:
        status_str = (
            click.style("PASS", fg="green") if r["status"] == "PASS"
            else click.style("FAIL", fg="red")
        )
        wns  = f"{r['wns_ns']:.4f}" if r["wns_ns"] is not None else "—"
        tns  = f"{r['tns_ns']:.4f}" if r["tns_ns"] is not None else "—"
        rt   = f"{r['runtime_sec']:.0f}s"
        click.echo(f"{r['run_id']:<22} {r['design']:<12} {status_str:<6} "
                   f"{wns:<10} {tns:<10} {r['cell_count']:<8} {rt}")


# ─────────────────────────────────────────────────────────────────────────────
# signoff-copilot report
# ─────────────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("run_id")
@click.option("--db", default=None, type=click.Path(), help="Path to SQLite database.")
@click.pass_context
def report(ctx: click.Context, run_id: str, db: str | None) -> None:
    """Open the HTML report for RUN_ID in the browser."""
    from .datastore import DataStore

    root    = ctx.obj["project_root"]
    db_path = Path(db) if db else (root / "runs" / "signoff.db")
    store   = DataStore(db_path=db_path)
    row     = store.get_run(run_id)

    if row is None:
        click.echo(f"Run '{run_id}' not found in database.", err=True)
        sys.exit(1)

    report_path = row.get("report_html_path")
    if report_path and Path(report_path).exists():
        click.echo(f"Opening: {report_path}")
        webbrowser.open(f"file://{Path(report_path).resolve()}")
    else:
        click.echo(f"Report file not found: {report_path}", err=True)
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# signoff-copilot ask  (NL-to-TCL bonus module)
# ─────────────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("request", nargs=-1, required=True)
@click.option("--dry-run", is_flag=True, default=False, help="Print translated TCL without executing it.")
@click.pass_context
def ask(ctx: click.Context, request: tuple[str, ...], dry_run: bool) -> None:
    """Translate a natural-language EDA request into TCL and (optionally) execute it.

    Example:
        signoff-copilot ask "set the clock period to 5ns and rerun timing"
    """
    from .nl_to_tcl import translate_to_tcl

    nl_request = " ".join(request)
    click.echo(f"🌐  Translating: {nl_request!r}")

    result = translate_to_tcl(nl_request, dry_run=dry_run)

    if result.success:
        click.echo(f"\n  TCL command:\n    {result.tcl}")
        if not dry_run and result.executed:
            click.echo(f"\n  Output:\n{result.output}")
    else:
        click.echo(click.style(f"\n  ✗ {result.error}", fg="red"), err=True)
        if result.tcl:
            click.echo(f"  (Generated but blocked by allowlist: {result.tcl})")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# signoff-copilot dashboard
# ─────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--port", default=8501, show_default=True, type=int, help="Streamlit server port.")
@click.pass_context
def dashboard(ctx: click.Context, port: int) -> None:
    """Launch the Streamlit run-history dashboard."""
    root  = ctx.obj["project_root"]
    app   = root / "dashboard" / "app.py"

    if not app.exists():
        click.echo(f"Dashboard not found: {app}", err=True)
        sys.exit(1)

    click.echo(f"🖥️   Launching dashboard at http://localhost:{port} ...")
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(app), "--server.port", str(port)],
        check=False,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _print_stage_summary(stages) -> None:
    click.echo("\n  Flow stages:")
    for s in stages:
        icon = "✓" if s.success else "✗"
        click.echo(
            f"    [{icon}] {s.stage:<15} {s.runtime_sec:.1f}s"
            + (f"  ← {s.error_msg}" if not s.success else "")
        )
    click.echo()
