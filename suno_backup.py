"""Durable local backups and recovery bookkeeping for Suno music tracks."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import re
from urllib.parse import urlparse

import requests


AFFECTED_START = datetime(2026, 8, 28, 20, 0, tzinfo=timezone.utc)
AFFECTED_END = datetime(2026, 8, 28, 21, 0, tzinfo=timezone.utc)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def audio_url(track: dict) -> str | None:
    return track.get("audio_url") or track.get("audioUrl") or track.get("url")


def parse_provider_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        # Provider timestamps are occasionally milliseconds.
        return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                parsed = datetime.strptime(str(value), fmt)
                break
            except ValueError:
                parsed = None
        if parsed is None:
            return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def is_affected_period(track: dict) -> bool:
    created = parse_provider_time(track.get("createTime") or track.get("createdAt"))
    return bool(created and AFFECTED_START <= created < AFFECTED_END)


def local_backup_exists(track: dict, backup_dir: Path) -> bool:
    saved_path = (track.get("backup") or {}).get("localPath")
    if not saved_path:
        return False
    path = Path(saved_path)
    if not path.is_absolute():
        path = backup_dir.parent / path
    return path.is_file() and path.stat().st_size > 0


def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "track"


class SunoBackupManager:
    def __init__(self, backup_dir: str | Path | None = None, session=None):
        self.backup_dir = Path(backup_dir or os.getenv("SUNO_AUDIO_BACKUP_DIR", "audio_backups"))
        self.session = session or requests.Session()

    def local_path(self, track: dict, source_url: str | None = None) -> Path:
        suffix = Path(urlparse(source_url or audio_url(track) or "").path).suffix.lower()
        if suffix not in {".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg"}:
            suffix = ".mp3"
        track_id = safe_filename(str(track.get("id") or "unknown"))
        task_id = safe_filename(str(track.get("taskId") or "no-task"))[:24]
        return self.backup_dir / f"{task_id}_{track_id}{suffix}"

    def remote_is_playable(self, url: str | None) -> tuple[bool, str | None]:
        if not url:
            return False, "No audio URL is stored."
        try:
            response = self.session.get(url, headers={"Range": "bytes=0-1"}, stream=True, timeout=(10, 30), allow_redirects=True)
            try:
                if response.status_code not in (200, 206):
                    return False, f"Remote server returned HTTP {response.status_code}."
                if response.headers.get("Content-Length") == "0":
                    return False, "Remote server returned an empty file."
                next(response.iter_content(chunk_size=1), None)
                return True, None
            finally:
                response.close()
        except requests.RequestException as exc:
            return False, f"Audio URL could not be reached: {exc}"

    def backup_track(self, track: dict, source_url: str | None = None, recovery_task_id: str | None = None) -> tuple[bool, str]:
        url = source_url or audio_url(track)
        backup = track.setdefault("backup", {})
        destination = self.local_path(track, url)
        if local_backup_exists(track, self.backup_dir) or (destination.is_file() and destination.stat().st_size > 0):
            backup.update({"status": "backed_up", "localPath": str(destination), "updatedAt": utc_now()})
            return True, "Already backed up."
        if not url:
            backup.update({"status": "failed", "lastError": "No audio URL is stored.", "updatedAt": utc_now()})
            return False, backup["lastError"]
        partial = destination.with_suffix(destination.suffix + ".part")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with self.session.get(url, stream=True, timeout=(10, 120), allow_redirects=True) as response:
                response.raise_for_status()
                with open(partial, "wb") as output:
                    for chunk in response.iter_content(chunk_size=1024 * 128):
                        if chunk:
                            output.write(chunk)
            if not partial.exists() or partial.stat().st_size == 0:
                raise ValueError("Downloaded file was empty.")
            os.replace(partial, destination)
            backup.update({"status": "backed_up", "localPath": str(destination), "sourceUrl": url,
                           "recoveryTaskId": recovery_task_id, "lastError": None, "updatedAt": utc_now()})
            return True, f"Saved to {destination}."
        except (OSError, requests.RequestException, ValueError) as exc:
            if partial.exists():
                partial.unlink()
            backup.update({"status": "failed", "sourceUrl": url, "lastError": str(exc), "updatedAt": utc_now()})
            return False, str(exc)

    def candidates(self, tracks: list[dict], *, affected_only=False, missing_only=True) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for track in tracks:
            if local_backup_exists(track, self.backup_dir):
                continue
            if affected_only and not is_affected_period(track):
                continue
            if missing_only and audio_url(track):
                continue
            task_id = track.get("taskId")
            if task_id:
                grouped.setdefault(str(task_id), []).append(track)
        return grouped
