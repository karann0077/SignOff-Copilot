"""
tests/test_rules_engine.py — Unit tests for the rules engine.

Tests every threshold boundary using in-memory ParsedMetrics objects.
The RulesEngine is now driven by RunSpec (not thresholds.yaml directly).
No file I/O, no EDA tools needed.
"""

import pytest
from pathlib import Path

from signoff_copilot.parser import ParsedMetrics
from signoff_copilot.rules_engine import RulesEngine, Outcome
from signoff_copilot.spec_interpreter import RunSpec

_REPO_ROOT      = Path(__file__).parent.parent
_THRESHOLDS_CFG = _REPO_ROOT / "config" / "thresholds.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a clean passing metrics object
# Note: num_timing_violations is now a @property = setup + hold.
#       Use num_setup_violations / num_hold_violations directly.
# ─────────────────────────────────────────────────────────────────────────────

def _passing_metrics(**overrides) -> ParsedMetrics:
    m = ParsedMetrics(
        run_id="test-rules-001",
        design="picorv32",
        clock_period_ns=4.0,
        wns_ns=0.22,
        tns_ns=0.0,
        num_setup_violations=0,
        num_hold_violations=0,
        num_drc_violations=0,
        cell_count=3421,
        utilization_pct=62.5,
        runtime_sec=180.0,
        timestamp="2026-09-14T10:00:00Z",
    )
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


def _default_spec(**overrides) -> RunSpec:
    """RunSpec with strict defaults matching the legacy thresholds.yaml."""
    spec = RunSpec(
        design="picorv32",
        clock_period_ns=4.0,
        check_setup=True,
        wns_ns_min=0.0,
        tns_ns_min=0.0,
        max_setup_violations=0,
        check_hold=True,
        max_hold_violations=0,
        max_drc_violations=0,
        max_utilization_pct=85.0,
        min_cell_count=1,
    )
    for k, v in overrides.items():
        setattr(spec, k, v)
    return spec


@pytest.fixture
def engine() -> RulesEngine:
    """Engine driven by a strict default spec — same semantics as old YAML."""
    return RulesEngine(spec=_default_spec())


# ─────────────────────────────────────────────────────────────────────────────
# Backward-compat: legacy constructor path (thresholds_path= still works)
# ─────────────────────────────────────────────────────────────────────────────

class TestLegacyConstructor:
    def test_legacy_thresholds_path_still_works(self):
        engine = RulesEngine(thresholds_path=_THRESHOLDS_CFG)
        verdict = engine.evaluate(_passing_metrics())
        assert verdict.overall_status == "PASS"

    def test_none_thresholds_path_uses_defaults(self):
        engine = RulesEngine(thresholds_path=None)
        verdict = engine.evaluate(_passing_metrics())
        assert verdict.overall_status == "PASS"


# ─────────────────────────────────────────────────────────────────────────────
# Nominal pass
# ─────────────────────────────────────────────────────────────────────────────

class TestNominalPass:
    def test_overall_status_pass(self, engine):
        verdict = engine.evaluate(_passing_metrics())
        assert verdict.overall_status == "PASS"

    def test_all_active_metrics_pass(self, engine):
        verdict = engine.evaluate(_passing_metrics())
        for mv in verdict.metric_verdicts:
            if mv.outcome in (Outcome.SKIPPED, Outcome.UNSUPPORTED):
                continue
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

    def test_wns_none_when_check_setup_true_is_missing(self):
        """Missing WNS when check_setup=True must be MISSING → FAIL."""
        spec = _default_spec(check_setup=True, wns_ns_min=0.0)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(wns_ns=None))
        wns_mv = next(v for v in verdict.metric_verdicts if v.metric == "WNS (ns)")
        assert wns_mv.outcome == Outcome.MISSING
        assert verdict.overall_status == "FAIL"


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

    def test_tns_not_checked_when_tns_min_none(self):
        """If tns_ns_min is None in the spec, TNS check is skipped."""
        spec = _default_spec(tns_ns_min=None)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(tns_ns=-99.0))
        # TNS check was skipped, so overall status should still be PASS
        # (assuming WNS and other metrics are fine)
        tns_mvs = [v for v in verdict.metric_verdicts if "TNS" in v.metric]
        assert len(tns_mvs) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Setup violation count
# ─────────────────────────────────────────────────────────────────────────────

class TestSetupViolationThreshold:
    def test_zero_violations_passes(self, engine):
        verdict = engine.evaluate(_passing_metrics(num_setup_violations=0))
        assert verdict.overall_status == "PASS"

    def test_one_setup_violation_fails(self, engine):
        verdict = engine.evaluate(_passing_metrics(num_setup_violations=1))
        assert verdict.overall_status == "FAIL"

    def test_many_setup_violations_fail(self, engine):
        verdict = engine.evaluate(_passing_metrics(num_setup_violations=100))
        assert verdict.overall_status == "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# Hold violation count (new)
# ─────────────────────────────────────────────────────────────────────────────

class TestHoldViolationThreshold:
    def test_zero_hold_violations_passes(self, engine):
        verdict = engine.evaluate(_passing_metrics(num_hold_violations=0))
        assert verdict.overall_status == "PASS"

    def test_one_hold_violation_fails(self, engine):
        verdict = engine.evaluate(_passing_metrics(num_hold_violations=1))
        assert verdict.overall_status == "FAIL"

    def test_hold_check_skipped_when_check_hold_false(self):
        spec = _default_spec(check_hold=False)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(num_hold_violations=99))
        hold_mvs = [v for v in verdict.metric_verdicts if "Hold" in v.metric]
        assert len(hold_mvs) == 0

    def test_num_timing_violations_is_sum(self):
        """num_timing_violations property = setup + hold."""
        m = _passing_metrics(num_setup_violations=2, num_hold_violations=3)
        assert m.num_timing_violations == 5


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

    def test_drc_not_checked_when_not_in_spec(self):
        spec = _default_spec(max_drc_violations=None)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(num_drc_violations=99))
        drc_mvs = [v for v in verdict.metric_verdicts if "DRC" in v.metric]
        assert len(drc_mvs) == 0


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

    def test_utilization_none_not_fail_when_not_measured(self):
        """If utilization is None but was requested, it becomes MISSING → FAIL."""
        spec = _default_spec(max_utilization_pct=85.0)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(utilization_pct=None))
        util_mv = next(v for v in verdict.metric_verdicts if "Utilization" in v.metric)
        # Requested but missing → MISSING
        assert util_mv.outcome == Outcome.MISSING

    def test_custom_util_threshold(self):
        spec = _default_spec(max_utilization_pct=75.0)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(utilization_pct=76.0))
        assert verdict.overall_status == "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# Area check (new)
# ─────────────────────────────────────────────────────────────────────────────

class TestAreaThreshold:
    def test_area_below_threshold_passes(self):
        spec = _default_spec(max_chip_area_um2=25000.0)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(chip_area_um2=15000.0))
        assert verdict.overall_status == "PASS"

    def test_area_above_threshold_fails(self):
        spec = _default_spec(max_chip_area_um2=10000.0)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(chip_area_um2=15000.0))
        assert verdict.overall_status == "FAIL"

    def test_area_not_checked_when_not_in_spec(self, engine):
        """Default spec has no area threshold → not checked."""
        verdict = engine.evaluate(_passing_metrics(chip_area_um2=999999.0))
        area_mvs = [v for v in verdict.metric_verdicts if "area" in v.metric.lower()]
        assert len(area_mvs) == 0


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
# MISSING outcome (Bug 2 fix)
# ─────────────────────────────────────────────────────────────────────────────

class TestMissingOutcome:
    def test_missing_wns_causes_fail(self):
        spec = _default_spec(check_setup=True, wns_ns_min=0.0)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(wns_ns=None))
        assert verdict.overall_status == "FAIL"

    def test_missing_wns_outcome_is_missing(self):
        spec = _default_spec(check_setup=True, wns_ns_min=0.0)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(wns_ns=None))
        wns_mv = next(v for v in verdict.metric_verdicts if v.metric == "WNS (ns)")
        assert wns_mv.outcome == Outcome.MISSING

    def test_failed_metrics_includes_missing(self):
        spec = _default_spec(check_setup=True, wns_ns_min=0.0)
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics(wns_ns=None))
        assert any(v.outcome == Outcome.MISSING for v in verdict.failed_metrics)


# ─────────────────────────────────────────────────────────────────────────────
# UNSUPPORTED requirements
# ─────────────────────────────────────────────────────────────────────────────

class TestUnsupportedRequirements:
    def test_unsupported_reqs_appear_in_verdict(self):
        spec = _default_spec()
        spec.unsupported_requirements = ["power < 50mW", "IR drop"]
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics())
        assert len(verdict.unsupported_metrics) == 2

    def test_unsupported_does_not_fail_run(self):
        spec = _default_spec()
        spec.unsupported_requirements = ["power < 50mW"]
        engine = RulesEngine(spec=spec)
        verdict = engine.evaluate(_passing_metrics())
        # Unsupported requirement should not cause FAIL
        assert verdict.overall_status == "PASS"


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
        verdict = engine.evaluate(_passing_metrics(num_drc_violations=3))
        assert verdict.overall_status == "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# Summary lines
# ─────────────────────────────────────────────────────────────────────────────

class TestSummaryLines:
    def test_summary_lines_non_empty(self, engine):
        verdict = engine.evaluate(_passing_metrics())
        lines = verdict.summary_lines()
        assert len(lines) > 0

    def test_summary_lines_skips_skipped_outcomes(self, engine):
        verdict = engine.evaluate(_passing_metrics())
        for line in verdict.summary_lines():
            assert "[SKIPPED]" not in line
