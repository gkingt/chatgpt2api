from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from services.log_service import LogService


class LogServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "logs.jsonl"
        self.service = LogService(self.path)

    def write_items(self, items):
        self.path.write_text("\n".join(json.dumps(item) for item in items) + "\n", encoding="utf-8")

    def test_list_handles_missing_empty_and_nonpositive_limit(self):
        self.assertEqual(self.service.list(), [])
        self.path.touch()
        self.assertEqual(self.service.list(), [])
        self.service.add("call")
        self.assertEqual(self.service.list(limit=0), [])
        self.assertEqual(self.service.list(limit=-1), [])

    def test_list_filters_before_limiting_and_skips_malformed_lines(self):
        self.write_items([
            {"id": "old", "type": "call", "time": "2026-11-01 00:00:00"},
            {"id": "first", "type": "call", "time": "2026-11-08 00:00:00"},
            {"id": "second", "type": "call", "time": "2026-11-09 00:00:00"},
            {"id": "account", "type": "account", "time": "2026-11-09 00:00:00"},
            {"id": "future", "type": "call", "time": "2026-11-10 00:00:00"},
        ])
        with self.path.open("ab") as output:
            output.write(b"not json\n[]\n{unfinished")
        items = self.service.list(type="call", start_date="2026-11-08", end_date="2026-11-09", limit=2)
        self.assertEqual([item["id"] for item in items], ["second", "first"])

    def test_reverse_reader_preserves_utf8_crlf_and_records_across_chunks(self):
        self.service.READ_CHUNK_SIZE = 7
        items = [{"id": str(index), "summary": "\u56fe\u7247\U0001f680" * 20} for index in range(3)]
        self.path.write_bytes("\r\n".join(json.dumps(item, ensure_ascii=False) for item in items).encode("utf-8"))
        self.assertEqual(self.service.list(), list(reversed(items)))

    def test_oversized_records_are_skipped_in_list_but_preserved_on_delete(self):
        self.service.READ_CHUNK_SIZE = 16
        self.service.MAX_RECORD_SIZE = 64
        oversized = b'{"summary":"' + b"x" * 1024 + b'"}\n'
        self.path.write_bytes(b'{"id":"old"}\n' + oversized + b'{"id":"remove"}\n')
        self.assertEqual([item["id"] for item in self.service.list()], ["remove", "old"])
        self.assertEqual(self.service.delete(["remove"]), {"removed": 1})
        self.assertIn(oversized, self.path.read_bytes())
        self.assertEqual(self.service.list(), [{"id": "old"}])

    def test_large_file_list_reads_only_tail_in_bounded_blocks(self):
        record = {"id": "latest", "type": "call"}
        # A sparse 1.5 GiB prefix must never be loaded to return the last record.
        with self.path.open("wb") as output:
            output.seek(1536 * 1024 * 1024)
            output.write(b"\n" + json.dumps(record).encode("utf-8") + b"\n")
        original_open = Path.open
        reads = []

        class TrackingReader:
            def __init__(self, file):
                self.file = file

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.file.close()

            def seek(self, *args):
                return self.file.seek(*args)

            def read(self, size=-1):
                self_test.assertGreater(size, 0)
                reads.append(size)
                return self.file.read(size)

        self_test = self

        def tracked_open(path, *args, **kwargs):
            file = original_open(path, *args, **kwargs)
            return TrackingReader(file) if path == self.path else file

        with mock.patch.object(Path, "open", tracked_open):
            self.assertEqual(self.service.list(limit=1), [record])
        self.assertLessEqual(sum(reads), self.service.READ_CHUNK_SIZE)

    def test_delete_streams_and_preserves_invalid_records_and_stable_ids(self):
        self.path.write_bytes(b'{"summary":"legacy"}\ninvalid\n{"id":"remove"}\n{"summary":"keep"}\n')
        before = self.service.list()
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")):
            result = self.service.delete(["remove", "remove", "missing"])
        self.assertEqual(result, {"removed": 1})
        self.assertIn(b"invalid\n", self.path.read_bytes())
        self.assertEqual(self.service.list(), [item for item in before if item["id"] != "remove"])
        self.assertEqual(self.service.delete([before[0]["id"]]), {"removed": 1})

    def test_delete_accepts_old_line_number_ids(self):
        lines = ['{"summary":"first"}', '{"summary":"second"}']
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        old_id = self.service._legacy_id(lines[1], 1)
        self.assertEqual(self.service.delete([old_id]), {"removed": 1})
        self.assertEqual(len(self.service.list()), 1)

    def test_delete_keeps_appends_made_during_rewrite(self):
        self.write_items([{"id": "remove"}, {"id": "keep"}])
        original_parse = self.service._parse_line
        appended = False

        def append_during_parse(raw, offset):
            nonlocal appended
            if not appended:
                appended = True
                self.service.add("call", "concurrent")
            return original_parse(raw, offset)

        with mock.patch.object(self.service, "_parse_line", side_effect=append_during_parse):
            self.assertEqual(self.service.delete(["remove"]), {"removed": 1})
        items = self.service.list()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["summary"], "concurrent")
        self.assertEqual(items[1]["id"], "keep")

    def test_failed_replace_does_not_damage_original_or_leave_temporary_file(self):
        self.write_items([{"id": "remove"}, {"id": "keep"}])
        original = self.path.read_bytes()
        with mock.patch("services.log_service.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                self.service.delete(["remove"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_appends_remain_valid_json(self):
        threads = [threading.Thread(target=lambda: [self.service.add("call") for _ in range(20)]) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.service.list(limit=100)), 80)

    def test_delete_empty_missing_and_unknown_ids_is_noop(self):
        self.assertEqual(self.service.delete([]), {"removed": 0})
        self.assertEqual(self.service.delete(["missing"]), {"removed": 0})
        self.write_items([{"id": "keep"}])
        original = self.path.read_bytes()
        self.assertEqual(self.service.delete(["missing"]), {"removed": 0})
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
