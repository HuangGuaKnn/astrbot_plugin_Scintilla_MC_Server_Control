"""验证流水线的选区、并行、还原与墙钟账单。"""
import contextlib
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import mutation_all as M

sys.path.insert(0, str(M.ROOT))
import run_release_verify as V


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


if __name__ == "__main__":
    unittest.main()
