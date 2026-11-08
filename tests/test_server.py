"""HTTP integration tests; no model downloads, GPU, or personal images needed."""
import http.client
import io
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote, unquote

import numpy as np
from PIL import Image, ImageCms

from studio.engine import ProcessingCancelled
from studio.server import ROOT, StudioServer, StudioState


def encoded_image(image, file_format="PNG", **kwargs):
    output = io.BytesIO()
    image.save(output, file_format, **kwargs)
    return output.getvalue()


class ControlledProcessor:
    """A stub with real file publication and an interruptible running stage."""

    def __init__(self):
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = False

    def __call__(self, source, destination, settings, progress, cancelled):
        self.calls.append((Path(source), Path(destination), dict(settings)))
        # A partial result deliberately exercises the server's failed/cancelled cleanup.
        if self.block:
            Path(destination).write_bytes(b"incomplete stub output")
        progress(35, "测试阶段", {"message": "等待测试继续", "actual_device": "cpu"})
        self.started.set()
        deadline = time.monotonic() + 10
        while self.block and not self.release.wait(.01):
            if cancelled():
                raise ProcessingCancelled("测试任务已取消")
            if time.monotonic() > deadline:
                raise RuntimeError("Test processor timed out")
        if cancelled():
            raise ProcessingCancelled("测试任务已取消")
        shutil.copyfile(source, destination)
        progress(100, "测试处理完成", {"actual_device": "cpu"})
        return {"actual_device": "cpu", "applied_stages": ["stub_copy"], "duration_seconds": .01,
                "output_path": str(destination)}


class ServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        workspace = (ROOT / "work").resolve()
        workspace.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="http-test-", dir=workspace)
        self.folder = Path(self.temporary.name).resolve()
        self.assertTrue(self.folder.is_relative_to(workspace))
        self.processor = ControlledProcessor()
        self.state = StudioState(root=ROOT, runtime=self.folder / "runtime",
                                 output=self.folder / "results", processor=self.processor, monitor=False)
        self.server = StudioServer(("127.0.0.1", 0), self.state)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        self.state.close()
        self.processor.release.set()
        self.state.worker.join(timeout=3)
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.assertFalse(self.state.worker.is_alive(), "Non-daemon processing worker did not stop")
        self.assertFalse(self.thread.is_alive(), "HTTP server did not stop")
        self.temporary.cleanup()

    def request(self, method, path, body=None, *, auth=True, headers=None):
        supplied = {}
        if method == "POST" and auth:
            supplied.update({"X-Token": self.state.token, "Origin": f"http://127.0.0.1:{self.port}"})
        if headers:
            supplied.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=supplied)
            response = connection.getresponse()
            raw = response.read()
            response_headers = dict(response.getheaders())
            if response_headers.get("Content-Type", "").startswith("application/json"):
                result = json.loads(raw)
            else:
                result = raw
            return response.status, result, response_headers
        finally:
            connection.close()

    def import_image(self, image=None, *, name="样例.png", file_format="PNG", **kwargs):
        if image is None:
            image = Image.new("RGB", (24, 16), (31, 127, 221))
        status, result, _ = self.request("POST", "/api/images?name=" + quote(name),
                                        encoded_image(image, file_format, **kwargs))
        self.assertEqual(status, 201, result)
        return result

    def submit(self, image, **settings):
        payload = {"image_id": image["id"], "mode": "photo_portrait", "method": "default",
                   "strength": 30, "device": "cpu", **settings}
        status, result, _ = self.request("POST", "/api/jobs", json.dumps(payload).encode("utf-8"),
                                        headers={"Content-Type": "application/json"})
        self.assertEqual(status, 202, result)
        return result

    def wait_job(self, identity, expected):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status, result, _ = self.request("GET", f"/api/jobs/{identity}")
            self.assertEqual(status, 200, result)
            if result["status"] in expected:
                return result
            if result["status"] in {"failed", "cancelled", "completed"}:
                self.fail(f"Unexpected terminal job state: {result}")
            time.sleep(.01)
        self.fail(f"Job {identity} did not reach {expected}; last state {result}")

    def loaded_source(self, image):
        status, raw, _ = self.request("GET", image["source_url"])
        self.assertEqual(status, 200)
        with Image.open(io.BytesIO(raw)) as source:
            source.load()
            return source.copy()

    def test_import_is_manual_and_page_injects_current_token(self):
        status, page, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(self.state.token.encode("ascii"), page)
        self.assertNotIn(b"__TOKEN__", page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        image = self.import_image()
        self.assertFalse(self.processor.started.wait(.05), "Import unexpectedly started processing")
        self.assertEqual(self.state.jobs, {})
        self.assertEqual(self.processor.calls, [])
        self.assertEqual(list(self.state.output.iterdir()), [])
        self.assertEqual(self.loaded_source(image).getpixel((0, 0)), (31, 127, 221))

    def test_token_foreign_origin_and_foreign_host_are_rejected(self):
        body = encoded_image(Image.new("RGB", (16, 16)))
        cases = [
            {"auth": False},
            {"headers": {"X-Token": "incorrect-token"}},
            {"headers": {"Origin": "https://foreign.example"}},
            {"headers": {"Origin": "null"}},
            {"headers": {"Host": f"foreign.example:{self.port}"}},
        ]
        for options in cases:
            with self.subTest(options=options):
                status, result, _ = self.request("POST", "/api/images?name=denied.png", body, **options)
                self.assertEqual(status, 403, result)
        status, _, _ = self.request("GET", "/api/health", headers={"Host": "foreign.example"})
        self.assertEqual(status, 403)
        self.assertEqual(self.state.images, {})
        self.assertEqual(list(self.state.imports.iterdir()), [])

    def test_completed_jobs_publish_unique_results_and_receipts(self):
        image = self.import_image(name="相同名称.png")
        source = self.state.images[image["id"]]["path"]
        original = source.read_bytes()
        jobs = [self.submit(image), self.submit(image)]
        completed = [self.wait_job(job["id"], {"completed"}) for job in jobs]
        outputs = sorted(self.state.output.glob("*.png"))
        self.assertEqual(len(outputs), 2)
        self.assertNotEqual(outputs[0].name, outputs[1].name)
        self.assertEqual({p.name for p in outputs}, {
            "相同名称_实拍人像_默认_强度30.png", "相同名称_实拍人像_默认_强度30_02.png"})
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(len(self.processor.calls), 2)
        for job in completed:
            self.assertEqual(job["progress"], 100)
            self.assertEqual(job["actual_device"], "cpu")
            self.assertGreaterEqual(job["elapsed_seconds"], 0)
            status, downloaded, headers = self.request("GET", job["download_url"])
            self.assertEqual(status, 200)
            self.assertEqual(downloaded, original)
            self.assertEqual(headers["Content-Type"], "image/png")
            self.assertIn("attachment", headers["Content-Disposition"])
            self.assertIn(job["result_name"], unquote(headers["Content-Disposition"]))
            self.assertNotIn("cancel", job)
            self.assertNotIn("result_path", job)
        for output in outputs:
            receipt = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(receipt["app"], "ClearFrame")
            self.assertEqual(receipt["source_name"], "相同名称.png")
            self.assertEqual(receipt["result_name"], output.name)
            self.assertEqual(receipt["settings"]["mode"], "photo_portrait")
            self.assertEqual(receipt["metadata"]["actual_device"], "cpu")
            self.assertEqual(Path(receipt["metadata"]["output_path"]), output)
            self.assertTrue(receipt["completed_at"].endswith("Z"))

    def test_output_change_persists_and_only_redirects_new_submissions(self):
        old_output = self.state.output
        image = self.import_image()
        self.processor.block = True
        first = self.submit(image)
        self.assertTrue(self.processor.started.wait(2))
        queued = self.submit(image)
        new_output = self.folder / "自定义结果 空格"
        status, response, _ = self.request("POST", "/api/output",
            json.dumps({"path": str(new_output)}).encode())
        self.assertEqual(status, 200, response)
        self.assertEqual(Path(response["output_dir"]), new_output)
        status, config, _ = self.request("GET", "/api/config")
        self.assertEqual(Path(config["output_dir"]), new_output)
        self.assertEqual(Path(config["output_default_dir"]), ROOT / "results")
        newer = self.submit(image)
        self.processor.release.set()
        for job in (first, queued, newer):
            result = self.wait_job(job["id"], {"completed"})
            self.assertEqual(self.request("GET", result["download_url"])[0], 200)
        destinations = [self.state.jobs[job["id"]]["result_path"].parent for job in (first, queued, newer)]
        self.assertEqual(destinations, [old_output, old_output, new_output])
        restored = StudioState(root=self.folder, runtime=self.state.runtime, monitor=False)
        try:
            self.assertEqual(restored.output, new_output)
        finally:
            restored.close()
            restored.worker.join(timeout=3)

    def test_output_rejects_invalid_paths_and_protects_new_write_routes(self):
        before = self.state.output
        plain_file = self.folder / "not-a-folder.txt"
        plain_file.write_text("keep", encoding="utf-8")
        for value in ("relative/path", str(plain_file), 123, None):
            with self.subTest(value=value):
                status, response, _ = self.request("POST", "/api/output",
                    json.dumps({"path": value}).encode())
                self.assertEqual(status, 400, response)
                self.assertEqual(self.state.output, before)
        for route, payload in (("/api/output", {"path": str(self.folder / "denied")}),
                               ("/api/output/open", {}),
                               ("/api/exports", {"image_ids": []}),
                               ("/api/exports/" + "a" * 32 + "/open", {})):
            self.assertEqual(self.request("POST", route, json.dumps(payload).encode(), auth=False)[0], 403)
        self.assertFalse((self.folder / "denied").exists())
        self.assertEqual(plain_file.read_text(), "keep")

    def test_export_uses_latest_success_even_during_rerun_and_keeps_originals(self):
        a = self.import_image(name="同名.png")
        b = self.import_image(name="同名.png")
        pending = self.import_image(name="尚未处理.png")
        old = self.wait_job(self.submit(a, strength=10)["id"], {"completed"})
        latest = self.wait_job(self.submit(a, strength=20)["id"], {"completed"})
        other = self.wait_job(self.submit(b, strength=20)["id"], {"completed"})
        originals = {p.name: p.read_bytes() for p in self.state.output.glob("*") if p.is_file()}
        self.processor.block = True
        self.processor.started.clear()
        running = self.submit(a, strength=90)
        self.assertTrue(self.processor.started.wait(2))
        export_body = json.dumps({"image_ids": [a["id"], b["id"], pending["id"]]}).encode()
        status, response, _ = self.request("POST", "/api/exports", export_body)
        self.assertEqual(status, 201, response)
        folder = Path(response["path"])
        self.assertEqual(folder.parent, self.state.output)
        self.assertTrue(folder.name.startswith("队列导出_"))
        self.assertEqual(response["count"], 2)
        self.assertEqual(response["skipped"], [pending["id"]])
        self.assertEqual({p.name for p in folder.glob("*.png")}, {latest["result_name"], other["result_name"]})
        self.assertFalse((folder / old["result_name"]).exists())
        for filename in (latest["result_name"], other["result_name"]):
            self.assertEqual((folder / filename).read_bytes(), originals[filename])
            receipt = Path(filename).with_suffix(".json").name
            self.assertEqual((folder / receipt).read_bytes(), originals[receipt])
        status, repeated, _ = self.request("POST", "/api/exports", export_body)
        self.assertEqual(status, 201, repeated)
        self.assertNotEqual(repeated["path"], str(folder))
        for name, content in originals.items():
            self.assertEqual((self.state.output / name).read_bytes(), content)
        self.request("POST", f"/api/jobs/{running['id']}/cancel")
        self.wait_job(running["id"], {"cancelled"})

    def test_export_rejects_unknown_or_empty_queue_without_creating_folders(self):
        image = self.import_image()
        for payload in ({"image_ids": []}, {"image_ids": ["a" * 32]},
                        {"image_ids": [image["id"]]}, {"image_ids": "not-a-list"}):
            status, response, _ = self.request("POST", "/api/exports", json.dumps(payload).encode())
            self.assertEqual(status, 400, response)
        self.assertEqual(list(self.state.output.iterdir()), [])

    def test_cancel_running_task_removes_partial_output(self):
        self.processor.block = True
        job = self.submit(self.import_image())
        self.assertTrue(self.processor.started.wait(2))
        running = self.wait_job(job["id"], {"running"})
        self.assertEqual(running["progress"], 35)
        self.assertEqual(running["actual_device"], "cpu")
        status, _, _ = self.request("GET", f"/api/jobs/{job['id']}/result")
        self.assertEqual(status, 409, "Running partial output must not be downloadable")
        status, response, _ = self.request("POST", f"/api/jobs/{job['id']}/cancel")
        self.assertEqual((status, response), (200, {"ok": True}))
        cancelled = self.wait_job(job["id"], {"cancelled"})
        self.assertIsNone(cancelled["result_url"])
        self.assertIsNone(cancelled["error"])
        self.assertEqual(list(self.state.output.iterdir()), [])

    def test_cancel_queued_task_never_invokes_processor(self):
        self.processor.block = True
        image = self.import_image()
        first = self.submit(image)
        self.assertTrue(self.processor.started.wait(2))
        second = self.submit(image)
        queued = self.wait_job(second["id"], {"queued"})
        self.assertIsNone(queued["actual_device"])
        status, _, _ = self.request("POST", f"/api/jobs/{second['id']}/cancel")
        self.assertEqual(status, 200)
        self.wait_job(second["id"], {"cancelled"})
        self.processor.release.set()
        self.wait_job(first["id"], {"completed"})
        deadline = time.monotonic() + 2
        while self.state.work.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.state.work.unfinished_tasks, 0)
        self.assertEqual(len(self.processor.calls), 1)
        self.assertEqual(len(list(self.state.output.glob("*.png"))), 1)

    def test_shutdown_cancels_active_work_and_worker_exits(self):
        self.processor.block = True
        job = self.submit(self.import_image())
        self.assertTrue(self.processor.started.wait(2))
        status, result, _ = self.request("POST", "/api/shutdown")
        self.assertEqual((status, result), (200, {"ok": True}))
        self.state.worker.join(timeout=2)
        self.thread.join(timeout=2)
        self.assertFalse(self.state.worker.is_alive())
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.state.jobs[job["id"]]["status"], "cancelled")
        self.assertEqual(list(self.state.output.iterdir()), [])

    def test_corrupt_import_is_rejected_without_orphans(self):
        status, result, _ = self.request("POST", "/api/images?name=not-an-image.txt", b"not an image")
        self.assertEqual(status, 400, result)
        self.assertIn("无法识别", result["error"])
        self.assertIn("PNG", result["error"])
        self.assertNotIn("BytesIO", result["error"])
        self.assertNotIn("0x", result["error"])
        self.assertEqual(self.state.images, {})
        self.assertEqual(list(self.state.imports.iterdir()), [])
        self.assertEqual(self.processor.calls, [])

    def test_truncated_pixels_return_chinese_input_error(self):
        pixels = np.random.default_rng(17).integers(0, 256, (32, 32, 3), dtype=np.uint8)
        complete = encoded_image(Image.fromarray(pixels))
        status, result, _ = self.request("POST", "/api/images?name=truncated.png", complete[:len(complete) // 2])
        self.assertEqual(status, 400, result)
        self.assertIn("损坏或不完整", result["error"])
        self.assertNotIn("0x", result["error"])
        self.assertEqual(self.state.images, {})
        self.assertEqual(list(self.state.imports.iterdir()), [])

    def test_bad_cmyk_profile_returns_actionable_input_error(self):
        data = encoded_image(Image.new("CMYK", (24, 16)), "JPEG", icc_profile=b"invalid-profile")
        status, result, _ = self.request("POST", "/api/images?name=bad-profile.jpg", data)
        self.assertEqual(status, 400, result)
        self.assertIn("色彩配置", result["error"])
        self.assertIn("sRGB", result["error"])
        self.assertEqual(self.state.images, {})
        self.assertEqual(list(self.state.imports.iterdir()), [])

    def test_import_output_failure_remains_server_error(self):
        data = encoded_image(Image.new("RGB", (24, 16)))
        with mock.patch("studio.server.Image.Image.save", side_effect=OSError("disk write failed")):
            status, result, _ = self.request("POST", "/api/images?name=valid.png", data)
        self.assertEqual(status, 500, result)
        self.assertEqual(self.state.images, {})
        self.assertEqual(list(self.state.imports.iterdir()), [])

    def test_float_and_low_value_16_bit_imports_are_scaled(self):
        values = np.tile(np.linspace(0, 1, 16, dtype=np.float32), (16, 1))
        float_image = self.import_image(Image.fromarray(values), name="float.tif", file_format="TIFF")
        actual = np.asarray(self.loaded_source(float_image))[..., 0]
        expected = np.clip(values.astype(np.float64) * 255 + .5, 0, 255).astype(np.uint8)
        np.testing.assert_array_equal(actual, expected)

        # Even a dark 16-bit image whose max is below 255 must use its 16-bit scale.
        values16 = np.tile(np.array([0, 127, 128, 255], dtype=np.uint16), (16, 4))
        integer_image = self.import_image(Image.fromarray(values16), name="dark16.tif", file_format="TIFF")
        actual16 = np.asarray(self.loaded_source(integer_image))[..., 0]
        expected16 = np.clip(values16.astype(np.float64) / 257 + .5, 0, 255).astype(np.uint8)
        np.testing.assert_array_equal(actual16, expected16)

    def test_import_preserves_alpha_rgb_pixels_and_icc(self):
        values = np.zeros((16, 24, 4), dtype=np.uint8)
        values[..., :3] = (51, 121, 191)
        values[..., 3] = np.arange(24, dtype=np.uint8) * 11
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        image = self.import_image(Image.fromarray(values, "RGBA"), icc_profile=profile)
        self.assertTrue(image["has_alpha"])
        normalized = self.loaded_source(image)
        self.assertEqual(normalized.mode, "RGBA")
        np.testing.assert_array_equal(np.asarray(normalized), values)
        self.assertEqual(normalized.info.get("icc_profile"), profile)


if __name__ == "__main__":
    unittest.main()
