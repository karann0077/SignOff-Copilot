"""
tests/test_datastore.py — Unit tests for the SQLite data store.

Uses a temporary database file so no persistent state is left behind.
"""

import json
import pytest
import tempfile
from pathlib import Path
from signoff_copilot.datastore import DataStore
from signoff_copilot.parser import ParsedMetrics
from signoff_copilot.rules_engine import RulesEngine, MetricVerdict, RunVerdict


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path) -> DataStore:
    return DataStore(db_path=tmp_path / "test_signoff.db")


def _sample_metrics(run_id: str = "2026-09-14T10-00-00") -> ParsedMetrics:
    return ParsedMetrics(
        run_id=run_id,
        design="picorv32",
        clock_period_ns=4.0,
        wns_ns=0.22,
        tns_ns=0.0,
        num_setup_violations=0,   # replaces num_timing_violations constructor arg
        num_hold_violations=0,
        num_drc_violations=0,
        cell_count=3421,
        chip_area_um2=12345.67,
        utilization_pct=62.5,
        runtime_sec=184.0,
        status="PASS",
        timestamp="2026-09-14T10:35:12Z",
    )


def _sample_verdict(status: str = "PASS") -> RunVerdict:
    from signoff_copilot.rules_engine import Outcome
    return RunVerdict(
        overall_status=status,
        metric_verdicts=[
            MetricVerdict("WNS (ns)", 0.22, 0.0, True, Outcome.PASS, "0.2200 ns ≥ 0.0ns"),
            MetricVerdict("TNS (ns)", 0.0,  0.0, True, Outcome.PASS, "0.0000 ns ≥ 0.0ns"),
        ],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Schema creation
# ─────────────────────────────────────────────────────────────────────────────

class TestSchemaCreation:
    def test_db_file_created(self, tmp_path):
        db_path = tmp_path / "new.db"
        DataStore(db_path=db_path)
        assert db_path.exists()

    def test_parent_dirs_created(self, tmp_path):
        db_path = tmp_path / "nested" / "dir" / "signoff.db"
        DataStore(db_path=db_path)
        assert db_path.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Insert and read-back
# ─────────────────────────────────────────────────────────────────────────────

class TestInsertAndGet:
    def test_insert_run(self, db):
        db.insert_run(_sample_metrics(), _sample_verdict())
        # No exception = success

    def test_get_run_returns_dict(self, db):
        m = _sample_metrics()
        db.insert_run(m, _sample_verdict())
        row = db.get_run(m.run_id)
        assert isinstance(row, dict)

    def test_get_run_correct_values(self, db):
        m = _sample_metrics()
        db.insert_run(m, _sample_verdict(), llm_summary="All looks good.", report_html_path="/tmp/report.html")
        row = db.get_run(m.run_id)
        assert row["run_id"]          == m.run_id
        assert row["design"]          == "picorv32"
        assert abs(row["wns_ns"] - 0.22) < 1e-6
        assert row["status"]          == "PASS"
        assert row["llm_summary"]     == "All looks good."
        assert row["report_html_path"] == "/tmp/report.html"

    def test_get_nonexistent_run_returns_none(self, db):
        assert db.get_run("does-not-exist") is None

    def test_duplicate_run_id_raises(self, db):
        import sqlite3
        m = _sample_metrics()
        db.insert_run(m, _sample_verdict())
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_run(m, _sample_verdict())


# ─────────────────────────────────────────────────────────────────────────────
# List runs
# ─────────────────────────────────────────────────────────────────────────────

class TestListRuns:
    def test_list_returns_all_runs(self, db):
        db.insert_run(_sample_metrics("run-001"), _sample_verdict())
        db.insert_run(_sample_metrics("run-002"), _sample_verdict())
        rows = db.list_runs()
        assert len(rows) == 2

    def test_list_ordered_by_timestamp_desc(self, db):
        m1 = _sample_metrics("run-001")
        m1.timestamp = "2026-09-14T10:00:00Z"
        m2 = _sample_metrics("run-002")
        m2.timestamp = "2026-09-14T11:00:00Z"
        db.insert_run(m1, _sample_verdict())
        db.insert_run(m2, _sample_verdict())
        rows = db.list_runs()
        assert rows[0]["run_id"] == "run-002"  # most recent first

    def test_list_filtered_by_design(self, db):
        db.insert_run(_sample_metrics("run-001"), _sample_verdict())
        m2 = _sample_metrics("run-002")
        m2.design = "uart"
        db.insert_run(m2, _sample_verdict())
        rows = db.list_runs(design="uart")
        assert len(rows) == 1
        assert rows[0]["design"] == "uart"

    def test_list_limit(self, db):
        for i in range(5):
            db.insert_run(_sample_metrics(f"run-{i:03d}"), _sample_verdict())
        rows = db.list_runs(limit=3)
        assert len(rows) == 3


# ─────────────────────────────────────────────────────────────────────────────
# Update LLM summary
# ─────────────────────────────────────────────────────────────────────────────

class TestUpdateLLMSummary:
    def test_update_summary(self, db):
        m = _sample_metrics()
        db.insert_run(m, _sample_verdict())
        db.update_llm_summary(m.run_id, "Great timing closure achieved.")
        row = db.get_run(m.run_id)
        assert row["llm_summary"] == "Great timing closure achieved."


# ─────────────────────────────────────────────────────────────────────────────
# Designs list
# ─────────────────────────────────────────────────────────────────────────────

class TestDesigns:
    def test_empty_db_returns_empty_list(self, db):
        assert db.designs() == []

    def test_distinct_designs_returned(self, db):
        db.insert_run(_sample_metrics("run-001"), _sample_verdict())
        m2 = _sample_metrics("run-002")
        m2.design = "uart"
        db.insert_run(m2, _sample_verdict())
        designs = db.designs()
        assert set(designs) == {"picorv32", "uart"}

    def test_designs_sorted(self, db):
        m1 = _sample_metrics("run-001"); m1.design = "uart"
        m2 = _sample_metrics("run-002"); m2.design = "aes"
        db.insert_run(m1, _sample_verdict())
        db.insert_run(m2, _sample_verdict())
        assert db.designs() == sorted(db.designs())


# ─────────────────────────────────────────────────────────────────────────────
# Allowlist in get_trend
# ─────────────────────────────────────────────────────────────────────────────

class TestGetTrendAllowlist:
    def test_invalid_metric_raises(self, db):
        with pytest.raises(ValueError, match="not in allowlist"):
            db.get_trend("picorv32", metric="DROP TABLE runs --")
