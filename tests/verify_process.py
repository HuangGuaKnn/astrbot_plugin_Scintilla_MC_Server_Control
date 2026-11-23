"""验收子进程超时时清理整棵进程树，包括浏览器。"""
import os
import signal
import subprocess


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            result = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                    capture_output=True, timeout=15)
            if result.returncode and proc.poll() is None:
                proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        if proc.poll() is None:
            proc.kill()
