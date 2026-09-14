"""
nl_to_tcl.py — Natural-language to TCL translator (bonus module).

Safety design
-------------
The LLM output is NEVER passed directly to eval/exec.
It is matched against a strict allowlist of known-safe command patterns
before any execution.  Every translated command is logged to a file.

This is the one place in SignOff Copilot where generated text could
become an executed action — hence it gets the strictest guardrails.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Allowlist of safe TCL command patterns
# Each entry is a compiled regex that must match the ENTIRE translated command.
# ─────────────────────────────────────────────────────────────────────────────

_SAFE_PATTERNS: list[re.Pattern] = [
    # Clock creation / modification
    re.compile(r"create_clock\s+-period\s+\d+(?:\.\d+)?\s+\[get_ports\s+\w+\]"),
    re.compile(r"set_clock_uncertainty\s+\d+(?:\.\d+)?\s+\[get_clocks\s+\w+\]"),

    # I/O delays
    re.compile(r"set_input_delay\s+-clock\s+\w+\s+-max\s+\d+(?:\.\d+)?\s+\[all_inputs\]"),
    re.compile(r"set_output_delay\s+-clock\s+\w+\s+-max\s+\d+(?:\.\d+)?\s+\[all_outputs\]"),

    # Max/min delay overrides
    re.compile(r"set_max_delay\s+\d+(?:\.\d+)?\s+-from\s+\[all_inputs\]\s+-to\s+\[all_outputs\]"),
    re.compile(r"set_false_path\s+-from\s+\[get_ports\s+\w+\]\s+-to\s+\[get_ports\s+\w+\]"),

    # Reporting commands (read-only)
    re.compile(r"report_checks(?:\s+-\w+(?:\s+\w+)*)?"),
    re.compile(r"report_wns"),
    re.compile(r"report_tns"),
    re.compile(r"report_check_types"),
    re.compile(r"report_design_area"),
    re.compile(r"report_power"),

    # Basic Yosys stat commands
    re.compile(r"stat(?:\s+-liberty\s+\S+)?"),
    re.compile(r"check_setup(?:\s+-verbose)?"),
]

# ─────────────────────────────────────────────────────────────────────────────
# Prompt template
# ─────────────────────────────────────────────────────────────────────────────

_NL_SYSTEM_PROMPT = """\
You are an OpenSTA / Yosys expert.  Given a plain-English EDA instruction, \
output ONLY the single corresponding TCL command.  No explanation, no markdown, \
no backticks — just the raw TCL command on one line.

Only output commands from this set of allowed patterns:
- create_clock -period <ns> [get_ports <name>]
- set_clock_uncertainty <ns> [get_clocks <name>]
- set_input_delay -clock <clk> -max <ns> [all_inputs]
- set_output_delay -clock <clk> -max <ns> [all_outputs]
- set_max_delay <ns> -from [all_inputs] -to [all_outputs]
- set_false_path -from [get_ports <name>] -to [get_ports <name>]
- report_checks [-path_delay max] [-group_count <n>]
- report_wns
- report_tns
- report_check_types
- check_setup [-verbose]

If you cannot map the request to one of these, output exactly: UNSAFE
"""


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TranslationResult:
    success:   bool
    tcl:       str = ""
    executed:  bool = False
    output:    str = ""
    error:     str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def translate_to_tcl(
    nl_request: str,
    dry_run: bool = True,
    opensta_bin: str = "opensta",
    log_path: Optional[Path] = None,
) -> TranslationResult:
    """
    Translate a natural-language EDA request into a TCL command.

    Parameters
    ----------
    nl_request  : Plain-English instruction
    dry_run     : If True, return the TCL without executing it
    opensta_bin : OpenSTA binary name / path
    log_path    : Optional file to append every translation attempt to
    """
    tcl = _llm_translate(nl_request)
    _log_translation(nl_request, tcl, log_path)

    if not tcl or tcl.strip().upper() == "UNSAFE":
        return TranslationResult(
            success=False,
            tcl=tcl,
            error="LLM indicated the request cannot be safely translated.",
        )

    tcl = tcl.strip()

    # ── Allowlist check ────────────────────────────────────────────────────
    if not _is_safe(tcl):
        return TranslationResult(
            success=False,
            tcl=tcl,
            error=(
                f"Generated command did not match any safe pattern: {tcl!r}\n"
                "Command blocked by allowlist — not executed."
            ),
        )

    log.info("Allowlist check PASSED for: %s", tcl)

    # ── Execution ─────────────────────────────────────────────────────────
    if dry_run:
        return TranslationResult(success=True, tcl=tcl, executed=False)

    output, exec_ok = _execute_tcl(tcl, opensta_bin)
    return TranslationResult(
        success=exec_ok,
        tcl=tcl,
        executed=True,
        output=output,
        error="" if exec_ok else "Execution failed — see output for details.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _llm_translate(nl_request: str) -> str:
    """Call the LLM to translate the NL request.  Returns empty string on failure."""
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("openai not installed — cannot translate NL request")
        return ""

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        log.warning("No LLM API key — cannot translate NL request")
        return ""

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _NL_SYSTEM_PROMPT},
                {"role": "user",   "content": nl_request},
            ],
            max_tokens=80,
            temperature=0.0,   # deterministic output for safety
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM translation failed: %s", exc)
        return ""


def _is_safe(tcl: str) -> bool:
    """Return True if the command matches at least one allowlist pattern."""
    tcl_normalised = " ".join(tcl.split())   # collapse whitespace
    for pattern in _SAFE_PATTERNS:
        if pattern.fullmatch(tcl_normalised):
            return True
    return False


def _execute_tcl(tcl: str, opensta_bin: str) -> tuple[str, bool]:
    """
    Execute a single TCL command in OpenSTA.

    Uses an inline heredoc so the command is never written to a shell-
    executable script file and cannot be injected beyond the allowlisted value.
    """
    # Feed TCL via stdin — not via shell eval
    cmd = [opensta_bin, "-exit"]
    try:
        proc = subprocess.run(
            cmd,
            input=f"{tcl}\n",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        output = proc.stdout + proc.stderr
        log.info("Executed TCL: %s (exit %d)", tcl, proc.returncode)
        return output, proc.returncode == 0
    except FileNotFoundError:
        return f"opensta binary not found: {opensta_bin!r}", False
    except subprocess.TimeoutExpired:
        return "Command timed out after 30s", False


def _log_translation(nl: str, tcl: str, log_path: Optional[Path]) -> None:
    """Append translation attempt to a log file (audit trail)."""
    if log_path is None:
        return
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"NL : {nl}\nTCL: {tcl}\n---\n")
