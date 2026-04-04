"""
tests/test_replay.py — Tests for replay/recorder.py and replay/player.py
"""
import json
import pytest
from pathlib import Path
from replay import recorder, player


# ── Recorder tests ───────────────────────────────────────────────────────

class TestRecorder:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        """Redirect recordings to tmp dir and ensure clean state."""
        if recorder.is_active():
            recorder.stop()
        monkeypatch.setattr(recorder, "RECORDINGS_DIR", tmp_path)
        yield
        if recorder.is_active():
            recorder.stop()

    def test_start_creates_file(self, tmp_path):
        path = recorder.start("test_scenario")
        assert Path(path).exists()
        assert recorder.is_active() is True

    def test_metadata_header(self, tmp_path):
        path = recorder.start("my_test")
        recorder.stop()

        with open(path, "r") as f:
            first_line = json.loads(f.readline())
        assert first_line["meta"] is True
        assert first_line["scenario"] == "my_test"
        assert "version" in first_line

    def test_capture_frame(self, tmp_path):
        path = recorder.start("capture_test", min_interval=0.0)
        snapshot = {"tracks": [{"id": "T1", "lat": 41.0, "lon": 29.0}]}
        captured = recorder.capture_frame(lambda: snapshot)
        assert captured is True
        recorder.stop()

        frames = []
        with open(path, "r") as f:
            for line in f:
                obj = json.loads(line)
                if not obj.get("meta") and not obj.get("footer"):
                    frames.append(obj)
        assert len(frames) >= 1
        assert frames[0]["state"]["tracks"][0]["id"] == "T1"

    def test_stop_writes_footer(self, tmp_path):
        path = recorder.start("footer_test", min_interval=0.0)
        recorder.capture_frame(lambda: {"tracks": []})
        summary = recorder.stop()

        assert summary is not None
        assert summary["scenario"] == "footer_test"
        assert summary["frames"] >= 1

        # Read last line
        with open(path, "r") as f:
            lines = f.readlines()
        footer = json.loads(lines[-1])
        assert footer["footer"] is True

    def test_double_stop(self, tmp_path):
        recorder.start("double_test")
        recorder.stop()
        result = recorder.stop()
        assert result is None

    def test_get_status_active(self, tmp_path):
        recorder.start("status_test")
        status = recorder.get_status()
        assert status["recording"] is True
        assert status["scenario"] == "status_test"
        recorder.stop()

    def test_get_status_inactive(self):
        status = recorder.get_status()
        assert status["recording"] is False

    def test_trim_snapshot(self):
        """Track history should be trimmed to MAX_TRACK_HISTORY."""
        state = {
            "tracks": [{
                "id": "T1",
                "history": [{"lat": i, "lon": i} for i in range(50)],
            }]
        }
        trimmed = recorder._trim_snapshot(state)
        assert len(trimmed["tracks"][0]["history"]) == recorder.MAX_TRACK_HISTORY


# ── Player tests ─────────────────────────────────────────────────────────

class TestPlayer:
    @pytest.fixture
    def recording_file(self, tmp_path, monkeypatch):
        """Create a sample recording file."""
        monkeypatch.setattr(player, "RECORDINGS_DIR", tmp_path)

        filepath = tmp_path / "test_recording.jsonl"
        with open(filepath, "w") as f:
            # Meta
            f.write(json.dumps({"meta": True, "version": 1,
                                "scenario": "test", "start_time": 1000.0}) + "\n")
            # 5 frames
            for i in range(5):
                frame = {
                    "t": 1000.0 + i,
                    "elapsed_s": float(i),
                    "frame": i + 1,
                    "state": {
                        "tracks": [{"id": "T1", "lat": 41.0 + i * 0.001,
                                     "lon": 29.0}],
                        "threats": [],
                        "zones": [],
                        "assets": [],
                    },
                }
                f.write(json.dumps(frame) + "\n")
            # Footer
            f.write(json.dumps({"footer": True, "total_frames": 5,
                                "duration_s": 4.0}) + "\n")

        return filepath

    def test_list_recordings(self, recording_file, monkeypatch):
        monkeypatch.setattr(player, "RECORDINGS_DIR", recording_file.parent)
        recs = player.list_recordings()
        assert len(recs) >= 1
        assert recs[0]["filename"] == "test_recording.jsonl"

    def test_load_recording(self, recording_file):
        p = player.Player()
        p._recordings_dir = recording_file.parent
        meta = p.load("test_recording.jsonl")
        assert p.state == "LOADED"
        assert meta["scenario"] == "test"
        assert meta["total_frames"] == 5

    def test_play_pause_stop(self, recording_file):
        p = player.Player()
        p._recordings_dir = recording_file.parent
        p.load("test_recording.jsonl")

        p.play(speed=1.0)
        assert p.state == "PLAYING"

        p.pause()
        assert p.state == "PAUSED"

        p.stop()
        assert p.state == "IDLE"

    def test_seek(self, recording_file):
        p = player.Player()
        p._recordings_dir = recording_file.parent
        p.load("test_recording.jsonl")
        p.play(speed=1.0)

        p.seek(2.0)
        frame = p.get_current_frame()
        assert frame is not None

    def test_get_frame_at(self, recording_file):
        p = player.Player()
        p._recordings_dir = recording_file.parent
        p.load("test_recording.jsonl")

        frame = p.get_frame_at(0.0)
        assert frame is not None
        assert frame["frame"] == 1

        frame = p.get_frame_at(3.0)
        assert frame is not None
        assert frame["frame"] == 4

    def test_set_speed(self, recording_file):
        p = player.Player()
        p._recordings_dir = recording_file.parent
        p.load("test_recording.jsonl")
        p.play(speed=1.0)
        p.set_speed(5.0)
        info = p.get_info()
        assert info["speed"] == 5.0

    def test_get_info(self, recording_file):
        p = player.Player()
        p._recordings_dir = recording_file.parent
        p.load("test_recording.jsonl")
        info = p.get_info()
        assert info["state"] == "LOADED"
        assert "duration_s" in info
        assert "filename" in info
