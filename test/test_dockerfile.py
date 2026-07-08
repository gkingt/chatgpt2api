import unittest
from pathlib import Path


class DockerfileTests(unittest.TestCase):
    def test_runtime_image_installs_node_for_sentinel_sdk(self):
        dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
        content = dockerfile.read_text(encoding="utf-8")

        self.assertIn("python:3.13-slim AS app", content)
        self.assertIn("nodejs", content)


if __name__ == "__main__":
    unittest.main()