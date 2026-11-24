"""验证流水线的选区、并行、还原与墙钟账单。"""
import ast
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import mutation_all as M

sys.path.insert(0, str(M.ROOT))
import run_release_verify as V

import _no_window as NO_WINDOW


def fixture_case(path, number):
    command = (sys.executable, "-B", "-X", "utf8", "tests/probe.py", path)
    return M.Case(f"E4-{number}", f"fixture {number}", path, "original", f"changed-{number}",
                  command, "E4 目标内容已变化", baseline=command)


def timing_rows(output):
    return [(name, float(value)) for name, value in re.findall(r"^\[TIME\] (.+) ([\d.]+)s$", output, re.M)]


class VerifyPipeline(unittest.TestCase):
    def test_selection_uses_all_registered_cases_for_a_diff(self):
        registry = [
            M.RegisteredCase("suite-a", 1, fixture_case("core/alpha.py", 1)),
            M.RegisteredCase("suite-b", 1, fixture_case("core/alpha.py", 2)),
            M.RegisteredCase("suite-b", 2, fixture_case("core/beta.py", 3)),
        ]
        with contextlib.redirect_stdout(io.StringIO()) as log:
            try:
                chosen = M.select_cases(registry, {"core/alpha.py"})
            except RuntimeError as exc:
                self.fail(f"E3 增量选区必须完整。{exc}")
        self.assertEqual(chosen, registry[:2], "E3 增量选区必须完整")
        self.assertIn("本次只跑了 2/3", log.getvalue(), "E1 增量输出须给出分子、分母")
        with self.assertRaisesRegex(RuntimeError, "E3"):
            M.verify_selection(registry, chosen[:1], {"core/alpha.py"})
        with self.assertRaisesRegex(RuntimeError, "E3"):
            M.verify_selection(registry, chosen[:1] * 2, {"core/alpha.py"})
        for path in ("core/command_result.py", "tests/new_helper.py"):
            with contextlib.redirect_stdout(io.StringIO()) as log:
                selected = M.select_cases(registry, {path})
            self.assertEqual(selected, registry, "E2 共享内核与未知依赖必须退回全量")
            self.assertIn("[WARN]", log.getvalue())
            self.assertIn("本次跑全量 3/3", log.getvalue())

    def test_changed_paths_include_git_diff_and_untracked(self):
        root = Path("fixture")
        path_with_newline = "core/prefix\nname.py"
        with patch.object(M, "git", side_effect=[
            b"0123456789abcdef\n", f"{path_with_newline}\0core/unstaged.py\0".encode(),
            "core/新增.py\0".encode(),
        ]) as git:
            got = M.changed_files(root, "HEAD~1")
        self.assertEqual(got, {path_with_newline, "core/unstaged.py", "core/新增.py"})
        git.assert_any_call(root, "diff", "--name-only", "--no-renames", "-z", "0123456789abcdef", "--")
        git.assert_any_call(root, "ls-files", "--others", "--exclude-standard", "-z")

    def test_restore_after_test_process_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target.py"
            target.write_bytes(b"original\r\n")
            case = fixture_case("target.py", 1)
            before = M.digests(root, [case.path])
            with patch.object(M, "run_test", side_effect=OSError("fixture failed")):
                try:
                    verdict, _ = M.exercise(root, case, {}, {case.command: ""})
                except RuntimeError as exc:
                    self.fail(f"E5 失败后目标文件 SHA 必须还原。{exc}")
            self.assertEqual(verdict, "INVALID")
            self.assertEqual(M.digests(root, [case.path]), before, "E5 失败后目标文件 SHA 必须还原")

    def test_serial_parallel_verdicts_and_isolation(self):
        suites = {
            "suite-a": [fixture_case("alpha.txt", 1), fixture_case("beta.txt", 2)],
            "suite-b": [fixture_case("alpha.txt", 3)],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tests").mkdir()
            (root / "tests" / "probe.py").write_text(
                "import sys\nfrom pathlib import Path\n"
                "assert Path(sys.argv[1]).read_text() == 'original', 'E4 目标内容已变化'\n", encoding="utf-8",
            )
            for name in ("alpha.txt", "beta.txt"):
                (root / name).write_bytes(b"original")
            originals = M.digests(root, ("alpha.txt", "beta.txt"))
            results = []
            for jobs in (1, 2):
                with patch.object(M, "git", return_value=b"fixture\n"), patch.object(
                    M, "copy_workspace",
                    side_effect=lambda src, dst: shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".mutation.lock")),
                ), contextlib.redirect_stdout(io.StringIO()) as log:
                    status = M.run(suites, source=root, jobs=jobs)
                output = log.getvalue()
                self.assertEqual(status, 0, output)
                self.assertEqual(M.digests(root, originals), originals)
                self.assertIn(f"{jobs} 路独立副本", output)
                self.assertEqual(output.count("[BASELINE PASS]"), 2, "相同基线命令只允许执行一次")
                self.assertEqual(output.count("[SHA-256]"), jobs + 1)
                self.assert_bill(output, {"选区", "副本准备", "未修改基线", "变异执行", "SHA-256 核对及清理"})
                results.append(sorted(re.findall(r"^\[(KILLED|SURVIVED|INVALID)\] (suite-\w+/\d+ E4-\d+)", output, re.M)))
            self.assertEqual(results[0], results[1], "E4 并行与串行的逐条判据和状态须一致")
            self.assertEqual(len(results[1]), 3)

    def assert_bill(self, output, expected):
        rows = timing_rows(output)
        self.assertEqual({name for name, _ in rows}, expected | {"汇总及其余开销", "总计"})
        total = dict(rows)["总计"]
        self.assertGreater(total, 0)
        self.assertAlmostEqual(sum(seconds for name, seconds in rows if name != "总计"), total,
                               delta=max(0.003, total * 0.1), msg="E6 阶段墙钟之和须接近总时长")

    def test_gate_runs_four_files_concurrently_and_keeps_a_failure(self):
        workers = Barrier(4)

        def run_file(rel, timeout):
            workers.wait(timeout=10)
            return rel != "tests/bad.py", 0.01, "fixture failure", False

        with patch.object(V, "ui_plan", return_value=["a", "b", "c", "bad"]), patch.object(
            V, "run_file", side_effect=run_file,
        ), contextlib.redirect_stdout(io.StringIO()) as log:
            status = V.main(["--ui-only", "--jobs", "4"])
        output = log.getvalue()
        self.assertEqual(status, 1)
        self.assertIn("3/4 通过", output)
        self.assertIn("[FAIL] tests/bad.py", output)
        self.assert_bill(output, {"③ WebUI 契约类"})


# ---------------------------------------------------------------- 不弹黑框（C1~C3）

#: 仓库里所有可能拉子进程的源码：根脚本 + tests/ + 插件本体。
def harness_sources():
    for pattern in ("*.py", "tests/*.py", "core/**/*.py", "pages/**/*.py"):
        for path in sorted(M.ROOT.glob(pattern)):
            if path.name.startswith(".") or "__pycache__" in path.parts:
                continue
            yield path


#: 判「这是一次子进程创建」：`subprocess.run/Popen/call/check_output/check_call(...)`。
SPAWN_CALLS = frozenset({"run", "Popen", "call", "check_output", "check_call"})


def spawn_sites(tree):
    for node in ast.walk(tree):
        func = getattr(node, "func", None)
        if (isinstance(node, ast.Call) and isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
                and func.attr in SPAWN_CALLS):
            yield node


def wears_no_window_hat(node) -> bool:
    """显式声明 creationflags（或展开 no_window_kw()）才算戴了帽子。

    只看「显式」：默认值在不同父进程下行为不一样（有控制台的父进程不弹、没有的每调一次
    弹一个），指望继承就是 2026-11-24 那场满屏黑框的成因。
    """
    return any(k.arg == "creationflags" or "no_window_kw" in ast.unparse(k.value) for k in node.keywords)


def visible_python_consoles() -> list[tuple[int, str]]:
    """当前**可见**的控制台窗口里，标题带 python 的那些（(hwnd, title)）。"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[tuple[int, str]] = []
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            name = ctypes.create_unicode_buffer(260)
            user32.GetClassNameW(hwnd, name, 260)
            title = ctypes.create_unicode_buffer(400)
            user32.GetWindowTextW(hwnd, title, 400)
            if "Console" in name.value and "python" in title.value.lower():
                found.append((hwnd, title.value))
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return found


#: 中间人：自己**没有控制台**（DETACHED_PROCESS），再按收到的 flags 拉一个孙子进程。
#: 复刻的就是「AstrBot 计划任务 / CI runner 拉流水线」的处境 —— 孙子弹不弹，全看帽子。
_PROBE = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(40)'], creationflags=int(sys.argv[1]))\n"
    "time.sleep(40)\n"
)


class NoConsoleWindow(unittest.TestCase):
    """C1~C3：流水线拉起的每一个子进程都不许弹黑框。

    缘起（2026-11-24，主人反馈）：跑一次门禁，python 黑框像潮水一样往外冒 ——
    回归 110 件 + UI 18 件 + 变异 165 条里每条一次子进程，每个都新建一个可见控制台窗口。
    判据分三层：C1 扫源码（漏一处就红），C2 先证明这把尺子量得出窗口，C3 再量帽子管用。
    """

    def test_c1_every_spawn_declares_creationflags(self):
        offenders = []
        for path in harness_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in spawn_sites(tree):
                if not wears_no_window_hat(node):
                    offenders.append(f"{path.relative_to(M.ROOT)}:{node.lineno} subprocess.{node.func.attr}()")
        self.assertEqual(offenders, [],
                         "子进程没显式声明 creationflags（Windows 上会弹黑框，见 tests/_no_window.py）：\n"
                         + "\n".join(offenders))

    def _probe_windows(self, child_flags: int) -> tuple[list, list]:
        """在「没有控制台的父进程」下起一个孙子进程，返回（起前窗口, 起后窗口）。"""
        before = visible_python_consoles()
        middle = subprocess.Popen([sys.executable, "-c", _PROBE, str(child_flags)],
                                  creationflags=subprocess.DETACHED_PROCESS)   # 显式：故意无控制台
        try:
            deadline = time.monotonic() + 8
            after = before
            while time.monotonic() < deadline:
                time.sleep(0.5)
                after = visible_python_consoles()
                if len(after) != len(before):
                    break
        finally:
            V.kill_tree(middle)
            with contextlib.suppress(Exception):        # 收尸：不留 ResourceWarning
                middle.wait(timeout=10)
        return before, after

    def test_c2_probe_sees_an_undeclared_console(self):
        """量尺自检：没有帽子的孙子进程，必须能量到它新开出来的窗口。"""
        if os.name != "nt":
            self.skipTest("黑框是 Windows 概念")
        before, after = self._probe_windows(0)
        self.assertGreater(len(after), len(before),
                           "量尺是坏的：没戴帽子的子进程都没能量到窗口，C3 就等于恒真")

    def test_c3_no_window_flag_suppresses_the_console(self):
        if os.name != "nt":
            self.skipTest("黑框是 Windows 概念")
        before, after = self._probe_windows(int(NO_WINDOW.CREATE_NO_WINDOW))
        self.assertEqual([t for _, t in after if t not in [x for _, x in before]], [],
                         "戴了 CREATE_NO_WINDOW 还弹窗 —— 说明帽子没送到子进程手上")



if __name__ == "__main__":
    unittest.main()
