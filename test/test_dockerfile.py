import unittest
from pathlib import Path


class DockerfileTests(unittest.TestCase):
    def test_runtime_image_installs_node_for_sentinel_sdk(self):
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
        content = dockerfile.read_text(encoding="utf-8")

        self.assertIn("python:3.13-slim AS app", content)
        self.assertIn("nodejs", content)

    def test_warp_initializer_mounts_safe_storage_code_and_persistent_backups(self):
        compose = Path(__file__).resolve().parents[1] / "docker-compose.warp.yml"
        initializer = compose.read_text(encoding="utf-8").split("  warp-proxy:", 1)[0]
        self.assertIn("./utils:/app/utils:ro", initializer)
        self.assertIn("./data:/app/data", initializer)


if __name__ == "__main__":
    unittest.main()
