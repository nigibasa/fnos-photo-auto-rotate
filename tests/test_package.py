from __future__ import annotations

import subprocess
import sys
import tarfile
import tempfile
import unittest
import io
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_python_sources_compile(self) -> None:
        for name in ["photo_auto_rotate.py", "server.py", "build_fpk.py", "verify_fpk.py"]:
            subprocess.run([sys.executable, "-m", "py_compile", str(ROOT / name)], check=True)

    def test_build_and_verify_fpk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            for platform in ("x86", "arm"):
                output = Path(temp) / f"PhotoAutoRotate_{platform}.fpk"
                subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "build_fpk.py"),
                        "--image",
                        "ghcr.io/example/fnos-photo-auto-rotate:2.1.0",
                        "--platform",
                        platform,
                        "--output",
                        str(output),
                    ],
                    check=True,
                )
                subprocess.run([sys.executable, str(ROOT / "verify_fpk.py"), str(output)], check=True)
                with tarfile.open(output, "r:gz") as package:
                    main = package.getmember("cmd/main")
                    self.assertEqual(main.mode & 0o111, 0o111)
                    app_bytes = package.extractfile("app.tgz").read()
                with tarfile.open(fileobj=io.BytesIO(app_bytes), mode="r:gz") as app:
                    compose = app.extractfile("docker/docker-compose.yaml").read().decode("utf-8")
                if platform == "x86":
                    self.assertIn("/dev/dri:/dev/dri", compose)
                else:
                    self.assertNotIn("/dev/dri:/dev/dri", compose)

    def test_release_workflow_exists(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("packages: write", workflow)
        self.assertIn("docker/build-push-action", workflow)
        self.assertIn("build_fpk.py", workflow)

    def test_v2_safety_contract_is_present(self) -> None:
        rotator = (ROOT / "photo_auto_rotate.py").read_text(encoding="utf-8")
        server = (ROOT / "server.py").read_text(encoding="utf-8")
        web = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn("apply_metadata_orientation", rotator)
        self.assertIn("decoded_pixel_fingerprint", rotator)
        self.assertIn("照片在扫描后发生变化，拒绝处理", rotator)
        self.assertIn("已有 EXIF Orientation=", rotator)
        self.assertNotIn("jpegtran", rotator)
        self.assertNotIn("Image.Transpose", rotator)
        self.assertIn("/api/apply", server)
        self.assertIn("/api/rollback", server)
        self.assertIn("APPLY METADATA", server)
        self.assertIn("只有勾选的照片才会写入 EXIF", web)
        self.assertIn("不旋转、不裁剪、不重新压缩照片像素", web)
        self.assertIn("ProcessPoolExecutor", rotator)
        self.assertIn("FaceDetectorYN", rotator)
        self.assertIn("photo-orientation-scan-progress", rotator)
        self.assertIn("/api/selections", server)
        self.assertIn('id="pageInput"', web)
        self.assertIn("function goToPage(page)", web)
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("opencv-python-headless==4.10.0.84", dockerfile)
        self.assertNotIn("python3-opencv", dockerfile)


if __name__ == "__main__":
    unittest.main()
