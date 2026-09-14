"""
tests/test_parser.py — Unit tests for the log parser.

Tests run against fixture logs in tests/sample_logs/ — no EDA tool
installation required.
"""

import pytest
from pathlib import Path
from signoff_copilot.parser import parse_run, ParsedMetrics

# ── Fixture paths ──────────────────────────────────────────────────────────
SAMPLE_DIR = Path(__file__).parent / "sample_logs"

TIMING_PASS_RPT  = SAMPLE_DIR / "timing_pass.rpt"
TIMING_FAIL_RPT  = SAMPLE_DIR / "timing_fail.rpt"
SYNTH_PASS_LOG   = SAMPLE_DIR / "synth_pass.log"


# ─────────────────────────────────────────────────────────────────────────────
# Helper
# ─────────────────────────────────────────────────────────────────────────────

def _parse(timing_rpt=None, synth_log=None, drc_rpt=None) -> ParsedMetrics:
    return parse_run(
        run_id="test-run-001",
        design="picorv32",
        clock_period_ns=4.0,
        runtime_sec=42.0,
        timestamp="2026-09-14T10:00:00Z",
        synth_log_path=synth_log,
        timing_rpt_path=timing_rpt,
        drc_rpt_path=drc_rpt,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Timing — passing run
# ─────────────────────────────────────────────────────────────────────────────

class TestTimingPassReport:
    def test_wns_is_zero_or_positive(self):
        m = _parse(timing_rpt=TIMING_PASS_RPT)
        assert m.wns_ns is not None
        assert m.wns_ns >= 0.0, f"Expected WNS ≥ 0, got {m.wns_ns}"

    def test_tns_is_zero(self):
        m = _parse(timing_rpt=TIMING_PASS_RPT)
        assert m.tns_ns is not None
        assert m.tns_ns == 0.0, f"Expected TNS = 0, got {m.tns_ns}"

    def test_no_timing_violations(self):
        m = _parse(timing_rpt=TIMING_PASS_RPT)
        assert m.num_timing_violations == 0

    def test_worst_path_excerpt_populated(self):
        m = _parse(timing_rpt=TIMING_PASS_RPT)
        assert len(m.worst_path_excerpt) > 0

    def test_run_metadata(self):
        m = _parse(timing_rpt=TIMING_PASS_RPT)
        assert m.run_id == "test-run-001"
        assert m.design == "picorv32"
        assert m.clock_period_ns == 4.0
        assert m.runtime_sec == 42.0


# ─────────────────────────────────────────────────────────────────────────────
# Timing — failing run
# ─────────────────────────────────────────────────────────────────────────────

class TestTimingFailReport:
    def test_wns_is_negative(self):
        m = _parse(timing_rpt=TIMING_FAIL_RPT)
        assert m.wns_ns is not None
        assert m.wns_ns < 0.0, f"Expected WNS < 0, got {m.wns_ns}"

    def test_wns_value(self):
        m = _parse(timing_rpt=TIMING_FAIL_RPT)
        assert abs(m.wns_ns - (-0.15)) < 0.001, f"Expected WNS ≈ -0.15, got {m.wns_ns}"

    def test_tns_is_negative(self):
        m = _parse(timing_rpt=TIMING_FAIL_RPT)
        assert m.tns_ns is not None
        assert m.tns_ns < 0.0

    def test_tns_value(self):
        m = _parse(timing_rpt=TIMING_FAIL_RPT)
        assert abs(m.tns_ns - (-1.20)) < 0.01, f"Expected TNS ≈ -1.20, got {m.tns_ns}"

    def test_violation_count(self):
        m = _parse(timing_rpt=TIMING_FAIL_RPT)
        assert m.num_timing_violations > 0

    def test_worst_path_excerpt_contains_violated(self):
        m = _parse(timing_rpt=TIMING_FAIL_RPT)
        assert "VIOLATED" in m.worst_path_excerpt


# ─────────────────────────────────────────────────────────────────────────────
# Synthesis log
# ─────────────────────────────────────────────────────────────────────────────

class TestSynthLog:
    def test_cell_count_parsed(self):
        m = _parse(synth_log=SYNTH_PASS_LOG)
        assert m.cell_count > 0, f"Expected cell_count > 0, got {m.cell_count}"

    def test_cell_count_value(self):
        m = _parse(synth_log=SYNTH_PASS_LOG)
        assert m.cell_count == 3421, f"Expected 3421, got {m.cell_count}"

    def test_chip_area_parsed(self):
        m = _parse(synth_log=SYNTH_PASS_LOG)
        assert m.chip_area_um2 is not None
        assert m.chip_area_um2 > 0


# ─────────────────────────────────────────────────────────────────────────────
# Robustness — missing/empty files
# ─────────────────────────────────────────────────────────────────────────────

class TestRobustness:
    def test_missing_synth_log_does_not_crash(self):
        m = _parse(timing_rpt=TIMING_PASS_RPT, synth_log=None)
        assert m is not None
        assert m.cell_count == 0   # default value

    def test_missing_timing_rpt_does_not_crash(self):
        m = _parse(synth_log=SYNTH_PASS_LOG, timing_rpt=None)
        assert m is not None
        assert m.wns_ns is None    # could not be parsed

    def test_nonexistent_file_does_not_crash(self, tmp_path):
        m = _parse(timing_rpt=tmp_path / "does_not_exist.rpt")
        assert m is not None

    def test_to_dict_serialisable(self):
        m = _parse(timing_rpt=TIMING_FAIL_RPT, synth_log=SYNTH_PASS_LOG)
        d = m.to_dict()
        assert isinstance(d, dict)
        assert d["run_id"] == "test-run-001"
        assert "wns_ns" in d
