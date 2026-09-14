"""
tests/test_spec_interpreter.py — Tests for RunSpec creation.

Tests both paths:
  • CLI-flag path (no LLM — uses spec_from_flags)
  • NL path with a mocked LLM response (uses spec_from_nl with monkeypatching)
"""
from __future__ import annotations

import json
import pytest

from signoff_copilot.spec_interpreter import (
    RunSpec,
    spec_from_flags,
    spec_from_nl,
    apply_yaml_defaults,
    _parse_llm_response,
)


# ─────────────────────────────────────────────────────────────────────────────
# CLI-flag path (no LLM needed)
# ─────────────────────────────────────────────────────────────────────────────

class TestSpecFromFlags:

    def test_basic_clock_period(self):
        spec = spec_from_flags(design="picorv32", clock_period_ns=4.0)
        assert spec.design == "picorv32"
        assert spec.clock_period_ns == 4.0
        assert spec.resolved_clock_period_ns() == 4.0
        assert spec.source == "cli"

    def test_freq_derivation(self):
        spec = spec_from_flags(design="picorv32", clock_period_ns=4.0)
        # 4 ns → 250 MHz
        assert abs(spec.resolved_freq_mhz() - 250.0) < 0.01

    def test_250mhz_period(self):
        spec = spec_from_flags(design="picorv32", clock_period_ns=4.0)
        assert abs(spec.resolved_clock_period_ns() - 4.0) < 1e-9

    def test_300mhz_period(self):
        # 300 MHz → 3.333... ns
        spec = spec_from_flags(design="picorv32", clock_period_ns=round(1000/300, 6))
        assert abs(spec.resolved_freq_mhz() - 300.0) < 0.5

    def test_yaml_defaults_filled(self, tmp_path):
        """Spec from flags should fill YAML defaults for timing thresholds."""
        import yaml
        cfg = {"thresholds": {"wns_ns_min": 0.0, "tns_ns_min": 0.0,
                               "max_timing_violations": 0, "max_drc_violations": 0,
                               "max_utilization_pct": 85.0, "min_cell_count": 1}}
        p = tmp_path / "t.yaml"
        p.write_text(yaml.dump(cfg))
        spec = spec_from_flags(design="test", clock_period_ns=5.0, thresholds_path=p)
        assert spec.wns_ns_min == 0.0
        assert spec.max_utilization_pct == 85.0
        assert spec.min_cell_count == 1

    def test_explicit_overrides(self):
        spec = spec_from_flags(
            design="picorv32",
            clock_period_ns=4.0,
            max_utilization_pct=75.0,
            max_chip_area_um2=20000.0,
        )
        assert spec.max_utilization_pct == 75.0
        assert spec.max_chip_area_um2 == 20000.0

    def test_compare_with_previous_flag(self):
        spec = spec_from_flags(design="picorv32", clock_period_ns=4.0,
                               compare_with_previous=True)
        assert spec.compare_with_previous is True

    def test_io_delay_fields(self):
        spec = spec_from_flags(design="d", clock_period_ns=5.0,
                               input_delay_ns=0.3, output_delay_ns=0.4,
                               clock_uncertainty_ns=0.05)
        assert spec.input_delay_ns == 0.3
        assert spec.output_delay_ns == 0.4
        assert spec.clock_uncertainty_ns == 0.05

    def test_design_stored(self):
        spec = spec_from_flags(design="mydesign", clock_period_ns=5.0)
        assert spec.design == "mydesign"


# ─────────────────────────────────────────────────────────────────────────────
# LLM response parsing (no real API call)
# ─────────────────────────────────────────────────────────────────────────────

class TestParseLLMResponse:

    def _response(self, **kwargs) -> str:
        """Build a minimal valid LLM JSON response."""
        data = {
            "design": "picorv32",
            "clock_freq_mhz": 250,
            "check_setup": True,
            "check_hold": True,
            "compare_with_previous": False,
            "unsupported_requirements": [],
            "explicitly_excluded": [],
        }
        data.update(kwargs)
        return json.dumps(data)

    def test_250mhz_request(self):
        raw = self._response(clock_freq_mhz=250)
        spec = _parse_llm_response(raw, "picorv32", "test")
        assert abs(spec.resolved_clock_period_ns() - 4.0) < 0.01

    def test_300mhz_request(self):
        raw = self._response(clock_freq_mhz=300)
        spec = _parse_llm_response(raw, "picorv32", "test")
        assert abs(spec.resolved_freq_mhz() - 300.0) < 0.5

    def test_area_threshold_parsed(self):
        raw = self._response(max_chip_area_um2=25000.0)
        spec = _parse_llm_response(raw, "picorv32", "test")
        assert spec.max_chip_area_um2 == 25000.0

    def test_utilization_threshold_parsed(self):
        raw = self._response(max_utilization_pct=75.0)
        spec = _parse_llm_response(raw, "picorv32", "test")
        assert spec.max_utilization_pct == 75.0

    def test_hold_check_enabled(self):
        raw = self._response(check_hold=True, max_hold_violations=0)
        spec = _parse_llm_response(raw, "picorv32", "test")
        assert spec.check_hold is True
        assert spec.max_hold_violations == 0

    def test_unsupported_requirements_captured(self):
        raw = self._response(unsupported_requirements=["power < 50mW", "IR drop < 10%"])
        spec = _parse_llm_response(raw, "picorv32", "test")
        assert len(spec.unsupported_requirements) == 2
        assert "power < 50mW" in spec.unsupported_requirements

    def test_regression_flag_parsed(self):
        raw = self._response(compare_with_previous=True)
        spec = _parse_llm_response(raw, "picorv32", "test")
        assert spec.compare_with_previous is True

    def test_excluded_checks(self):
        raw = self._response(check_hold=False,
                             explicitly_excluded=["hold timing"])
        spec = _parse_llm_response(raw, "picorv32", "test")
        assert spec.check_hold is False
        assert "hold timing" in spec.explicitly_excluded

    def test_invalid_json_returns_empty_spec(self):
        spec = _parse_llm_response("NOT JSON {{{", "picorv32", "bad request")
        assert spec.design == "picorv32"
        assert spec.source == "nl_parse_error"

    def test_clock_period_derives_freq(self):
        raw = self._response(clock_period_ns=5.0)
        spec = _parse_llm_response(raw, "d", "t")
        assert abs(spec.resolved_freq_mhz() - 200.0) < 0.5

    def test_nl_request_stored(self):
        raw = self._response()
        spec = _parse_llm_response(raw, "picorv32", "Analyze at 250 MHz")
        assert spec.nl_request == "Analyze at 250 MHz"


# ─────────────────────────────────────────────────────────────────────────────
# RunSpec derived helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestRunSpecHelpers:

    def test_resolved_period_from_period(self):
        spec = RunSpec(design="d", clock_period_ns=3.333)
        assert abs(spec.resolved_clock_period_ns() - 3.333) < 1e-9

    def test_resolved_period_from_freq(self):
        spec = RunSpec(design="d", clock_freq_mhz=500.0)
        assert abs(spec.resolved_clock_period_ns() - 2.0) < 1e-6

    def test_resolved_period_fallback(self):
        spec = RunSpec(design="d")
        assert spec.resolved_clock_period_ns(fallback=5.0) == 5.0

    def test_to_dict_includes_design(self):
        spec = RunSpec(design="myrv32", clock_period_ns=4.0)
        d = spec.to_dict()
        assert d["design"] == "myrv32"
        assert d["clock_period_ns"] == 4.0
