"""Start a hidden local service and open the browser only when it is ready."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = "http://127.0.0.1:7866"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ready():
    try:
        with OPENER.open(URL + "/api/health", timeout=1) as response:
            return json.load(response).get("app") == "clearframe"
    except Exception:
        return False


def main():
    if ready():
        webbrowser.open(URL)
        return 0
    try:
        import PIL, numpy, cv2, psutil
    except ImportError:
        print("Dependencies are missing. Run Setup-ClearFrame.cmd, then retry.")
        return 1
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    executable = Path(sys.executable)
    if os.name == "nt" and executable.with_name("pythonw.exe").exists():
        executable = executable.with_name("pythonw.exe")
    with (runtime / "server.log").open("ab", buffering=0) as log:
        child = subprocess.Popen([str(executable), "-u", "-m", "studio.server", "--no-browser"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt")
    for _ in range(100):
        if ready():
            webbrowser.open(URL)
            print("ClearFrame opened. Use the page's Exit button to stop the service.")
            return 0
        if child.poll() is not None:
            break
        time.sleep(.15)
    print(f"ClearFrame could not start. See {runtime / 'server.log'}")
    print("If port 7866 is occupied, run: .venv\\Scripts\\python.exe -m studio.server --port 7867")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
