import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import Mock, patch

from suno_backup import AFFECTED_START, SunoBackupManager, is_affected_period, local_backup_exists
from suno_client import SunoClient


class FakeResponse:
    status_code = 200
    headers = {"Content-Length": "3"}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def raise_for_status(self): pass
    def iter_content(self, chunk_size=1): yield b"abc"
    def close(self): pass


class FakeSession:
    def get(self, *args, **kwargs): return FakeResponse()


class SunoBackupTests(unittest.TestCase):
    def test_v6_is_the_default_model_and_legacy_configuration_is_preserved(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(SunoClient().model, "V6")
        with patch.dict(os.environ, {"SUNO_API_MODEL": "V5_5"}, clear=True):
            self.assertEqual(SunoClient().model, "V5_5")

    def test_affected_window_is_utc_and_exclusive_at_end(self):
        self.assertTrue(is_affected_period({"createTime": "2026-08-28T20:30:00Z"}))
        self.assertFalse(is_affected_period({"createTime": "2026-08-28T21:00:00Z"}))

    def test_backup_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = SunoBackupManager(Path(temp) / "audio", session=FakeSession())
            track = {"id": "song-id", "taskId": "task-id", "audioUrl": "https://example.test/song.mp3"}
            self.assertTrue(manager.backup_track(track)[0])
            self.assertTrue(local_backup_exists(track, manager.backup_dir))
            self.assertEqual(manager.backup_track(track)[1], "Already backed up.")

    def test_candidates_group_tracks_by_original_task(self):
        manager = SunoBackupManager("unused", session=FakeSession())
        grouped = manager.candidates([{"id": "a", "taskId": "one"}, {"id": "b", "taskId": "one"}])
        self.assertEqual(list(grouped), ["one"])
        self.assertEqual(len(grouped["one"]), 2)

    @patch("suno_client.requests.post")
    def test_recovery_submission_uses_documented_fields(self, post):
        post.return_value = Mock(raise_for_status=Mock(), json=Mock(return_value={"data": {"task_id": "recovery-1"}}))
        client = SunoClient(base_url="https://api.example", api_key="secret")
        client.start_audio_recovery("original-1", "https://callback.example/recovery")
        self.assertEqual(post.call_args.kwargs["json"], {"sunoTaskId": "original-1", "callBackUrl": "https://callback.example/recovery"})
        self.assertEqual(post.call_args.args[0], "https://api.example/api/v1/suno/recovery")

    @patch("suno_client.requests.get")
    def test_recovery_status_uses_dedicated_task_id_query(self, get):
        get.return_value = Mock(raise_for_status=Mock(), json=Mock(return_value={"code": 201, "data": None}))
        SunoClient(base_url="https://api.example", api_key="secret").get_audio_recovery_details("recovery-1")
        self.assertEqual(get.call_args.args[0], "https://api.example/api/v1/suno/recovery/record-info")
        self.assertEqual(get.call_args.kwargs["params"], {"task_id": "recovery-1"})
