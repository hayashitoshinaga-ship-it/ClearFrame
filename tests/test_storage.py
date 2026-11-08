"""Storage behavior with synthetic bytes; no model, GUI, or live server required."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from studio.presets import validate_settings
from studio.storage import OutputPreferences, export_snapshots, open_directory, publish_result, result_stem


class StorageTests(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[1] / "work"
        workspace.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="storage-test-", dir=workspace)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(workspace.resolve()))
        self.runtime = self.root / ".runtime"
        self.output = self.root / "output"
        self.output.mkdir()
        self.source = self.root / "source.png"
        self.source.write_bytes(b"unchanged PNG sample")

    def tearDown(self):
        self.temporary.cleanup()

    def receipt(self):
        return {"metadata": {"output_path": str(self.source)}, "source_name": "原图.png"}

    def test_preferences_priority_and_default_marker_after_move(self):
        environment = self.root / "environment"
        chosen = self.root / "chosen"
        with mock.patch.dict(os.environ, {"CLEARFRAME_OUTPUT": str(environment)}):
            store = OutputPreferences(self.root, self.runtime)
            self.assertEqual(store.current, environment)
            store.set(str(chosen))
            self.assertEqual(OutputPreferences(self.root, self.runtime).current, chosen)
            explicit = self.root / "explicit"
            self.assertEqual(OutputPreferences(self.root, self.runtime, explicit=explicit).current, explicit)
            store.set("")
            relocated = self.root / "moved-application"
            self.assertEqual(OutputPreferences(relocated, self.runtime).current, relocated / "results")
            self.assertTrue(json.loads(store.file.read_text(encoding="utf-8"))["use_default"])

    def test_bad_or_unwritable_directory_does_not_change_preference(self):
        store = OutputPreferences(self.root, self.runtime, explicit=self.output)
        file_path = self.root / "not-a-folder"
        file_path.write_text("keep", encoding="utf-8")
        for value in (None, 123, "relative-folder", str(file_path)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                store.set(value)
            self.assertEqual(store.current, self.output)
        with mock.patch("studio.storage.Path.open", side_effect=PermissionError("denied")), self.assertRaises(ValueError):
            store.set(str(self.root / "denied"))
        self.assertEqual(store.current, self.output)
        self.assertEqual(file_path.read_text(encoding="utf-8"), "keep")

    def test_persist_replace_failure_does_not_update_memory_or_existing_setting(self):
        store = OutputPreferences(self.root, self.runtime, explicit=self.output)
        store.set(str(self.output))
        previous = store.file.read_bytes()
        with mock.patch("studio.storage.Path.replace", side_effect=OSError("disk error")), self.assertRaises(OSError):
            store.set(str(self.root / "another"))
        self.assertEqual(store.current, self.output)
        self.assertEqual(store.file.read_bytes(), previous)
        self.assertEqual(list(self.runtime.glob("*.tmp")), [])

    def test_quoted_absolute_path_is_accepted_without_shell_expansion(self):
        store = OutputPreferences(self.root, self.runtime, explicit=self.output)
        chosen = self.root / "有 空格 $VARIABLE"
        self.assertEqual(store.set('  "' + str(chosen) + '"  '), chosen)
        self.assertTrue(chosen.is_dir())

    def test_unavailable_saved_directory_falls_back_without_losing_preference(self):
        from studio.storage import writable_directory
        store = OutputPreferences(self.root, self.runtime, explicit=self.output)
        chosen = self.root / "external-drive"
        store.set(str(chosen))
        saved = store.file.read_bytes()
        def check(path):
            if Path(path) == chosen:
                raise ValueError("drive disconnected")
            return writable_directory(path)
        with mock.patch("studio.storage.writable_directory", side_effect=check):
            restored = OutputPreferences(self.root, self.runtime)
            self.assertEqual(restored.current, self.root / "results")
            self.assertIn("原设置已保留", restored.warning)
            self.assertEqual(store.file.read_bytes(), saved)
            with self.assertRaises(ValueError):
                OutputPreferences(self.root, self.runtime, explicit=chosen)
        reconnected = OutputPreferences(self.root, self.runtime)
        self.assertEqual(reconnected.current, chosen)
        self.assertIsNone(reconnected.warning)
        restored.set(str(self.output))
        self.assertIsNone(restored.warning)

    def test_readable_names_include_choices_and_zero_identity(self):
        settings = validate_settings({"mode": "photo_portrait", "strength": 30})
        self.assertEqual(result_stem("同名.png", settings), "同名_实拍人像_默认_强度30")
        self.assertEqual(result_stem("原图.png", {**settings, "strength": 0}), "原图_原图_强度0")
        custom = validate_settings({"method": "custom", "stages": ["sharpen", "l0"], "strength": 42.5})
        self.assertIn("自定义-L0平滑+锐化_强度42.5", result_stem("test.png", custom))
        self.assertNotIn("/", result_stem("../../bad/name.png", settings))

    def test_companion_collisions_and_parallel_publish_never_overwrite(self):
        (self.output / "结果.png").write_bytes(b"existing PNG")
        (self.output / "结果_02.json").write_bytes(b"existing JSON")
        def publish(_):
            return publish_result(self.source, self.output, "结果", self.receipt())[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            paths = list(pool.map(publish, range(6)))
        self.assertEqual(len(set(paths)), 6)
        self.assertEqual((self.output / "结果.png").read_bytes(), b"existing PNG")
        self.assertEqual((self.output / "结果_02.json").read_bytes(), b"existing JSON")
        self.assertIn(self.output / "结果_03.png", paths)
        for path in paths:
            record = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(record["result_name"], path.name)
            self.assertEqual(record["metadata"]["output_path"], str(path))

    def test_publish_failure_removes_only_its_own_pair(self):
        (self.output / "结果.json").write_bytes(b"preexisting JSON")
        with mock.patch("studio.storage.json.dump", side_effect=OSError("receipt failed")), self.assertRaises(OSError):
            publish_result(self.source, self.output, "结果", self.receipt())
        self.assertEqual([path.name for path in self.output.iterdir()], ["结果.json"])
        self.assertEqual((self.output / "结果.json").read_bytes(), b"preexisting JSON")
        self.assertEqual(self.source.read_bytes(), b"unchanged PNG sample")

    def test_companion_created_during_publish_is_kept_and_suffix_retried(self):
        original_open = Path.open
        collision = self.output / "结果.json"
        inserted = False
        def racing_open(path, mode="r", *args, **kwargs):
            nonlocal inserted
            if path == collision and mode == "x" and not inserted:
                inserted = True
                with original_open(path, "wb") as stream:
                    stream.write(b"another writer's JSON")
            return original_open(path, mode, *args, **kwargs)
        with mock.patch("studio.storage.Path.open", new=racing_open):
            path, _ = publish_result(self.source, self.output, "结果", self.receipt())
        self.assertEqual(path.name, "结果_02.png")
        self.assertEqual(collision.read_bytes(), b"another writer's JSON")
        self.assertFalse((self.output / "结果.png").exists())

    def test_probe_collision_does_not_remove_an_existing_file(self):
        from studio.storage import writable_directory
        collision = self.output / ".clearframe-write-fixed.tmp"
        collision.write_bytes(b"belongs to another writer")
        with mock.patch("studio.storage.uuid.uuid4") as identity:
            identity.return_value.hex = "fixed"
            with self.assertRaises(ValueError):
                writable_directory(self.output)
        self.assertEqual(collision.read_bytes(), b"belongs to another writer")

    def snapshots(self):
        first, _ = publish_result(self.source, self.output, "结果", self.receipt())
        return [{"png": first, "json": first.with_suffix(".json")}]

    def test_export_same_second_collision_and_duplicate_names_preserve_snapshots(self):
        snapshots = self.snapshots()
        with mock.patch("studio.storage.datetime") as clock:
            clock.now.return_value.strftime.return_value = "20261108_120000"
            first = export_snapshots(self.output, snapshots + snapshots)
            second = export_snapshots(self.output, snapshots)
        self.assertEqual(first.name, "队列导出_20261108_120000")
        self.assertEqual(second.name, "队列导出_20261108_120000_02")
        self.assertEqual(len(list(first.glob("*.png"))), 2)
        self.assertEqual(len(list(first.glob("*.json"))), 2)
        self.assertEqual((first / "结果_02.json").read_bytes(), snapshots[0]["json"].read_bytes())
        self.assertEqual((second / "结果.png").read_bytes(), snapshots[0]["png"].read_bytes())
        self.assertEqual(list(self.output.glob(".clearframe-*")), [])

    def test_export_copy_failure_never_publishes_partial_directory(self):
        from studio.storage import _copy_exclusive
        snapshots = self.snapshots()
        existing = export_snapshots(self.output, snapshots)
        original = {path: path.read_bytes() for path in self.output.rglob("*") if path.is_file()}
        calls = 0
        def fail_second(source, destination, owned):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("second copy failed")
            return _copy_exclusive(source, destination, owned)
        with mock.patch("studio.storage._copy_exclusive", side_effect=fail_second), self.assertRaises(OSError):
            export_snapshots(self.output, snapshots)
        self.assertEqual({path: path.read_bytes() for path in self.output.rglob("*") if path.is_file()}, original)
        self.assertEqual([path for path in self.output.iterdir() if path.is_dir()], [existing])

    def test_state_failure_keeps_output_choice_and_does_not_register_export(self):
        from studio.server import StudioState
        from studio.storage import _copy_exclusive
        snapshot = self.snapshots()[0]
        state = StudioState(root=self.root, runtime=self.runtime, output=self.output, monitor=False)
        try:
            with mock.patch("studio.storage.Path.replace", side_effect=OSError("cannot persist")), self.assertRaises(OSError):
                state.set_output({"path": str(self.root / "new-output")})
            self.assertEqual(state.output, self.output)
            state.images["one"] = {"id": "one"}
            state.jobs["successful"] = {"image_id": "one", "status": "completed", "result_path": snapshot["png"], "finished": 1}
            def copy(source, destination, owned):
                if Path(source).suffix == ".json":
                    raise OSError("copy interrupted")
                return _copy_exclusive(source, destination, owned)
            with mock.patch("studio.storage._copy_exclusive", side_effect=copy), self.assertRaises(OSError):
                state.export_queue({"image_ids": ["one"]})
            self.assertEqual(state.exports, {})
            self.assertEqual(list(self.output.glob("队列导出_*")), [])
            self.assertEqual(list(self.output.glob(".clearframe-*")), [])
            self.assertTrue(snapshot["png"].is_file())
            self.assertTrue(snapshot["json"].is_file())
        finally:
            state.close()
            state.worker.join(timeout=3)
            self.assertFalse(state.worker.is_alive())

    @unittest.skipUnless(os.name == "nt", "Windows opener semantics")
    def test_windows_open_uses_path_argument_without_shell(self):
        with mock.patch("studio.storage.os.startfile") as opener:
            result = open_directory(self.output)
        opener.assert_called_once_with(str(self.output), "open")
        self.assertEqual(result["path"], str(self.output))


if __name__ == "__main__":
    unittest.main()
