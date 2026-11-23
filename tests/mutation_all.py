"""按目标文件在隔离副本运行变异，支持工作区增量和完整验收。"""
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from time import perf_counter

from verify_timing import print_bill
from verify_process import kill_tree


ROOT = Path(__file__).resolve().parents[1]
SUITES = ("v0247_round2", "v0248", "v0249", "v0260", "v0270", "v0300", "v0310")
SPECIAL_CHECKS = {"v0330-checks": "v0.33.0", "v0340-checks": "v0.34.0", "v0390-runner": "v0.39.0 流水线"}
VERDICTS = ("KILLED", "SURVIVED", "INVALID")
LOCK_PATH = "tests/.mutation.lock"
SHARED_PATHS = frozenset({
    "core/command_result.py", "core/version_caps.py", "core/rcon.py",
    "core/server_card.py", "core/tool_guard.py", "core/strict_bool.py", "core/java_commands.py",
    "main.py", "core/web_api.py", "core/log_watcher.py", "_conf_schema.json",
    "core/player_admin.py", "core/knowledge_base.py",
    ".gitignore", "run_release_verify.py", "tests/_paths.py",
})


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


@dataclass(frozen=True)
class RegisteredCase:
    suite: str
    index: int
    case: Case

    @property
    def key(self):
        return self.suite, self.index


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
    lock = source / LOCK_PATH
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


def changed_files(source: Path, ref: str) -> set[str]:
    revision = git(source, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    changed = git(source, "diff", "--name-only", "--no-renames", "-z", revision, "--")
    untracked = git(source, "ls-files", "--others", "--exclude-standard", "-z")
    return set((changed + untracked).decode("utf-8").split("\0")) - {"", LOCK_PATH}


def group_by_path(registry):
    groups = {}
    for item in registry:
        groups.setdefault(item.case.path, []).append(item)
    return groups


def verify_selection(registry, chosen, paths):
    expected = {item.key for item in registry if item.case.path in paths}
    if len(chosen) != len(expected) or {item.key for item in chosen} != expected:
        raise RuntimeError("E3 增量选区与全量登记中的目标文件交集不一致")


def select_cases(registry, paths):
    if paths is None:
        return registry
    groups = group_by_path(registry)
    shared = paths & SHARED_PATHS
    unknown = paths - groups.keys() - shared
    if shared or unknown:
        reason = []
        if shared:
            reason.append("共享文件 " + "、".join(sorted(shared)))
        if unknown:
            reason.append("无法从变异目标判断依赖的文件 " + "、".join(sorted(unknown)))
        print("[WARN] " + "；".join(reason) + "，退回全量")
        chosen = registry
    else:
        chosen = [item for path in sorted(paths) for item in groups[path]]
        verify_selection(registry, chosen, paths)
        chosen.sort(key=lambda item: item.key)
    scope = "本次只跑了" if len(chosen) < len(registry) else "本次跑全量"
    print(f"{scope} {len(chosen)}/{len(registry)}，来源文件为 " + ("、".join(sorted(paths)) or "无"))
    return chosen


def copy_workspace(source: Path, snapshot: Path) -> None:
    # 旧用例会考察 Git/忽略规则。元数据另克隆一份，再覆盖当前工作区的可交付文件。
    git(source, "clone", "--quiet", "--no-hardlinks", "--no-checkout", "--", str(source), str(snapshot))
    git(snapshot, "read-tree", "HEAD")
    files = git(source, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    for rel in set(files.decode("utf-8").split("\0")) - {"", LOCK_PATH}:
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
    with subprocess.Popen(command, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace",
                          start_new_session=os.name != "nt") as proc:
        try:
            stdout, stderr = proc.communicate(timeout=300)
        except BaseException:
            kill_tree(proc)
            raise
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)


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


def baseline_command(case: Case):
    return case.baseline or case.command[:5]


def check_baseline(root, command, env):
    cost = perf_counter()
    try:
        result = run_test(root, command, env)
        error = "" if result.returncode == 0 else (result.stdout + result.stderr or f"rc={result.returncode}，无输出")
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = str(exc)
    return error, perf_counter() - cost


def exercise(root: Path, case: Case, env, baselines) -> tuple[str, str]:
    path = root / case.path
    if not path.is_file():
        return "INVALID", "目标文件不存在，锚点已漂移，需更新"
    raw = path.read_bytes()
    count, before, after = anchor(raw, case)
    if count != 1 or before == after:
        return "INVALID", f"锚点已漂移，需更新（出现 {count} 次，要求唯一且替换内容不同）"
    error = baselines[baseline_command(case)]
    if error:
        return "INVALID", "未修改时用例不通过\n" + error[-3000:]
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


def estimated_cost(command):
    return 15 if any(arg.replace("\\", "/").startswith("tests/ui_") for arg in command) else 3


def distribute(items, jobs, cost):
    batches = [[] for _ in range(jobs)]
    lengths = [0] * jobs
    for item in sorted(items, key=cost, reverse=True):
        worker = lengths.index(min(lengths))
        batches[worker].append(item)
        lengths[worker] += cost(item)
    return batches


def worker_env(directory):
    scratch = directory / "tmp"
    scratch.mkdir()
    astrbot = directory / "astrbot"
    return {**os.environ, "ASTRBOT_ROOT": str(astrbot), "ASTRBOT_DATA_DIR": str(astrbot / "data"),
            "TMP": str(scratch), "TEMP": str(scratch), "TMPDIR": str(scratch),
            "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}


def execute_groups(groups, roots, envs, originals, legacy):
    jobs = len(roots)
    stopping = Event()
    output = Lock()
    stages = []

    def log(message):
        with output:
            print(message, flush=True)

    def baselines_for_worker(worker, commands):
        results = {}
        for command in commands:
            if stopping.is_set():
                break
            error, cost = check_baseline(roots[worker], command, envs[worker])
            results[command] = error
            log(f"[BASELINE {'FAIL' if error else 'PASS'}] {command[-1]} ({cost:.3f}s)")
        return results

    def run_groups(worker, assigned):
        results = {}
        for items in assigned:
            path = items[0].case.path
            started = perf_counter()
            tally = Counter()
            for item in items:
                if stopping.is_set():
                    break
                case = item.case
                case_started = perf_counter()
                verdict, detail = exercise(roots[worker], case, envs[worker], baselines)
                cost = perf_counter() - case_started
                results[item.key] = verdict
                tally[verdict] += 1
                label = verdict
                if legacy and item.suite in ("v0247_round2", "v0248", "v0249"):
                    label = "INFRA-ERR" if verdict == "INVALID" else verdict
                elif legacy and verdict == "KILLED":
                    label = "RED"
                log(f"[{label}] {item.suite}/{item.index:02} {case.criterion} {case.description} "
                    f"｜ {path} ({cost:.3f}s) → {detail}")
            if digests(roots[worker], [path])[path] != originals[path]:
                raise RuntimeError(f"{path} 变异组结束后未恢复")
            log(f"[GROUP] {path} {sum(tally.values())}/{len(items)} 条 ({perf_counter() - started:.3f}s)"
                " ｜ " + ", ".join(f"{tally[v]} {v}" for v in VERDICTS))
        return results

    def schedule(pool, function, batches):
        futures = [pool.submit(function, worker, items) for worker, items in enumerate(batches) if items]
        results = {}
        try:
            for future in as_completed(futures):
                results.update(future.result())
        except BaseException:
            stopping.set()
            raise
        return results

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        commands = dict.fromkeys(baseline_command(item.case) for items in groups.values() for item in items)
        started = perf_counter()
        baselines = schedule(pool, baselines_for_worker, distribute(commands, jobs, estimated_cost))
        stages.append(("未修改基线", perf_counter() - started))
        started = perf_counter()
        assigned = distribute(groups.values(), jobs, lambda group: sum(estimated_cost(i.case.command) for i in group))
        results = schedule(pool, run_groups, assigned)
        stages.append(("变异执行", perf_counter() - started))
    return results, stages


def run(suites, *, source: Path = ROOT, legacy: bool = False, jobs: int = 4,
        paths: set[str] | None = None, plan: bool = False) -> int:
    started = perf_counter()
    registry = [RegisteredCase(name, index, case) for name, cases in suites.items()
                for index, case in enumerate(cases, 1)]
    chosen = select_cases(registry, paths)
    groups = group_by_path(chosen)
    jobs = min(jobs, len(groups))
    if plan or not chosen:
        for item in chosen:
            print(f"[PLAN] {item.suite}/{item.index:02} {item.case.criterion} ｜ {item.case.path}")
        print(f"共 {len(chosen)} 条变异，{len(groups)} 个目标文件，{jobs} 路")
        if not plan:
            print_bill("变异", started, [("选区", perf_counter() - started)])
        return 0
    stages = [("选区", perf_counter() - started)]
    counts = {name: Counter() for name in suites}
    with pid_lock(source):
        setup_started = perf_counter()
        revision = git(source, "rev-parse", "--short", "HEAD").decode().strip()
        originals = digests(source, groups)
        try:
            with tempfile.TemporaryDirectory(prefix="scintilla-mutations-") as temp:
                directories = [Path(temp) / str(worker + 1) for worker in range(jobs)]
                roots = [directory / source.name for directory in directories]
                copy_workspace(source, roots[0])
                for root in roots[1:]:
                    shutil.copytree(roots[0], root)
                if any(digests(root, originals) != originals for root in roots):
                    raise RuntimeError("隔离副本与源工作区不一致")
                envs = [worker_env(directory) for directory in directories]
                print(f"{len(chosen)} 条变异，{len(groups)} 个目标文件，{jobs} 路独立副本", flush=True)
                stages.append(("副本准备", perf_counter() - setup_started))
                try:
                    results, costs = execute_groups(groups, roots, envs, originals, legacy)
                    stages.extend(costs)
                finally:
                    restore_started = perf_counter()
                    snapshot_clean = all([verify_hashes(root, originals, f"隔离副本 {index}", revision)
                                          for index, root in enumerate(roots, 1)])
        finally:
            source_clean = verify_hashes(source, originals, "源工作区", revision)
        stages.append(("SHA-256 核对及清理", perf_counter() - restore_started))
    if results.keys() != {item.key for item in chosen}:
        raise RuntimeError("变异结果缺项或多出未计划的项目")
    selected_counts = Counter(item.suite for item in chosen)
    for (name, _), verdict in results.items():
        counts[name][verdict] += 1
    if legacy:
        name, tally = next(iter(counts.items()))
        total = selected_counts[name]
        print(f"KILLED {tally['KILLED']}/{total}" if name in ("v0247_round2", "v0248", "v0249")
              else f"定向变异 {tally['KILLED']}/{total} 变红，逐条字节级还原")
    else:
        historical = {name: tally for name, tally in counts.items() if name not in SPECIAL_CHECKS}
        if historical:
            print("\n| 脚本 | 总数 | KILLED | SURVIVED | INVALID |")
            print("| --- | ---: | ---: | ---: | ---: |")
            for name, tally in historical.items():
                print(f"| mutation_{name}.py | {selected_counts[name]} | "
                      + " | ".join(str(tally[v]) for v in VERDICTS) + " |")
            totals = sum(historical.values(), Counter())
            print(f"| 合计 | {sum(selected_counts[name] for name in historical)} | "
                  + " | ".join(str(totals[v]) for v in VERDICTS) + " |")
        for name, label in SPECIAL_CHECKS.items():
            if name in counts:
                tally = counts[name]
                print(f"\n{label} 专项（独立于历史合计） {selected_counts[name]} 条"
                      + "：" + ", ".join(f"{tally[v]} {v}" for v in VERDICTS))
    print(f"\n本轮总计 {len(chosen)}/{len(registry)} 条，"
          + ", ".join(f"{sum(tally[v] for tally in counts.values())} {v}" for v in VERDICTS))
    print_bill("变异", started, stages)
    return 0 if snapshot_clean and source_clean and all(
        counts[name]["KILLED"] == selected_counts[name] for name in suites
    ) else 1


def entry(suites, *, legacy=False, jobs=4, ref=None, plan=False) -> int:
    try:
        paths = changed_files(ROOT, ref) if ref is not None else None
        return run(suites, source=ROOT, legacy=legacy, jobs=jobs, paths=paths, plan=plan)
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
    selected.add_argument("--changed", action="store_true", help="按 HEAD 与当前工作区的改动选区")
    selected.add_argument("--since", metavar="REF", help="按指定提交与当前工作区的改动选区")
    selected.add_argument("--runner-checks", action="store_true", help="仅运行 v0.39.0 验证流水线专项")
    parser.add_argument("--jobs", type=int, default=4, help="隔离执行路数，1 为串行（默认 4）")
    parser.add_argument("--plan", action="store_true", help="仅显示选区和用例")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs 至少为 1")
    if args.runner_checks:
        from mutation_v0390 import mutation_cases
        return entry({"v0390-runner": mutation_cases()}, jobs=args.jobs, plan=args.plan)
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
    return entry(suites, jobs=args.jobs, ref="HEAD" if args.changed else args.since, plan=args.plan)


if __name__ == "__main__":
    sys.exit(main())
