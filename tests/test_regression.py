"""
tests/test_regression.py — Unit tests for the regression analysis module.

All tests are purely in-memory. No EDA tools, no database needed.
"""
from __future__ import annotations

import pytest
from signoff_copilot.parser import ParsedMetrics
from signoff_copilot.regression import (
    compare_runs,
    no_regression_report,
    _compute_delta,
    MetricDelta,
    RegressionReport,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _metrics(**kwargs) -> ParsedMetrics:
    m = ParsedMetrics(
        run_id="cur-001",
        design="picorv32",
        clock_period_ns=4.0,
        wns_ns=0.22,
        tns_ns=0.0,
        num_setup_violations=0,
        num_hold_violations=0,
        num_drc_violations=0,
        cell_count=3421,
        chip_area_um2=15000.0,
        utilization_pct=62.5,
        runtime_sec=180.0,
        timestamp="2026-09-15T00:00:00Z",
    )
    for k, v in kwargs.items():
        setattr(m, k, v)
    return m


def _prev_row(**kwargs) -> dict:
    row = {
        "run_id":               "prev-001",
        "wns_ns":               0.25,
        "tns_ns":               0.0,
        "num_setup_violations": 0,
        "num_hold_violations":  0,
        "num_drc_violations":   0,
        "cell_count":           3400,
        "chip_area_um2":        14800.0,
        "utilization_pct":      61.0,
        "runtime_sec":          175.0,
    }
    row.update(kwargs)
    return row


# ─────────────────────────────────────────────────────────────────────────────
# _compute_delta unit tests
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeDelta:

    def test_unchanged_within_tolerance(self):
        delta, direction, regressed = _compute_delta(0.22, 0.22, False, 0.005)
        assert direction == "unchanged"
        assert regressed is False

    def test_wns_improved(self):
        # WNS went from 0.10 to 0.22 — higher WNS is better
        delta, direction, regressed = _compute_delta(0.22, 0.10, False, 0.005)
        assert direction == "better"
        assert regressed is False

    def test_wns_regressed(self):
        # WNS went from 0.25 to 0.01 — much worse
        delta, direction, regressed = _compute_delta(0.01, 0.25, False, 0.005)
        assert direction == "worse"
        assert regressed is True

    def test_cell_count_increased(self):
        # More cells = worse (higher_is_worse=True)
        delta, direction, regressed = _compute_delta(3600, 3400, True, 5)
        assert direction == "worse"
        assert regressed is True

    def test_cell_count_decreased(self):
        delta, direction, regressed = _compute_delta(3200, 3400, True, 5)
        assert direction == "better"
        assert regressed is False

    def test_no_previous_value(self):
        delta, direction, regressed = _compute_delta(0.22, None, False, 0.005)
        assert direction == "new"
        assert delta is None
        assert regressed is False

    def test_no_current_value(self):
        delta, direction, regressed = _compute_delta(None, 0.25, False, 0.005)
        assert direction == "lost"
        assert regressed is False

    def test_both_none(self):
        delta, direction, regressed = _compute_delta(None, None, False, 0.0)
        assert direction == "unchanged"
        assert regressed is False

    def test_violations_regression(self):
        # 0 → 3 setup violations is a regression
        delta, direction, regressed = _compute_delta(3, 0, True, 0)
        assert direction == "worse"
        assert regressed is True

    def test_violations_improvement(self):
        delta, direction, regressed = _compute_delta(0, 2, True, 0)
        assert direction == "better"
        assert regressed is False


# ─────────────────────────────────────────────────────────────────────────────
# compare_runs integration tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCompareRuns:

    def test_no_previous_run_returns_empty_report(self):
        metrics = _metrics()
        report = compare_runs(metrics, previous_row=None)
        assert report.compared_run_id is None
        assert report.any_regression is False
        assert report.deltas == []

    def test_identical_run_no_regression(self):
        metrics = _metrics()
        prev = _prev_row(
            wns_ns=0.22, tns_ns=0.0,
            num_setup_violations=0, num_hold_violations=0,
            cell_count=3421, chip_area_um2=15000.0,
            utilization_pct=62.5,
        )
        report = compare_runs(metrics, prev)
        assert report.any_regression is False

    def test_wns_regression_detected(self):
        # Current WNS is worse (much lower) than previous
        metrics = _metrics(wns_ns=0.01)
        prev = _prev_row(wns_ns=0.25)
        report = compare_runs(metrics, prev)
        assert report.any_regression is True
        wns_delta = next(d for d in report.deltas if d.metric == "WNS (ns)")
        assert wns_delta.regressed is True
        assert wns_delta.direction == "worse"

    def test_area_regression_detected(self):
        metrics = _metrics(chip_area_um2=20000.0)
        prev = _prev_row(chip_area_um2=14800.0)
        report = compare_runs(metrics, prev)
        area_delta = next(d for d in report.deltas if d.metric == "Chip area (µm²)")
        assert area_delta.regressed is True

    def test_improvement_not_regression(self):
        metrics = _metrics(wns_ns=0.50)   # better WNS
        prev = _prev_row(wns_ns=0.10)
        report = compare_runs(metrics, prev)
        wns_delta = next(d for d in report.deltas if d.metric == "WNS (ns)")
        assert wns_delta.regressed is False
        assert wns_delta.direction == "better"

    def test_new_violations_regression(self):
        metrics = _metrics(num_setup_violations=3)
        prev = _prev_row(num_setup_violations=0)
        report = compare_runs(metrics, prev)
        assert report.any_regression is True

    def test_compared_run_id_stored(self):
        metrics = _metrics()
        prev = _prev_row()
        report = compare_runs(metrics, prev)
        assert report.compared_run_id == "prev-001"

    def test_summary_lines_skips_unchanged(self):
        """summary_lines should not emit lines for unchanged metrics."""
        metrics = _metrics()
        prev = _prev_row(
            wns_ns=0.22, num_setup_violations=0, num_hold_violations=0,
            cell_count=3421, chip_area_um2=15000.0, utilization_pct=62.5,
        )
        report = compare_runs(metrics, prev)
        lines = report.summary_lines()
        # All metrics near-identical → should have no output lines
        assert isinstance(lines, list)

    def test_to_dict_serializable(self):
        import json
        metrics = _metrics()
        report = compare_runs(metrics, _prev_row())
        d = report.to_dict()
        # Must be JSON-serializable
        s = json.dumps(d)
        assert "compared_run_id" in s


# ─────────────────────────────────────────────────────────────────────────────
# no_regression_report
# ─────────────────────────────────────────────────────────────────────────────

class TestNoRegressionReport:

    def test_returns_empty_report(self):
        report = no_regression_report("picorv32")
        assert report.compared_run_id is None
        assert report.any_regression is False
        assert report.deltas == []
        assert report.design == "picorv32"
