import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from studio.engine import ProcessingCancelled, _Reporter, _band_reporting, _ensure_model, _photo_denoise, _select_device, cuda_compatibility_error, process_image
from studio.presets import get_config, resolve_recipe, validate_settings


class PresetTests(unittest.TestCase):
    def test_photo_defaults_never_use_anime_reconstruction(self):
        for mode in ("photo_portrait", "photo_landscape"):
            for method in ("default", "gentle", "texture_clean"):
                recipe = resolve_recipe({"mode": mode, "method": method})
                self.assertNotIn("anime_sr", recipe["stages"])
                self.assertNotIn("l0", recipe["stages"])

    def test_custom_is_canonical_and_rejects_invalid_parameters(self):
        recipe = validate_settings({"method": "custom", "stages": ["sharpen", "l0", "photo_denoise"]})
        self.assertEqual(recipe["stages"], ["photo_denoise", "l0", "sharpen"])
        for settings in ({"mode": []}, {"method": {}}, {"strength": -1}, {"strength": float("nan")}, {"strength": True}, {"device": "mps"}, {"method": "custom", "stages": []}, {"method": "custom", "stages": ["l0", "l0"]}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                validate_settings(settings)

    def test_config_is_json_and_not_mutable_global_state(self):
        data = get_config()
        json.dumps(data)
        data["modes"][0]["label"] = "changed"
        self.assertNotEqual(get_config()["modes"][0]["label"], "changed")


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        rng = np.random.default_rng(9)
        self.rgba = rng.integers(0, 256, (48, 64, 4), dtype=np.uint8)
        self.source = self.folder / "source.png"
        Image.fromarray(self.rgba).save(self.source)
        self.output = self.folder / "result.png"

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_blackwell_build_never_launches_a_kernel(self):
        torch_stub = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True, get_device_capability=lambda: (12, 0)),
                                     version=SimpleNamespace(cuda="11.7"), ones=mock.Mock(side_effect=AssertionError("legacy JIT must never start")))
        upstream = SimpleNamespace(torch=torch_stub)
        with mock.patch("studio.engine._upstream", return_value=upstream):
            self.assertEqual(_select_device("auto", True), ("cpu", upstream))
            with self.assertRaisesRegex(ValueError, "CUDA 12.8"):
                _select_device("cuda", True)
            self.assertEqual(_select_device("cpu", True), ("cpu", upstream))
        torch_stub.ones.assert_not_called()

    def test_cuda_build_guard_accepts_current_blackwell_and_older_gpu(self):
        stub = SimpleNamespace(cuda=SimpleNamespace(get_device_capability=lambda: (12, 0)), version=SimpleNamespace(cuda="12.8"))
        self.assertIsNone(cuda_compatibility_error(stub))
        stub.version.cuda = "13.0"
        self.assertIsNone(cuda_compatibility_error(stub))
        stub.version.cuda = None
        self.assertIn("CUDA 12.8", cuda_compatibility_error(stub))
        stub.cuda.get_device_capability = lambda: (8, 6)
        stub.version.cuda = "11.7"
        self.assertIsNone(cuda_compatibility_error(stub))

    def test_zero_strength_exact_identity_no_torch_dependency(self):
        with mock.patch("studio.engine._upstream", side_effect=AssertionError("torch should not load")):
            meta = process_image(self.source, self.output, {"strength": 0, "device": "cuda"})
        np.testing.assert_array_equal(np.array(Image.open(self.output)), self.rgba)
        self.assertEqual(meta["applied_stages"], [])

    def test_photo_pipeline_preserves_alpha_dimensions_and_progress(self):
        events = []
        with mock.patch("studio.engine._upstream", side_effect=AssertionError("photo should not load torch")):
            meta = process_image(self.source, self.output, {"mode": "photo_portrait", "strength": 35}, lambda p, s, d: events.append(p))
        actual = np.array(Image.open(self.output))
        self.assertEqual(actual.shape, self.rgba.shape)
        np.testing.assert_array_equal(actual[:, :, 3], self.rgba[:, :, 3])
        self.assertEqual(events[-1], 100)
        self.assertEqual(events, sorted(events))
        self.assertEqual(meta["actual_device"], "cpu")

    def test_cancel_does_not_publish_result(self):
        is_cancelled = False

        def progress(p, stage, detail):
            nonlocal is_cancelled
            if p >= 6:
                is_cancelled = True

        with self.assertRaises(ProcessingCancelled):
            process_image(self.source, self.output, {"mode": "photo_landscape"}, progress, lambda: is_cancelled)
        self.assertFalse(self.output.exists())
        np.testing.assert_array_equal(np.array(Image.open(self.source)), self.rgba)

    def test_photo_cancellation_between_real_bands(self):
        Image.new("RGB", (64, 320), (120, 130, 140)).save(self.source)
        is_cancelled = False
        bands = []
        def progress(p, stage, detail):
            nonlocal is_cancelled
            if stage == "照片非局部均值降噪":
                bands.append(detail)
                is_cancelled = True
        with self.assertRaises(ProcessingCancelled):
            process_image(self.source, self.output, {"mode": "photo_portrait"}, progress, lambda: is_cancelled)
        self.assertEqual(len(bands), 1)
        self.assertIn("128/320", bands[0]["message"])
        self.assertFalse(self.output.exists())

    def test_nlm_bands_match_whole_image_and_reduce_flat_field_noise(self):
        import cv2
        rng = np.random.default_rng(12)
        noisy = np.clip(127 + rng.normal(0, 7, (270, 48, 3)), 0, 255).astype(np.uint8)
        params = resolve_recipe({"mode": "photo_landscape", "strength": 70})["parameters"]
        actual = np.asarray(_photo_denoise(Image.fromarray(noisy), params, _Reporter(None, None), 0, 100))
        expected = cv2.cvtColor(cv2.fastNlMeansDenoisingColored(cv2.cvtColor(noisy, cv2.COLOR_RGB2BGR), None, params["nlm_h"], params["nlm_color_h"], 7, 21), cv2.COLOR_BGR2RGB)
        np.testing.assert_array_equal(actual, expected)
        self.assertLess(actual.var(), noisy.var())

    def test_output_never_overwrites_source_or_prior_result(self):
        with self.assertRaises(ValueError):
            process_image(self.source, self.source, {"strength": 0})
        self.output.write_bytes(b"previous result")
        with self.assertRaises(ValueError):
            process_image(self.source, self.output, {"strength": 0})
        self.assertEqual(self.output.read_bytes(), b"previous result")

    def test_sharpen_custom_preserves_rgb_icc(self):
        rgb = self.folder / "icc.png"
        Image.fromarray(self.rgba[:, :, :3]).save(rgb, icc_profile=b"test-profile-payload")
        process_image(rgb, self.output, {"method": "custom", "stages": ["sharpen"], "strength": 45})
        with Image.open(self.output) as result:
            self.assertEqual(result.info["icc_profile"], b"test-profile-payload")

    @unittest.skipUnless(importlib.util.find_spec("torch"), "L0 upstream module requires the optional torch dependency")
    def test_real_l0_cpu_preserves_edges_and_reports_iterations(self):
        rng = np.random.default_rng(42)
        clean = np.full((32, 48, 3), 80.0)
        clean[:, 24:, :] = 170.0
        noisy = np.clip(clean + rng.normal(0, 4, clean.shape), 0, 255).astype(np.uint8)
        Image.fromarray(noisy).save(self.source)
        events = []
        meta = process_image(self.source, self.output, {"method": "custom", "stages": ["l0"], "strength": 70, "device": "cpu"}, lambda p, s, d: events.append((p, s, d)))
        with Image.open(self.output) as result:
            pixels = np.asarray(result)
        self.assertLess(np.square(pixels.astype(float) - clean).mean(), np.square(noisy.astype(float) - clean).mean())
        self.assertGreater(pixels[:, 24:, :].mean() - pixels[:, :24, :].mean(), 80)
        self.assertGreater(len([e for e in events if e[1] == "L0 色块平滑"]), 20)
        self.assertEqual(meta["actual_device"], "cpu")

    def test_anime_adapter_can_be_cancelled_between_stages(self):
        seen = []
        def simulated_sr(im, down, device, dn, report, begin, end):
            report.emit(end, "模拟模型完成", device=device)
            seen.append("sr")
            return im.copy()
        with mock.patch("studio.engine._select_device", return_value=("cpu", object())), mock.patch("studio.engine._anime_sr", side_effect=simulated_sr), mock.patch("studio.engine._l0", side_effect=AssertionError("must cancel before L0")):
            with self.assertRaises(ProcessingCancelled):
                process_image(self.source, self.output, {"mode": "anime_character"}, cancelled=lambda: bool(seen))
        self.assertFalse(self.output.exists())

    def test_band_adapter_reports_work_and_restores_upstream_on_cancel(self):
        def bands(fn, n, budget, per_row, halo, stop):
            fn(0, n // 2, 0, n)
            fn(n // 2, n, 0, n)
        upstream = SimpleNamespace(_run_bands=bands, HEAD_HALO=96)
        events = []
        report = _Reporter(lambda p, s, d: events.append(p), None)
        with _band_reporting(upstream, report, 10, 90, "cpu"):
            upstream._run_bands(lambda *args: None, 20, 1, 1, 96, None)
            upstream._run_bands(lambda *args: None, 20, 1, 1, 3, None)
        self.assertIs(upstream._run_bands, bands)
        self.assertEqual(events, sorted(set(events)))
        self.assertEqual(events[-1], 90)
        with self.assertRaises(ProcessingCancelled), _band_reporting(upstream, _Reporter(None, lambda: True), 0, 100, "cpu"):
            upstream._run_bands(lambda *args: None, 20, 1, 1, 3, None)
        self.assertIs(upstream._run_bands, bands)

    def test_existing_model_cache_is_verified_before_loading(self):
        payload = b"known-good-model"
        name = "model.pth"
        upstream = SimpleNamespace(MODELS={"esrgan": (name, "https://example.invalid/model")}, SHA256={name: hashlib.sha256(payload).hexdigest()})
        cached = self.folder / name
        cached.write_bytes(payload)
        with mock.patch.dict("os.environ", {"CLEARFRAME_MODELS": str(self.folder)}), mock.patch("urllib.request.urlopen", side_effect=AssertionError("cached models must not download")):
            self.assertEqual(_ensure_model(upstream, _Reporter(None, None), 0, 10), cached)
            cached.write_bytes(b"corrupt")
            with self.assertRaisesRegex(RuntimeError, "校验失败"):
                _ensure_model(upstream, _Reporter(None, None), 0, 10)

    def test_cancel_after_save_removes_unpublished_output(self):
        from studio.engine import _save_result
        cancelled = False
        def saving(*args):
            nonlocal cancelled
            _save_result(*args)
            cancelled = True
        with mock.patch("studio.engine._save_result", side_effect=saving), self.assertRaises(ProcessingCancelled):
            process_image(self.source, self.output, {"strength": 0}, cancelled=lambda: cancelled)
        self.assertFalse(self.output.exists())

    def test_import_is_lightweight(self):
        code = "import sys; import studio.engine; print('torch' in sys.modules)"
        result = subprocess.check_output([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], text=True)
        self.assertEqual(result.strip(), "False")


if __name__ == "__main__":
    unittest.main()
