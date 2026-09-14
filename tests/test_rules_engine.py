"""
tests/test_rules_engine.py — Unit tests for the rules engine.

Tests every threshold boundary using in-memory ParsedMetrics objects.
No file I/O, no EDA tools needed.
"""

import pytest
from pathlib import Path

from signoff_copilot.parser import ParsedMetrics
from signoff_copilot.rules_engine import RulesEngine

# Load the real thresholds.yaml from the config directory
_REPO_ROOT      = Path(__file__).parent.parent
_THRESHOLDS_CFG = _REPO_ROOT / "config" / "thresholds.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a clean passing metrics object
# ─────────────────────────────────────────────────────────────────────────────

def _passing_metrics(**overrides) -> ParsedMetrics:
    m = ParsedMetrics(
        run_id="test-rules-001",
        design="picorv32",
        clock_period_ns=4.0,
        wns_ns=0.22,
        tns_ns=0.0,
        num_timing_violations=0,
        num_drc_violations=0,
        cell_count=3421,
        utilization_pct=62.5,
        runtime_sec=180.0,
        timestamp="2026-09-14T10:00:00Z",
    )
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


@pytest.fixture
def engine() -> RulesEngine:
    return RulesEngine(thresholds_path=_THRESHOLDS_CFG)


# ─────────────────────────────────────────────────────────────────────────────
# Nominal pass
# ─────────────────────────────────────────────────────────────────────────────

class TestNominalPass:
    def test_overall_status_pass(self, engine):
        verdict = engine.evaluate(_passing_metrics())
        assert verdict.overall_status == "PASS"

    def test_all_metrics_pass(self, engine):
        verdict = engine.evaluate(_passing_metrics())
        for mv in verdict.metric_verdicts:
            assert mv.passed, f"Expected {mv.metric} to pass, reason: {mv.reason}"

    def test_no_failed_metrics(self, engine):
        verdict = engine.evaluate(_passing_metrics())
        assert len(verdict.failed_metrics) == 0


# ─────────────────────────────────────────────────────────────────────────────
# WNS boundary
# ─────────────────────────────────────────────────────────────────────────────

class TestWNSThreshold:
    def test_wns_exactly_zero_passes(self, engine):
        verdict = engine.evaluate(_passing_metrics(wns_ns=0.0))
        assert verdict.overall_status == "PASS"

    def test_wns_negative_fails(self, engine):
        verdict = engine.evaluate(_passing_metrics(wns_ns=-0.01))
        assert verdict.overall_status == "FAIL"

    def test_wns_negative_fails_correct_metric(self, engine):
        verdict = engine.evaluate(_passing_metrics(wns_ns=-0.15))
        failed_names = [v.metric for v in verdict.failed_metrics]
        assert "WNS (ns)" in failed_names

    def test_wns_none_skipped(self, engine):
        """If WNS is unavailable, the metric should be skipped (not fail)."""
        verdict = engine.evaluate(_passing_metrics(wns_ns=None))
        wns_mv = next(v for v in verdict.metric_verdicts if v.metric == "WNS (ns)")
        assert wns_mv.passed, "Missing WNS should not cause a FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# TNS boundary
# ─────────────────────────────────────────────────────────────────────────────

class TestTNSThreshold:
    def test_tns_zero_passes(self, engine):
        verdict = engine.evaluate(_passing_metrics(tns_ns=0.0))
        assert verdict.overall_status == "PASS"

    def test_tns_negative_fails(self, engine):
        verdict = engine.evaluate(_passing_metrics(tns_ns=-1.20))
        assert verdict.overall_status == "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# Timing violations
# ─────────────────────────────────────────────────────────────────────────────

class TestViolationThreshold:
    def test_zero_violations_passes(self, engine):
        verdict = engine.evaluate(_passing_metrics(num_timing_violations=0))
        assert verdict.overall_status == "PASS"

    def test_one_violation_fails(self, engine):
        verdict = engine.evaluate(_passing_metrics(num_timing_violations=1))
        assert verdict.overall_status == "FAIL"

    def test_many_violations_fail(self, engine):
        verdict = engine.evaluate(_passing_metrics(num_timing_violations=100))
        assert verdict.overall_status == "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# DRC violations
# ─────────────────────────────────────────────────────────────────────────────

class TestDRCThreshold:
    def test_zero_drc_passes(self, engine):
        verdict = engine.evaluate(_passing_metrics(num_drc_violations=0))
        assert verdict.overall_status == "PASS"

    def test_one_drc_fails(self, engine):
        verdict = engine.evaluate(_passing_metrics(num_drc_violations=1))
        assert verdict.overall_status == "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# Utilization
# ─────────────────────────────────────────────────────────────────────────────

class TestUtilizationThreshold:
    def test_85_pct_passes(self, engine):
        verdict = engine.evaluate(_passing_metrics(utilization_pct=85.0))
        assert verdict.overall_status == "PASS"

    def test_above_85_fails(self, engine):
        verdict = engine.evaluate(_passing_metrics(utilization_pct=86.0))
        assert verdict.overall_status == "FAIL"

    def test_utilization_none_skipped(self, engine):
        """Utilization is optional — absent data should not cause a FAIL."""
        verdict = engine.evaluate(_passing_metrics(utilization_pct=None))
        assert verdict.overall_status == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# Cell count sanity
# ─────────────────────────────────────────────────────────────────────────────

class TestCellCountSanity:
    def test_positive_cell_count_passes(self, engine):
        verdict = engine.evaluate(_passing_metrics(cell_count=100))
        assert verdict.overall_status == "PASS"

    def test_zero_cell_count_fails(self, engine):
        """Zero cells = degenerate / empty synthesis."""
        verdict = engine.evaluate(_passing_metrics(cell_count=0))
        assert verdict.overall_status == "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# Multiple failures
# ─────────────────────────────────────────────────────────────────────────────

class TestMultipleFailures:
    def test_two_failures_reported(self, engine):
        verdict = engine.evaluate(_passing_metrics(wns_ns=-0.15, tns_ns=-1.20))
        failed = [v.metric for v in verdict.failed_metrics]
        assert "WNS (ns)" in failed
        assert "TNS (ns)" in failed

    def test_overall_fail_if_any_metric_fails(self, engine):
        # Only DRC fails, everything else passes
        verdict = engine.evaluate(_passing_metrics(num_drc_violations=3))
        assert verdict.overall_status == "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# Default thresholds (no YAML file)
# ─────────────────────────────────────────────────────────────────────────────

class TestDefaultThresholds:
    def test_works_without_yaml(self):
        engine = RulesEngine(thresholds_path=None)
        verdict = engine.evaluate(_passing_metrics())
        assert verdict.overall_status == "PASS"

    def test_summary_lines_non_empty(self):
        engine = RulesEngine(thresholds_path=None)
        verdict = engine.evaluate(_passing_metrics())
        lines = verdict.summary_lines()
        assert len(lines) > 0
