"""
regression.py — Deterministic regression analysis for SignOff Copilot.

Compares the current run's ParsedMetrics against a previous run retrieved
from the DataStore.  All comparisons are purely arithmetic — the LLM plays
no role here.

The results feed into:
  • llm_summary.py  (context for the AI debugging explanation)
  • cli.py          (displayed in the terminal summary)
  • report_generator.py (shown in the HTML report)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .parser import ParsedMetrics

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MetricDelta:
    """Delta for a single metric between current and previous run."""
    metric:    str
    previous:  Optional[float]
    current:   Optional[float]
    delta:     Optional[float]     # current - previous; positive = increased
    direction: str                 # "better" | "worse" | "unchanged" | "new" | "lost"
    regressed: bool                # True if measurably worse

    def to_dict(self) -> dict:
        return {
            "metric":    self.metric,
            "previous":  self.previous,
            "current":   self.current,
            "delta":     self.delta,
            "direction": self.direction,
            "regressed": self.regressed,
        }


@dataclass
class RegressionReport:
    """Full regression comparison result."""
    compared_run_id: Optional[str]
    design:          str
    deltas:          list[MetricDelta] = field(default_factory=list)
    any_regression:  bool = False

    def summary_lines(self) -> list[str]:
        lines = []
        for d in self.deltas:
            if d.direction == "unchanged":
                continue
            icon = "↑" if d.regressed else ("↓" if d.direction in ("better",) else "→")
            prev = f"{d.previous:.4f}" if d.previous is not None else "N/A"
            curr = f"{d.current:.4f}" if d.current is not None else "N/A"
            delta_str = f"{d.delta:+.4f}" if d.delta is not None else "N/A"
            lines.append(
                f"  {icon}  {d.metric:<32} {prev} → {curr}  ({delta_str})  "
                f"[{'REGRESSION' if d.regressed else d.direction.upper()}]"
            )
        return lines

    def to_dict(self) -> dict:
        return {
            "compared_run_id": self.compared_run_id,
            "design":          self.design,
            "any_regression":  self.any_regression,
            "deltas":          [d.to_dict() for d in self.deltas],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Metric definitions: (attr_name, label, higher_is_worse)
# ─────────────────────────────────────────────────────────────────────────────

# For WNS and TNS: negative is bad, so a decrease is regression
# For violations and area: increase is regression
_METRICS = [
    # (ParsedMetrics attr,      display label,              higher_is_worse, tolerance)
    ("wns_ns",               "WNS (ns)",                   False,  0.005),
    ("tns_ns",               "TNS (ns)",                   False,  0.01),
    ("num_setup_violations", "Setup violations",            True,   0),
    ("num_hold_violations",  "Hold violations",             True,   0),
    ("num_drc_violations",   "DRC violations",              True,   0),
    ("cell_count",           "Cell count",                  True,   5),
    ("chip_area_um2",        "Chip area (µm²)",             True,   10.0),
    ("utilization_pct",      "Utilization (%)",             True,   0.5),
    ("runtime_sec",          "Runtime (s)",                 True,   5.0),
]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def compare_runs(
    current: ParsedMetrics,
    previous_row: Optional[dict],
) -> RegressionReport:
    """
    Compute per-metric deltas between the current run and a previous run dict.

    Parameters
    ----------
    current      : ParsedMetrics from the current run
    previous_row : dict from DataStore.get_run() / get_previous_passing_run()
                   May be None if no previous run exists.
    """
    if previous_row is None:
        log.info("No previous run to compare against — skipping regression analysis")
        return RegressionReport(
            compared_run_id=None,
            design=current.design,
            deltas=[],
            any_regression=False,
        )

    prev_run_id = previous_row.get("run_id", "unknown")
    log.info("Comparing run %s against previous run %s", current.run_id, prev_run_id)

    deltas: list[MetricDelta] = []
    any_regression = False

    for attr, label, higher_is_worse, tolerance in _METRICS:
        curr_val = _get_metric(current, attr)
        prev_val = _get_metric_from_dict(previous_row, attr)

        delta, direction, regressed = _compute_delta(
            curr_val, prev_val, higher_is_worse, tolerance
        )

        if regressed:
            any_regression = True

        deltas.append(MetricDelta(
            metric=label,
            previous=prev_val,
            current=curr_val,
            delta=delta,
            direction=direction,
            regressed=regressed,
        ))

    report = RegressionReport(
        compared_run_id=prev_run_id,
        design=current.design,
        deltas=deltas,
        any_regression=any_regression,
    )

    if any_regression:
        log.warning("Regression detected vs run %s:", prev_run_id)
        for line in report.summary_lines():
            log.warning(line)
    else:
        log.info("No regressions detected vs run %s", prev_run_id)

    return report


def no_regression_report(design: str) -> RegressionReport:
    """Return an empty report when regression comparison was not requested."""
    return RegressionReport(compared_run_id=None, design=design, deltas=[])


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_metric(metrics: ParsedMetrics, attr: str) -> Optional[float]:
    val = getattr(metrics, attr, None)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _get_metric_from_dict(row: dict, attr: str) -> Optional[float]:
    val = row.get(attr)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _compute_delta(
    current: Optional[float],
    previous: Optional[float],
    higher_is_worse: bool,
    tolerance: float,
) -> tuple[Optional[float], str, bool]:
    """
    Return (delta, direction, regressed).

    direction: "better" | "worse" | "unchanged" | "new" | "lost"
    regressed: True only if the change is measurably worse beyond tolerance
    """
    if current is None and previous is None:
        return None, "unchanged", False
    if previous is None:
        return None, "new", False
    if current is None:
        return None, "lost", False

    delta = current - previous
    abs_delta = abs(delta)

    if abs_delta <= tolerance:
        return delta, "unchanged", False

    if higher_is_worse:
        regressed = delta > tolerance
        direction = "worse" if delta > 0 else "better"
    else:
        # Lower is worse (e.g. WNS — more negative = worse)
        regressed = delta < -tolerance
        direction = "worse" if delta < 0 else "better"

    return delta, direction, regressed
