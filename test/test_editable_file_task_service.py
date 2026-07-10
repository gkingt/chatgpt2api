from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable

from services.editable_file_task_service import EDITABLE_FILE_TASK_WORKER_LIMIT, EditableFileTaskService


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}


class StubEditableFileTaskService(EditableFileTaskService):
    def __init__(self, path: Path, worker: Callable[[], None]) -> None:
        self.worker = worker
        super().__init__(path)

    def _run_task(
        self,
        key: str,
        kind: str,
        prompt: str,
        base64_images: list[str],
        identity: dict[str, object],
        base_url: str,
    ) -> None:
        self._update_task(key, status="running", error="", started_ts=time.time())
        self.worker()
        self._update_task(
            key,
            status="success",
            result={"primary_url": f"/files/{kind}.pptx", "zip_url": f"/files/{kind}.zip"},
            ended_ts=time.time(),
        )


def wait_for_task(service: EditableFileTaskService, task_id: str, status: str, timeout: float = 2.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = service.list_tasks(OWNER, [task_id])
        last = (result.get("items") or [None])[0]
        if last and last.get("status") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {status}, last={last}")


class EditableFileTaskServiceTests(unittest.TestCase):
    def test_file_task_workers_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            active = 0
            max_active = 0
            lock = threading.Lock()
            saturated = threading.Event()
            release = threading.Event()

            def worker() -> None:
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    if active == EDITABLE_FILE_TASK_WORKER_LIMIT:
                        saturated.set()
                try:
                    self.assertTrue(release.wait(2.0))
                finally:
                    with lock:
                        active -= 1

            service = StubEditableFileTaskService(Path(tmp_dir) / "editable_file_tasks.json", worker)
            for index in range(EDITABLE_FILE_TASK_WORKER_LIMIT + 2):
                service.submit_ppt(
                    OWNER,
                    client_task_id=f"bounded-{index}",
                    prompt="deck",
                    base64_images=[],
                    base_url="http://local.test",
                )

            self.assertTrue(saturated.wait(1.0))
            tasks = service.list_tasks(OWNER, [f"bounded-{index}" for index in range(EDITABLE_FILE_TASK_WORKER_LIMIT + 2)])["items"]
            self.assertLessEqual(sum(1 for item in tasks if item["status"] == "running"), EDITABLE_FILE_TASK_WORKER_LIMIT)
            self.assertGreaterEqual(sum(1 for item in tasks if item["status"] == "queued"), 1)
            release.set()
            for index in range(EDITABLE_FILE_TASK_WORKER_LIMIT + 2):
                wait_for_task(service, f"bounded-{index}", "success", timeout=3.0)
            self.assertEqual(max_active, EDITABLE_FILE_TASK_WORKER_LIMIT)


if __name__ == "__main__":
    unittest.main()