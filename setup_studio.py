"""Create a project-local environment; never modify global Python packages."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def run(arguments):
    subprocess.run([str(a) for a in arguments], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description="Install ClearFrame's isolated environment")
    parser.add_argument("--cpu", action="store_true", help="Install CPU-only PyTorch")
    args = parser.parse_args()
    if not (3, 10) <= sys.version_info[:2] <= (3, 13):
        raise SystemExit("Please run this installer with Python 3.10–3.13 (64 bit).")
    if sys.maxsize <= 2**32:
        raise SystemExit("64-bit Python is required.")
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        print("Creating .venv ...", flush=True)
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    run([python, "-m", "pip", "install", "--upgrade", "pip"])
    cuda = not args.cpu and shutil.which("nvidia-smi") is not None
    wheel = "cu128" if cuda else "cpu"
    print(f"Installing PyTorch 2.7.1 ({wheel}). The GPU package is several GB.", flush=True)
    # Exact local build tags make rerunning --cpu / GPU setup actually switch wheels.
    run([python, "-m", "pip", "install", f"torch==2.7.1+{wheel}", f"torchvision==0.22.1+{wheel}",
         "--index-url", f"https://download.pytorch.org/whl/{wheel}"])
    run([python, "-m", "pip", "install", "-r", ROOT / "requirements-studio.txt"])
    run([python, "-c", "import torch, cv2, spandrel, PIL, psutil; print('ClearFrame dependencies ready. GPU available:', torch.cuda.is_available())"])
    print("Setup complete. Images are processed locally; no API key is needed.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"Installation failed (exit {exc.returncode}). Check network access and retry.", file=sys.stderr)
        raise SystemExit(1)
