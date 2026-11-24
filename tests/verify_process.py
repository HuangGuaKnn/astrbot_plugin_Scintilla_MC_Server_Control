"""验收子进程超时时清理整棵进程树，包括浏览器。"""
import os
import signal
import subprocess
from _no_window import no_window_kw  # noqa: E402


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                    capture_output=True, timeout=15, **no_window_kw())
            if result.returncode and proc.poll() is None:
                proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        if proc.poll() is None:
            proc.kill()
