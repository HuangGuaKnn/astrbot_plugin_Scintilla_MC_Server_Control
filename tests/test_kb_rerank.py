"""重排序精排（knowledge_rerank）的离线回归测试。

不打任何网络请求、不依赖真实 Rerank 模型：用「确定性桩打分器」把精排通道
跑通，验证七件事：

  1. 精排能把「召回到了、但排在后面」的真命中提到榜首；
  2. 与语义通道相互独立 —— 没有嵌入模型也能单独精排（semantic 关着照样工作）；
  3. 精排只对候选池前 RERANK_POOL 条打分（池子有上限，不会整库丢给模型）；
  4. 桩只回了部分候选时，未打分的候选按原召回顺序兜底（不丢条目）；
  5. rerank 调用抛异常 → 安静退回普通检索（精排不该让整次检索失败）；
  6. 开关关闭 → 完全不调用 rerank 调用（零额外开销）；
  7. 精排路径下模板补召照样生效（与普通检索同口径）。

运行：
  python tests\\test_kb_rerank.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (  # noqa: E402
    RERANK_POOL,
    ModKnowledgeBase,
)

PASS = 0
FAIL = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def make_kb(tmp: Path, rerank: bool = True, **kw) -> ModKnowledgeBase:
    return ModKnowledgeBase(str(tmp), server_id="stub", preset_id="p",
                            rerank_enabled=rerank, **kw)


def gold_scorer(query: str, docs: list[str]):
    """桩打分：文档里含 gold 标记的给 10 分，其余 0 分。"""
    return [(i, 10.0 if "gold" in d.lower() else 0.0) for i, d in enumerate(docs)]


def make_rerank(calls: list, scorer=gold_scorer, fail: bool = False):
    async def _rerank(query: str, docs: list[str], **_kw):
        calls.append(list(docs))
        if fail:
            raise RuntimeError("rerank boom（模拟模型挂了）")
        return scorer(query, docs)

    return _rerank


def run(coro):
    return asyncio.run(coro)


def topics(rows) -> list[str]:
    return [r["topic"] for r in rows]


def main() -> int:
    print("[1] 精排把「召回到了但排后面」的真命中提到榜首")
    with tempfile.TemporaryDirectory() as td:
        kb = make_kb(Path(td))
        # 干扰项：主题里连写三个 apple → BM25 主题加权后分数很高，稳坐第一
        kb.save_entry("apple apple apple 方案", "无关内容", status="verified")
        # 真命中：主题不含 apple，只在正文里出现一次 → 能召回，但排在后面
        kb.save_entry("gold 特制配方", "正文里提到 apple 一次", status="verified")
        calls: list = []
        kb.rerank_fn = make_rerank(calls)

        plain = topics(kb.search("apple", limit=6))
        reranked = topics(run(kb.asearch("apple", limit=6)))
        gold = kb._topic_key("gold 特制配方")
        p0 = plain.index(gold) if gold in plain else -1
        check("前提：普通检索里真命中不在榜首（否则本用例无意义）",
              p0 > 0, f"→ {plain}")
        check("精排后：真命中被提到榜首",
              reranked and reranked[0] == gold, f"→ {reranked}")
        check("精排确实调用了 rerank 调用（1 次）", len(calls) == 1, f"→ {len(calls)}")
        check("送给模型的文档是「主题 + 正文」口径",
              bool(calls) and any("gold" in d.lower() for d in calls[0]),
              f"→ {calls[:1]}")

    print("[2] 与语义通道相互独立：没有嵌入模型也能单独精排")
    with tempfile.TemporaryDirectory() as td:
        kb = make_kb(Path(td))
        kb.save_entry("gold 条目", "内容", status="verified")
        kb.rerank_fn = make_rerank([])
        check("语义通道没开（semantic_ready 为 False）", kb.semantic_ready() is False)
        check("精排通道可用（rerank_ready 为 True）", kb.rerank_ready() is True)
        check("没注入 embed_fn 时仍能给出精排结果",
              "gold 条目" in topics(run(kb.asearch("gold", limit=6))))

    print("[3] 候选池有上限：只把前 RERANK_POOL 条丢给模型")
    with tempfile.TemporaryDirectory() as td:
        kb = make_kb(Path(td))
        for i in range(RERANK_POOL + 10):
            kb.save_entry(f"apple 条目{i}", f"内容{i}", status="verified")
        calls: list = []
        kb.rerank_fn = make_rerank(calls)
        run(kb.asearch("apple", limit=6))
        check(f"候选池截断到 {RERANK_POOL} 条",
              len(calls) == 1 and len(calls[0]) == RERANK_POOL,
              f"→ {len(calls[0]) if calls else 0}")

    print("[4] 模型只回部分候选 → 未打分的按原召回顺序兜底，不丢条目")
    with tempfile.TemporaryDirectory() as td:
        kb = make_kb(Path(td))
        for i in range(4):
            kb.save_entry(f"apple 条目{i}", f"内容{i}", status="verified")
        pool = kb._rank_candidates("apple")[:RERANK_POOL]
        # 只给池里「第 2 条」打分，其余一条都不回
        kb.rerank_fn = make_rerank([], scorer=lambda q, d: [(1, 99.0)])
        reranked = topics(run(kb.asearch("apple", limit=6)))
        check("被打了高分的第 2 条排到首位", reranked and reranked[0] == pool[1],
              f"→ {reranked}")
        check("其余候选全部保留（不因未打分而丢失）",
              len(reranked) == len(pool), f"→ {len(reranked)} vs {len(pool)}")
        check("未打分候补顺序 = 原召回顺序",
              reranked[1:] == [t for i, t in enumerate(pool) if i != 1],
              f"→ {reranked}")

    print("[5] rerank 调用抛异常 → 安静退回普通检索，不报错")
    with tempfile.TemporaryDirectory() as td:
        kb = make_kb(Path(td))
        kb.save_entry("apple apple 方案", "内容", status="verified")
        kb.save_entry("apple 另一条", "内容", status="verified")
        kb.rerank_fn = make_rerank([], fail=True)
        plain = topics(kb.search("apple", limit=6))
        got = topics(run(kb.asearch("apple", limit=6)))
        check("异常被吞掉，结果与普通检索一致", got == plain, f"→ {got} vs {plain}")

    print("[6] 开关关闭 → 完全不调用 rerank 调用")
    with tempfile.TemporaryDirectory() as td:
        kb = make_kb(Path(td), rerank=False)
        kb.save_entry("gold 条目", "内容", status="verified")
        calls: list = []
        kb.rerank_fn = make_rerank(calls)
        check("rerank_ready() 为 False", kb.rerank_ready() is False)
        check("asearch 结果照常返回",
              "gold 条目" in topics(run(kb.asearch("gold", limit=6))))
        check("一次都没调用模型（零额外开销）", calls == [], f"→ {len(calls)} 次")

    print("[7] 精排路径下模板补召照常生效（与普通检索同口径）")
    with tempfile.TemporaryDirectory() as td:
        kb = make_kb(Path(td))
        kb.save_entry("tac 满配 ak 方案", "命令模板：/give @s tac:ak47", status="verified")
        for k, e in kb._searchable().items():
            kb._data["entries"][k]["mod"] = "tac"
            kb._data["entries"][k]["kind"] = "template"
        kb.save()
        kb.rerank_fn = make_rerank([])
        got = run(kb.asearch("tac 怎么满配", limit=6))
        check("精排路径也能补召到同模组模板条目",
              "tac 满配 ak 方案" in topics(got), f"→ {topics(got)}")

    print("[8] 配置键贯通：schema / 后端白名单 / 热生效 / 前端绑定 必须一致")
    root = Path(__file__).resolve().parent.parent
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    node = ((schema.get("knowledge") or {}).get("items") or {}).get("knowledge_rerank")
    check("schema 里有 knowledge_rerank", isinstance(node, dict), f"→ {node}")
    if isinstance(node, dict):
        check("schema type=bool / default=false",
              node.get("type") == "bool" and node.get("default") is False,
              f"→ {node.get('type')} / {node.get('default')}")
    web = (root / "core" / "web_api.py").read_text(encoding="utf-8")
    check("后端保存白名单含该键（缺了保存会被丢弃）",
          '"knowledge_rerank",' in web, "")
    check("后端保存后触发热生效（切换 + 注入 rerank 调用）",
          "set_rerank_enabled" in web and "_inject_rerank_fn" in web)
    main_src = (root / "main.py").read_text(encoding="utf-8")
    check("main.py 读配置 knowledge_rerank", '_cfg("knowledge_rerank", False)' in main_src)
    check("main.py 下发到 KnowledgePresetManager",
          "rerank_enabled=self._kb_rerank()" in main_src)
    check("main.py 启动时注入（_inject_rerank_fn 定义 + 调用）",
          "def _inject_rerank_fn" in main_src and "self._inject_rerank_fn()" in main_src)
    html = (root / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
    check("前端设置页有开关 cfg_kb_rerank", 'id="cfg_kb_rerank"' in html)
    check("前端 CFG_FIELDS 已挂绑定",
          '["cfg_kb_rerank","knowledge_rerank","b",false]' in html)
    kb_src = (root / "core" / "knowledge_base.py").read_text(encoding="utf-8")
    check("KB 实例支持 rerank_enabled 参数",
          "rerank_enabled: bool = False" in kb_src)
    check("预设管理器支持热切换（set_rerank_enabled）",
          "def set_rerank_enabled" in kb_src)

    print(f"\n{'✅ 全部通过' if FAIL == 0 else '❌ 有失败项'}：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
