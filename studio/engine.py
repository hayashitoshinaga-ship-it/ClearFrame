"""Manual image processing. CPU photo workflows do not import PyTorch.

Anime SR and L0 reuse the upstream denoise.py core (CXP-2024, MIT).
This adapter adds scenario policy, bounded downloads, verified caches, cancellation,
stage progress, conservative result mixing, and non-overwriting output writes.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import importlib
import io
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid

import numpy as np
from PIL import Image, ImageCms, ImageFilter, ImageOps, UnidentifiedImageError

from .presets import resolve_recipe, validate_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAX_PIXELS = 16_777_216
MAX_BYTES = 80 * 1024 * 1024
_ENGINE_LOCK = threading.RLock()
_MODEL_CACHE = OrderedDict()
_MODEL_CACHE_LIMIT = 2


class ProcessingCancelled(Exception):
    """The caller cancelled a job at a safe boundary."""


def _check(cancelled):
    if cancelled and cancelled():
        raise ProcessingCancelled("处理已取消。")


class _Reporter:
    def __init__(self, callback, cancelled):
        self.callback = callback or (lambda *_: None)
        self.cancelled = cancelled
        self.last = 0.0

    def emit(self, percent, stage, detail=None, device=None):
        _check(self.cancelled)
        self.last = max(self.last, min(100.0, float(percent)))
        if device is not None:
            detail = {"message": detail or "", "actual_device": device}
        self.callback(round(self.last, 2), stage, detail)


def read_image(source_path):
    """Return displayed RGB, original alpha and applicable ICC without torch."""
    path = Path(source_path)
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError("图片超过 80 MB，请先缩小文件。")
        with Image.open(path) as raw:
            if getattr(raw, "is_animated", False):
                raise ValueError("仅支持静态图片，请先导出需要处理的一帧。")
            if min(raw.size) < 16 or raw.width * raw.height > MAX_PIXELS:
                raise ValueError("图片最小为 16×16，最大约 1677 万像素。")
            raw.load()
            image = ImageOps.exif_transpose(raw)
            mode = image.mode
            icc = image.info.get("icc_profile") if mode in ("RGB", "RGBA", "RGBX", "P") else None
            if mode == "CMYK" and raw.info.get("icc_profile"):
                try:
                    image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(raw.info["icc_profile"])), ImageCms.createProfile("sRGB"), outputMode="RGB")
                    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
                except (OSError, ValueError, ImageCms.PyCMSError) as exc:
                    raise ValueError("图片的 CMYK 色彩配置无效，请先转换为 sRGB 后再导入。") from exc
            if mode in ("I;16", "I;16B", "I;16L", "I;16N", "I", "F"):
                array = np.asarray(image, np.float64)
                if not np.isfinite(array).all():
                    raise ValueError("图片含有无效的浮点像素。")
                if mode == "F" and array.min() >= 0 and array.max() <= 1:
                    array *= 255.0
                elif mode.startswith("I;16") or array.max() > 255:
                    array /= 257.0
                image = Image.fromarray(np.clip(np.rint(array), 0, 255).astype(np.uint8))
            if image.mode in ("RGBa", "La"):
                image = image.convert("RGBA")
            alpha = None
            if "A" in image.getbands() or "transparency" in image.info:
                alpha = image.convert("RGBA").getchannel("A").copy()
            return image.convert("RGB").copy(), alpha, icc
    except (UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("无法读取图片，请使用有效的 PNG、JPEG、WebP、BMP 或 TIFF 文件。") from exc
    except OSError as exc:
        raise ValueError("图片无法打开或已损坏，请重新导入可读取的图片。") from exc


def _upstream():
    try:
        return importlib.import_module("denoise")
    except ImportError as exc:
        raise RuntimeError("动漫重建和 L0 需要 PyTorch 依赖，请运行本程序的安装依赖入口。") from exc


def cuda_compatibility_error(torch_module):
    """Return a known build mismatch before launching any CUDA kernel.

    Call only after cuda.is_available(). Blackwell can theoretically JIT older
    PTX, but the old cu117 wheel hangs on this app's RTX 5090 probe. Require the
    native-support generation here rather than starting unbounded legacy JIT.
    PyTorch 2.7 introduced Blackwell/cu128 wheels:
    https://pytorch.org/blog/pytorch-2-7/
    """
    major, minor = torch_module.cuda.get_device_capability()
    cuda_version = getattr(torch_module.version, "cuda", None)
    match = re.match(r"^(\d+)\.(\d+)", str(cuda_version or ""))
    cuda_pair = tuple(map(int, match.groups())) if match else (0, 0)
    if major >= 10 and cuda_pair < (12, 8):
        return (f"当前显卡计算能力为 {major}.{minor}，但 PyTorch 使用 CUDA {cuda_version or '未知版本'}。"
                "为避免旧版内核编译长时间等待，本程序需要 CUDA 12.8 或更新的 PyTorch 构建。"
                "请运行安装依赖入口并从独立环境启动，或选择 CPU。")
    return None


def _select_device(requested, needed):
    if not needed:
        return "cpu", None
    dn = _upstream()
    if requested == "cpu":
        return "cpu", dn
    if not dn.torch.cuda.is_available():
        if requested == "cuda":
            raise ValueError("GPU 不可用。请安装匹配显卡的 PyTorch/CUDA，或选择 CPU。")
        return "cpu", dn
    try:
        incompatibility = cuda_compatibility_error(dn.torch)
        if not incompatibility:
            # Detection alone does not prove the installed wheel can run kernels.
            probe = dn.torch.ones(1, device="cuda")
            (probe + 1).cpu()
            del probe
    except Exception as exc:
        if requested == "cuda":
            raise RuntimeError("GPU 初始化失败，可能是 PyTorch/CUDA 与显卡不匹配；请选择 CPU 或更新独立环境。") from exc
        return "cpu", dn
    if incompatibility:
        if requested == "cuda":
            raise ValueError(incompatibility)
        return "cpu", dn
    return "cuda", dn


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_model(dn, report, begin, end):
    folder = Path(os.environ.get("CLEARFRAME_MODELS", PROJECT_ROOT / ".models")).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)
    filename, url = dn.MODELS["esrgan"]
    path = folder / filename
    expected = dn.SHA256[filename]
    report.emit(begin, "校验动漫模型", "首次使用会下载模型；已有模型也会核对 SHA-256。")
    if path.exists():
        if _sha256(path) != expected:
            raise RuntimeError(f"模型缓存校验失败，请删除此损坏文件后重试：{path}")
        report.emit(end, "模型校验完成")
        return path
    temporary = folder / (filename + "." + uuid.uuid4().hex + ".download")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "ClearFrame-local/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response, temporary.open("xb") as output:
            total = int(response.headers.get("Content-Length", 0))
            downloaded = 0
            digest = hashlib.sha256()
            while True:
                _check(report.cancelled)
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > 128 * 1024 * 1024:
                    raise RuntimeError("模型下载大小异常，已停止。")
                output.write(chunk)
                digest.update(chunk)
                fraction = min(downloaded / total, 1.0) if total else 0.0
                report.emit(begin + (end - begin) * fraction, "下载动漫模型", f"已下载 {downloaded / 1048576:.1f} MB" + (f" / {total / 1048576:.1f} MB" if total else ""))
            if digest.hexdigest() != expected:
                raise RuntimeError("下载的模型 SHA-256 校验不符，临时文件已清理，请重试。")
        _check(report.cancelled)
        temporary.replace(path)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _check(report.cancelled)
        raise RuntimeError("动漫模型下载失败，请检查到 GitHub 的网络连接后重试。") from exc
    finally:
        temporary.unlink(missing_ok=True)
    report.emit(end, "模型下载完成")
    return path


def _get_model(dn, device, report, begin, end):
    path = _ensure_model(dn, report, begin, begin + (end - begin) * 0.8)
    key = (str(path), dn.SHA256[path.name], device)
    model = _MODEL_CACHE.get(key)
    if model is None:
        report.emit(begin + (end - begin) * 0.8, "加载动漫模型", device=device)
        try:
            import spandrel
            model = spandrel.ModelLoader().load_from_file(str(path)).model.to(device).eval()
        except ImportError as exc:
            raise RuntimeError("缺少 spandrel 模型加载依赖，请运行安装依赖入口。") from exc
        _MODEL_CACHE[key] = model
        while len(_MODEL_CACHE) > _MODEL_CACHE_LIMIT:
            _MODEL_CACHE.popitem(last=False)
    else:
        _MODEL_CACHE.move_to_end(key)
    report.emit(end, "动漫模型已就绪", device=device)
    return model


@contextmanager
def _band_reporting(dn, report, begin, end, device):
    """Report actual upstream band boundaries without pretending to estimate remaining time."""
    original = dn._run_bands

    def run_bands(fn, n, budget, per_row, halo, stop):
        # Upstream performs a trunk phase and a tail phase. When the trunk fits
        # in memory it has no band callback, but reaching the tail proves it is
        # finished. These weights describe completed work units, not elapsed time.
        is_head = halo == dn.HEAD_HALO
        phase_begin, phase_end = (0.0, 0.65) if is_head else (0.65, 1.0)
        def observed(r0, r1, a, b):
            fn(r0, r1, a, b)
            fraction = phase_begin + (phase_end - phase_begin) * r1 / n
            report.emit(begin + (end - begin) * fraction, "动漫超分重建", f"{'主干' if is_head else '输出'}阶段已完成 {r1}/{n} 行；进度按处理步骤计算。", device=device)
        return original(observed, n, budget, per_row, halo, stop)

    dn._run_bands = run_bands
    try:
        yield
    finally:
        dn._run_bands = original


def _anime_sr(image, downscale, device, dn, report, begin, end):
    model = _get_model(dn, device, report, begin, begin + (end - begin) * 0.18)
    work_start = begin + (end - begin) * 0.2
    report.emit(work_start, "动漫超分重建", f"预缩至 {downscale * 100:.0f}%，处理后恢复原尺寸。", device=device)
    small = image.resize((max(16, round(image.width * downscale)), max(16, round(image.height * downscale))), Image.Resampling.BOX)
    try:
        with _band_reporting(dn, report, work_start, end, device):
            result = dn.upscale(model, small, device, stop=report.cancelled)
    except dn.Cancelled as exc:
        raise ProcessingCancelled("处理已取消。") from exc
    result = result.resize(image.size, Image.Resampling.LANCZOS)
    report.emit(end, "动漫重建完成", device=device)
    return result


def _l0(image, lam, device, dn, report, begin, end):
    if lam <= 0:
        return image.copy()
    iterations = math.ceil(math.log(100000 / (2 * lam), 2))
    completed = 0

    def stop():
        nonlocal completed
        _check(report.cancelled)
        report.emit(begin + (end - begin) * min(completed / iterations, 1), "L0 色块平滑", f"已完成 {min(completed, iterations)}/{iterations} 轮优化。", device=device)
        completed += 1
        return False

    try:
        if device == "cuda":
            values = dn._l0_torch(dn.to_arr(image), lam, 2.0, 100000, device, stop)
        else:
            values = dn.l0_smooth(dn.to_arr(image), lam, device="cpu", stop=stop)
    except dn.Cancelled as exc:
        raise ProcessingCancelled("处理已取消。") from exc
    report.emit(end, "L0 平滑完成", device=device)
    return dn.to_img(values)


def _photo_denoise(image, params, report, begin, end):
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("照片降噪需要 OpenCV，请运行本程序的安装依赖入口。") from exc
    pixels = np.asarray(image, dtype=np.uint8)
    bgr = cv2.cvtColor(pixels, cv2.COLOR_RGB2BGR)
    result = np.empty_like(bgr)
    # Search radius 10 + template radius 3. A 16-pixel halo isolates retained rows
    # from tile boundaries; work progresses at completed row bands, not a timer.
    band, halo = 128, 16
    for y in range(0, image.height, band):
        _check(report.cancelled)
        stop_y = min(y + band, image.height)
        start = max(0, y - halo)
        stop = min(image.height, stop_y + halo)
        cleaned = cv2.fastNlMeansDenoisingColored(bgr[start:stop], None, params["nlm_h"], params["nlm_color_h"], 7, 21)
        result[y:stop_y] = cleaned[y - start:stop_y - start]
        report.emit(begin + (end - begin) * stop_y / image.height, "照片非局部均值降噪", f"已完成 {stop_y}/{image.height} 行。", device="cpu")
    return Image.fromarray(cv2.cvtColor(result, cv2.COLOR_BGR2RGB))


def _save_result(rgb, output_path, alpha, icc, cancelled):
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("目标结果已存在，请使用新的文件名，原结果不会覆盖。")
    image = rgb.copy()
    if alpha is not None:
        image.putalpha(alpha)
    temporary = output.with_name("." + output.name + "." + uuid.uuid4().hex + ".tmp")
    created = False
    try:
        image.save(temporary, "PNG", **({"icc_profile": icc} if icc else {}))
        _check(cancelled)
        with output.open("xb") as target, temporary.open("rb") as source:
            created = True
            shutil.copyfileobj(source, target)
        _check(cancelled)
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


def process_image(source_path, output_path, settings, progress=None, cancelled=None):
    started = time.perf_counter()
    recipe = resolve_recipe(settings)
    source, output = Path(source_path).resolve(), Path(output_path).resolve()
    if source == output:
        raise ValueError("输出位置不能覆盖原图。")
    if output.exists():
        raise ValueError("结果文件已存在，请选择新的文件名。")
    report = _Reporter(progress, cancelled)
    report.emit(0, "读取图片")
    original, alpha, icc = read_image(source)
    report.emit(3, "图片已读取", f"{original.width} × {original.height}，保留原尺寸和透明通道。")
    stages = recipe["stages"] if recipe["strength"] > 0 else []
    needs_acceleration = any(s in ("anime_sr", "l0") for s in stages)
    report.emit(4, "准备计算设备", "首次加载计算依赖可能需要几秒。")
    selected, dn = _select_device(recipe["device"], needs_acceleration)
    report.emit(6, "计算设备已就绪", device=selected)
    current = original.copy()
    stage_devices = {}
    warnings = []
    if stages and not needs_acceleration and recipe["device"] == "cuda":
        warnings.append("当前阶段使用 CPU；GPU 选项仅影响动漫重建和 L0。")
    weights = {"photo_denoise": 3, "anime_sr": 5, "l0": 3, "sharpen": 1}
    total = sum(weights[s] for s in stages) or 1
    position = 0
    with _ENGINE_LOCK:
        for stage in stages:
            _check(cancelled)
            begin = 6 + 86 * position / total
            position += weights[stage]
            end = 6 + 86 * position / total
            actual = selected if stage in ("anime_sr", "l0") else "cpu"
            stage_devices[stage] = actual
            report.emit(begin, "开始处理", stage, device=actual)
            try:
                if stage == "photo_denoise":
                    current = _photo_denoise(current, recipe["parameters"], report, begin, end)
                elif stage == "anime_sr":
                    current = _anime_sr(current, recipe["parameters"]["downscale"], actual, dn, report, begin, end)
                elif stage == "l0":
                    current = _l0(current, recipe["parameters"]["l0_lambda"], actual, dn, report, begin, end)
                else:
                    current = current.filter(ImageFilter.UnsharpMask(radius=0.8, percent=recipe["parameters"]["sharpen_percent"], threshold=3))
                    report.emit(end, "轻度锐化完成", device="cpu")
            except ProcessingCancelled:
                raise
            except Exception as exc:
                if dn is not None and isinstance(exc, dn.torch.cuda.OutOfMemoryError):
                    dn.torch.cuda.empty_cache()
                    raise RuntimeError("GPU 显存不足。请关闭占用显存的程序、缩小图片，或选择 CPU 重试。") from exc
                raise
        _check(cancelled)
        report.emit(94, "整理结果", "按所选强度与原图混合，恢复透明通道与色彩配置。", device="cpu")
        if stages:
            current = Image.blend(original, current, recipe["parameters"]["result_mix"])
        _save_result(current, output, alpha, icc, cancelled)
    devices = set(stage_devices.values())
    actual_device = "cuda+cpu" if devices == {"cuda", "cpu"} else (next(iter(devices)) if devices else "cpu")
    # A cancellation arriving after the save boundary must not leave a hidden
    # completed file behind a cancelled job. Include the final report in cleanup.
    try:
        report.emit(100, "处理完成", "结果已保存，原图保持不变。", device=actual_device)
    except ProcessingCancelled:
        output.unlink(missing_ok=True)
        raise
    return {"actual_device": actual_device, "stage_devices": stage_devices, "applied_stages": stages,
            "duration_seconds": round(time.perf_counter() - started, 3), "parameters": recipe["parameters"],
            "width": original.width, "height": original.height, "has_alpha": alpha is not None,
            "settings": recipe, "warnings": warnings, "output_path": str(output)}
