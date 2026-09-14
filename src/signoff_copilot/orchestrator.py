"""
orchestrator.py — Flow orchestrator for SignOff Copilot.

Sequences the EDA tool stages (Yosys → OpenSTA → optional OpenROAD)
via subprocess calls.  Creates a timestamped run directory and returns
the paths of all generated raw logs.

Design principles
-----------------
- Python handles sequencing, directories, environment setup, and timing.
- TCL scripts do the actual tool-facing work (matching real EDA practice).
- Each stage is run independently; failure in one stage is captured and
  reported without silently skipping downstream stages.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StageResult:
    stage:       str
    success:     bool
    returncode:  int
    runtime_sec: float
    log_path:    Optional[Path] = None
    error_msg:   str = ""


@dataclass
class OrchestrationResult:
    run_id:      str
    run_dir:     Path
    design:      str
    clock_period_ns: float
    timestamp:   str
    total_runtime_sec: float = 0.0
    stages:      list[StageResult] = field(default_factory=list)

    # Paths to parsed artefacts
    synth_log_path:   Optional[Path] = None
    netlist_path:     Optional[Path] = None
    timing_rpt_path:  Optional[Path] = None
    drc_rpt_path:     Optional[Path] = None

    @property
    def success(self) -> bool:
        """True only if all executed stages succeeded."""
        return all(s.success for s in self.stages)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class Orchestrator:
    """
    Runs Yosys → OpenSTA → (optional) OpenROAD for a given design.

    Parameters
    ----------
    project_root    : Absolute path to the signoff-copilot repository root
    tools_cfg_path  : Path to config/tools.yaml
    designs_dir     : Path to the designs/ directory
    runs_dir        : Path where run artifacts are written
    """

    def __init__(
        self,
        project_root: Path,
        tools_cfg_path: Optional[Path] = None,
        designs_dir: Optional[Path] = None,
        runs_dir: Optional[Path] = None,
    ):
        self.project_root = Path(project_root)
        self.tools_cfg_path = tools_cfg_path or (self.project_root / "config" / "tools.yaml")
        self.designs_dir = designs_dir or (self.project_root / "designs")
        self.runs_dir    = runs_dir    or (self.project_root / "runs")

        self._tools_cfg = self._load_tools_cfg()

    # ── Public ───────────────────────────────────────────────────────────────

    def run(
        self,
        design: str,
        clock_period_ns: float = 4.0,   # kept for backward compat
        enable_pnr: bool = False,
        spec=None,                       # RunSpec (preferred); overrides clock_period_ns
    ) -> OrchestrationResult:
        """
        Execute the full EDA flow for *design*.

        Parameters
        ----------
        design          : Design name (must match a subdirectory under designs/)
        clock_period_ns : Target clock period in ns (used when spec is None)
        enable_pnr      : If True, run OpenROAD P&R after STA
        spec            : RunSpec (preferred). If provided, all timing parameters
                          are read from it; clock_period_ns is ignored.
        """
        # Resolve the RunSpec
        if spec is not None:
            clock_period_ns = spec.resolved_clock_period_ns(fallback=clock_period_ns)
        else:
            # Legacy path: build a minimal spec from the bare clock period
            from .spec_interpreter import spec_from_flags
            spec = spec_from_flags(design=design, clock_period_ns=clock_period_ns)

        run_id   = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        ts       = datetime.now(timezone.utc).isoformat()
        run_dir  = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        design_dir = self.designs_dir / design
        if not design_dir.exists():
            raise FileNotFoundError(f"Design directory not found: {design_dir}")

        result = OrchestrationResult(
            run_id=run_id,
            run_dir=run_dir,
            design=design,
            clock_period_ns=clock_period_ns,
            timestamp=ts,
        )

        # Write run metadata (now includes full spec)
        self._write_run_config(run_dir, design, clock_period_ns, enable_pnr, ts, spec)

        log.info("Starting run %s for design '%s' (clock=%.2f ns)", run_id, design, clock_period_ns)
        flow_start = time.perf_counter()

        # ── Generate per-run SDC from template (Bug 1 fix) ───────────────────
        sdc_path = self._generate_sdc(design_dir, run_dir, spec)

        # ── Stage 1: Synthesis ────────────────────────────────────────────────
        synth_result = self._run_synthesis(design, design_dir, run_dir)
        result.stages.append(synth_result)
        result.synth_log_path = synth_result.log_path
        result.netlist_path   = run_dir / "netlist.v"

        if not synth_result.success:
            log.error("Synthesis failed — aborting flow")
            result.total_runtime_sec = time.perf_counter() - flow_start
            return result

        # ── Stage 2: Static Timing Analysis ──────────────────────────────────
        sta_result = self._run_sta(design, design_dir, run_dir, clock_period_ns, sdc_path)
        result.stages.append(sta_result)
        result.timing_rpt_path = run_dir / "timing.rpt"

        if not sta_result.success:
            log.error("STA failed — skipping optional stages")
            result.total_runtime_sec = time.perf_counter() - flow_start
            return result

        # ── Stage 3: Place & Route (optional) ────────────────────────────────
        if enable_pnr:
            pnr_result = self._run_pnr(design, design_dir, run_dir)
            result.stages.append(pnr_result)
            result.drc_rpt_path = run_dir / "drc.rpt"

        result.total_runtime_sec = time.perf_counter() - flow_start
        log.info("Run %s complete in %.1f s", run_id, result.total_runtime_sec)
        return result

    # ── Private: stage runners ────────────────────────────────────────────────

    def _run_synthesis(
        self,
        design: str,
        design_dir: Path,
        run_dir: Path,
    ) -> StageResult:
        synth_script = design_dir / "synth.ys"
        netlist_out  = run_dir / "netlist.v"
        synth_log    = run_dir / "synth.log"

        env = self._base_env()
        env.update({
            "DESIGN_TOP": design,
            "RTL_DIR":    str(design_dir / "rtl"),
            "NETLIST_OUT":str(netlist_out),
            "LIBERTY":    self._liberty_path(),
            "SYNTH_LOG":  str(synth_log),
        })

        cmd = [self._tool("yosys"), str(synth_script)]
        return self._invoke(
            stage="synthesis",
            cmd=cmd,
            env=env,
            log_path=synth_log,
            timeout=self._tools_cfg.get("tool_timeout_sec", 600),
        )

    def _run_sta(
        self,
        design: str,
        design_dir: Path,
        run_dir: Path,
        clock_period_ns: float,
        sdc_path: Optional[Path] = None,
    ) -> StageResult:
        sta_script  = design_dir / "sta.tcl"
        timing_rpt  = run_dir / "timing.rpt"
        netlist     = run_dir / "netlist.v"

        # Use the per-run generated SDC (Bug 1 fix); fall back to static file
        sdc = sdc_path if (sdc_path and sdc_path.exists()) else (design_dir / "constraints.sdc")

        env = self._base_env()
        env.update({
            "DESIGN_TOP":   design,
            "LIBERTY":      self._liberty_path(),
            "NETLIST":      str(netlist),
            "SDC":          str(sdc),
            "TIMING_RPT":   str(timing_rpt),
            "CLOCK_PERIOD": str(clock_period_ns),
        })

        cmd = [self._tool("opensta"), "-exit", str(sta_script)]
        result = self._invoke(
            stage="sta",
            cmd=cmd,
            env=env,
            log_path=timing_rpt,
            timeout=self._tools_cfg.get("tool_timeout_sec", 600),
        )
        return result

    def _run_pnr(
        self,
        design: str,
        design_dir: Path,
        run_dir: Path,
    ) -> StageResult:
        """Run OpenROAD P&R (optional stage, gracefully skips if binary absent)."""
        openroad_bin = self._tool("openroad")
        if not shutil.which(openroad_bin):
            log.warning("openroad binary '%s' not found — skipping P&R stage", openroad_bin)
            return StageResult(
                stage="pnr",
                success=True,
                returncode=0,
                runtime_sec=0.0,
                error_msg="Skipped — openroad not found on PATH",
            )

        drc_rpt = run_dir / "drc.rpt"
        env = self._base_env()
        env.update({
            "DESIGN_TOP": design,
            "NETLIST":    str(run_dir / "netlist.v"),
            "LIBERTY":    self._liberty_path(),
            "DRC_RPT":    str(drc_rpt),
        })

        # Generic OpenROAD P&R script — assumes OpenROAD-flow-scripts conventions
        openroad_script = (
            self.designs_dir / design / "pnr.tcl"
            if (self.designs_dir / design / "pnr.tcl").exists()
            else self.project_root / "scripts" / "generic_pnr.tcl"
        )

        cmd = [openroad_bin, "-exit", str(openroad_script)]
        return self._invoke(
            stage="pnr",
            cmd=cmd,
            env=env,
            log_path=drc_rpt,
            timeout=self._tools_cfg.get("tool_timeout_sec", 600),
        )

    # ── Private: subprocess wrapper ───────────────────────────────────────────

    def _invoke(
        self,
        stage: str,
        cmd: list[str],
        env: dict,
        log_path: Path,
        timeout: int = 600,
    ) -> StageResult:
        log.info("[%s] Running: %s", stage, " ".join(cmd))
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd,
                env=env,
                capture_output=False,
                stdout=open(log_path, "w"),   # noqa: WPS515
                stderr=subprocess.STDOUT,
                timeout=timeout if timeout > 0 else None,
                check=False,
            )
            elapsed = time.perf_counter() - t0
            success = proc.returncode == 0
            if success:
                log.info("[%s] ✓ Completed in %.1f s", stage, elapsed)
            else:
                log.error("[%s] ✗ Exited with code %d after %.1f s",
                          stage, proc.returncode, elapsed)
            return StageResult(
                stage=stage,
                success=success,
                returncode=proc.returncode,
                runtime_sec=elapsed,
                log_path=log_path,
                error_msg="" if success else f"Exit code {proc.returncode}",
            )
        except FileNotFoundError:
            elapsed = time.perf_counter() - t0
            msg = f"Binary not found: {cmd[0]!r} — is it installed and on PATH?"
            log.error("[%s] %s", stage, msg)
            return StageResult(
                stage=stage, success=False, returncode=-1,
                runtime_sec=elapsed, log_path=log_path, error_msg=msg,
            )
        except subprocess.TimeoutExpired:
            elapsed = time.perf_counter() - t0
            msg = f"Stage timed out after {timeout}s"
            log.error("[%s] %s", stage, msg)
            return StageResult(
                stage=stage, success=False, returncode=-2,
                runtime_sec=elapsed, log_path=log_path, error_msg=msg,
            )

    # ── Private: helpers ──────────────────────────────────────────────────────

    def _load_tools_cfg(self) -> dict:
        if self.tools_cfg_path.exists():
            with open(self.tools_cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            return cfg or {}
        log.warning("tools.yaml not found at %s — using defaults", self.tools_cfg_path)
        return {}

    def _tool(self, name: str) -> str:
        return (self._tools_cfg.get("tools") or {}).get(name, name)

    def _liberty_path(self) -> str:
        pdk = self._tools_cfg.get("pdk") or {}
        raw = pdk.get("liberty", "sky130_fd_sc_hd__tt_025C_1v80.lib")
        return str(Path(raw).expanduser())

    def _base_env(self) -> dict:
        """Inherit current environment so PATH etc. are preserved."""
        env = os.environ.copy()
        # Allow PDK root override via environment variable
        if "SIGNOFF_PDK_ROOT" in env:
            pdk_root = env["SIGNOFF_PDK_ROOT"]
            env.setdefault(
                "LIBERTY",
                str(Path(pdk_root) / "libs.ref/sky130_fd_sc_hd/lib"
                    "/sky130_fd_sc_hd__tt_025C_1v80.lib"),
            )
        return env

    def _write_run_config(
        self,
        run_dir: Path,
        design: str,
        clock_period_ns: float,
        enable_pnr: bool,
        timestamp: str,
        spec=None,
    ) -> None:
        cfg = {
            "design":          design,
            "clock_period_ns": clock_period_ns,
            "enable_pnr":      enable_pnr,
            "timestamp":       timestamp,
            "spec":            spec.to_dict() if spec is not None else None,
        }
        with open(run_dir / "run_config.yaml", "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, default_flow_style=False)

    def _generate_sdc(
        self,
        design_dir: Path,
        run_dir: Path,
        spec,   # RunSpec
    ) -> Path:
        """
        Generate a per-run SDC file by substituting RunSpec values into the
        design's constraints.sdc.tmpl template.

        Returns the path to the generated SDC.  Falls back to the static
        constraints.sdc if the template is missing (backward compatibility).
        """
        tmpl_path   = design_dir / "constraints.sdc.tmpl"
        static_path = design_dir / "constraints.sdc"
        out_path    = run_dir / "constraints_runtime.sdc"

        if not tmpl_path.exists():
            log.warning(
                "SDC template not found at %s — using static constraints.sdc "
                "(clock period may be hardcoded)", tmpl_path
            )
            return static_path

        period   = spec.resolved_clock_period_ns()
        half     = round(period / 2.0, 6)
        unc      = spec.clock_uncertainty_ns if spec.clock_uncertainty_ns is not None else 0.0
        latency  = spec.clock_latency_ns     if spec.clock_latency_ns     is not None else 0.0
        in_del   = spec.input_delay_ns       if spec.input_delay_ns       is not None else 0.5
        out_del  = spec.output_delay_ns      if spec.output_delay_ns      is not None else 0.5

        tmpl = tmpl_path.read_text(encoding="utf-8")
        sdc  = tmpl.format(
            clock_period_ns=period,
            clock_half_period_ns=half,
            clock_uncertainty_ns=unc,
            clock_latency_ns=latency,
            input_delay_ns=in_del,
            output_delay_ns=out_del,
        )
        out_path.write_text(sdc, encoding="utf-8")
        log.info("Generated per-run SDC: %s (period=%.3f ns)", out_path.name, period)
        return out_path
