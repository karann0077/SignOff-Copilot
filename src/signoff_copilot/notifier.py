"""
notifier.py — Run completion notifier for SignOff Copilot.

Supports two backends:
  - Slack Incoming Webhook
  - SMTP email

Both backends no-op silently if the required environment variables
are absent, so the tool works fully offline.

Environment variables
---------------------
Slack:
  SLACK_WEBHOOK_URL   — Slack Incoming Webhook URL

Email (all four must be set):
  SMTP_HOST           — e.g. smtp.gmail.com
  SMTP_PORT           — e.g. 587
  SMTP_USER           — sender address
  SMTP_PASS           — sender password / app password
  NOTIFY_EMAIL_TO     — recipient address
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import textwrap
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def notify_run_complete(
    metrics,           # ParsedMetrics
    verdict,           # RunVerdict
    llm_summary: str,
    report_html_path: Optional[Path] = None,
) -> dict[str, bool]:
    """
    Send run-complete notifications via all configured backends.

    Returns a dict mapping backend name → success flag.
    """
    results: dict[str, bool] = {}

    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    if slack_url:
        results["slack"] = _notify_slack(slack_url, metrics, verdict, llm_summary, report_html_path)
    else:
        log.debug("SLACK_WEBHOOK_URL not set — skipping Slack notification")

    smtp_host = os.environ.get("SMTP_HOST")
    email_to  = os.environ.get("NOTIFY_EMAIL_TO")
    if smtp_host and email_to:
        results["email"] = _notify_email(metrics, verdict, llm_summary, email_to)
    else:
        log.debug("SMTP_HOST / NOTIFY_EMAIL_TO not set — skipping email notification")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Slack backend
# ─────────────────────────────────────────────────────────────────────────────

def _notify_slack(
    webhook_url: str,
    metrics,
    verdict,
    llm_summary: str,
    report_html_path: Optional[Path],
) -> bool:
    status      = verdict.overall_status
    color       = "#36a64f" if status == "PASS" else "#e01e5a"
    icon        = "✅" if status == "PASS" else "❌"
    wns_str     = f"{metrics.wns_ns:.4f} ns" if metrics.wns_ns is not None else "N/A"
    tns_str     = f"{metrics.tns_ns:.4f} ns" if metrics.tns_ns is not None else "N/A"
    report_line = (
        f"\n📄 Report: `{report_html_path}`" if report_html_path else ""
    )

    payload = {
        "text": f"{icon} *SignOff Copilot* — `{metrics.design}` run `{metrics.run_id}` is *{status}*",
        "attachments": [
            {
                "color": color,
                "fields": [
                    {"title": "Design",            "value": metrics.design,        "short": True},
                    {"title": "Clock period",       "value": f"{metrics.clock_period_ns:.1f} ns",  "short": True},
                    {"title": "WNS",                "value": wns_str,              "short": True},
                    {"title": "TNS",                "value": tns_str,              "short": True},
                    {"title": "Timing violations",  "value": str(metrics.num_timing_violations), "short": True},
                    {"title": "DRC violations",     "value": str(metrics.num_drc_violations),    "short": True},
                    {"title": "Cells",              "value": f"{metrics.cell_count:,}",          "short": True},
                    {"title": "Runtime",            "value": f"{metrics.runtime_sec:.0f} s",     "short": True},
                ],
                "text": f"*AI Summary:* {llm_summary}{report_line}",
                "footer": "SignOff Copilot",
                "ts": int(__import__("time").time()),
            }
        ],
    }

    try:
        resp = requests.post(
            webhook_url,
            data=json.dumps(payload),
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("Slack notification sent for run %s", metrics.run_id)
            return True
        log.warning("Slack webhook returned %d: %s", resp.status_code, resp.text)
        return False
    except Exception as exc:  # noqa: BLE001
        log.warning("Slack notification failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Email backend
# ─────────────────────────────────────────────────────────────────────────────

def _notify_email(
    metrics,
    verdict,
    llm_summary: str,
    to_addr: str,
) -> bool:
    status   = verdict.overall_status
    icon     = "✅ PASS" if status == "PASS" else "❌ FAIL"
    wns_str  = f"{metrics.wns_ns:.4f} ns" if metrics.wns_ns is not None else "N/A"
    tns_str  = f"{metrics.tns_ns:.4f} ns" if metrics.tns_ns is not None else "N/A"

    subject = f"[SignOff Copilot] {icon} — {metrics.design} run {metrics.run_id}"

    body_plain = textwrap.dedent(f"""\
        SignOff Copilot Run Summary
        ===========================

        Design         : {metrics.design}
        Run ID         : {metrics.run_id}
        Status         : {status}
        Clock period   : {metrics.clock_period_ns:.1f} ns

        Timing
        ------
        WNS            : {wns_str}
        TNS            : {tns_str}
        Violations     : {metrics.num_timing_violations}

        Physical
        --------
        Cells          : {metrics.cell_count:,}
        DRC violations : {metrics.num_drc_violations}
        Runtime        : {metrics.runtime_sec:.0f} s

        AI Summary
        ----------
        {llm_summary}
    """)

    body_html = f"""\
    <html><body>
    <h2>SignOff Copilot — {icon}</h2>
    <p><b>Design:</b> {metrics.design} &nbsp; | &nbsp; <b>Run:</b> {metrics.run_id}</p>
    <table border="1" cellpadding="4" cellspacing="0">
      <tr><th>Metric</th><th>Value</th></tr>
      <tr><td>WNS</td><td>{wns_str}</td></tr>
      <tr><td>TNS</td><td>{tns_str}</td></tr>
      <tr><td>Timing violations</td><td>{metrics.num_timing_violations}</td></tr>
      <tr><td>DRC violations</td><td>{metrics.num_drc_violations}</td></tr>
      <tr><td>Cells</td><td>{metrics.cell_count:,}</td></tr>
      <tr><td>Runtime</td><td>{metrics.runtime_sec:.0f} s</td></tr>
    </table>
    <h3>AI Summary</h3>
    <p>{llm_summary}</p>
    </body></html>
    """

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = smtp_user
    msg["To"]      = to_addr
    msg.attach(MIMEText(body_plain, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_addr, msg.as_string())
        log.info("Email notification sent to %s for run %s", to_addr, metrics.run_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Email notification failed: %s", exc)
        return False
