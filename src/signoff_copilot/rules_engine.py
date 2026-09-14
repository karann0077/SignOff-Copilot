"""
rules_engine.py — Deterministic pass/fail verdict for SignOff Copilot.

Loads threshold configuration from thresholds.yaml and evaluates each
parsed metric.  The overall run status is FAIL if *any* metric fails.

The verdict never depends on a probabilistic model — this is intentional.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .parser import ParsedMetrics

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Defaults — used if thresholds.yaml is missing a key
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULTS: dict = {
    "wns_ns_min":            0.0,
    "tns_ns_min":            0.0,
    "max_timing_violations": 0,
    "max_drc_violations":    0,
    "max_utilization_pct":   85.0,
    "min_cell_count":        1,
}


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MetricVerdict:
    """Pass/fail result for a single metric."""
    metric:    str
    value:     Optional[float]
    threshold: Optional[float]
    passed:    bool
    reason:    str = ""


@dataclass
class RunVerdict:
    """Aggregated pass/fail verdict for an entire run."""
    overall_status: str                          # "PASS" | "FAIL" | "SKIP"
    metric_verdicts: list[MetricVerdict] = field(default_factory=list)

    @property
    def failed_metrics(self) -> list[MetricVerdict]:
        return [v for v in self.metric_verdicts if not v.passed]

    def summary_lines(self) -> list[str]:
        lines = []
        for v in self.metric_verdicts:
            icon = "✓" if v.passed else "✗"
            val  = f"{v.value:.4f}" if isinstance(v.value, float) else str(v.value)
            thr  = f"{v.threshold}"  if v.threshold is not None else "—"
            lines.append(f"  {icon}  {v.metric:<30} value={val:<12} threshold={thr}  {v.reason}")
        return lines


# ─────────────────────────────────────────────────────────────────────────────
# Rules engine
# ─────────────────────────────────────────────────────────────────────────────

class RulesEngine:
    """
    Evaluates parsed metrics against configured thresholds.

    Parameters
    ----------
    thresholds_path : Path to thresholds.yaml  (or None to use defaults)
    """

    def __init__(self, thresholds_path: Optional[Path] = None):
        self._thresholds = dict(_DEFAULTS)

        if thresholds_path and Path(thresholds_path).exists():
            with open(thresholds_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            if cfg and "thresholds" in cfg:
                self._thresholds.update(cfg["thresholds"])
                log.debug("Loaded thresholds from %s", thresholds_path)
        else:
            log.warning("thresholds.yaml not found — using built-in defaults")

    # ── Public ───────────────────────────────────────────────────────────────

    def evaluate(self, metrics: ParsedMetrics) -> RunVerdict:
        """
        Evaluate all metrics and return a RunVerdict.

        Parameters
        ----------
        metrics : ParsedMetrics from the log parser
        """
        verdicts: list[MetricVerdict] = []
        all_pass = True

        # ── WNS ──────────────────────────────────────────────────────────────
        v = self._check_min(
            metric="WNS (ns)",
            value=metrics.wns_ns,
            threshold=self._thresholds["wns_ns_min"],
            unit="ns",
        )
        verdicts.append(v)
        if not v.passed:
            all_pass = False

        # ── TNS ──────────────────────────────────────────────────────────────
        v = self._check_min(
            metric="TNS (ns)",
            value=metrics.tns_ns,
            threshold=self._thresholds["tns_ns_min"],
            unit="ns",
        )
        verdicts.append(v)
        if not v.passed:
            all_pass = False

        # ── Timing violations ─────────────────────────────────────────────────
        v = self._check_max_int(
            metric="Timing violations",
            value=metrics.num_timing_violations,
            threshold=int(self._thresholds["max_timing_violations"]),
        )
        verdicts.append(v)
        if not v.passed:
            all_pass = False

        # ── DRC violations ───────────────────────────────────────────────────
        v = self._check_max_int(
            metric="DRC violations",
            value=metrics.num_drc_violations,
            threshold=int(self._thresholds["max_drc_violations"]),
        )
        verdicts.append(v)
        if not v.passed:
            all_pass = False

        # ── Utilization ──────────────────────────────────────────────────────
        if metrics.utilization_pct is not None:
            v = self._check_max(
                metric="Utilization (%)",
                value=metrics.utilization_pct,
                threshold=self._thresholds["max_utilization_pct"],
                unit="%",
            )
            verdicts.append(v)
            if not v.passed:
                all_pass = False
        else:
            verdicts.append(MetricVerdict(
                metric="Utilization (%)",
                value=None,
                threshold=self._thresholds["max_utilization_pct"],
                passed=True,
                reason="Not available (run without --pnr)",
            ))

        # ── Cell count sanity ─────────────────────────────────────────────────
        v = self._check_min_int(
            metric="Cell count",
            value=metrics.cell_count,
            threshold=int(self._thresholds["min_cell_count"]),
        )
        verdicts.append(v)
        if not v.passed:
            all_pass = False

        overall = "PASS" if all_pass else "FAIL"
        log.info("Run %s verdict: %s", metrics.run_id, overall)
        return RunVerdict(overall_status=overall, metric_verdicts=verdicts)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _check_min(
        self,
        metric: str,
        value: Optional[float],
        threshold: float,
        unit: str = "",
    ) -> MetricVerdict:
        if value is None:
            return MetricVerdict(
                metric=metric, value=None, threshold=threshold,
                passed=True, reason="Not measured — skipped",
            )
        passed = value >= threshold
        reason = (
            f"{value:.4f}{unit} ≥ {threshold}{unit}"
            if passed
            else f"{value:.4f}{unit} < {threshold}{unit} ← VIOLATION"
        )
        return MetricVerdict(metric=metric, value=value, threshold=threshold,
                             passed=passed, reason=reason)

    def _check_max(
        self,
        metric: str,
        value: Optional[float],
        threshold: float,
        unit: str = "",
    ) -> MetricVerdict:
        if value is None:
            return MetricVerdict(
                metric=metric, value=None, threshold=threshold,
                passed=True, reason="Not measured — skipped",
            )
        passed = value <= threshold
        reason = (
            f"{value:.2f}{unit} ≤ {threshold}{unit}"
            if passed
            else f"{value:.2f}{unit} > {threshold}{unit} ← VIOLATION"
        )
        return MetricVerdict(metric=metric, value=value, threshold=threshold,
                             passed=passed, reason=reason)

    def _check_max_int(
        self,
        metric: str,
        value: int,
        threshold: int,
    ) -> MetricVerdict:
        passed = value <= threshold
        reason = (
            f"{value} ≤ {threshold}"
            if passed
            else f"{value} > {threshold} ← VIOLATION"
        )
        return MetricVerdict(metric=metric, value=float(value),
                             threshold=float(threshold), passed=passed, reason=reason)

    def _check_min_int(
        self,
        metric: str,
        value: int,
        threshold: int,
    ) -> MetricVerdict:
        passed = value >= threshold
        reason = (
            f"{value} cells synthesised"
            if passed
            else f"Only {value} cells — degenerate synthesis?"
        )
        return MetricVerdict(metric=metric, value=float(value),
                             threshold=float(threshold), passed=passed, reason=reason)
