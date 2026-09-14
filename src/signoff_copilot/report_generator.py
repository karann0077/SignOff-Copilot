"""
report_generator.py — HTML (and optional PDF) report generator.

Uses a Jinja2 template to combine structured metrics, per-metric
pass/fail badges, the worst timing path excerpt, and the AI summary
into a self-contained HTML file.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import jinja2

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# ReportGenerator
# ─────────────────────────────────────────────────────────────────────────────

class ReportGenerator:
    """
    Renders the Jinja2 HTML template with run data.

    Parameters
    ----------
    templates_dir : Directory containing report.html.j2
    """

    def __init__(self, templates_dir: Optional[Path] = None):
        if templates_dir is None:
            templates_dir = Path(__file__).parent / "templates"
        self.templates_dir = Path(templates_dir)
        self._env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(self.templates_dir)),
            autoescape=jinja2.select_autoescape(["html"]),
        )

    def render(
        self,
        metrics,           # ParsedMetrics
        verdict,           # RunVerdict
        llm_summary: str,
        output_dir: Path,
        generate_pdf: bool = False,
    ) -> Path:
        """
        Render report.html.j2 and write it to output_dir/report.html.

        Parameters
        ----------
        metrics      : Parsed run metrics
        verdict      : Rules-engine verdict
        llm_summary  : LLM-generated plain-English summary
        output_dir   : Directory to write report.html (and optionally report.pdf)
        generate_pdf : If True, also generate report.pdf via weasyprint

        Returns the path of the written report.html.
        """
        template = self._env.get_template("report.html.j2")

        # Build per-metric table rows
        metric_rows = []
        for mv in verdict.metric_verdicts:
            val_str = (
                f"{mv.value:.4f}" if isinstance(mv.value, float) and mv.value is not None
                else str(mv.value) if mv.value is not None
                else "—"
            )
            thr_str = (
                f"{mv.threshold}" if mv.threshold is not None else "—"
            )
            metric_rows.append({
                "name":      mv.metric,
                "value":     val_str,
                "threshold": thr_str,
                "passed":    mv.passed,
                "reason":    mv.reason,
            })

        freq_mhz = 1000.0 / metrics.clock_period_ns if metrics.clock_period_ns else 0.0

        context = {
            "run_id":              metrics.run_id,
            "design":              metrics.design,
            "timestamp":           metrics.timestamp,
            "clock_period_ns":     metrics.clock_period_ns,
            "freq_mhz":            f"{freq_mhz:.0f}",
            "status":              verdict.overall_status,
            "metric_rows":         metric_rows,
            "wns_ns":              metrics.wns_ns,
            "tns_ns":              metrics.tns_ns,
            "num_timing_violations": metrics.num_timing_violations,
            "num_drc_violations":  metrics.num_drc_violations,
            "cell_count":          metrics.cell_count,
            "chip_area_um2":       metrics.chip_area_um2,
            "utilization_pct":     metrics.utilization_pct,
            "runtime_sec":         metrics.runtime_sec,
            "worst_path_excerpt":  metrics.worst_path_excerpt or "",
            "llm_summary":         llm_summary,
            "failed_metrics":      [mv.metric for mv in verdict.failed_metrics],
        }

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        html_path = output_dir / "report.html"

        rendered = template.render(**context)
        html_path.write_text(rendered, encoding="utf-8")
        log.info("HTML report written to %s", html_path)

        if generate_pdf:
            pdf_path = output_dir / "report.pdf"
            self._export_pdf(html_path, pdf_path)

        return html_path

    # ── PDF export ────────────────────────────────────────────────────────────

    def _export_pdf(self, html_path: Path, pdf_path: Path) -> None:
        try:
            from weasyprint import HTML  # optional dependency
            HTML(filename=str(html_path)).write_pdf(str(pdf_path))
            log.info("PDF report written to %s", pdf_path)
        except ImportError:
            log.warning(
                "weasyprint not installed — skipping PDF export. "
                "Install with: pip install weasyprint"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("PDF export failed: %s", exc)
