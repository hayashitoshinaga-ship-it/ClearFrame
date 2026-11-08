"""Sample real host measurements without blocking HTTP requests."""
import csv
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import threading
import time

import psutil


class HardwareMonitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._data = dict(cpu_percent=None, ram_used_gb=None, ram_total_gb=None,
                          gpu=None, cuda_available=None, cuda_error=None)
        self._smi = shutil.which("nvidia-smi")
        threading.Thread(target=self._sample_loop, daemon=True).start()
        threading.Thread(target=self._probe_cuda, daemon=True).start()

    def _probe_cuda(self):
        available, error = False, None
        try:
            if importlib.util.find_spec("torch") is None:
                error = "未安装 PyTorch；照片降噪仍可使用 CPU。"
            else:
                # Isolate the probe: incompatible drivers can stall native CUDA calls.
                # A bounded child also releases its temporary GPU context afterwards.
                code = "import json, torch; from studio.engine import cuda_compatibility_error; ok=torch.cuda.is_available(); error=cuda_compatibility_error(torch) if ok else None; ok=bool(ok and not error and (torch.ones((8,8),device='cuda')*2).sum().item()==128); print(json.dumps({'ok':ok, 'error':error}))"
                child = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                       text=True, timeout=35,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                data = json.loads(child.stdout.strip().splitlines()[-1]) if child.returncode == 0 else {}
                available = data.get("ok", False)
                if not available:
                    error = data.get("error") or "当前 PyTorch 无可用 CUDA 设备，使用 CPU。"
        except subprocess.TimeoutExpired:
            error = "GPU 运行检测超时；请运行安装器更新独立环境，照片处理仍可使用 CPU。"
        except Exception as exc:
            error = "CUDA 运行检查失败：" + str(exc).splitlines()[0][:220]
            available = False
        with self._lock:
            self._data.update(cuda_available=available, cuda_error=error)

    @staticmethod
    def _number(value):
        try:
            return float(value.strip())
        except (ValueError, AttributeError):
            return None

    def _gpu(self):
        if not self._smi:
            return None
        try:
            query = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
            process = subprocess.run([self._smi, "--query-gpu=" + query,
                                      "--format=csv,noheader,nounits"], capture_output=True,
                                     text=True, encoding="utf-8", errors="replace", timeout=2,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if process.returncode:
                return None
            row = next(csv.reader(io.StringIO(process.stdout)))
            used, total = self._number(row[2]), self._number(row[3])
            return dict(name=row[0].strip(), utilization=self._number(row[1]),
                        memory_used_gb=round(used / 1024, 2) if used is not None else None,
                        memory_total_gb=round(total / 1024, 2) if total is not None else None,
                        temperature_c=self._number(row[4]))
        except (OSError, subprocess.SubprocessError, StopIteration, IndexError):
            return None

    def _sample_loop(self):
        psutil.cpu_percent(interval=None)
        while not self._stop.is_set():
            memory = psutil.virtual_memory()
            sample = dict(cpu_percent=psutil.cpu_percent(interval=None),
                          ram_used_gb=round((memory.total - memory.available) / 1024**3, 2),
                          ram_total_gb=round(memory.total / 1024**3, 2), gpu=self._gpu(),
                          sampled_at=time.time())
            with self._lock:
                self._data.update(sample)
            self._stop.wait(2)

    def snapshot(self):
        with self._lock:
            return dict(self._data)

    def close(self):
        self._stop.set()
