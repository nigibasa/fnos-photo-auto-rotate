from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_python_sources_compile(self) -> None:
        for name in ["photo_auto_rotate.py", "server.py", "build_fpk.py", "verify_fpk.py"]:
            subprocess.run([sys.executable, "-m", "py_compile", str(ROOT / name)], check=True)

    def test_build_and_verify_fpk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "PhotoAutoRotate.fpk"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "build_fpk.py"),
                    "--image",
                    "ghcr.io/example/fnos-photo-auto-rotate:0.1.5",
                    "--platform",
                    "x86",
                    "--output",
                    str(output),
                ],
                check=True,
            )
            subprocess.run([sys.executable, str(ROOT / "verify_fpk.py"), str(output)], check=True)
            with tarfile.open(output, "r:gz") as package:
                main = package.getmember("cmd/main")
                self.assertEqual(main.mode & 0o111, 0o111)

    def test_release_workflow_exists(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("packages: write", workflow)
        self.assertIn("docker/build-push-action", workflow)
        self.assertIn("build_fpk.py", workflow)

    def test_csv_import_contract_is_present(self) -> None:
        rotator = (ROOT / "photo_auto_rotate.py").read_text(encoding="utf-8")
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        web = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("--input-csv", rotator)
        self.assertIn("would-normalize-exif", rotator)
        self.assertIn("当前 EXIF 已是正常方向", rotator)
        self.assertIn("/api/run-csv", server)
        self.assertIn("/api/restore-task", server)
        self.assertNotIn('id="applyFace"', web)
        self.assertIn("从备份恢复本次全部改动", web)
        self.assertIn("CSV 执行已暂停", web)


if __name__ == "__main__":
    unittest.main()
