"""Loopback-only, asynchronous image-processing server for ClearFrame."""
import argparse
import copy
import io
import json
import mimetypes
import os
import queue
import re
import secrets
import threading
import time
import traceback
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from . import __version__
from .hardware import HardwareMonitor
from .storage import OutputPreferences, export_snapshots, open_directory, publish_result, result_stem, result_workspace, safe_stem

ROOT = Path(__file__).resolve().parent.parent
MAX_BYTES = 80 * 1024 * 1024
MAX_PIXELS = 16_777_216
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def normalise_image(data, destination):
    try:
        with Image.open(io.BytesIO(data)) as image:
            if getattr(image, "is_animated", False):
                raise ValueError("请导入静态图片；动图需先导出单帧。")
            if image.width * image.height > MAX_PIXELS or min(image.size) < 16:
                raise ValueError("图片需至少 16×16，且不超过 1677 万像素。")
            image.load()
            image = ImageOps.exif_transpose(image)
            profile = image.info.get("icc_profile")
            if image.mode == "CMYK":
                if profile:
                    try:
                        image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                                                         ImageCms.createProfile("sRGB"), outputMode="RGB")
                    except (OSError, ValueError, ImageCms.PyCMSError) as exc:
                        raise ValueError("图片的色彩配置无法读取，请先转换为 sRGB 图片后再导入。") from exc
                else:
                    image = image.convert("RGB")
                profile = None
            elif image.mode in ("I;16", "I;16B", "I;16L", "I", "F"):
                import numpy as np
                values = np.asarray(image, dtype=np.float64)
                if not np.isfinite(values).all():
                    raise ValueError("图片含有无效的浮点像素。")
                if image.mode == "F" and values.min() >= 0 and values.max() <= 1:
                    values *= 255
                scale = 257 if image.mode.startswith("I;16") or values.max() > 255 else 1
                image = Image.fromarray(np.clip(values / scale + .5, 0, 255).astype("uint8"), "L")
                profile = None
            elif image.mode not in ("RGB", "RGBA", "P"):
                profile = None
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            image = image.convert("RGBA" if has_alpha else "RGB")
    except UnidentifiedImageError as exc:
        raise ValueError("无法识别这个文件，可能不是图片、格式不受支持或文件已损坏。请导入有效的 PNG、JPEG、WebP、BMP 或 TIFF 图片。") from exc
    except Image.DecompressionBombError as exc:
        raise ValueError("图片像素过多，请缩小至不超过 1677 万像素后再导入。") from exc
    except ImageCms.PyCMSError as exc:
        raise ValueError("图片的色彩配置无法读取，请先转换为 sRGB 图片后再导入。") from exc
    except (OSError, SyntaxError, EOFError) as exc:
        raise ValueError("图片数据已损坏或不完整，请重新导出或下载完整图片后再导入。") from exc
    # Keep output I/O failures separate from invalid-image errors (HTTP 500 vs 400).
    kwargs = {"icc_profile": profile} if profile else {}
    image.save(destination, "PNG", **kwargs)
    return image.width, image.height, has_alpha


class StudioState:
    def __init__(self, root=ROOT, runtime=None, output=None, processor=None, monitor=True):
        self.root = Path(root).resolve()
        self.runtime = Path(runtime or self.root / ".runtime").resolve()
        self.imports = self.runtime / "imports"
        self.imports.mkdir(parents=True, exist_ok=True)
        self.output_preferences = OutputPreferences(self.root, self.runtime, explicit=output)
        self.output = self.output_preferences.current
        self.output_default = self.output_preferences.default
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.images, self.jobs, self.exports = {}, {}, {}
        self.work = queue.Queue()
        self.stopping = threading.Event()
        self.active_job_id = None
        self.processor = processor
        self.hardware = HardwareMonitor() if monitor else None
        # Finish the current cancellable band before exiting, so partial files are cleaned.
        self.worker = threading.Thread(target=self._worker, name="clearframe-worker", daemon=False)
        self.worker.start()

    def import_image(self, data, name):
        identity = uuid.uuid4().hex
        path = self.imports / (identity + ".png")
        try:
            width, height, alpha = normalise_image(data, path)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        image = dict(id=identity, name=Path(name.replace("\\", "/")).name[:160] or "image.png",
                     width=width, height=height, has_alpha=alpha,
                     source_url=f"/api/images/{identity}/source", path=path)
        with self.lock:
            self.images[identity] = image
        return {k: v for k, v in image.items() if k != "path"}

    def set_output(self, payload):
        if not isinstance(payload, dict) or "path" not in payload:
            raise ValueError("请提供输出文件夹的绝对路径；留空可恢复默认文件夹。")
        with self.lock:
            selected = self.output_preferences.set(payload["path"])
            self.output = selected
            return {"output_dir": str(selected)}

    def open_output(self):
        with self.lock:
            path = self.output
        return open_directory(path)

    def export_queue(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("image_ids"), list):
            raise ValueError("请提供需要导出的队列图片列表。")
        requested = payload["image_ids"]
        if not requested or any(not isinstance(identity, str) for identity in requested):
            raise ValueError("导出队列为空或包含无效图片，请先添加并处理图片。")
        # Duplicates are harmless: preserve first occurrence and queue order.
        image_ids = list(dict.fromkeys(requested))
        with self.lock:
            if any(identity not in self.images for identity in image_ids):
                raise ValueError("导出列表中有不存在的图片，请刷新队列后重试。")
            directory = self.output
            snapshots, skipped = [], []
            for identity in image_ids:
                completed = [job for job in self.jobs.values() if job["image_id"] == identity and job["status"] == "completed"]
                latest = max(completed, key=lambda job: (job["finished"] or job["started"] or job["created"]), default=None)
                png = latest["result_path"] if latest else None
                if not png or not png.is_file() or not png.with_suffix(".json").is_file():
                    skipped.append(identity)
                    continue
                snapshots.append({"image_id": identity, "png": png, "json": png.with_suffix(".json")})
        if not snapshots:
            raise ValueError("队列中没有可导出的成功结果，请先完成处理；已删除的结果需要重新生成。")
        path = export_snapshots(directory, snapshots)
        export_id = uuid.uuid4().hex
        result = {"export_id": export_id, "path": str(path), "count": len(snapshots), "skipped": skipped}
        with self.lock:
            self.exports[export_id] = dict(result)
        return result

    def open_export(self, identity):
        with self.lock:
            record = self.exports.get(identity)
            if record is None:
                raise KeyError(identity)
            path = record["path"]
        return open_directory(path)

    def create_job(self, payload):
        from .engine import validate_settings
        if not isinstance(payload, dict):
            raise ValueError("任务设置必须是对象。")
        image_id = payload.get("image_id")
        if not isinstance(image_id, str):
            raise ValueError("图片不存在，请重新导入。")
        with self.lock:
            if image_id not in self.images:
                raise ValueError("图片不存在，请重新导入。")
            if self.stopping.is_set():
                raise ValueError("程序正在退出。")
        settings = validate_settings({k: v for k, v in payload.items() if k != "image_id"})
        identity = uuid.uuid4().hex
        with self.lock:
            job = dict(id=identity, image_id=image_id, status="queued", progress=0, stage="等待处理",
                       detail=None, actual_device=None, error=None, settings=settings, created=time.time(),
                       started=None, finished=None, result_url=None, download_url=None, result_name=None,
                       output_dir=str(self.output),
                       cancel=threading.Event(), result_path=None, metadata=None)
            self.jobs[identity] = job
            self.work.put(identity)
        return dict(id=identity, image_id=image_id, status="queued")

    def public_job(self, identity):
        with self.lock:
            job = self.jobs.get(identity)
            if not job:
                raise KeyError(identity)
            public = {k: copy.deepcopy(v) for k, v in job.items()
                      if k not in {"cancel", "result_path", "created", "started", "finished"}}
            public["elapsed_seconds"] = round((job["finished"] or time.time()) - job["started"], 2) if job["started"] else 0
            return public

    def cancel_job(self, identity):
        with self.lock:
            job = self.jobs.get(identity)
            if not job:
                raise KeyError(identity)
            if job["status"] in ("queued", "running"):
                job["cancel"].set()
                if job["status"] == "queued":
                    job.update(status="cancelled", stage="已取消", finished=time.time())
            return {"ok": True}

    def _worker(self):
        while not self.stopping.is_set():
            try:
                identity = self.work.get(timeout=.25)
            except queue.Empty:
                continue
            with self.lock:
                job = self.jobs[identity]
                if job["cancel"].is_set():
                    self.work.task_done()
                    continue
                image = self.images[job["image_id"]]
                job.update(status="running", started=time.time(), stage="准备处理")
                self.active_job_id = identity
            def progress(percent, stage, detail=None):
                with self.lock:
                    job["progress"] = max(job["progress"], min(99.9, float(percent)))
                    job["stage"] = str(stage)
                    if isinstance(detail, dict):
                        job["detail"] = detail.get("message")
                        if detail.get("actual_device"):
                            job["actual_device"] = detail["actual_device"]
                    else:
                        job["detail"] = detail

            try:
                from .engine import process_image, ProcessingCancelled
                runner = self.processor or process_image
                with result_workspace(job["output_dir"]) as temporary:
                    metadata = runner(image["path"], temporary, job["settings"], progress, job["cancel"].is_set)
                    if job["cancel"].is_set():
                        raise ProcessingCancelled("已取消")
                    if not temporary.is_file():
                        raise RuntimeError("处理器未生成结果文件。")
                    receipt = dict(app="ClearFrame", version=__version__, source_name=image["name"],
                                   settings=job["settings"], metadata=metadata,
                                   completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                    # Cancellation, publication and completed status share one
                    # lock, so a late cancel cannot hide a successful file pair.
                    with self.lock:
                        path, receipt = publish_result(temporary, job["output_dir"], result_stem(image["name"], job["settings"]), receipt, job["cancel"].is_set)
                        metadata = receipt["metadata"]
                        job.update(status="completed", progress=100, stage="处理完成", detail=None,
                                   actual_device=metadata.get("actual_device", job["actual_device"]),
                                   metadata=metadata, result_path=path, result_name=path.name, finished=time.time(),
                                   result_url=f"/api/jobs/{identity}/result",
                                   download_url=f"/api/jobs/{identity}/result?download=1")
            except Exception as exc:
                cancelled = job["cancel"].is_set() or type(exc).__name__ == "ProcessingCancelled"
                with self.lock:
                    job.update(status="cancelled" if cancelled else "failed",
                               stage="已取消" if cancelled else "处理失败",
                               error=None if cancelled else str(exc)[:800])
                if not cancelled:
                    traceback.print_exc()
            finally:
                with self.lock:
                    if job["finished"] is None:
                        job["finished"] = time.time()
                    self.active_job_id = None
                self.work.task_done()

    def hardware_snapshot(self):
        values = self.hardware.snapshot() if self.hardware else dict(cpu_percent=None, ram_used_gb=None,
                    ram_total_gb=None, gpu=None, cuda_available=None)
        with self.lock:
            active = self.jobs.get(self.active_job_id)
            values.update(active_job_id=self.active_job_id,
                          active_device=active["actual_device"] if active else None)
        return values

    def close(self):
        self.stopping.set()
        with self.lock:
            for job in self.jobs.values():
                if job["status"] in ("queued", "running"):
                    job["cancel"].set()
        if self.hardware:
            self.hardware.close()


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, state):
        self.state = state
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    @property
    def state(self):
        return self.server.state

    def send(self, code, data, content_type="application/json; charset=utf-8", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def json(self, value, code=200):
        self.send(code, json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))

    def allowed_host(self):
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def allowed_write(self):
        origin = self.headers.get("Origin")
        port = self.server.server_port
        return self.allowed_host() and origin in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}") and secrets.compare_digest(self.headers.get("X-Token", ""), self.state.token)

    def body(self, limit=MAX_BYTES):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ValueError("无效请求长度。")
        if not 0 < length <= limit:
            raise ValueError("内容为空或超过允许大小（图片上限 80 MB）。")
        self.connection.settimeout(30)
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValueError("上传未完成，请重试。")
        return data

    def do_GET(self):
        if not self.allowed_host():
            return self.json({"error": "仅允许本机访问。"}, 403)
        route = urlparse(self.path)
        path = route.path
        try:
            if path == "/":
                data = (self.state.root / "web/index.html").read_bytes().replace(b"__TOKEN__", self.state.token.encode())
                return self.send(200, data, "text/html; charset=utf-8", {"Content-Security-Policy":
                    "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
            if path in ("/web/app.js", "/web/style.css"):
                local = self.state.root / path.lstrip("/")
                return self.send(200, local.read_bytes(), "text/javascript; charset=utf-8" if path.endswith(".js") else "text/css; charset=utf-8")
            if path == "/api/health":
                return self.json(dict(ok=True, app="clearframe", version=__version__))
            if path == "/api/config":
                from .presets import get_config
                config = get_config()
                with self.state.lock:
                    config.update(app_name="澄图 ClearFrame", version=__version__, output_dir=str(self.state.output), output_default_dir=str(self.state.output_default), output_warning=self.state.output_preferences.warning)
                return self.json(config)
            if path == "/api/hardware":
                return self.json(self.state.hardware_snapshot())
            match = re.fullmatch(r"/api/images/([0-9a-f]{32})/source", path)
            if match:
                image = self.state.images[match[1]]
                return self.send(200, image["path"].read_bytes(), "image/png")
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})(/result)?", path)
            if match:
                if not match[2]:
                    return self.json(self.state.public_job(match[1]))
                with self.state.lock:
                    job = self.state.jobs[match[1]]
                    local = job["result_path"] if job["status"] == "completed" else None
                if not local:
                    return self.json({"error": "结果尚未生成。"}, 409)
                headers = {}
                if parse_qs(route.query).get("download"):
                    headers["Content-Disposition"] = "attachment; filename=clearframe-result.png; filename*=UTF-8''" + quote(local.name)
                return self.send(200, local.read_bytes(), "image/png", headers)
            return self.json({"error": "未找到。"}, 404)
        except (KeyError, FileNotFoundError):
            return self.json({"error": "内容不存在，请重新导入图片。"}, 404)
        except Exception as exc:
            return self.json({"error": str(exc)[:500]}, 500)

    def do_POST(self):
        if not self.allowed_write():
            return self.json({"error": "页面已过期或请求来源无效，请刷新本机页面。"}, 403)
        route = urlparse(self.path)
        try:
            if route.path == "/api/images":
                name = parse_qs(route.query).get("name", ["image.png"])[0]
                return self.json(self.state.import_image(self.body(), name), 201)
            if route.path == "/api/jobs":
                return self.json(self.state.create_job(json.loads(self.body(64 * 1024))), 202)
            if route.path == "/api/output":
                return self.json(self.state.set_output(json.loads(self.body(64 * 1024))))
            if route.path == "/api/output/open":
                return self.json(self.state.open_output())
            if route.path == "/api/exports":
                return self.json(self.state.export_queue(json.loads(self.body(64 * 1024))), 201)
            export = re.fullmatch(r"/api/exports/([0-9a-f]{32})/open", route.path)
            if export:
                return self.json(self.state.open_export(export[1]))
            match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/cancel", route.path)
            if match:
                return self.json(self.state.cancel_job(match[1]))
            if route.path == "/api/shutdown":
                self.json({"ok": True})
                self.state.close()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            return self.json({"error": "没有这个操作。"}, 404)
        except (ValueError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
            return self.json({"error": str(exc)[:500]}, 400)
        except KeyError:
            return self.json({"error": "任务或导出记录不存在。"}, 404)
        except Exception as exc:
            return self.json({"error": str(exc)[:500]}, 500)


def main():
    parser = argparse.ArgumentParser(description="ClearFrame 本地手动图片处理程序")
    parser.add_argument("--port", type=int, default=7866)
    parser.add_argument("--no-browser", action="store_true")
    arguments = parser.parse_args()
    url = f"http://127.0.0.1:{arguments.port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        running = json.loads(opener.open(url + "/api/health", timeout=1).read())
        if running.get("app") == "clearframe":
            if not arguments.no_browser:
                webbrowser.open(url)
            print("ClearFrame 已在运行：" + url, flush=True)
            return
    except Exception:
        pass
    state = StudioState()
    try:
        server = StudioServer(("127.0.0.1", arguments.port), state)
    except OSError:
        state.close()
        raise SystemExit(f"端口 {arguments.port} 被其他程序使用，请用 --port 指定其他端口。")
    print(f"ClearFrame {__version__} · {url} · 结果目录 {state.output}", flush=True)
    if not arguments.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        state.close()
        server.server_close()


if __name__ == "__main__":
    main()
