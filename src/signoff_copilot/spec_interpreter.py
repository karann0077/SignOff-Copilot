"""
spec_interpreter.py — Natural-language signoff specification interpreter.

Responsibility (single):
    Convert a natural-language debugging/signoff request  ─OR─  a set of
    explicit CLI parameters into a validated RunSpec dataclass.

The RunSpec is the single source of truth for what a run must check.
It flows downstream into:
    • orchestrator  (clock period → generates SDC)
    • rules_engine  (thresholds to evaluate)
    • llm_explain   (context for the AI explanation)
    • regression    (what to compare)

Design constraints
------------------
- The LLM only *interprets* the request.  It never executes anything.
- RunSpec is schema-validated before leaving this module.
- If no LLM key is available the module still works: it populates RunSpec
  from the explicit parameters supplied by the caller (CLI-flag path).
- Unsupported requirements (metrics we cannot measure) are surfaced in
  RunSpec.unsupported_requirements and shown to the user; they never
  silently become PASS.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# RunSpec — the validated signoff specification
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RunSpec:
    """
    Validated specification for a single signoff run.

    All threshold fields are Optional.  None means "not requested — skip this
    check".  This allows partial specifications such as "only check timing,
    not area".

    The rules engine must treat a *requested* check whose metric could not be
    measured as MISSING (→ FAIL), not as a skip.
    """

    # ── Design identity ───────────────────────────────────────────────────────
    design: str

    # ── Clock ─────────────────────────────────────────────────────────────────
    clock_period_ns: Optional[float] = None     # derived from freq if absent
    clock_freq_mhz:  Optional[float] = None     # derived from period if absent

    # ── Setup timing requirements ─────────────────────────────────────────────
    check_setup: bool = True
    wns_ns_min:  Optional[float] = None         # None → use flow default (0.0)
    tns_ns_min:  Optional[float] = None
    max_setup_violations: Optional[int] = None

    # ── Hold timing requirements ──────────────────────────────────────────────
    check_hold: bool = True
    max_hold_violations: Optional[int] = None

    # ── Area / capacity requirements ──────────────────────────────────────────
    max_chip_area_um2:  Optional[float] = None
    max_cell_count:     Optional[int]   = None
    min_cell_count:     Optional[int]   = None  # sanity floor (degenerate synth)
    max_utilization_pct: Optional[float] = None

    # ── Physical verification ─────────────────────────────────────────────────
    max_drc_violations: Optional[int] = None

    # ── I/O constraints (written into the generated SDC) ─────────────────────
    input_delay_ns:       Optional[float] = None
    output_delay_ns:      Optional[float] = None
    clock_uncertainty_ns: Optional[float] = None
    clock_latency_ns:     Optional[float] = None

    # ── Regression ────────────────────────────────────────────────────────────
    compare_with_previous: bool = False
    compare_with_run_id:   Optional[str] = None

    # ── Audit / provenance ────────────────────────────────────────────────────
    nl_request:               str        = ""
    unsupported_requirements: list[str]  = field(default_factory=list)
    explicitly_excluded:      list[str]  = field(default_factory=list)
    source:                   str        = "cli"  # "cli" | "nl"

    # ── Derived helpers ───────────────────────────────────────────────────────

    def resolved_clock_period_ns(self, fallback: float = 4.0) -> float:
        """Return the clock period, deriving it from frequency if needed."""
        if self.clock_period_ns is not None:
            return self.clock_period_ns
        if self.clock_freq_mhz is not None and self.clock_freq_mhz > 0:
            return 1000.0 / self.clock_freq_mhz
        return fallback

    def resolved_freq_mhz(self, fallback: float = 4.0) -> float:
        period = self.resolved_clock_period_ns(fallback)
        return 1000.0 / period if period > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "design":               self.design,
            "clock_period_ns":      self.resolved_clock_period_ns(),
            "clock_freq_mhz":       self.resolved_freq_mhz(),
            "check_setup":          self.check_setup,
            "wns_ns_min":           self.wns_ns_min,
            "tns_ns_min":           self.tns_ns_min,
            "max_setup_violations": self.max_setup_violations,
            "check_hold":           self.check_hold,
            "max_hold_violations":  self.max_hold_violations,
            "max_chip_area_um2":    self.max_chip_area_um2,
            "max_cell_count":       self.max_cell_count,
            "min_cell_count":       self.min_cell_count,
            "max_utilization_pct":  self.max_utilization_pct,
            "max_drc_violations":   self.max_drc_violations,
            "input_delay_ns":       self.input_delay_ns,
            "output_delay_ns":      self.output_delay_ns,
            "clock_uncertainty_ns": self.clock_uncertainty_ns,
            "clock_latency_ns":     self.clock_latency_ns,
            "compare_with_previous":self.compare_with_previous,
            "compare_with_run_id":  self.compare_with_run_id,
            "nl_request":           self.nl_request,
            "unsupported_requirements": self.unsupported_requirements,
            "explicitly_excluded":  self.explicitly_excluded,
            "source":               self.source,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Default thresholds (loaded from thresholds.yaml, used as fallback template)
# ─────────────────────────────────────────────────────────────────────────────

def _load_yaml_defaults(thresholds_path=None) -> dict:
    """Load thresholds.yaml and return the dict under 'thresholds' key."""
    if thresholds_path is None:
        from pathlib import Path
        thresholds_path = Path(__file__).resolve().parents[3] / "config" / "thresholds.yaml"
    try:
        import yaml
        from pathlib import Path
        p = Path(thresholds_path)
        if p.exists():
            with open(p, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            return (cfg or {}).get("thresholds", {})
    except Exception as exc:
        log.warning("Could not load thresholds.yaml: %s", exc)
    return {}


def apply_yaml_defaults(spec: RunSpec, thresholds_path=None) -> RunSpec:
    """
    Fill in any None threshold fields from thresholds.yaml.
    Only fills fields that are still None — explicit spec values take priority.
    """
    defaults = _load_yaml_defaults(thresholds_path)

    def _fill(attr: str, key: str, cast=float):
        if getattr(spec, attr) is None and key in defaults:
            try:
                setattr(spec, attr, cast(defaults[key]))
            except (ValueError, TypeError):
                pass

    _fill("wns_ns_min",          "wns_ns_min")
    _fill("tns_ns_min",          "tns_ns_min")
    _fill("max_setup_violations","max_timing_violations", int)
    _fill("max_hold_violations", "max_hold_violations",   int)
    _fill("max_drc_violations",  "max_drc_violations",    int)
    _fill("max_utilization_pct", "max_utilization_pct")
    _fill("min_cell_count",      "min_cell_count",        int)
    _fill("max_chip_area_um2",   "max_chip_area_um2")

    return spec


# ─────────────────────────────────────────────────────────────────────────────
# CLI-flag → RunSpec  (no LLM needed)
# ─────────────────────────────────────────────────────────────────────────────

def spec_from_flags(
    design: str,
    clock_period_ns: float = 4.0,
    *,
    thresholds_path=None,
    # optional explicit overrides
    max_utilization_pct: Optional[float] = None,
    max_drc_violations:  Optional[int]   = None,
    max_chip_area_um2:   Optional[float] = None,
    compare_with_previous: bool = False,
    compare_with_run_id:   Optional[str] = None,
    input_delay_ns:        Optional[float] = None,
    output_delay_ns:       Optional[float] = None,
    clock_uncertainty_ns:  Optional[float] = None,
) -> RunSpec:
    """
    Build a RunSpec purely from CLI parameters — no LLM call.
    Missing thresholds are filled from thresholds.yaml defaults.
    """
    spec = RunSpec(
        design=design,
        clock_period_ns=clock_period_ns,
        clock_freq_mhz=round(1000.0 / clock_period_ns, 3) if clock_period_ns else None,
        max_utilization_pct=max_utilization_pct,
        max_drc_violations=max_drc_violations,
        max_chip_area_um2=max_chip_area_um2,
        compare_with_previous=compare_with_previous,
        compare_with_run_id=compare_with_run_id,
        input_delay_ns=input_delay_ns,
        output_delay_ns=output_delay_ns,
        clock_uncertainty_ns=clock_uncertainty_ns,
        source="cli",
    )
    return apply_yaml_defaults(spec, thresholds_path)


# ─────────────────────────────────────────────────────────────────────────────
# LLM prompt for NL → RunSpec
# ─────────────────────────────────────────────────────────────────────────────

_NL_SYSTEM_PROMPT = """\
You are a signoff specification parser for an EDA (chip design) automation tool.
Your ONLY job is to extract a structured signoff specification from the engineer's
natural-language request. You do NOT run any tools or make any pass/fail decisions.

The supported measurable checks are ONLY:
  clock_period_ns, clock_freq_mhz,
  wns_ns_min, tns_ns_min, max_setup_violations,
  max_hold_violations,
  max_chip_area_um2, max_cell_count, min_cell_count, max_utilization_pct,
  max_drc_violations,
  input_delay_ns, output_delay_ns, clock_uncertainty_ns, clock_latency_ns,
  compare_with_previous, compare_with_run_id,
  check_setup, check_hold,
  explicitly_excluded

If the engineer requests something that is NOT in the supported list above
(e.g. power, leakage, IR drop, thermal, routing congestion), put a short
description of the unsupported requirement into "unsupported_requirements".

If the engineer says "no X" or "exclude X", put X in "explicitly_excluded"
and set the corresponding check field to false if it exists.

Return ONLY a valid JSON object matching the schema below. No extra text.

Schema (all fields optional except design):
{
  "design": "string — design name, e.g. picorv32",
  "clock_period_ns": number | null,
  "clock_freq_mhz": number | null,
  "check_setup": boolean,
  "wns_ns_min": number | null,
  "tns_ns_min": number | null,
  "max_setup_violations": integer | null,
  "check_hold": boolean,
  "max_hold_violations": integer | null,
  "max_chip_area_um2": number | null,
  "max_cell_count": integer | null,
  "min_cell_count": integer | null,
  "max_utilization_pct": number | null,
  "max_drc_violations": integer | null,
  "input_delay_ns": number | null,
  "output_delay_ns": number | null,
  "clock_uncertainty_ns": number | null,
  "clock_latency_ns": number | null,
  "compare_with_previous": boolean,
  "compare_with_run_id": "string | null",
  "unsupported_requirements": ["list of strings"],
  "explicitly_excluded": ["list of strings"]
}
"""

_NL_USER_TEMPLATE = """\
Design context: the design being analysed is "{design}".

Engineer's request:
{nl_request}

Extract the signoff specification as JSON now:
"""


# ─────────────────────────────────────────────────────────────────────────────
# Public API: NL → RunSpec
# ─────────────────────────────────────────────────────────────────────────────

def spec_from_nl(
    nl_request: str,
    design: str,
    *,
    thresholds_path=None,
    model: str = "gpt-4o-mini",
    api_base_url: Optional[str] = None,
) -> RunSpec:
    """
    Interpret a natural-language signoff request and return a validated RunSpec.

    Falls back to a minimal spec (YAML defaults only) if the LLM API is
    unavailable — the design name and nl_request are still recorded for audit.

    Parameters
    ----------
    nl_request      : Free-form engineer request string
    design          : Design name (passed as context to the LLM)
    thresholds_path : Path to thresholds.yaml for default filling
    model           : LLM model name
    api_base_url    : Override LLM endpoint (Groq / Gemini / local)
    """
    raw_json = _call_llm(nl_request, design, model, api_base_url)

    if raw_json is None:
        log.warning("LLM unavailable — building minimal spec from YAML defaults")
        spec = RunSpec(design=design, nl_request=nl_request, source="nl_fallback")
        return apply_yaml_defaults(spec, thresholds_path)

    spec = _parse_llm_response(raw_json, design, nl_request)
    spec = apply_yaml_defaults(spec, thresholds_path)
    return spec


def _call_llm(
    nl_request: str,
    design: str,
    model: str,
    api_base_url: Optional[str],
) -> Optional[str]:
    """Call the LLM and return raw JSON string, or None on any failure."""
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("openai package not installed — cannot parse NL request")
        return None

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        log.warning("No LLM API key found — cannot parse NL request")
        return None

    user_msg = _NL_USER_TEMPLATE.format(
        design=design,
        nl_request=nl_request,
    )

    try:
        client_kwargs: dict = {"api_key": api_key}
        if api_base_url:
            client_kwargs["base_url"] = api_base_url

        client = OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _NL_SYSTEM_PROMPT},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.0,       # deterministic extraction
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content.strip()
        log.debug("LLM spec response (%d chars): %s", len(raw), raw[:200])
        return raw

    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return None


def _parse_llm_response(raw_json: str, design: str, nl_request: str) -> RunSpec:
    """Deserialise and validate the LLM JSON into a RunSpec."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        log.warning("LLM returned invalid JSON (%s) — using empty spec", exc)
        return RunSpec(design=design, nl_request=nl_request, source="nl_parse_error")

    def _get(key, cast=None, default=None):
        val = data.get(key)
        if val is None:
            return default
        if cast is not None:
            try:
                return cast(val)
            except (ValueError, TypeError):
                return default
        return val

    spec = RunSpec(
        design=_get("design", str, design),
        nl_request=nl_request,
        source="nl",

        clock_period_ns=_get("clock_period_ns", float),
        clock_freq_mhz=_get("clock_freq_mhz", float),

        check_setup=bool(_get("check_setup", bool, True)),
        wns_ns_min=_get("wns_ns_min", float),
        tns_ns_min=_get("tns_ns_min", float),
        max_setup_violations=_get("max_setup_violations", int),

        check_hold=bool(_get("check_hold", bool, True)),
        max_hold_violations=_get("max_hold_violations", int),

        max_chip_area_um2=_get("max_chip_area_um2", float),
        max_cell_count=_get("max_cell_count", int),
        min_cell_count=_get("min_cell_count", int),
        max_utilization_pct=_get("max_utilization_pct", float),
        max_drc_violations=_get("max_drc_violations", int),

        input_delay_ns=_get("input_delay_ns", float),
        output_delay_ns=_get("output_delay_ns", float),
        clock_uncertainty_ns=_get("clock_uncertainty_ns", float),
        clock_latency_ns=_get("clock_latency_ns", float),

        compare_with_previous=bool(_get("compare_with_previous", bool, False)),
        compare_with_run_id=_get("compare_with_run_id", str),

        unsupported_requirements=list(_get("unsupported_requirements") or []),
        explicitly_excluded=list(_get("explicitly_excluded") or []),
    )

    # Derive missing clock dimension
    if spec.clock_period_ns is None and spec.clock_freq_mhz is not None and spec.clock_freq_mhz > 0:
        spec.clock_period_ns = round(1000.0 / spec.clock_freq_mhz, 6)
    elif spec.clock_freq_mhz is None and spec.clock_period_ns is not None and spec.clock_period_ns > 0:
        spec.clock_freq_mhz = round(1000.0 / spec.clock_period_ns, 3)

    # Validate: design must be non-empty
    if not spec.design:
        spec.design = design

    log.info(
        "RunSpec parsed: clock=%.2f ns, setup=%s, hold=%s, area=%s, util=%s, regression=%s, unsupported=%s",
        spec.resolved_clock_period_ns(),
        spec.check_setup,
        spec.check_hold,
        spec.max_chip_area_um2,
        spec.max_utilization_pct,
        spec.compare_with_previous,
        spec.unsupported_requirements,
    )
    return spec
