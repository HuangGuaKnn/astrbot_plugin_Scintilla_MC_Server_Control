"""v0.23.0 回归：插件配置的每个键都必须能穿越「保存 → 重载」。

为什么要有它：
  `notify_target_events`（各会话的播报内容）在 schema 里被声明成
  `"type": "object"` + 空 `items`。但 AstrBot 配置系统里：
    · `"object"` = 按 `items` 子键校验的**固定结构**；
    · `"dict"`   = 自由映射（用户自己往里加键，官方注释原文 "free-form mappings"）。
  于是每次 `AstrBotConfig` 加载（= 插件每次重载）都会跑 `check_config_integrity`，
  把用户加的会话键全当「过期条目」删掉再写回空 `{}`：

      [INFO] [config.astrbot_config:245] Config key removed:
             notify.notify_target_events.TestBot:FriendMessage:100000001

  症状（用户报的那条）：给群聊静音、私聊只留「指令调用」，一重载插件全部恢复默认全开。
  更隐蔽的是它在**内存里是好用的**（保存后即时生效），只有重载/重启才现形。

覆盖：
  1) 契约：schema 里任何 `type: "object"` 都必须带非空 `items`（空结构 = 会清用户数据的坑）；
     自由映射用途的 `notify_target_events` 必须是 `"dict"`；
  2) 文档对账（v0.23.5 补）：`docs/configure.md` 与 `README.md` 里写的分组数 / 项数 /
     逐条列出的配置键，必须与 schema 完全一致 —— 数字是给用户的承诺，不能每次加配置就漂；
  3) 真往返：真 `AstrBotConfig` + 真 schema，逐键写入探针值 → 重新加载 → 逐键比对；
  4) 定点：群聊静音 / 私聊只留指令调用，重载后必须一模一样。

用法：
  python tests\\test_config_persistence_contract.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths, require_app  # noqa: E402
add_sys_paths()
require_app()                    # AstrBot 运行目录：自动发现（见 tests/_paths.py）

try:
    from astrbot.core.config.astrbot_config import AstrBotConfig  # noqa: E402
    HAS_ABC, ABC_SKIP = True, ""
except Exception as _e:   # 环境里没有 AstrBot 运行时时跳过「真往返」，静态契约层照跑
    AstrBotConfig = None
    HAS_ABC, ABC_SKIP = False, f"{type(_e).__name__}: {_e}"

SCHEMA_PATH = PLUGIN_DIR / "_conf_schema.json"
FAILED: list[str] = []


def check(desc: str, cond: bool, extra: str = "") -> None:
    print(f"  {'✓' if cond else '✗'} {desc}{('  ← ' + str(extra)) if (extra and not cond) else ''}")
    if not cond:
        FAILED.append(desc)


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def probe_for(schema_node: dict):
    """按 schema 类型造一个「一眼能认出」的探针值（bool 取反，避免撞默认值）。"""
    ty = schema_node.get("type")
    if ty == "bool":
        return not bool(schema_node.get("default", False))
    if ty in ("string", "text"):
        return f"PROBE_{ty.upper()}"
    if ty == "int":
        return int(schema_node.get("default", 0) or 0) + 7
    if ty == "float":
        return float(schema_node.get("default", 0.0) or 0.0) + 0.5
    if ty == "list":
        return ["PROBE_LIST_ITEM"]
    if ty in ("dict", "object"):
        # object 也当自由映射写探针：空 items 的 object 正是「被误标成固定结构」的坑，
        # 必须让它也进往返比对，否则这个守卫看不见它。
        return {"PROBE_KEY": ["command"]}
    return None


def walk_schema(schema: dict, path: tuple = ()):
    """产出 (路径, schema节点)，只到叶子键（object 继续下钻）。"""
    for k, v in schema.items():
        if not isinstance(v, dict):
            continue
        items = v.get("items")
        if v.get("type") == "object" and isinstance(items, dict) and items:
            yield from walk_schema(items, path + (k,))
        else:
            # 叶子：含「object 但 items 为空」的键（它其实该是 dict，见模块头注释）
            yield path + (k,), v


def doc_group_sections(text: str) -> dict:
    """从 docs/configure.md 析出 {分组: (标注项数, [条目标题...])}。

    文档体例：`` `group` · 共 N 项 `` 起一段，下面每条 ``**`key`** — 说明``。
    """
    pat = re.compile(r"`(\w+)` · 共 (\d+) 项")
    marks = list(pat.finditer(text))
    out: dict = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end]
        out[m.group(1)] = (int(m.group(2)), re.findall(r"^\*\*`([^`]+)`\*\*", body, re.M))
    return out


def check_docs_match_schema(schema: dict) -> None:
    """v0.23.5（外部审查 D1）：文档里的「N 个分组、M 项」必须与 schema 对得上。

    为什么写进 CI：README 曾写「10 组、83 项」、configure.md 写「86 项」，
    而实际是 **93 项** —— 数字是给人看的**承诺**，对不上就等于文档在说谎，
    而且每次加配置都会再漂一次。这里把「说数」和「数数」绑死：

      · configure.md 每个分组的标注数 == 该分组的真实叶子键数；
      · configure.md 逐条列出的键名集合 == 该分组的真实键名集合（**一个都不能漏**）；
      · configure.md 开头的总数 == 各组之和；
      · README 文档表里的「全部 X 组、Y 项配置」== 同一组数字。
    """
    conf_md = (PLUGIN_DIR / "docs" / "configure.md").read_text(encoding="utf-8")
    readme = (PLUGIN_DIR / "README.md").read_text(encoding="utf-8")
    sections = doc_group_sections(conf_md)

    real = {g: [path[-1] for path, _ in walk_schema(node.get("items", {}))]
            for g, node in schema.items()}
    total = sum(len(v) for v in real.values())

    check("configure.md 覆盖了全部分组", set(sections) == set(real),
          f"文档缺 {sorted(set(real) - set(sections))}；文档多 {sorted(set(sections) - set(real))}")
    for g in real:
        if g not in sections:
            continue
        n_doc, listed = sections[g]
        check(f"configure.md · {g} 的项数标注正确（{len(real[g])}）", n_doc == len(real[g]),
              f"文档写 {n_doc}")
        missing = [k for k in real[g] if k not in listed]
        extra = [k for k in listed if k not in real[g]]
        check(f"configure.md · {g} 的条目一个不漏不多",
              not missing and not extra,
              f"缺 {missing}；多 {extra}")

    doc_total = re.search(r"共 \*\*(\d+) 个分组、(\d+) 项\*\*", conf_md)
    check("configure.md 开头的总数行存在", doc_total is not None)
    if doc_total:
        check(f"configure.md 的总数正确（{len(real)} 组、{total} 项）",
              (int(doc_total.group(1)), int(doc_total.group(2))) == (len(real), total),
              f"文档写 {doc_total.group(1)} 组 {doc_total.group(2)} 项")
    readme_num = re.search(r"全部 (\d+) 组、(\d+) 项配置", readme)
    check("README 文档表里的数字存在", readme_num is not None)
    if readme_num:
        check(f"README 的数字与 schema 一致（{len(real)} 组、{total} 项）",
              (int(readme_num.group(1)), int(readme_num.group(2))) == (len(real), total),
              f"README 写 {readme_num.group(1)} 组 {readme_num.group(2)} 项")


def main() -> int:
    schema = load_schema()

    print("[1] 契约：object 必须带非空 items；自由映射必须用 dict")
    bad_obj = [(".".join(p)) for p, v in walk_schema(schema)
               if v.get("type") == "object" and not (v.get("items") or {})]
    check("★没有「type=object + 空 items」的键（空结构会把用户数据当过期项清掉）",
          not bad_obj, "、".join(bad_obj))
    nte = schema["notify"]["items"]["notify_target_events"]
    check("★notify_target_events 用 type=dict（自由映射）", nte.get("type") == "dict",
          f"实际 {nte.get('type')!r}")

    print()
    print("[2] 文档：configure.md / README 的数字与条目必须与 schema 一致")
    check_docs_match_schema(schema)

    if not HAS_ABC:
        print(f"\n[3] 真往返：SKIP —— 本环境没有 AstrBotConfig（{ABC_SKIP}）")
        print("      （静态契约层已在上面跑过；用 AstrBot 自带解释器在本机跑时这层会真实执行）")
        return _finish()

    print("\n[3] 真往返：逐键写入探针 → AstrBotConfig 重新加载 → 逐键比对")
    leaves = list(walk_schema(schema))
    print(f"      schema 叶子键 {len(leaves)} 个")
    tmp = Path(tempfile.mkdtemp())
    conf_path = tmp / "roundtrip_config.json"

    seed = AstrBotConfig(str(tmp / "seed.json"), schema=schema)   # 借它生成 schema 骨架
    payload = json.loads(json.dumps(dict(seed), ensure_ascii=False))
    expected: dict[tuple, object] = {}
    for path, node in leaves:
        probe = probe_for(node)
        if probe is None:
            continue
        cur = payload
        for key in path[:-1]:
            cur = cur.setdefault(key, {})
        cur[path[-1]] = probe
        expected[path] = probe
    conf_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    cfg = AstrBotConfig(str(conf_path), schema=schema)            # ← 插件重载那一刻
    lost = []
    for path, want in expected.items():
        cur = cfg
        for key in path:
            cur = cur.get(key, "<缺失>") if isinstance(cur, dict) else "<缺失>"
        if cur != want:
            lost.append(f"{'.'.join(path)}: {want!r} → {cur!r}")
    check(f"★{len(expected)} 个键全部穿越重载（一个都不许被清）", not lost,
          "；".join(lost[:6]))

    print("\n[4] 定点：用户那次的场景")
    scene = {"TestBot:GroupMessage:200000002": [], "TestBot:FriendMessage:100000001": ["command"]}
    seed2 = AstrBotConfig(str(tmp / "seed2.json"), schema=schema)
    data = json.loads(json.dumps(dict(seed2), ensure_ascii=False))
    data["notify"]["notify_targets"] = list(scene.keys())
    data["notify"]["notify_target_events"] = json.loads(json.dumps(scene))
    p2 = tmp / "scene_config.json"
    p2.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg2 = AstrBotConfig(str(p2), schema=schema)                  # 重载
    got = json.loads(json.dumps(cfg2.get("notify", {}).get("notify_target_events", "<缺失>")))
    check("★群聊静音 / 私聊只留指令调用，重载后原样保留", got == scene, json.dumps(got, ensure_ascii=False))
    check("目标列表也在", cfg2.get("notify", {}).get("notify_targets") == list(scene.keys()),
          json.dumps(cfg2.get("notify", {}).get("notify_targets"), ensure_ascii=False))

    return _finish()


def _finish() -> int:
    print("==========================================")
    if FAILED:
        print(f"FAILED {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("全部通过：配置的每个键都能穿越重载，播报内容不会再被静默清空")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
