"""验证流水线自身的定向变异，独立于既有 165 条。"""
import sys

from mutation_all import Case


def mutation_cases():
    baseline = (sys.executable, "-B", "-X", "utf8", "tests/test_v0390_verify_pipeline.py")
    return [
        Case(
            "E3", "增量选区漏掉最后一条", "tests/mutation_all.py",
            "        chosen = [item for path in sorted(paths) for item in groups[path]]",
            "        chosen = [item for path in sorted(paths) for item in groups[path]][:-1]",
            (*baseline, "VerifyPipeline.test_selection_uses_all_registered_cases_for_a_diff"),
            "E3 增量选区必须完整", baseline,
        ),
        Case(
            "E5", "子进程失败后跳过工作副本还原", "tests/mutation_all.py",
            "    finally:\n        path.write_bytes(raw)",
            "    finally:\n        pass",
            (*baseline, "VerifyPipeline.test_restore_after_test_process_fails"),
            "E5 失败后目标文件 SHA 必须还原", baseline,
        ),
    ]
