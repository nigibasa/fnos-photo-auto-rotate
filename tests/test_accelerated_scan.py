from __future__ import annotations

import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import photo_auto_rotate as rotator
import server as webserver


def create_jpeg(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (101, 77), color).save(path, "JPEG", quality=90)


def scan_item(path: Path, source: Path) -> dict:
    stat = path.stat()
    relative = path.relative_to(source).as_posix()
    fingerprint = rotator.fast_file_fingerprint(path)
    return {
        "id": rotator.candidate_id(relative, fingerprint),
        "relative_path": relative,
        "width": 101,
        "height": 77,
        "orientation": 1,
        "status": "suggested",
        "suggested_angle": 90,
        "confidence": 2.0,
        "reason": "测试建议",
        "file_sha256": "",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "scan_fingerprint": fingerprint,
        "scores": "",
    }


class InlineExecutor:
    def __init__(self, *args, **kwargs) -> None:
        self.initializer = kwargs.get("initializer")
        self.initargs = kwargs.get("initargs", ())

    def __enter__(self):
        if self.initializer:
            self.initializer(*self.initargs)
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def submit(self, function, argument):
        future = concurrent.futures.Future()
        try:
            future.set_result(function(argument))
        except BaseException as exc:
            future.set_exception(exc)
        return future


class AcceleratedScanTests(unittest.TestCase):
    @unittest.skipUnless(
        os.environ.get("YUNET_MODEL") and rotator.cv2 is not None,
        "YuNet model/OpenCV unavailable in this test environment",
    )
    def test_real_yunet_model_runs_on_cpu(self) -> None:
        import numpy as np

        detector = rotator.create_yunet_detector(Path(os.environ["YUNET_MODEL"]), "cpu")
        frame = np.zeros((320, 320, 3), dtype=np.uint8)
        score, count = rotator.face_score(detector, frame)
        self.assertEqual((score, count), (0.0, 0))
        self.assertTrue(hasattr(rotator.cv2, "FaceDetectorYN"))
        with tempfile.TemporaryDirectory() as temp_dir:
            photo = Path(temp_dir) / "scan.jpg"
            create_jpeg(photo, (40, 60, 80))
            item = rotator.classify_frame(photo, photo.name, detector, 1.35, False)
            self.assertEqual(item.scan_fingerprint, rotator.fast_file_fingerprint(photo))

    def test_resume_journal_skips_completed_photo_and_finishes_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "photos"
            work = root / "data"
            source.mkdir()
            first = source / "first.jpg"
            second = source / "second.jpg"
            create_jpeg(first, (10, 30, 50))
            create_jpeg(second, (80, 100, 120))

            settings = {
                "recursive": True,
                "min_confidence": 1.35,
                "allow_180": False,
                "min_age_minutes": 0,
                "cpu_workers": 2,
                "acceleration": "cpu",
            }
            scan_id = rotator.scan_identity(source.resolve(), settings)
            progress = work / "scans" / "in-progress"
            progress.mkdir(parents=True)
            journal = progress / f"{scan_id}.jsonl"
            journal.write_text(json.dumps(scan_item(first, source), ensure_ascii=False) + "\n", encoding="utf-8")

            def fake_worker(args):
                return scan_item(Path(args[0]), source)

            with (
                patch.object(rotator, "benchmark_acceleration", return_value=("cpu", {"requested": "cpu"})),
                patch.object(rotator, "init_cpu_worker"),
                patch.object(rotator, "classify_cpu_worker", side_effect=fake_worker),
                patch.object(rotator.concurrent.futures, "ProcessPoolExecutor", InlineExecutor),
            ):
                result = rotator.scan(
                    source.resolve(),
                    work.resolve(),
                    recursive=True,
                    min_confidence=1.35,
                    allow_180=False,
                    min_age_minutes=0,
                    cpu_workers=2,
                    acceleration="cpu",
                    checkpoint_every=1,
                    model=root / "fake.onnx",
                )

            self.assertEqual(result, 0)
            scans = list((work / "scans").glob("photo-orientation-scan-*.json"))
            self.assertEqual(len(scans), 1)
            payload = json.loads(scans[0].read_text(encoding="utf-8"))
            self.assertEqual(payload["counts"]["total"], 2)
            self.assertEqual({item["relative_path"] for item in payload["items"]}, {"first.jpg", "second.jpg"})
            self.assertFalse(journal.exists())
            self.assertFalse((progress / f"{scan_id}.json").exists())

    def test_gpu_auto_falls_back_when_opencl_is_unavailable(self) -> None:
        class FakeOcl:
            @staticmethod
            def haveOpenCL():
                return False

        class FakeCv2:
            ocl = FakeOcl()

        with patch.object(rotator, "cv2", FakeCv2()):
            backend, details = rotator.benchmark_acceleration(Path("missing.onnx"), "auto")
        self.assertEqual(backend, "cpu")
        self.assertIn("OpenCL", details["fallback"])

    def test_resume_rescans_photo_changed_after_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "photos"
            work = root / "data"
            source.mkdir()
            photo = source / "changed.jpg"
            create_jpeg(photo, (10, 30, 50))
            settings = {
                "recursive": True,
                "min_confidence": 1.35,
                "allow_180": False,
                "min_age_minutes": 0,
                "cpu_workers": 2,
                "acceleration": "cpu",
            }
            scan_id = rotator.scan_identity(source.resolve(), settings)
            progress = work / "scans" / "in-progress"
            progress.mkdir(parents=True)
            journal = progress / f"{scan_id}.jsonl"
            journal.write_text(json.dumps(scan_item(photo, source)) + "\n", encoding="utf-8")
            create_jpeg(photo, (200, 20, 40))
            calls = []

            def fake_worker(args):
                calls.append(args[1])
                return scan_item(Path(args[0]), source)

            with (
                patch.object(rotator, "benchmark_acceleration", return_value=("cpu", {"requested": "cpu"})),
                patch.object(rotator, "init_cpu_worker"),
                patch.object(rotator, "classify_cpu_worker", side_effect=fake_worker),
                patch.object(rotator.concurrent.futures, "ProcessPoolExecutor", InlineExecutor),
            ):
                result = rotator.scan(
                    source.resolve(),
                    work.resolve(),
                    recursive=True,
                    min_confidence=1.35,
                    allow_180=False,
                    min_age_minutes=0,
                    cpu_workers=2,
                    acceleration="cpu",
                    checkpoint_every=1,
                    model=root / "fake.onnx",
                )

            self.assertEqual(result, 0)
            self.assertEqual(calls, ["changed.jpg"])

    def test_scan_identity_changes_when_acceleration_settings_change(self) -> None:
        source = Path("/storage/vol2/photos")
        base = {
            "recursive": True,
            "min_confidence": 1.35,
            "allow_180": False,
            "min_age_minutes": 10,
            "cpu_workers": 2,
            "acceleration": "auto",
        }
        cpu = {**base, "acceleration": "cpu"}
        self.assertNotEqual(rotator.scan_identity(source, base), rotator.scan_identity(source, cpu))

    def test_fast_fingerprint_detects_same_size_same_mtime_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            photo = Path(temp_dir) / "sample.jpg"
            photo.write_bytes(b"A" * (256 * 1024))
            original_stat = photo.stat()
            original = rotator.fast_file_fingerprint(photo)
            with photo.open("r+b") as handle:
                handle.seek(128 * 1024)
                handle.write(b"B" * 4096)
            os.utime(photo, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            self.assertEqual(photo.stat().st_size, original_stat.st_size)
            self.assertEqual(photo.stat().st_mtime_ns, original_stat.st_mtime_ns)
            self.assertNotEqual(rotator.fast_file_fingerprint(photo), original)

    def test_stop_terminates_entire_process_group_on_linux(self) -> None:
        class FakeProcess:
            pid = 4321

            @staticmethod
            def poll():
                return None

        job = webserver.Job()
        job.process = FakeProcess()
        with (
            patch.object(webserver.os, "name", "posix"),
            patch.object(webserver.os, "killpg", create=True) as killpg,
        ):
            job.stop()
        killpg.assert_called_once_with(4321, webserver.signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
