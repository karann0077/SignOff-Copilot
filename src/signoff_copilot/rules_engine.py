"""
rules_engine.py — Deterministic pass/fail verdict for SignOff Copilot.

Driven by a RunSpec (the validated signoff specification for this run).
Evaluates each requested check against the measured ParsedMetrics.

Outcome per metric
------------------
  PASS        — measured and meets threshold
  FAIL        — measured and violates threshold
  MISSING     — was requested but metric could not be measured  → run FAILS
  UNSUPPORTED — user requested a metric the flow cannot measure (recorded only)
  SKIPPED     — check not requested in RunSpec (None threshold, check=False)

The overall run status is FAIL if any metric is FAIL or MISSING.
The LLM never touches this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .parser import ParsedMetrics

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Outcome enum
# ─────────────────────────────────────────────────────────────────────────────

class Outcome:
    PASS        = "PASS"
    FAIL        = "FAIL"
    MISSING     = "MISSING"       # requested but not measured → counts as FAIL
    UNSUPPORTED = "UNSUPPORTED"   # flow cannot measure this; informational only
    SKIPPED     = "SKIPPED"       # not requested in RunSpec


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MetricVerdict:
    """Pass/fail/missing/skipped result for a single metric."""
    metric:    str
    value:     Optional[float]
    threshold: Optional[float]
    passed:    bool                    # True for PASS and SKIPPED outcomes
    outcome:   str = Outcome.PASS      # one of Outcome.*
    reason:    str = ""


@dataclass
class RunVerdict:
    """Aggregated verdict for an entire run."""
    overall_status: str                           # "PASS" | "FAIL"
    metric_verdicts: list[MetricVerdict] = field(default_factory=list)

    @property
    def failed_metrics(self) -> list[MetricVerdict]:
        return [v for v in self.metric_verdicts
                if v.outcome in (Outcome.FAIL, Outcome.MISSING)]

    @property
    def unsupported_metrics(self) -> list[MetricVerdict]:
        return [v for v in self.metric_verdicts if v.outcome == Outcome.UNSUPPORTED]

    def summary_lines(self) -> list[str]:
        lines = []
        for v in self.metric_verdicts:
            if v.outcome == Outcome.SKIPPED:
                continue
            icons = {
                Outcome.PASS:        "✓",
                Outcome.FAIL:        "✗",
                Outcome.MISSING:     "?",
                Outcome.UNSUPPORTED: "⚠",
                Outcome.SKIPPED:     "–",
            }
            icon = icons.get(v.outcome, "?")
            val  = f"{v.value:.4f}" if isinstance(v.value, float) else str(v.value)
            thr  = f"{v.threshold}" if v.threshold is not None else "—"
            lines.append(
                f"  {icon}  {v.metric:<32} value={val:<12} threshold={thr:<10}  "
                f"[{v.outcome}]  {v.reason}"
            )
        return lines


# ─────────────────────────────────────────────────────────────────────────────
# Rules engine
# ─────────────────────────────────────────────────────────────────────────────

class RulesEngine:
    """
    Evaluates ParsedMetrics against a RunSpec.

    Parameters
    ----------
    spec : RunSpec — the validated signoff specification for this run.
           Thresholds come from the spec; the YAML file is not read here.
    """

    def __init__(self, spec=None, thresholds_path=None):
        """
        Backward-compatible constructor.

        New usage:   RulesEngine(spec=run_spec)
        Legacy usage: RulesEngine(thresholds_path=path)  → builds a minimal
                      spec from YAML defaults for old CLI workflows.
        """
        if spec is not None:
            self._spec = spec
        else:
            # Legacy path: build a spec from YAML defaults
            from .spec_interpreter import RunSpec, apply_yaml_defaults
            self._spec = apply_yaml_defaults(
                RunSpec(design="unknown"),
                thresholds_path,
            )

    # ── Public ───────────────────────────────────────────────────────────────

    def evaluate(self, metrics: ParsedMetrics) -> RunVerdict:
        """Evaluate all requested checks and return a RunVerdict."""
        spec = self._spec
        verdicts: list[MetricVerdict] = []
        any_fail = False

        def _add(v: MetricVerdict) -> None:
            nonlocal any_fail
            verdicts.append(v)
            if v.outcome in (Outcome.FAIL, Outcome.MISSING):
                any_fail = True

        # ── WNS (setup) ───────────────────────────────────────────────────────
        if spec.check_setup:
            threshold = spec.wns_ns_min if spec.wns_ns_min is not None else 0.0
            _add(self._check_min(
                metric="WNS (ns)",
                value=metrics.wns_ns,
                threshold=threshold,
                unit="ns",
                requested=True,
            ))

        # ── TNS (setup) ───────────────────────────────────────────────────────
        if spec.check_setup and spec.tns_ns_min is not None:
            _add(self._check_min(
                metric="TNS (ns)",
                value=metrics.tns_ns,
                threshold=spec.tns_ns_min,
                unit="ns",
                requested=True,
            ))

        # ── Setup violations ──────────────────────────────────────────────────
        if spec.check_setup:
            threshold = spec.max_setup_violations if spec.max_setup_violations is not None else 0
            _add(self._check_max_int(
                metric="Setup violations",
                value=metrics.num_setup_violations,
                threshold=threshold,
            ))

        # ── Hold violations ───────────────────────────────────────────────────
        if spec.check_hold:
            threshold = spec.max_hold_violations if spec.max_hold_violations is not None else 0
            _add(self._check_max_int(
                metric="Hold violations",
                value=metrics.num_hold_violations,
                threshold=threshold,
            ))

        # ── DRC violations ────────────────────────────────────────────────────
        if spec.max_drc_violations is not None:
            _add(self._check_max_int(
                metric="DRC violations",
                value=metrics.num_drc_violations,
                threshold=spec.max_drc_violations,
            ))

        # ── Utilization ───────────────────────────────────────────────────────
        if spec.max_utilization_pct is not None:
            _add(self._check_max(
                metric="Utilization (%)",
                value=metrics.utilization_pct,
                threshold=spec.max_utilization_pct,
                unit="%",
                requested=True,
            ))

        # ── Chip area ─────────────────────────────────────────────────────────
        if spec.max_chip_area_um2 is not None:
            _add(self._check_max(
                metric="Chip area (µm²)",
                value=metrics.chip_area_um2,
                threshold=spec.max_chip_area_um2,
                unit=" µm²",
                requested=True,
            ))

        # ── Cell count ceiling ────────────────────────────────────────────────
        if spec.max_cell_count is not None:
            _add(self._check_max_int(
                metric="Cell count (max)",
                value=metrics.cell_count,
                threshold=spec.max_cell_count,
            ))

        # ── Cell count floor (sanity) ─────────────────────────────────────────
        min_cells = spec.min_cell_count if spec.min_cell_count is not None else 1
        _add(self._check_min_int(
            metric="Cell count (min sanity)",
            value=metrics.cell_count,
            threshold=min_cells,
        ))

        # ── Unsupported requirements (informational only) ─────────────────────
        for req in spec.unsupported_requirements:
            verdicts.append(MetricVerdict(
                metric=req,
                value=None,
                threshold=None,
                passed=True,        # doesn't count against the verdict
                outcome=Outcome.UNSUPPORTED,
                reason=f"Not measurable by this EDA flow — skipped",
            ))

        overall = "FAIL" if any_fail else "PASS"
        log.info("Run %s verdict: %s (%d failed)",
                 metrics.run_id, overall, len([v for v in verdicts if not v.passed]))
        return RunVerdict(overall_status=overall, metric_verdicts=verdicts)

    # ── Private: check helpers ─────────────────────────────────────────────────

    def _check_min(
        self,
        metric: str,
        value: Optional[float],
        threshold: float,
        unit: str = "",
        requested: bool = False,
    ) -> MetricVerdict:
        if value is None:
            if requested:
                # Bug 2 fix: requested but not measured → MISSING → FAIL
                return MetricVerdict(
                    metric=metric, value=None, threshold=threshold,
                    passed=False, outcome=Outcome.MISSING,
                    reason=f"Measurement not available — run may have failed to produce output",
                )
            return MetricVerdict(
                metric=metric, value=None, threshold=threshold,
                passed=True, outcome=Outcome.SKIPPED,
                reason="Not measured — check skipped",
            )
        passed = value >= threshold
        return MetricVerdict(
            metric=metric, value=value, threshold=threshold,
            passed=passed,
            outcome=Outcome.PASS if passed else Outcome.FAIL,
            reason=(
                f"{value:.4f}{unit} ≥ {threshold}{unit}"
                if passed
                else f"{value:.4f}{unit} < {threshold}{unit} ← VIOLATION"
            ),
        )

    def _check_max(
        self,
        metric: str,
        value: Optional[float],
        threshold: float,
        unit: str = "",
        requested: bool = False,
    ) -> MetricVerdict:
        if value is None:
            if requested:
                return MetricVerdict(
                    metric=metric, value=None, threshold=threshold,
                    passed=False, outcome=Outcome.MISSING,
                    reason="Measurement not available — run may have failed to produce output",
                )
            return MetricVerdict(
                metric=metric, value=None, threshold=threshold,
                passed=True, outcome=Outcome.SKIPPED,
                reason="Not measured — check skipped",
            )
        passed = value <= threshold
        return MetricVerdict(
            metric=metric, value=value, threshold=threshold,
            passed=passed,
            outcome=Outcome.PASS if passed else Outcome.FAIL,
            reason=(
                f"{value:.2f}{unit} ≤ {threshold}{unit}"
                if passed
                else f"{value:.2f}{unit} > {threshold}{unit} ← VIOLATION"
            ),
        )

    def _check_max_int(
        self,
        metric: str,
        value: int,
        threshold: int,
    ) -> MetricVerdict:
        passed = value <= threshold
        return MetricVerdict(
            metric=metric, value=float(value), threshold=float(threshold),
            passed=passed,
            outcome=Outcome.PASS if passed else Outcome.FAIL,
            reason=(
                f"{value} ≤ {threshold}"
                if passed
                else f"{value} > {threshold} ← VIOLATION"
            ),
        )

    def _check_min_int(
        self,
        metric: str,
        value: int,
        threshold: int,
    ) -> MetricVerdict:
        passed = value >= threshold
        return MetricVerdict(
            metric=metric, value=float(value), threshold=float(threshold),
            passed=passed,
            outcome=Outcome.PASS if passed else Outcome.FAIL,
            reason=(
                f"{value} cells synthesised"
                if passed
                else f"Only {value} cells — degenerate synthesis?"
            ),
        )
