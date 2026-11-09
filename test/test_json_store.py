from __future__ import annotations

import errno
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from services.config import ConfigStore
from services import config as config_module
from utils.json_store import ConfigFileError, load_json_config, save_json_config


class JsonStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "config.json"
        self.backup = self.path.with_name("config.json.bak")
        self.path.write_text(json.dumps({"auth-key": "test", "mail": {"secret": "keep"}}), encoding="utf-8")

    def test_save_creates_backup_and_recovers_corrupt_primary(self):
        original, _ = load_json_config(self.path, self.backup)
        save_json_config(self.path, {**original, "new": True}, self.backup)
        self.path.write_text("truncated", encoding="utf-8")
        recovered, used_backup = load_json_config(self.path, self.backup)
        self.assertTrue(used_backup)
        self.assertEqual(recovered, original)

    def test_corrupt_config_without_valid_backup_raises_without_overwrite(self):
        self.path.write_text("invalid", encoding="utf-8")
        with self.assertRaises(ConfigFileError):
            save_json_config(self.path, {"defaults": True}, self.backup)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "invalid")

    def test_bind_mount_ebusy_fallback_preserves_backup_and_writes_valid_json(self):
        import utils.json_store as module
        original_replace = module.os.replace

        def busy_primary(source, target):
            if target == self.path:
                raise OSError(errno.EBUSY, "mounted file")
            return original_replace(source, target)

        with mock.patch.object(module.os, "replace", side_effect=busy_primary):
            save_json_config(self.path, {"auth-key": "test", "updated": True}, self.backup)
        self.assertTrue(json.loads(self.path.read_text(encoding="utf-8"))["updated"])
        self.assertIn("mail", json.loads(self.backup.read_text(encoding="utf-8")))

    def test_failed_replace_leaves_original_and_backup_valid(self):
        original = self.path.read_bytes()
        with mock.patch("utils.json_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                save_json_config(self.path, {"wrong": True}, self.backup)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_separate_stores_merge_nonconflicting_updates_from_disk(self):
        first = ConfigStore(self.path)
        second = ConfigStore(self.path)
        first.update({"first": 1})
        second.update({"second": 2})
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(stored["first"], 1)
        self.assertEqual(stored["second"], 2)
        self.assertEqual(stored["mail"], {"secret": "keep"})

    def test_concurrent_config_updates_preserve_all_keys(self):
        store = ConfigStore(self.path)
        threads = [threading.Thread(target=store.update, args=({f"key_{index}": index},)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        for index in range(8):
            self.assertEqual(stored[f"key_{index}"], index)

    def test_failed_config_save_does_not_publish_unsaved_memory_state(self):
        store = ConfigStore(self.path)
        before = dict(store.data)
        with mock.patch.object(store, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.update({"proxy": "unsaved"})
        self.assertEqual(store.data, before)

    def test_main_settings_load_recovers_auth_key_from_backup(self):
        self.path.write_text("invalid", encoding="utf-8")
        self.backup.write_text(json.dumps({"auth-key": "recovered", "refresh_account_interval_minute": 15}), encoding="utf-8")
        with (
            mock.patch.object(config_module, "CONFIG_FILE", self.path),
            mock.patch.object(config_module, "DATA_DIR", self.path.parent),
            mock.patch.dict(config_module.os.environ, {"CHATGPT2API_AUTH_KEY": ""}),
        ):
            settings = config_module._load_settings()
        self.assertEqual(settings.auth_key, "recovered")
        self.assertEqual(settings.refresh_account_interval_minute, 15)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "invalid")


if __name__ == "__main__":
    unittest.main()
