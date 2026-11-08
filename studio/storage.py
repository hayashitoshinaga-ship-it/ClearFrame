"""Local output preferences and collision-safe result/export publication."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid

from .presets import MODES

_PUBLISH_LOCK = threading.RLock()
_MODE_LABELS = {mode["id"]: mode["label"] for mode in MODES}
_METHOD_LABELS = {"default": "默认", "gentle": "轻柔", "texture_clean": "强化", "custom": "自定义"}
_STAGE_LABELS = {"photo_denoise": "照片降噪", "anime_sr": "动漫重建", "l0": "L0平滑", "sharpen": "锐化"}


def safe_stem(name):
    name = Path(str(name).replace("\\", "/")).stem
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:80]
    if not stem or re.fullmatch(r"(?i)(con|prn|aux|nul|com\d|lpt\d)", stem):
        return "图片"
    return stem


def result_stem(name, settings):
    stem = safe_stem(name)
    strength = format(float(settings["strength"]), ".12g")
    if float(settings["strength"]) == 0:
        return f"{stem}_原图_强度0"
    method = _METHOD_LABELS[settings["method"]]
    if settings["method"] == "custom":
        method += "-" + "+".join(_STAGE_LABELS[stage] for stage in settings["stages"])
    return f"{stem}_{_MODE_LABELS[settings['mode']]}_{method}_强度{strength}"


def writable_directory(path):
    """Validate by creating an exclusive probe, rather than trusting os.access."""
    directory = Path(path).expanduser().resolve()
    owned = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise ValueError("输出位置必须是文件夹，不能是普通文件。")
        probe = directory / (".clearframe-write-" + uuid.uuid4().hex + ".tmp")
        with probe.open("xb") as stream:
            _remember_file(stream, probe, owned)
            stream.write(b"writable")
    except OSError as exc:
        raise ValueError("无法写入这个输出文件夹，请检查路径、磁盘空间和访问权限。") from exc
    finally:
        _cleanup_owned(owned)
    return directory


class OutputPreferences:
    def __init__(self, root, runtime, explicit=None):
        self.default = (Path(root) / "results").resolve()
        self.file = Path(runtime) / "output-settings.json"
        self._lock = threading.RLock()
        self.warning = None
        persisted = None
        restore_default = False
        if explicit is None:
            try:
                stored = json.loads(self.file.read_text(encoding="utf-8"))
                restore_default = stored.get("use_default") is True
                candidate = stored.get("output_dir")
                if isinstance(candidate, str) and Path(candidate).is_absolute():
                    persisted = candidate
            except (OSError, ValueError, AttributeError):
                pass
        selected = explicit if explicit is not None else self.default if restore_default else persisted or os.environ.get("CLEARFRAME_OUTPUT") or self.default
        try:
            self.current = writable_directory(selected)
        except (ValueError, OSError):
            if explicit is not None or not persisted or restore_default:
                raise
            self.current = writable_directory(self.default)
            self.warning = "已保存的输出文件夹暂时无法写入，当前改用默认文件夹。原设置已保留，下次启动会重试；也可重新选择输出位置。"

    def set(self, value):
        if not isinstance(value, str):
            raise ValueError("输出路径必须是绝对路径文本；留空可恢复默认文件夹。")
        value = value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1].strip()
        if value and ("\x00" in value or not Path(value).is_absolute()):
            raise ValueError("请输入完整的绝对路径，例如 H:\\图片结果；留空可恢复默认文件夹。")
        with self._lock:
            selected = writable_directory(value or self.default)
            self.file.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.file.with_name(self.file.name + "." + uuid.uuid4().hex + ".tmp")
            owned = []
            try:
                with temporary.open("x", encoding="utf-8") as stream:
                    _remember_file(stream, temporary, owned)
                    json.dump({"use_default": not bool(value), "output_dir": str(selected) if value else None}, stream, ensure_ascii=False, indent=2)
                temporary.replace(self.file)
            finally:
                _cleanup_owned(owned)
            self.current = selected
            self.warning = None
            return selected


def open_directory(path):
    """Open a known directory, without interpolating paths into shell commands."""
    directory = Path(path).resolve()
    if not directory.is_dir():
        raise ValueError("文件夹已不存在，请重新选择输出位置或重新导出。")
    try:
        if os.name == "nt":
            os.startfile(str(directory), "open")
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(directory)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RuntimeError("无法打开文件夹，请复制路径后在文件管理器中打开。") from exc
    return {"ok": True, "path": str(directory)}


def _numbered(stem, number):
    return stem if number == 1 else f"{stem}_{number:02d}"


def _remember_file(stream, path, owned):
    info = os.fstat(stream.fileno())
    owned.append((path, (info.st_dev, info.st_ino)))


def _cleanup_owned(owned):
    for path, identity in reversed(owned):
        try:
            info = path.stat()
            if (info.st_dev, info.st_ino) == identity:
                path.unlink()
        except FileNotFoundError:
            pass


def _copy_exclusive(source, destination, owned):
    with Path(source).open("rb") as source_file, Path(destination).open("xb") as destination_file:
        _remember_file(destination_file, Path(destination), owned)
        shutil.copyfileobj(source_file, destination_file)


def publish_result(source_png, folder, stem, receipt, cancelled=None):
    """Publish a PNG/JSON pair without overwriting either existing companion."""
    directory = Path(folder)
    with _PUBLISH_LOCK:
        number = 1
        while True:
            png = directory / (_numbered(stem, number) + ".png")
            sidecar = png.with_suffix(".json")
            if png.exists() or sidecar.exists():
                number += 1
                continue
            owned = []
            try:
                if cancelled and cancelled():
                    from .engine import ProcessingCancelled
                    raise ProcessingCancelled("处理已取消。")
                _copy_exclusive(source_png, png, owned)
                metadata = dict(receipt.get("metadata") or {})
                if "output_path" in metadata:
                    metadata["output_path"] = str(png)
                content = {**receipt, "result_name": png.name, "metadata": metadata}
                with sidecar.open("x", encoding="utf-8") as stream:
                    _remember_file(stream, sidecar, owned)
                    json.dump(content, stream, ensure_ascii=False, indent=2, default=str)
                if cancelled and cancelled():
                    from .engine import ProcessingCancelled
                    raise ProcessingCancelled("处理已取消。")
                return png, content
            except FileExistsError:
                _cleanup_owned(owned)
                number += 1
            except BaseException:
                _cleanup_owned(owned)
                raise


@contextmanager
def result_workspace(folder):
    directory = writable_directory(folder)
    with tempfile.TemporaryDirectory(prefix=".clearframe-job-", dir=directory) as temporary:
        if not Path(temporary).resolve().is_relative_to(directory):
            raise RuntimeError("处理临时目录不在指定输出位置内。")
        yield Path(temporary) / "result.png"


def _copy_snapshot_pair(source_png, source_json, directory):
    number = 1
    stem = Path(source_png).stem
    while True:
        png = directory / (_numbered(stem, number) + ".png")
        sidecar = png.with_suffix(".json")
        if png.exists() or sidecar.exists():
            number += 1
            continue
        owned = []
        try:
            _copy_exclusive(source_png, png, owned)
            _copy_exclusive(source_json, sidecar, owned)
            return owned
        except FileExistsError:
            _cleanup_owned(owned)
            number += 1
        except BaseException:
            _cleanup_owned(owned)
            raise


def export_snapshots(folder, snapshots):
    """Copy immutable successful results, then publish a distinct export directory."""
    directory = writable_directory(folder)
    temporary = Path(tempfile.mkdtemp(prefix=".clearframe-export-", dir=directory))
    owned = []
    try:
        for snapshot in snapshots:
            owned.extend(_copy_snapshot_pair(snapshot["png"], snapshot["json"], temporary))
        stem = "队列导出_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        with _PUBLISH_LOCK:
            number = 1
            while True:
                destination = directory / _numbered(stem, number)
                if destination.exists():
                    number += 1
                    continue
                try:
                    if os.name == "nt":
                        # Windows rename is atomic and refuses an existing target.
                        temporary.rename(destination)
                    else:
                        # POSIX rename can replace an empty directory. Reserve via
                        # mkdir and exclusively copy instead, keeping no-overwrite.
                        destination.mkdir()
                        published = []
                        try:
                            for source in temporary.iterdir():
                                _copy_exclusive(source, destination / source.name, published)
                        except BaseException:
                            _cleanup_owned(published)
                            destination.rmdir()
                            raise
                    return destination
                except FileExistsError:
                    number += 1
    finally:
        # On Windows the source paths disappear after the atomic directory rename.
        _cleanup_owned(owned)
        if temporary.exists():
            temporary.rmdir()
