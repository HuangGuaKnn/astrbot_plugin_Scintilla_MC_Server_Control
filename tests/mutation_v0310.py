"""v0.31.0：在隔离副本改坏巡检修复，每条只接受指定断言变红。"""
from __future__ import annotations

import argparse
import sys

from mutation_v0300 import CONSOLE, CONSOLE_TEST, HTML, PLAYER_TEST, UI_TEST, Mutation


PLAYER = "core/player_admin.py"
WORKFLOW = "core/workflow.py"
WORKFLOW_TEST = "tests/test_simple_workflow_result.py"
CONSOLE_CASE = "WebConsoleCheck.test_p22_world_write_subcommands_warn_without_flagging_queries"
MUTATIONS = (
    Mutation(
        "P21", "绕过封禁记录的 IP 校验", PLAYER,
        "            ip = normalize_ip(raw_ip)", "            ip = raw_ip",
        "P21 只有 normalize_ip 通过的目标可以解封", PLAYER_TEST,
        "PlayerAdminCheck.test_p21_invalid_ip_bans_remain_visible_without_unban_action",
    ),
    Mutation(
        "P21", "异常 IP 行重新渲染解封按钮", HTML,
        "    const actionCell = isIp && !canAct", "    const actionCell = false",
        "P21 异常 IP 行不得渲染解封按钮", UI_TEST, "P21",
    ),
    Mutation(
        "P22", "从 C 组删除 data remove", CONSOLE,
        '    ("data", "remove"): "会删除目标的 NBT 数据，删除不可撤销。",\n',
        "", "P22 写子命令须命中 C 组提示", CONSOLE_TEST, CONSOLE_CASE,
    ),
    Mutation(
        "P22", "降低 C 组写子命令的提示强度", CONSOLE,
        'warnings.append(_warning("world", world_change, "high"))',
        'warnings.append(_warning("world", world_change, "medium"))',
        "P22 C 组提示强度须为 high", CONSOLE_TEST, CONSOLE_CASE,
    ),
    Mutation(
        "P22", "删除不可撤销说明", CONSOLE,
        "会删除目标的 NBT 数据，删除不可撤销。", "会删除目标的 NBT 数据。",
        "P22 C 组提示须说明不可撤销", CONSOLE_TEST, CONSOLE_CASE,
    ),
    Mutation(
        "P22", "将 data get 误列为 C 组写操作", CONSOLE,
        '_WORLD_SUBCOMMANDS = {\n',
        '_WORLD_SUBCOMMANDS = {\n    ("data", "get"): "不可撤销。",\n',
        "P22 查询和引号中的命令文字不可触发写操作提示", CONSOLE_TEST, CONSOLE_CASE,
    ),
    Mutation(
        "P23", "将设置页总开关恢复为可编辑", HTML,
        'id="cfg_wf_enabled" disabled', 'id="cfg_wf_enabled"',
        "P23 设置页两个工作流开关必须只读", UI_TEST, "P23",
    ),
    Mutation(
        "P23", "设置页批量保存重新写入工作流总开关", HTML,
        '  ["cfg_wf_provider","llm_provider_id","s"],',
        '  ["cfg_wf_enabled","agent_workflow_enabled","b",true],\n'
        '  ["cfg_wf_provider","llm_provider_id","s"],',
        "P23 设置页批量保存不得包含工作流开关", UI_TEST, "P23",
    ),
    Mutation(
        "P23", "设置页跳转按钮未绑定", HTML,
        '  $("cfg_wf_jump").onclick = ()=>switchTab("workflow");', "",
        "P23 跳转必须打开工作流页", UI_TEST, "P23",
    ),
    Mutation(
        "P23", "保存过程中仍可再次操作开关", HTML,
        "  for(const control of controls) control.disabled = true;", "",
        "P23 保存期间两开关必须共同禁用", UI_TEST, "P23",
    ),
    Mutation(
        "P24", "跳过最近任务持久化", WORKFLOW,
        '        self._persist_recent_logs()', "",
        "P24 两条日志重建实例后仍可读取", WORKFLOW_TEST, "P24",
    ),
    Mutation(
        "P24", "取消 200 条保留上限", WORKFLOW,
        "        if len(self.logs) > RECENT_LOG_LIMIT:", "        if False:",
        "P24 210条后删去最旧一半", WORKFLOW_TEST, "P24",
    ),
    Mutation(
        "P24", "详情直接截断并写入真实 IP", WORKFLOW,
        '_redact(str(row.get("detail", row.get("detail_excerpt", ""))))[:200]',
        'str(row.get("detail", row.get("detail_excerpt", "")))[:200]',
        "P24 与审计同源遮蔽 IPv4/IPv6", WORKFLOW_TEST, "P24",
    ),
    Mutation(
        "P24", "将历史运行记录计入当前任务", HTML,
        'logs.filter(l=>l.status==="running" && !l.historical).length',
        'logs.filter(l=>l.status==="running").length',
        "P24 恢复的历史任务不得计为正在执行", UI_TEST, "P24",
    ),
)

# v0.33.0 专项与历史 117 条分开计数，全部随统一入口默认执行。
Q2_RULES = (
    ("fillbiome", "fillbiome 0 64 0 15 80 15 minecraft:plains", "生物群系",
     '    "fillbiome": "会改写整片区域的生物群系，恢复时需按原分布重新填充。",\n'),
    ("place structure", "place structure minecraft:village_plains 0 64 0", "覆盖现有方块",
     '    ("place", "structure"): "生成结构会覆盖现有方块，无法自动恢复原布局。",\n'),
    ("damage", "damage Steve 100 minecraft:generic", "可能致死",
     '    "damage": "会直接减少目标实体的生命值，可能致死。",\n'),
    ("worldborder add", "worldborder add -100", "困在墙外",
     '    ("worldborder", "add"): "会收放世界边界。收窄时边界墙可能越过玩家位置，把玩家困在墙外并造成伤害。",\n'),
    ("scoreboard objectives remove", "scoreboard objectives remove Example", "所有分数",
     '    ("scoreboard", "objectives", "remove"): "会删除整个计分板项目及其所有分数。",\n'),
    ("scoreboard players reset", "scoreboard players reset Steve", "全部分数",
     '    ("scoreboard", "players", "reset"): "会清除目标玩家的指定分数。省略计分板项目时会清除其全部分数。",\n'),
    ("advancement revoke", "advancement revoke Steve everything", "重置全部进度",
     '    ("advancement", "revoke"): "会撤销已有进度，使用 everything 会重置全部进度。",\n'),
    ("item modify", "item modify entity Steve weapon.mainhand example:change", "NBT",
     '    ("item", "modify"): "会修改背包或容器物品的 NBT 或组件，无法自动撤销。",\n'),
    ("loot replace", "loot replace entity Steve weapon.mainhand loot example:chest", "覆盖容器或实体",
     '    ("loot", "replace"): "会用战利品表生成的物品覆盖容器或实体的指定槽位。",\n'),
    ("experience set 0", "experience set @s 0", "清零",
     '        if (name in ("experience", "xp") and subcommand == "set" and len(words) > 3\n'
     '                and re.fullmatch(r"-?0+", words[3])):\n'
     '            world_change = "会将目标玩家指定的经验点数或等级清零，无法自动恢复。"\n'),
)


def verify_q2() -> int:
    from types import SimpleNamespace

    from _paths import add_sys_paths

    add_sys_paths()
    from astrbot_plugin_Scintilla_MC_Server_Control.core.web_console import preview

    plugin = SimpleNamespace(_cfg=lambda key, default: default)

    def warnings(command):
        result, = preview(plugin, command)["results"]
        assert result["accepted"], f"Q2 预览不应挡住完整模式中的 {command}"
        return result["warnings"]

    for label, command, wording, _ in Q2_RULES:
        world = [hint for hint in warnings(command) if hint["code"] == "world"]
        assert len(world) == 1 and world[0]["level"] == "high", f"Q2 {label} 必须命中 C 组 high"
        assert wording in world[0]["text"], f"Q2 {label} 缺少风险说明：{wording}"
    for command in (
        "experience add @s 100", "experience add @s 0 levels",
        "experience set @s 1", "experience query @s levels", "experience set @s",
        "experience set @s 0.0", "xp add @s 100",
        "scoreboard objectives list", "scoreboard players get Steve Example",
        "worldborder get", "place feature minecraft:oak 0 64 0",
        "advancement grant Steve everything", "loot give Steve loot example:chest",
    ):
        assert not warnings(command), f"Q2 该预览不应出现风险提示：{command}"
    for command in ("xp set @s -0 levels", "/minecraft:experience set @s 00 points",
                    "execute as @s run experience set @s 0 levels"):
        assert any(h["code"] == "world" and h["level"] == "high" for h in warnings(command)), \
            f"Q2 经验清零的别名、命名空间或嵌套未分类：{command}"
    print("[PASS] Q2 十条新增规则、相近非目标命令和经验清零边界")
    return 0


def v0330_cases():
    from mutation_all import Case

    python = (sys.executable, "-B", "-X", "utf8")
    q1 = python + ("tests/test_permission_gate.py",)
    q2 = python + ("tests/mutation_v0310.py", "--verify-q2")
    q4 = python + ("tests/ui_plain_format_check.py",)
    wording = "命令工具（执行指令 / 发物品 / 广播）默认仅管理员可用。"
    return [
        *(Case("Q1", f"拒绝文案遗漏{word}", "core/java_commands.py",
               wording, wording.replace(word, "其他操作"), q1,
               "Q1 拒绝文案应逐项说明被限制的能力")
          for word in ("执行指令", "发物品", "广播")),
        *(Case("Q2", f"删除 {label} 风险规则", CONSOLE, anchor, "", q2,
               f"Q2 {label} 必须命中 C 组 high", baseline=q2)
          for label, _, _, anchor in Q2_RULES),
        Case("Q4", "截图恢复到固定目录", "tests/_paths.py",
             '    return Path(tempfile.mkdtemp(dir=parent, prefix=f"{os.getpid()}-"))',
             '    return PLUGIN_DIR / "tests" / "_shots"', q4,
             "Q4 截图目录必须区分浏览器通道和进程"),
    ]


def main(argv: list[str]) -> int:
    from mutation_all import entry, registered_cases

    parser = argparse.ArgumentParser(description=__doc__)
    cases = registered_cases(sys.modules[__name__])
    parser.add_argument("--verify-q2", action="store_true", help="验证终端新增风险提示")
    parser.add_argument("criteria", nargs="*", help="P/T criterion IDs")
    args = parser.parse_args(argv)
    if args.verify_q2:
        return verify_q2()
    criteria = set(args.criteria)
    if criteria - {c.criterion for c in cases}:
        parser.error("unknown criterion")
    selected = [c for c in cases if not criteria or c.criterion in criteria]
    return entry({"v0310": selected}, legacy=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
