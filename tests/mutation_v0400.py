"""TPS 探针的定向变异。"""
import sys

from mutation_all import Case


def mutation_cases():
    baseline = (sys.executable, "-B", "-X", "utf8", "tests/test_v0400_tps_probe.py")
    card = "core/server_card.py"
    parser = "core/tps_parse.py"
    variants = [
        ("TPS1", "另写一份 TPS 解析", card, "def _tps_field(result: TpsProbe) -> dict:",
         "def parse_tps_reply(raw):\n    return {}\n\n\ndef _tps_field(result: TpsProbe) -> dict:",
         "test_same_implementation", "TPS1 解析器只能定义一处"),
        ("TPS2", "把 Forge 首维度的 tick time 当 TPS", parser,
         'return {"tps_1m": float(match[2]), "mspt": float(match[1])}',
         'return {"tps_1m": float(re.search(r"Mean tick time: ([0-9.]+)", text)[1]), "mspt": float(match[1])}',
         "test_forge_overall", "TPS2 Forge 必须从 Overall 取 TPS 与 MSPT"),
        ("TPS3", "tick query 的目标速率冒充 TPS", parser,
         'result = {"mspt": float(match[1])}',
         'result = {"mspt": float(match[1]), "tps_1m": 20.0}',
         "test_only_mspt", "TPS3 tick query 只能产生 MSPT 与运行信息"),
        ("TPS3", "从 Paper MSPT 反推 TPS", parser,
         'return {"mspt": float(match[1])}',
         'return {"mspt": float(match[1]), "tps_1m": min(20.0, 1000 / float(match[1]))}',
         "test_only_mspt", "TPS3 mspt 只能产生 MSPT"),
        ("TPS4", "丢掉有效数字的来源", card,
         "return TpsProbe(STATE_OK, source, raw, note, tuple(attempts), **values)",
         'return TpsProbe(STATE_OK, "", raw, note, tuple(attempts), **values)',
         "test_chain_and_sources", "TPS4 每个数字必须带来源"),
        ("TPS5", "在 Forge 前插回 spark tps", card,
         '("tps", "forge tps", "mspt", "tick query")',
         '("tps", "spark tps", "forge tps", "mspt", "tick query")',
         "test_chain_and_sources", "TPS5 按顺序停止，禁用 spark RCON"),
        ("TPS6", "丢掉尝试的原始回包", card,
         "attempts.append(TpsAttempt(source, raw, state, note))",
         'attempts.append(TpsAttempt(source, "", state, note))',
         "test_chain_and_sources", "TPS6 保留每次尝试的完整原文"),
        ("TPS7", "空回包记成不支持", card,
         "elif not clean:\n            state = STATE_EMPTY",
         "elif not clean:\n            state = STATE_UNSUPPORTED",
         "test_different_failures", "TPS7 空回答独立于不支持和认不出"),
    ]
    return [
        Case(criterion, description, path, before, after, (*baseline, f"TpsChecks.{test}"), failure, baseline)
        for criterion, description, path, before, after, test, failure in variants
    ]
