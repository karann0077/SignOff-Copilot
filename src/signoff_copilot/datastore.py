"""
datastore.py — SQLite persistence layer for SignOff Copilot.

One table (`runs`), one row per run.  The same row stores both the
deterministic metrics and the cached LLM summary (so re-viewing a
report never re-calls the API).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id                  TEXT    PRIMARY KEY,
    design                  TEXT    NOT NULL,
    clock_period_ns         REAL    NOT NULL,
    wns_ns                  REAL,
    tns_ns                  REAL,
    num_timing_violations   INTEGER NOT NULL DEFAULT 0,
    num_drc_violations      INTEGER NOT NULL DEFAULT 0,
    cell_count              INTEGER NOT NULL DEFAULT 0,
    chip_area_um2           REAL,
    utilization_pct         REAL,
    runtime_sec             REAL    NOT NULL DEFAULT 0.0,
    status                  TEXT    NOT NULL DEFAULT 'UNKNOWN',
    timestamp               TEXT    NOT NULL,
    llm_summary             TEXT,
    report_html_path        TEXT,
    metric_verdicts_json    TEXT    -- JSON blob: list[MetricVerdict]
);

CREATE INDEX IF NOT EXISTS idx_runs_design    ON runs (design);
CREATE INDEX IF NOT EXISTS idx_runs_timestamp ON runs (timestamp);
CREATE INDEX IF NOT EXISTS idx_runs_status    ON runs (status);
"""


# ─────────────────────────────────────────────────────────────────────────────
# DataStore
# ─────────────────────────────────────────────────────────────────────────────

class DataStore:
    """
    Thin wrapper around a SQLite database.

    Parameters
    ----------
    db_path : Path to the SQLite file.  Created (with parent dirs) if absent.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Context manager ───────────────────────────────────────────────────────

    @contextmanager
    def _connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_DDL)
        log.debug("DataStore ready: %s", self.db_path)

    # ── Write ─────────────────────────────────────────────────────────────────

    def insert_run(
        self,
        metrics,                       # ParsedMetrics
        verdict,                       # RunVerdict
        llm_summary: str = "",
        report_html_path: str = "",
    ) -> None:
        """
        Insert a new run record.  Raises sqlite3.IntegrityError if run_id
        already exists (use update_llm_summary to patch the summary later).
        """
        verdicts_json = json.dumps(
            [
                {
                    "metric":    v.metric,
                    "value":     v.value,
                    "threshold": v.threshold,
                    "passed":    v.passed,
                    "reason":    v.reason,
                }
                for v in verdict.metric_verdicts
            ]
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, design, clock_period_ns,
                    wns_ns, tns_ns, num_timing_violations,
                    num_drc_violations, cell_count, chip_area_um2,
                    utilization_pct, runtime_sec, status, timestamp,
                    llm_summary, report_html_path, metric_verdicts_json
                ) VALUES (
                    :run_id, :design, :clock_period_ns,
                    :wns_ns, :tns_ns, :num_timing_violations,
                    :num_drc_violations, :cell_count, :chip_area_um2,
                    :utilization_pct, :runtime_sec, :status, :timestamp,
                    :llm_summary, :report_html_path, :metric_verdicts_json
                )
                """,
                {
                    "run_id":               metrics.run_id,
                    "design":               metrics.design,
                    "clock_period_ns":      metrics.clock_period_ns,
                    "wns_ns":               metrics.wns_ns,
                    "tns_ns":               metrics.tns_ns,
                    "num_timing_violations":metrics.num_timing_violations,
                    "num_drc_violations":   metrics.num_drc_violations,
                    "cell_count":           metrics.cell_count,
                    "chip_area_um2":        metrics.chip_area_um2,
                    "utilization_pct":      metrics.utilization_pct,
                    "runtime_sec":          metrics.runtime_sec,
                    "status":               verdict.overall_status,
                    "timestamp":            metrics.timestamp,
                    "llm_summary":          llm_summary,
                    "report_html_path":     report_html_path,
                    "metric_verdicts_json": verdicts_json,
                },
            )
        log.info("Inserted run %s (%s)", metrics.run_id, verdict.overall_status)

    def update_llm_summary(self, run_id: str, summary: str) -> None:
        """Patch the LLM summary on an existing row (called after generation)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET llm_summary = ? WHERE run_id = ?",
                (summary, run_id),
            )
        log.debug("Updated LLM summary for run %s", run_id)

    # ── Read ──────────────────────────────────────────────────────────────────

    def get_run(self, run_id: str) -> Optional[dict]:
        """Return a single run as a dict, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_runs(
        self,
        design: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Return runs ordered by timestamp descending.

        Parameters
        ----------
        design : Filter by design name (None = all designs)
        limit  : Maximum number of rows to return
        """
        with self._connect() as conn:
            if design:
                rows = conn.execute(
                    "SELECT * FROM runs WHERE design = ? ORDER BY timestamp DESC LIMIT ?",
                    (design, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM runs ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def get_trend(self, design: str, metric: str = "wns_ns") -> list[dict]:
        """
        Return (timestamp, metric_value) pairs for trend charts.

        Parameters
        ----------
        design : Design name to filter
        metric : Column name from the runs table
        """
        # Allowlist to prevent SQL injection via metric parameter
        allowed_metrics = {
            "wns_ns", "tns_ns", "num_timing_violations",
            "num_drc_violations", "cell_count", "utilization_pct",
            "runtime_sec", "chip_area_um2",
        }
        if metric not in allowed_metrics:
            raise ValueError(f"Metric '{metric}' not in allowlist: {allowed_metrics}")

        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT timestamp, {metric} FROM runs "   # noqa: S608 — metric is allowlisted
                "WHERE design = ? AND {metric} IS NOT NULL "
                "ORDER BY timestamp ASC".replace("{metric}", metric),
                (design,),
            ).fetchall()
        return [dict(r) for r in rows]

    def designs(self) -> list[str]:
        """Return sorted list of unique design names in the store."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT design FROM runs ORDER BY design"
            ).fetchall()
        return [r["design"] for r in rows]
