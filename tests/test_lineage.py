"""
tests/test_lineage.py  —  Unit tests for ai.lineage decision lineage store.
"""
import threading
import pytest
from ai import lineage


@pytest.fixture(autouse=True)
def _clean():
    """Ensure a clean lineage store for every test."""
    lineage.clear()
    yield
    lineage.clear()


class TestRecord:
    def test_basic_record_and_get(self):
        lineage.record("T-001", "ml_threat", "RF → HIGH (0.94)")
        chain = lineage.get_chain("T-001")
        assert len(chain) == 1
        rec = chain[0]
        assert rec["stage"] == "ml_threat"
        assert rec["summary"] == "RF → HIGH (0.94)"
        assert "decision_id" in rec
        assert "timestamp" in rec

    def test_multiple_stages(self):
        lineage.record("T-001", "ingest", "Track first seen")
        lineage.record("T-001", "threat_assess", "HIGH score=92")
        lineage.record("T-001", "ml_threat", "RF → HIGH (0.94)")
        lineage.record("T-001", "roe", "WEAPONS_TIGHT")
        chain = lineage.get_chain("T-001")
        assert len(chain) == 4
        stages = [r["stage"] for r in chain]
        assert stages == ["ingest", "threat_assess", "ml_threat", "roe"]

    def test_inputs_outputs_rule(self):
        lineage.record(
            "T-001", "ml_threat", "RF → HIGH",
            inputs={"speed": 32, "heading": 180},
            outputs={"ml_level": "HIGH", "ml_probability": 0.94},
            rule="RandomForestClassifier",
        )
        rec = lineage.get_chain("T-001")[0]
        assert rec["inputs"]["speed"] == 32
        assert rec["outputs"]["ml_level"] == "HIGH"
        assert rec["rule"] == "RandomForestClassifier"

    def test_empty_track_id_ignored(self):
        lineage.record("", "test", "should be ignored")
        lineage.record(None, "test", "should be ignored")
        assert lineage.get_all_track_ids() == []

    def test_unique_decision_ids(self):
        lineage.record("T-001", "a", "one")
        lineage.record("T-001", "b", "two")
        ids = [r["decision_id"] for r in lineage.get_chain("T-001")]
        assert len(set(ids)) == 2


class TestGetSummary:
    def test_empty_track(self):
        s = lineage.get_summary("NOPE")
        assert s["count"] == 0
        assert s["stages"] == []

    def test_populated_track(self):
        lineage.record("T-002", "ingest", "a")
        lineage.record("T-002", "ml_threat", "b")
        lineage.record("T-002", "ingest", "c")
        s = lineage.get_summary("T-002")
        assert s["count"] == 3
        assert sorted(s["stages"]) == ["ingest", "ml_threat"]
        assert s["first"] is not None
        assert s["last"] is not None


class TestBounds:
    def test_per_track_ring_buffer(self):
        for i in range(lineage._MAX_RECORDS_PER_TRACK + 20):
            lineage.record("T-003", "test", f"entry-{i}")
        chain = lineage.get_chain("T-003")
        assert len(chain) == lineage._MAX_RECORDS_PER_TRACK
        # Oldest entries should have been evicted.
        assert chain[0]["summary"] == "entry-20"

    def test_max_tracks_eviction(self):
        original_max = lineage._MAX_TRACKS
        lineage._MAX_TRACKS = 5
        try:
            for i in range(10):
                lineage.record(f"T-{i:03d}", "test", f"track {i}")
            # Only latest 5 tracks should survive.
            ids = lineage.get_all_track_ids()
            assert len(ids) == 5
            # First 5 should have been evicted.
            assert lineage.get_chain("T-000") == []
            assert lineage.get_chain("T-004") == []
            # Latest 5 still present.
            assert len(lineage.get_chain("T-009")) == 1
        finally:
            lineage._MAX_TRACKS = original_max


class TestDropAndClear:
    def test_drop_track(self):
        lineage.record("T-010", "test", "a")
        lineage.record("T-011", "test", "b")
        lineage.drop_track("T-010")
        assert lineage.get_chain("T-010") == []
        assert len(lineage.get_chain("T-011")) == 1

    def test_drop_nonexistent(self):
        lineage.drop_track("NOPE")  # should not raise

    def test_clear(self):
        lineage.record("T-020", "test", "a")
        lineage.record("T-021", "test", "b")
        lineage.clear()
        assert lineage.get_all_track_ids() == []
        assert lineage.stats()["tracks"] == 0


class TestStats:
    def test_stats(self):
        lineage.record("T-030", "a", "x")
        lineage.record("T-030", "b", "y")
        lineage.record("T-031", "a", "z")
        s = lineage.stats()
        assert s["tracks"] == 2
        assert s["total_records"] == 3


class TestThreadSafety:
    def test_concurrent_writes(self):
        errors = []

        def worker(tid, n):
            try:
                for i in range(n):
                    lineage.record(tid, "test", f"entry-{i}")
            except Exception as e:
                errors.append(e)

        threads = []
        per_thread = 200
        for i in range(8):
            t = threading.Thread(target=worker, args=(f"T-{i}", per_thread))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        total = sum(len(lineage.get_chain(f"T-{i}")) for i in range(8))
        # Each thread wrote per_thread records; ring buffer is 50 per track.
        expected_per_track = min(per_thread, lineage._MAX_RECORDS_PER_TRACK)
        assert total == 8 * expected_per_track
