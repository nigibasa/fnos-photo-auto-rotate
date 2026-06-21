from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import photo_auto_rotate as rotator
import server as webserver


def create_jpeg(path: Path, size=(101, 77), color=(30, 120, 220)) -> None:
    image = Image.new("RGB", size, color)
    image.save(path, "JPEG", quality=91)


def exif_orientation_segment(orientation: int) -> bytes:
    tiff = (
        b"II"
        + struct.pack("<H", 42)
        + struct.pack("<I", 8)
        + struct.pack("<H", 1)
        + struct.pack("<HHI", 0x0112, 3, 1)
        + struct.pack("<H", orientation)
        + b"\x00\x00"
        + struct.pack("<I", 0)
    )
    payload = b"Exif\x00\x00" + tiff
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def metadata_only_writer(path: Path, orientation: int) -> None:
    data = path.read_bytes()
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("not jpeg")
    path.write_bytes(data[:2] + exif_orientation_segment(orientation) + data[2:])


EXIFTOOL_AVAILABLE = bool(os.environ.get("EXIFTOOL_BIN") or shutil.which("exiftool"))


class SafeOrientationTests(unittest.TestCase):
    @unittest.skipUnless(EXIFTOOL_AVAILABLE, "ExifTool is not installed in this test environment")
    def test_real_exiftool_changes_only_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "photos"
            source.mkdir()
            photo = source / "real-exiftool.jpg"
            create_jpeg(photo, size=(101, 77))
            original_pixel_hash = rotator.decoded_pixel_fingerprint(photo)
            original_file_hash = rotator.sha256_file(photo)
            task_dir = root / "task"
            task_dir.mkdir()

            result = rotator.apply_metadata_orientation(
                source, photo.name, 90, original_file_hash, task_dir
            )

            self.assertEqual(rotator.read_image_info(photo), (101, 77, 6))
            self.assertEqual(rotator.decoded_pixel_fingerprint(photo), original_pixel_hash)
            self.assertNotEqual(result["after_sha256"], original_file_hash)

    def test_metadata_apply_preserves_odd_sized_jpeg_pixels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "photos"
            source.mkdir()
            photo = source / "odd.jpg"
            create_jpeg(photo)
            original_bytes = photo.read_bytes()
            original_pixel_hash = rotator.decoded_pixel_fingerprint(photo)
            task_dir = root / "task"
            task_dir.mkdir()

            with patch.object(rotator, "run_exiftool_set_orientation", metadata_only_writer):
                result = rotator.apply_metadata_orientation(
                    source, "odd.jpg", 90, rotator.sha256_file(photo), task_dir
                )

            self.assertEqual(rotator.read_image_info(photo), (101, 77, 6))
            self.assertEqual(rotator.decoded_pixel_fingerprint(photo), original_pixel_hash)
            self.assertNotEqual(photo.read_bytes(), original_bytes)
            backup = Path(result["backup"])
            self.assertEqual(backup.read_bytes(), original_bytes)
            self.assertEqual(result["pixel_sha256"], original_pixel_hash[0])

    def test_exiftool_failure_uses_exact_two_byte_orientation_patch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "photos"
            source.mkdir()
            photo = source / "broken-subifd.jpg"
            create_jpeg(photo)
            metadata_only_writer(photo, 1)
            original = photo.read_bytes()
            found = rotator.find_exif_orientation_value(original)
            self.assertIsNotNone(found)
            offset, order, value = found
            self.assertEqual(value, 1)
            task_dir = root / "task"
            task_dir.mkdir()

            with patch.object(
                rotator,
                "run_exiftool_set_orientation",
                side_effect=RuntimeError("Error reading OtherImageStart data in ExifIFD"),
            ):
                result = rotator.apply_metadata_orientation(
                    source, photo.name, 270, rotator.sha256_file(photo), task_dir
                )

            expected = bytearray(original)
            expected[offset : offset + 2] = (8).to_bytes(2, order)
            self.assertEqual(photo.read_bytes(), bytes(expected))
            self.assertEqual(result["write_method"], "surgical-two-byte-patch")
            self.assertEqual(rotator.read_image_info(photo)[2], 8)

    def test_exiftool_failure_without_existing_orientation_remains_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "photos"
            source.mkdir()
            photo = source / "no-orientation.jpg"
            create_jpeg(photo)
            original = photo.read_bytes()
            task_dir = root / "task"
            task_dir.mkdir()

            with patch.object(
                rotator,
                "run_exiftool_set_orientation",
                side_effect=RuntimeError("ExifTool failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "没有可安全原位修改"):
                    rotator.apply_metadata_orientation(
                        source, photo.name, 90, rotator.sha256_file(photo), task_dir
                    )
            self.assertEqual(photo.read_bytes(), original)

    def test_changed_since_scan_is_refused_without_touching_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "photos"
            source.mkdir()
            photo = source / "changed.jpg"
            create_jpeg(photo)
            stale_hash = rotator.sha256_file(photo)
            photo.write_bytes(photo.read_bytes() + b"changed")
            current = photo.read_bytes()
            task_dir = root / "task"
            task_dir.mkdir()

            with self.assertRaisesRegex(RuntimeError, "扫描后发生变化"):
                rotator.apply_metadata_orientation(source, "changed.jpg", 90, stale_hash, task_dir)

            self.assertEqual(photo.read_bytes(), current)
            self.assertFalse((task_dir / "backups").exists())

    def test_pixel_change_in_staging_is_refused_and_original_remains_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "photos"
            source.mkdir()
            photo = source / "unsafe.jpg"
            create_jpeg(photo)
            original = photo.read_bytes()
            task_dir = root / "task"
            task_dir.mkdir()

            def unsafe_writer(path: Path, orientation: int) -> None:
                Image.new("RGB", (101, 77), (255, 0, 0)).save(path, "JPEG")

            with patch.object(rotator, "run_exiftool_set_orientation", unsafe_writer):
                with self.assertRaisesRegex(RuntimeError, "像素或尺寸发生变化"):
                    rotator.apply_metadata_orientation(
                        source, "unsafe.jpg", 90, rotator.sha256_file(photo), task_dir
                    )

            self.assertEqual(photo.read_bytes(), original)

    def test_rollback_restores_exact_original_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "photos"
            source.mkdir()
            photo = source / "rollback.jpg"
            create_jpeg(photo)
            original = photo.read_bytes()
            task_dir = root / "tasks" / "one"
            task_dir.mkdir(parents=True)

            with patch.object(rotator, "run_exiftool_set_orientation", metadata_only_writer):
                result = rotator.apply_metadata_orientation(
                    source, "rollback.jpg", 270, rotator.sha256_file(photo), task_dir
                )

            manifest = {
                "schema": 2,
                "kind": "photo-orientation-task",
                "source": str(source.resolve()),
                "results": [result],
            }
            manifest_path = task_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(rotator.rollback_task(source.resolve(), manifest_path), 0)
            self.assertEqual(photo.read_bytes(), original)
            self.assertEqual(rotator.read_image_info(photo)[2], 1)
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["results"][0]["status"], "rolled-back")

    def test_interrupted_pending_item_can_be_proven_and_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "photos"
            source.mkdir()
            photo = source / "interrupted.jpg"
            create_jpeg(photo)
            original_hash = rotator.sha256_file(photo)
            task_dir = root / "tasks" / "interrupted"
            task_dir.mkdir(parents=True)

            with patch.object(rotator, "run_exiftool_set_orientation", metadata_only_writer):
                result = rotator.apply_metadata_orientation(
                    source, photo.name, 90, original_hash, task_dir
                )

            pending = {
                "relative_path": photo.name,
                "angle": 90,
                "orientation": 6,
                "before_sha256": original_hash,
                "backup": result["backup"],
                "status": "pending",
            }
            manifest = {
                "schema": 2,
                "kind": "photo-orientation-task",
                "source": str(source.resolve()),
                "results": [pending],
            }
            manifest_path = task_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            self.assertEqual(rotator.rollback_task(source.resolve(), manifest_path), 0)
            self.assertEqual(rotator.sha256_file(photo), original_hash)

    def test_existing_exif_orientation_is_never_suggested_for_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            photo = Path(temp_dir) / "managed.jpg"
            create_jpeg(photo)
            metadata_only_writer(photo, 6)
            item = rotator.classify(photo, "managed.jpg", cascade=None, min_confidence=1.35, allow_180=False)
            self.assertEqual(item.status, "exif-managed")
            self.assertEqual(item.orientation, 6)
            self.assertIn("不修改", item.reason)

    def test_pixel_rotating_tools_are_absent_from_v2_core(self) -> None:
        source = Path(rotator.__file__).read_text(encoding="utf-8")
        self.assertNotIn("jpegtran", source)
        self.assertNotIn("apply_transform", source)
        self.assertNotIn("Image.Transpose", source)

    def test_http_review_flow_validates_and_builds_approval_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data = root / "data"
            scans = data / "scans"
            photos = root / "photos"
            scans.mkdir(parents=True)
            photos.mkdir()
            photo = photos / "candidate.jpg"
            create_jpeg(photo)
            file_hash = rotator.sha256_file(photo)
            photo_stat = photo.stat()
            item_id = rotator.candidate_id(photo.name, file_hash)
            scan_name = "photo-orientation-scan-test.json"
            scan = {
                "schema": 2,
                "kind": "photo-orientation-scan",
                "created_at": "2026-06-20T12:00:00+08:00",
                "source": str(photos.resolve()),
                "model": rotator.MODEL_VERSION,
                "counts": {"suggested": 1, "total": 1, "errors": 0},
                "items": [
                    {
                        "id": item_id,
                        "relative_path": photo.name,
                        "width": 101,
                        "height": 77,
                        "orientation": 1,
                        "status": "suggested",
                        "suggested_angle": 90,
                        "confidence": 2.0,
                        "reason": "人工确认测试",
                        "file_sha256": "",
                        "size": photo_stat.st_size,
                        "mtime_ns": photo_stat.st_mtime_ns,
                        "scan_fingerprint": rotator.fast_file_fingerprint(photo),
                    }
                ],
            }
            (scans / scan_name).write_text(json.dumps(scan), encoding="utf-8")

            original_globals = (
                webserver.DATA_DIR,
                webserver.CONFIG_FILE,
                webserver.APPROVAL_DIR,
                webserver.SELECTION_DIR,
                webserver.ALLOWED_ROOT,
            )
            webserver.DATA_DIR = data
            webserver.CONFIG_FILE = data / "config.json"
            webserver.APPROVAL_DIR = data / "approvals"
            webserver.SELECTION_DIR = data / "selections"
            webserver.ALLOWED_ROOT = root.resolve()
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), webserver.Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            try:
                with urllib.request.urlopen(
                    f"{base}/api/candidates?scan={scan_name}&status=suggested"
                ) as response:
                    candidates = json.loads(response.read().decode("utf-8"))
                self.assertEqual(candidates["total"], 1)
                self.assertEqual(candidates["items"][0]["id"], item_id)

                with urllib.request.urlopen(
                    f"{base}/api/thumbnail?scan={scan_name}&id={item_id}"
                ) as response:
                    thumbnail = response.read()
                self.assertTrue(thumbnail.startswith(b"\xff\xd8"))

                payload = {
                    "scan": scan_name,
                    "confirm": "APPLY METADATA",
                    "config": {
                        "source": str(photos),
                        "recursive": True,
                        "allow_180": False,
                        "min_confidence": 1.35,
                        "min_age_minutes": 10,
                    },
                    "items": [{"id": item_id, "angle": 90}],
                }
                request = urllib.request.Request(
                    f"{base}/api/apply",
                    method="POST",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with patch.object(webserver.JOB, "start") as start:
                    with urllib.request.urlopen(request) as response:
                        self.assertEqual(response.status, 202)
                    start.assert_called_once()

                approvals = list((data / "approvals").glob("*.json"))
                self.assertEqual(len(approvals), 1)
                approval = json.loads(approvals[0].read_text(encoding="utf-8"))
                self.assertEqual(approval["items"][0]["relative_path"], photo.name)
                self.assertEqual(approval["items"][0]["file_sha256"], file_hash)

                original_stat = photo.stat()
                original_bytes = photo.read_bytes()
                changed = bytearray(original_bytes)
                changed[len(changed) // 2] ^= 1
                photo.write_bytes(changed)
                os.utime(photo, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 400)
                photo.write_bytes(original_bytes)
                os.utime(photo, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

                selection_payload = {
                    "scan": scan_name,
                    "items": [{"id": item_id, "angle": 90}],
                }
                selection_request = urllib.request.Request(
                    f"{base}/api/selections",
                    method="POST",
                    data=json.dumps(selection_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(selection_request) as response:
                    self.assertEqual(response.status, 200)
                with urllib.request.urlopen(
                    f"{base}/api/selections?scan={scan_name}"
                ) as response:
                    saved_selections = json.loads(response.read().decode("utf-8"))
                self.assertEqual(saved_selections["items"], [{"id": item_id, "angle": 90}])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=3)
                (
                    webserver.DATA_DIR,
                    webserver.CONFIG_FILE,
                    webserver.APPROVAL_DIR,
                    webserver.SELECTION_DIR,
                    webserver.ALLOWED_ROOT,
                ) = original_globals


if __name__ == "__main__":
    unittest.main()
