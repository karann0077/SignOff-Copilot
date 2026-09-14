"""
parser.py — Log parser for SignOff Copilot.

Converts raw EDA tool output (Yosys synthesis log, OpenSTA timing report,
OpenROAD DRC report) into a structured ParsedMetrics object.

All parsing is rule-based: regex + line matching.
No statistical inference, no ML.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedMetrics:
    """Structured metrics extracted from a single EDA run."""

    run_id: str
    design: str
    clock_period_ns: float

    # ── Timing ───────────────────────────────────────────────────────────────
    wns_ns: Optional[float] = None          # Worst Negative Slack (negative = violation)
    tns_ns: Optional[float] = None          # Total Negative Slack
    num_timing_violations: int = 0          # Count of setup violations

    # ── Physical ─────────────────────────────────────────────────────────────
    num_drc_violations: int = 0

    # ── Area / Capacity ──────────────────────────────────────────────────────
    cell_count: int = 0
    chip_area_um2: Optional[float] = None   # From Yosys stat
    utilization_pct: Optional[float] = None # From OpenROAD (if --pnr used)

    # ── Runtime ──────────────────────────────────────────────────────────────
    runtime_sec: float = 0.0

    # ── Excerpts (for report / LLM prompt) ───────────────────────────────────
    worst_path_excerpt: str = ""
    raw_log_paths: dict = field(default_factory=dict)

    # ── Verdict (set by rules engine, not parser) ─────────────────────────────
    status: str = "UNKNOWN"
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id":               self.run_id,
            "design":               self.design,
            "clock_period_ns":      self.clock_period_ns,
            "wns_ns":               self.wns_ns,
            "tns_ns":               self.tns_ns,
            "num_timing_violations":self.num_timing_violations,
            "num_drc_violations":   self.num_drc_violations,
            "cell_count":           self.cell_count,
            "chip_area_um2":        self.chip_area_um2,
            "utilization_pct":      self.utilization_pct,
            "runtime_sec":          self.runtime_sec,
            "status":               self.status,
            "timestamp":            self.timestamp,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Compiled patterns — keyed to actual OpenSTA / Yosys output formats
# ─────────────────────────────────────────────────────────────────────────────

# OpenSTA: report_wns produces exactly "wns -0.150000"
_RE_WNS = re.compile(r"^\s*wns\s+(-?\d+(?:\.\d+)?)", re.MULTILINE)

# OpenSTA: report_tns produces exactly "tns -1.200000"
_RE_TNS = re.compile(r"^\s*tns\s+(-?\d+(?:\.\d+)?)", re.MULTILINE)

# OpenSTA: path endpoint line contains "slack (VIOLATED)" or "slack (MET)"
_RE_SLACK_VIOLATED = re.compile(r"slack\s+\(VIOLATED\)\s+(-?\d+(?:\.\d+)?)")
_RE_SLACK_MET      = re.compile(r"slack\s+\(MET\)\s+(\d+(?:\.\d+)?)")

# OpenSTA: worst path block starts with "Startpoint:" and ends before next "Startpoint:"
_RE_PATH_BLOCK_START = re.compile(r"^Startpoint:", re.MULTILINE)

# Yosys stat output: "   Number of cells:               3421"
_RE_CELL_COUNT = re.compile(r"Number of cells:\s+(\d+)")

# Yosys stat output: "Chip area for module '\picorv32': 12345.678900"
_RE_CHIP_AREA = re.compile(r"Chip area for module[^:]*:\s+([\d.]+)")

# OpenROAD: "Design area 12345 u^2 62.50% utilization."
_RE_UTIL_OPENROAD = re.compile(
    r"Design area\s+[\d.]+\s+u\^2\s+([\d.]+)%\s+utilization"
)

# OpenROAD DRC: "Total violation count: 5"
_RE_DRC_TOTAL = re.compile(r"Total violation count:\s+(\d+)")
# Fallback: count "[DRC]" or "violation" lines
_RE_DRC_LINE = re.compile(r"\[DRC\]|\bviolation\b", re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def parse_run(
    run_id: str,
    design: str,
    clock_period_ns: float,
    runtime_sec: float,
    timestamp: str,
    synth_log_path: Optional[Path] = None,
    timing_rpt_path: Optional[Path] = None,
    drc_rpt_path: Optional[Path] = None,
) -> ParsedMetrics:
    """
    Parse all available raw logs for a run and return a ParsedMetrics object.

    Parameters
    ----------
    run_id          : Unique run identifier (ISO-8601 timestamp string)
    design          : RTL design name (e.g. "picorv32")
    clock_period_ns : Target clock period in nanoseconds
    runtime_sec     : Wall-clock runtime measured by the orchestrator
    timestamp       : ISO-8601 timestamp string of the run
    synth_log_path  : Path to Yosys synthesis log (stdout)
    timing_rpt_path : Path to OpenSTA timing report
    drc_rpt_path    : Path to OpenROAD DRC report (optional)
    """
    metrics = ParsedMetrics(
        run_id=run_id,
        design=design,
        clock_period_ns=clock_period_ns,
        runtime_sec=runtime_sec,
        timestamp=timestamp,
    )

    if synth_log_path and Path(synth_log_path).exists():
        _parse_synth_log(Path(synth_log_path), metrics)
    else:
        log.warning("Synthesis log not found: %s", synth_log_path)

    if timing_rpt_path and Path(timing_rpt_path).exists():
        _parse_timing_report(Path(timing_rpt_path), metrics)
    else:
        log.warning("Timing report not found: %s", timing_rpt_path)

    if drc_rpt_path and Path(drc_rpt_path).exists():
        _parse_drc_report(Path(drc_rpt_path), metrics)

    # Record which raw files were parsed
    metrics.raw_log_paths = {
        "synth_log":   str(synth_log_path) if synth_log_path else None,
        "timing_rpt":  str(timing_rpt_path) if timing_rpt_path else None,
        "drc_rpt":     str(drc_rpt_path) if drc_rpt_path else None,
    }

    return metrics


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_synth_log(path: Path, m: ParsedMetrics) -> None:
    """Extract cell count and chip area from Yosys 'stat' output."""
    text = path.read_text(encoding="utf-8", errors="replace")

    cell_match = _RE_CELL_COUNT.search(text)
    if cell_match:
        m.cell_count = int(cell_match.group(1))
        log.debug("Parsed cell_count=%d from %s", m.cell_count, path.name)
    else:
        log.warning("Could not find cell count in %s", path.name)

    area_match = _RE_CHIP_AREA.search(text)
    if area_match:
        m.chip_area_um2 = float(area_match.group(1))
        log.debug("Parsed chip_area_um2=%.2f from %s", m.chip_area_um2, path.name)

    # Utilization from OpenROAD (if present in synth log — unlikely but handled)
    util_match = _RE_UTIL_OPENROAD.search(text)
    if util_match:
        m.utilization_pct = float(util_match.group(1))


def _parse_timing_report(path: Path, m: ParsedMetrics) -> None:
    """Extract WNS, TNS, violation count, and worst-path excerpt from OpenSTA report."""
    text = path.read_text(encoding="utf-8", errors="replace")

    # ── WNS ──────────────────────────────────────────────────────────────────
    wns_match = _RE_WNS.search(text)
    if wns_match:
        m.wns_ns = float(wns_match.group(1))
        log.debug("Parsed wns_ns=%.4f from %s", m.wns_ns, path.name)
    else:
        # Fallback: look for the worst slack (VIOLATED) value
        violated = _RE_SLACK_VIOLATED.findall(text)
        if violated:
            m.wns_ns = min(float(v) for v in violated)
            log.debug("WNS from VIOLATED fallback: %.4f", m.wns_ns)
        else:
            met = _RE_SLACK_MET.findall(text)
            if met:
                m.wns_ns = 0.0   # all paths met
            log.warning("Could not parse WNS from %s", path.name)

    # ── TNS ──────────────────────────────────────────────────────────────────
    tns_match = _RE_TNS.search(text)
    if tns_match:
        m.tns_ns = float(tns_match.group(1))
        log.debug("Parsed tns_ns=%.4f from %s", m.tns_ns, path.name)
    else:
        # Fallback: sum all violated slack values
        violated = _RE_SLACK_VIOLATED.findall(text)
        if violated:
            m.tns_ns = sum(float(v) for v in violated)
        elif _RE_SLACK_MET.search(text):
            m.tns_ns = 0.0
        log.warning("Could not parse TNS from %s — using fallback sum")

    # ── Violation count ───────────────────────────────────────────────────────
    m.num_timing_violations = len(_RE_SLACK_VIOLATED.findall(text))
    log.debug("Timing violations: %d", m.num_timing_violations)

    # ── Worst-path excerpt ────────────────────────────────────────────────────
    m.worst_path_excerpt = _extract_worst_path(text)

    # ── Utilization (if OpenROAD output is embedded in timing rpt) ───────────
    if m.utilization_pct is None:
        util_match = _RE_UTIL_OPENROAD.search(text)
        if util_match:
            m.utilization_pct = float(util_match.group(1))


def _parse_drc_report(path: Path, m: ParsedMetrics) -> None:
    """Extract DRC violation count and utilization from OpenROAD DRC report."""
    text = path.read_text(encoding="utf-8", errors="replace")

    total_match = _RE_DRC_TOTAL.search(text)
    if total_match:
        m.num_drc_violations = int(total_match.group(1))
    else:
        # Fallback: count individual violation lines
        m.num_drc_violations = len(_RE_DRC_LINE.findall(text))
    log.debug("DRC violations: %d", m.num_drc_violations)

    util_match = _RE_UTIL_OPENROAD.search(text)
    if util_match:
        m.utilization_pct = float(util_match.group(1))


def _extract_worst_path(text: str, max_chars: int = 2000) -> str:
    """
    Return the first violated timing path block from the report text.
    Capped at max_chars to keep LLM prompts small.
    """
    # Find the first violated path
    first_violated = _RE_SLACK_VIOLATED.search(text)
    if not first_violated:
        # Return a short excerpt of the best path instead
        starts = list(_RE_PATH_BLOCK_START.finditer(text))
        if starts:
            start = starts[0].start()
            return text[start : start + max_chars].strip()
        return ""

    # Walk backwards to find the "Startpoint:" of this violated path
    path_block_end = first_violated.end()
    # Find the last "Startpoint:" before the violated slack
    starts = list(_RE_PATH_BLOCK_START.finditer(text[:path_block_end]))
    if starts:
        block_start = starts[-1].start()
        # Extend to end of the dashes separator after the slack line
        sep_after = text.find("-" * 20, path_block_end)
        block_end = sep_after + 80 if sep_after != -1 else path_block_end + 200
        excerpt = text[block_start:block_end].strip()
        return excerpt[:max_chars]

    return text[max(0, first_violated.start() - 100):first_violated.end() + 100].strip()
