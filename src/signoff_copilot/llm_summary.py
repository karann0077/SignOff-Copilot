"""
llm_summary.py — LLM summary service for SignOff Copilot.

Responsibilities
----------------
1. Build a structured prompt from already-computed metrics and verdict.
2. Call the LLM API (OpenAI-compatible by default).
3. Return a 2–4 sentence plain-English summary.
4. Degrade gracefully to a fallback string if the API is unavailable.

Design constraint: the LLM NEVER decides pass/fail.
It only explains the verdict that the rules engine already computed.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Prompt templates
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a senior digital IC design engineer writing a brief plain-English run \
summary for a junior engineer.  You have been given the structured metrics from \
an EDA (Electronic Design Automation) signoff run.  The pass/fail verdict has \
already been determined deterministically — your job is only to explain it \
clearly and suggest the first thing to check if the run failed.

Rules:
- Write 2–4 sentences maximum.
- Use plain English, avoid excessive jargon.
- Never override or question the provided verdict.
- If the run passed, say so confidently with the key numbers.
- If the run failed, explain the biggest violation and suggest ONE concrete \
  first debugging step.
"""

_USER_PROMPT_TEMPLATE = """\
## SignOff Run Summary Request

Design          : {design}
Clock period    : {clock_period_ns:.1f} ns  ({freq_mhz:.0f} MHz target)
Verdict         : {status}

### Timing Metrics
- Worst Negative Slack (WNS) : {wns_ns}
- Total Negative Slack (TNS) : {tns_ns}
- Timing violations          : {num_timing_violations}

### Physical Metrics
- Cell count      : {cell_count:,}
- DRC violations  : {num_drc_violations}
- Utilization     : {utilization_str}

### Worst Timing Path Excerpt
```
{worst_path}
```

Write your 2–4 sentence plain-English summary now:
"""

_FALLBACK_PASS = (
    "The signoff run passed all checks. "
    "Timing closure was achieved with WNS = {wns_ns} ns and {cell_count:,} cells synthesised."
)

_FALLBACK_FAIL = (
    "The signoff run failed. "
    "The worst negative slack was {wns_ns} ns with {num_violations} timing violation(s). "
    "Check the critical path in the timing report and consider relaxing the clock constraint "
    "or optimising the logic depth on the failing paths."
)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_summary(
    metrics,           # ParsedMetrics
    verdict,           # RunVerdict
    model: str = "gpt-4o-mini",
    max_tokens: int = 300,
    api_base_url: Optional[str] = None,
) -> str:
    """
    Generate a plain-English run summary using the LLM API.

    Returns a string summary.  Falls back gracefully if the API is not
    available (missing key, rate limit, network error).

    Parameters
    ----------
    metrics       : ParsedMetrics from the log parser
    verdict       : RunVerdict from the rules engine
    model         : OpenAI model name (or compatible model on another endpoint)
    max_tokens    : Maximum tokens in the completion
    api_base_url  : Override API base URL (e.g. for Groq/Gemini compatibility)
    """
    try:
        from openai import OpenAI  # lazy import — optional dependency
    except ImportError:
        log.warning("openai package not installed — returning fallback summary")
        return _fallback_summary(metrics, verdict)

    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    if not api_key:
        log.warning("No LLM API key found (OPENAI_API_KEY / LLM_API_KEY) — returning fallback")
        return _fallback_summary(metrics, verdict)

    user_prompt = _build_user_prompt(metrics, verdict)

    try:
        client_kwargs: dict = {"api_key": api_key}
        if api_base_url:
            client_kwargs["base_url"] = api_base_url

        client = OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=0.3,   # low temperature for factual consistency
        )
        summary = response.choices[0].message.content.strip()
        log.info("LLM summary generated (%d chars)", len(summary))
        return summary

    except Exception as exc:  # noqa: BLE001
        log.warning("LLM API call failed: %s — using fallback", exc)
        return _fallback_summary(metrics, verdict)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_user_prompt(metrics, verdict) -> str:
    wns_str  = f"{metrics.wns_ns:.4f} ns" if metrics.wns_ns is not None else "N/A"
    tns_str  = f"{metrics.tns_ns:.4f} ns" if metrics.tns_ns is not None else "N/A"
    util_str = f"{metrics.utilization_pct:.1f}%" if metrics.utilization_pct is not None else "N/A (no P&R)"
    freq_mhz = 1000.0 / metrics.clock_period_ns if metrics.clock_period_ns else 0.0

    # Keep path excerpt short (≤ 60 lines) to avoid huge tokens
    path_lines = (metrics.worst_path_excerpt or "Not available").splitlines()
    path_excerpt = "\n".join(path_lines[:60])

    return _USER_PROMPT_TEMPLATE.format(
        design=metrics.design,
        clock_period_ns=metrics.clock_period_ns,
        freq_mhz=freq_mhz,
        status=verdict.overall_status,
        wns_ns=wns_str,
        tns_ns=tns_str,
        num_timing_violations=metrics.num_timing_violations,
        cell_count=metrics.cell_count,
        num_drc_violations=metrics.num_drc_violations,
        utilization_str=util_str,
        worst_path=path_excerpt,
    )


def _fallback_summary(metrics, verdict) -> str:
    """Rule-based fallback used when the LLM API is unavailable."""
    if verdict.overall_status == "PASS":
        return _FALLBACK_PASS.format(
            wns_ns=f"{metrics.wns_ns:.4f}" if metrics.wns_ns is not None else "N/A",
            cell_count=metrics.cell_count,
        )
    return _FALLBACK_FAIL.format(
        wns_ns=f"{metrics.wns_ns:.4f}" if metrics.wns_ns is not None else "N/A",
        num_violations=metrics.num_timing_violations,
    )
