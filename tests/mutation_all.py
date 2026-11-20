"""历史变异与当前专项的统一入口：串行验收、隔离修改、按目标断言统计。"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUITES = ("v0247_round2", "v0248", "v0249", "v0260", "v0270", "v0300", "v0310")
SPECIAL_CHECKS = {"v0330-checks": "v0.33.0", "v0340-checks": "v0.34.0"}
VERDICTS = ("KILLED", "SURVIVED", "INVALID")


@dataclass(frozen=True)
class Case:
    criterion: str
    description: str
    path: str
    before: str
    after: str
    command: tuple[str, ...]
    failure: str = ""
    baseline: tuple[str, ...] | None = None


def registered_cases(module) -> list[Case]:
    if hasattr(module, "mutation_cases"):
        return module.mutation_cases()
    if hasattr(module, "CASES"):
        return [Case(c.cid, c.note, c.rel, c.old, c.new,
                     (sys.executable, "-B", "-X", "utf8", c.test),
                     getattr(c, "failure", ""))
                for c in module.CASES]
    return [Case(c.criterion, c.description, c.path, c.before, c.after,
                 tuple(c.command), c.failure) for c in module.MUTATIONS]


class LockError(RuntimeError):
    pass


def pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    # Windows 的 os.kill(pid, 0) 不是安全的存活探针，使用只等待、不终止的句柄。
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: 不存在
    try:
        return kernel.WaitForSingleObject(handle, 0) != 0  # WAIT_OBJECT_0: 已退出
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def pid_lock(source: Path):
    lock = source / "tests" / ".mutation.lock"
    token = f"{os.getpid()}\n"
    try:
        with lock.open("x", encoding="ascii", newline="\n") as file:
            file.write(token)
    except FileExistsError:
        try:
            pid = int(lock.read_text(encoding="ascii").strip())
            if not 0 < pid <= 2**31 - 1:
                raise ValueError
        except (OSError, ValueError):
            raise LockError(f"另一个变异正在跑，或锁尚未写完/无效：{lock}") from None
        if pid_alive(pid):
            raise LockError(f"另一个变异正在跑（PID {pid}）：{lock}") from None
        # 不自动抢占陈旧锁：两个进程同时删除旧锁会有机会误删新持有者的锁。
        raise LockError(f"变异锁 PID {pid} 已退出。确认没有变异进程后删除陈旧锁：{lock}") from None
    try:
        yield
    finally:
        if lock.exists() and lock.read_text(encoding="ascii") == token:
            lock.unlink()


def git(source: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(source), *args])


def copy_workspace(source: Path, snapshot: Path) -> None:
    # 旧用例会考察 Git/忽略规则。元数据另克隆一份，再覆盖当前工作区的可交付文件。
    git(source, "clone", "--quiet", "--no-hardlinks", "--no-checkout", "--", str(source), str(snapshot))
    git(snapshot, "read-tree", "HEAD")
    files = git(source, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for rel in set(files.decode("utf-8").split("\0")) - {"", "tests/.mutation.lock"}:
        src = source / rel
        if src.is_file() and not src.is_symlink():
            dest = snapshot / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)


def digests(root: Path, paths) -> dict[str, str | None]:
    result = {}
    for rel in sorted(set(paths)):
        path = root / rel
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"变异路径超出仓库：{rel}")
        result[rel] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return result


def verify_hashes(root: Path, expected, label: str, revision: str) -> bool:
    current = digests(root, expected)
    different = [name for name, digest in expected.items() if current[name] != digest]
    for name in different:
        print(f"[RESIDUE] {label} {name}: {expected[name]} → {current[name]}", flush=True)
    if different:
        print(f"[INVALID] {label}残留或并发改动：{len(different)} 个文件。"
              f"先保留本地改动并核对 git diff {revision}，确认后可从提交 {revision} 还原对应文件。",
              flush=True)
    else:
        print(f"[SHA-256] {label} {len(expected)} 个目标文件与运行前一致", flush=True)
    return not different


def anchor(raw: bytes, case: Case) -> tuple[int, bytes, bytes]:
    lf = case.before.encode("utf-8")
    crlf = lf.replace(b"\n", b"\r\n")
    options = {lf, crlf}
    count = sum(raw.count(option) for option in options) if lf else 0
    before = crlf if crlf != lf and raw.count(crlf) else lf
    after = case.after.encode("utf-8")
    if before == crlf and crlf != lf:
        after = after.replace(b"\n", b"\r\n")
    return count, before, after


def run_test(root: Path, command, env):
    return subprocess.run(command, cwd=root, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)


def classify(result, marker: str) -> tuple[str, str]:
    if result.returncode == 0:
        return "SURVIVED", "目标用例仍通过"
    # unittest、Playwright 和 P24 的失败都使用 AssertionError。查断言块而非源码回显，
    # 这样异常前的调用栈、或仅写出 FAILED 的其它错误，不会冒充目标断言。
    failures = re.findall(r"(?ms)^AssertionError: (.*?)(?=^={8,}|^-{8,}|\Z)", result.stderr)
    if marker and result.returncode == 1 and any(marker in part for part in failures):
        return "KILLED", f"AssertionError: {marker}"
    if result.returncode == 1 and "Traceback (most recent call last)" not in result.stderr:
        # 独立命令行检查器的断言印在 stdout。匹配明确标记的失败行。
        for line in result.stdout.splitlines():
            if re.match(r"^\s*(?:\[FAIL\]|✗)\s+", line) and (not marker or marker in line):
                return "KILLED", line.strip()[:190]
    detail = (result.stdout + result.stderr).strip().splitlines()
    return "INVALID", f"rc={result.returncode}，未匹配目标判据 {marker!r}\n" + "\n".join(detail[-16:])


def exercise(root: Path, case: Case, env, baselines) -> tuple[str, str]:
    path = root / case.path
    if not path.is_file():
        return "INVALID", "目标文件不存在，锚点已漂移，需更新"
    raw = path.read_bytes()
    count, before, after = anchor(raw, case)
    if count != 1 or before == after:
        return "INVALID", f"锚点已漂移，需更新（出现 {count} 次，要求唯一且替换内容不同）"
    base = case.baseline or case.command[:5]
    if base not in baselines:
        try:
            result = run_test(root, base, env)
            baselines[base] = "" if result.returncode == 0 else result.stdout + result.stderr
            if result.returncode and not baselines[base]:
                baselines[base] = f"rc={result.returncode}，无输出"
        except (OSError, subprocess.TimeoutExpired) as exc:
            baselines[base] = str(exc)
        label = "FAIL" if baselines[base] else "PASS"
        print(f"[BASELINE {label}] {base[-1]}", flush=True)
    if baselines[base]:
        return "INVALID", "未修改时用例不通过\n" + baselines[base][-3000:]
    try:
        path.write_bytes(raw.replace(before, after, 1))
        try:
            return classify(run_test(root, case.command, env), case.failure)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return "INVALID", f"运行失败：{exc}"
    finally:
        path.write_bytes(raw)
        if path.read_bytes() != raw:
            raise RuntimeError(f"{case.path} 未逐字节恢复")


def run(suites, *, source: Path = ROOT, legacy: bool = False) -> int:
    counts = {name: Counter() for name in suites}
    with pid_lock(source):
        revision = git(source, "rev-parse", "--short", "HEAD").decode().strip()
        originals = digests(source, (case.path for cases in suites.values() for case in cases))
        try:
            with tempfile.TemporaryDirectory(prefix="scintilla-mutations-") as temp:
                root = Path(temp) / source.name
                copy_workspace(source, root)
                if digests(root, originals) != originals:
                    raise RuntimeError("隔离副本与源工作区不一致")
                astrbot = Path(temp) / "astrbot"
                env = {**os.environ, "ASTRBOT_ROOT": str(astrbot), "ASTRBOT_DATA_DIR": str(astrbot / "data"),
                       "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
                baselines = {}
                try:
                    for name, cases in suites.items():
                        for index, case in enumerate(cases, 1):
                            verdict, detail = exercise(root, case, env, baselines)
                            counts[name][verdict] += 1
                            label = verdict
                            if legacy and name in ("v0247_round2", "v0248", "v0249"):
                                label = "INFRA-ERR" if verdict == "INVALID" else verdict
                            elif legacy and verdict == "KILLED":
                                label = "RED"
                            print(f"[{label}] {name}/{index:02} {case.criterion} {case.description} "
                                  f"｜ {case.path} → {detail}", flush=True)
                finally:
                    snapshot_clean = verify_hashes(root, originals, "隔离副本", revision)
        finally:
            source_clean = verify_hashes(source, originals, "源工作区", revision)
    if legacy:
        name, tally = next(iter(counts.items()))
        total = len(suites[name])
        print(f"KILLED {tally['KILLED']}/{total}" if name in ("v0247_round2", "v0248", "v0249")
              else f"定向变异 {tally['KILLED']}/{total} 变红，逐条字节级还原")
    else:
        historical = {name: tally for name, tally in counts.items() if name not in SPECIAL_CHECKS}
        if historical:
            print("\n| 脚本 | 总数 | KILLED | SURVIVED | INVALID |")
            print("| --- | ---: | ---: | ---: | ---: |")
            for name, tally in historical.items():
                print(f"| mutation_{name}.py | {len(suites[name])} | "
                      + " | ".join(str(tally[v]) for v in VERDICTS) + " |")
            totals = sum(historical.values(), Counter())
            print(f"| 合计 | {sum(len(suites[name]) for name in historical)} | "
                  + " | ".join(str(totals[v]) for v in VERDICTS) + " |")
        for name, label in SPECIAL_CHECKS.items():
            if name in counts:
                tally = counts[name]
                print(f"\n{label} 专项（独立于历史合计） {len(suites[name])} 条"
                      + "：" + ", ".join(f"{tally[v]} {v}" for v in VERDICTS))
    return 0 if snapshot_clean and source_clean and all(
        counts[name]["KILLED"] == len(cases) for name, cases in suites.items()
    ) else 1


def entry(suites, *, legacy=False) -> int:
    try:
        return run(suites, legacy=legacy)
    except LockError as exc:
        print(f"[INVALID] {exc}", file=sys.stderr, flush=True)
        return 1
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"[INVALID] 变异运行中止：{exc}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("[INVALID] 用户中断变异运行", file=sys.stderr, flush=True)
        return 130


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selected = parser.add_mutually_exclusive_group()
    selected.add_argument("--suite", choices=SUITES, action="append", help="仅运行指定历史组，可重复")
    selected.add_argument("--v0330-checks", action="store_true", help="仅运行 v0.33.0 专项")
    selected.add_argument("--v0340-checks", action="store_true", help="仅运行 v0.34.0 专项")
    args = parser.parse_args(argv)
    suites = {}
    if not (args.v0330_checks or args.v0340_checks):
        suites.update({name: registered_cases(importlib.import_module(f"mutation_{name}"))
                       for name in SUITES if not args.suite or name in args.suite})
    if not args.suite:
        from mutation_v0310 import v0330_cases, v0340_cases
        if not args.v0340_checks:
            suites["v0330-checks"] = v0330_cases()
        if not args.v0330_checks:
            suites["v0340-checks"] = v0340_cases()
    return entry(suites)


if __name__ == "__main__":
    sys.exit(main())
