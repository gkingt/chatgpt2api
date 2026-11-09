from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path
from threading import RLock


class ConfigFileError(ValueError):
    pass


_locks: dict[str, RLock] = {}
_locks_guard = RLock()


def file_lock(path: Path) -> RLock:
    with _locks_guard:
        return _locks.setdefault(str(path.resolve()), RLock())


def _read_object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("configuration root must be a JSON object")
    return value


def load_json_config(path: Path, backup: Path) -> tuple[dict, bool]:
    with file_lock(path):
        try:
            return _read_object(path), False
        except (OSError, ValueError) as exc:
            try:
                return _read_object(backup), True
            except (OSError, ValueError):
                if not path.exists() and not backup.exists():
                    return {}, False
                raise ConfigFileError(f"Cannot read configuration {path}; restore a valid JSON file or backup {backup}. Existing files were not overwritten.") from exc


def _replace_payload(path: Path, payload: bytes, *, allow_bind_mount: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        try:
            os.replace(temporary, path)
        except OSError as exc:
            if not allow_bind_mount or exc.errno != errno.EBUSY:
                raise
            # Single-file Docker mounts cannot be renamed. A durable backup is already saved.
            with path.open("r+b") as output:
                output.write(payload)
                output.truncate()
                output.flush()
                os.fsync(output.fileno())
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def save_json_config(path: Path, value: dict, backup: Path) -> None:
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with file_lock(path):
        try:
            old = _read_object(path)
        except (OSError, ValueError):
            # Never replace a known-good backup with a truncated/corrupt primary file.
            old, _ = load_json_config(path, backup)
        backup_payload = (json.dumps(old or value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        _replace_payload(backup, backup_payload)
        _replace_payload(path, payload, allow_bind_mount=True)
