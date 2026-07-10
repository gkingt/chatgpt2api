from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from services.image_task_service import IMAGE_TASK_WORKER_LIMIT, ImageTaskService


OWNER = {"id": "owner-1", "name": "Owner", "role": "admin"}
OTHER_OWNER = {"id": "owner-2", "name": "Other", "role": "user"}


def wait_for_task(service: ImageTaskService, identity: dict[str, object], task_id: str, status: str, timeout: float = 2.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        result = service.list_tasks(identity, [task_id])
        last = (result.get("items") or [None])[0]
        if last and last.get("status") == status:
            return last
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} did not reach {status}, last={last}")


class ImageTaskServiceTests(unittest.TestCase):
    def make_service(self, path: Path, handler=None) -> ImageTaskService:
        return ImageTaskService(
            path,
            generation_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/image.png"}]}),
            edit_handler=handler or (lambda _payload: {"data": [{"url": "http://example.test/edit.png"}]}),
            retention_days_getter=lambda: 30,
        )

    def test_submitted_generation_tasks_start_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            started = 0
            lock = threading.Lock()
            all_started = threading.Event()
            release = threading.Event()

            def handler(_payload):
                nonlocal started
                with lock:
                    started += 1
                    if started == 3:
                        all_started.set()
                self.assertTrue(release.wait(2.0))
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            for index in range(3):
                service.submit_generation(
                    OWNER,
                    client_task_id=f"concurrent-{index}",
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                    base_url="http://local.test",
                )

            self.assertTrue(all_started.wait(1.0))
            release.set()
            for index in range(3):
                wait_for_task(service, OWNER, f"concurrent-{index}", "success", timeout=3.0)

    def test_generation_task_workers_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            active = 0
            max_active = 0
            lock = threading.Lock()
            saturated = threading.Event()
            release = threading.Event()

            def handler(_payload):
                nonlocal active, max_active
                with lock:
                    active += 1
                    max_active = max(max_active, active)
                    if active == IMAGE_TASK_WORKER_LIMIT:
                        saturated.set()
                try:
                    self.assertTrue(release.wait(2.0))
                    return {"data": [{"url": "http://example.test/image.png"}]}
                finally:
                    with lock:
                        active -= 1

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            for index in range(IMAGE_TASK_WORKER_LIMIT + 2):
                service.submit_generation(
                    OWNER,
                    client_task_id=f"bounded-{index}",
                    prompt="cat",
                    model="gpt-image-2",
                    size=None,
                    base_url="http://local.test",
                )

            self.assertTrue(saturated.wait(1.0))
            tasks = service.list_tasks(OWNER, [f"bounded-{index}" for index in range(IMAGE_TASK_WORKER_LIMIT + 2)])["items"]
            self.assertLessEqual(sum(1 for item in tasks if item["status"] == "running"), IMAGE_TASK_WORKER_LIMIT)
            self.assertGreaterEqual(sum(1 for item in tasks if item["status"] == "queued"), 1)
            release.set()
            for index in range(IMAGE_TASK_WORKER_LIMIT + 2):
                wait_for_task(service, OWNER, f"bounded-{index}", "success", timeout=3.0)
            self.assertEqual(max_active, IMAGE_TASK_WORKER_LIMIT)

    def test_duplicate_submit_uses_existing_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            calls = 0

            def handler(_payload):
                nonlocal calls
                calls += 1
                time.sleep(0.05)
                return {"data": [{"url": "http://example.test/image.png"}]}

            service = self.make_service(Path(tmp_dir) / "image_tasks.json", handler)
            first = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            second = service.submit_generation(
                OWNER,
                client_task_id="task-1",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            self.assertEqual(first["id"], "task-1")
            self.assertEqual(second["id"], "task-1")
            task = wait_for_task(service, OWNER, "task-1", "success")
            self.assertEqual(task["data"][0]["url"], "http://example.test/image.png")
            self.assertEqual(calls, 1)

    def test_different_owner_cannot_query_task(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            service = self.make_service(Path(tmp_dir) / "image_tasks.json")
            service.submit_generation(
                OWNER,
                client_task_id="private-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )

            wait_for_task(service, OWNER, "private-task", "success")
            result = service.list_tasks(OTHER_OWNER, ["private-task"])

            self.assertEqual(result["items"], [])
            self.assertEqual(result["missing_ids"], ["private-task"])

    def test_success_task_persists_to_new_service_instance(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            service = self.make_service(path)
            service.submit_generation(
                OWNER,
                client_task_id="persisted-task",
                prompt="cat",
                model="gpt-image-2",
                size=None,
                base_url="http://local.test",
            )
            wait_for_task(service, OWNER, "persisted-task", "success")

            reloaded = self.make_service(path)
            result = reloaded.list_tasks(OWNER, ["persisted-task"])

            self.assertEqual(result["missing_ids"], [])
            self.assertEqual(result["items"][0]["status"], "success")
            self.assertEqual(result["items"][0]["data"][0]["url"], "http://example.test/image.png")

    def test_startup_marks_unfinished_tasks_as_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "image_tasks.json"
            path.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "id": "queued-task",
                                "owner_id": "owner-1",
                                "status": "queued",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                            {
                                "id": "running-task",
                                "owner_id": "owner-1",
                                "status": "running",
                                "mode": "generate",
                                "model": "gpt-image-2",
                                "created_at": "2099-01-01 00:00:00",
                                "updated_at": "2099-01-01 00:00:00",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            service = self.make_service(path)
            result = service.list_tasks(OWNER, ["queued-task", "running-task"])

            self.assertEqual([item["status"] for item in result["items"]], ["error", "error"])
            self.assertTrue(all("已中断" in item.get("error", "") for item in result["items"]))


if __name__ == "__main__":
    unittest.main()
