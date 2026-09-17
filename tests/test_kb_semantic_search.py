"""语义增强检索（knowledge_semantic_search）的离线回归测试。

不打任何网络请求、不依赖真实嵌入模型：用「确定性桩向量」把语义通道
跑通，验证三件事：

  1. 开关语义通道后，两条通道确实经由 RRF 融合（真命中会被抬到榜首）；
  2. 余弦相似度门槛 SEMANTIC_MIN_SIM 真的拦得住「整库都不相关」的查询
     （查询向量与库内任一条都远低于门槛 → 语义通道一条候选都不给）；
  3. 换嵌入模型（维度变了）时不会拿旧向量硬算 —— 直接放弃语义通道，
     退化为纯 BM25，而不是抛异常或算出一堆瞎结果。

运行：
  python tests\\test_kb_semantic_search.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import knowledge_base as KB  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (  # noqa: E402
    SEMANTIC_MIN_SIM,
    ModKnowledgeBase,
)

PASS = 0
FAIL = 0
DIM = 16


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


def vec(*hot: int) -> list[float]:
    """构造确定性桩向量：hot 位为 1、其余为 0，便于精确控制余弦相似度。"""
    v = [0.0] * DIM
    for i in hot:
        v[i] = 1.0
    return v


def make_kb(tmp: Path, enabled: bool = True):
    kb = ModKnowledgeBase(str(tmp), server_id="stub", preset_id="p",
                          semantic_enabled=enabled)
    return kb


def build(tmp: Path, entries: dict[str, tuple[str, list[float]]], enabled=True):
    """entries: topic -> (content, 向量)。"""
    kb = make_kb(tmp, enabled)
    for topic, (content, _) in entries.items():
        kb.save_entry(topic, content, status="verified")

    async def embed(texts):
        out = []
        for t in texts:
            # 建库传入的是 "topic content"（topic 已被 _topic_key 归一化成小写），
            # 查询传入的是用户原话 —— 所以这里必须忽略大小写比对
            low = t.lower()
            for topic, (content, v) in entries.items():
                tl = topic.lower()
                if low.startswith(tl) or low == tl:
                    out.append(v)
                    break
            else:
                out.append(_QUERY_VEC.get(t, vec()))
        return out

    kb.embed_fn = embed
    return kb


_QUERY_VEC: dict[str, list[float]] = {}


def build_vectors(kb):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(kb.build_vectors())


def main() -> int:
    print("[1] 语义通道确实参与融合：口语改写查询能捞到真命中")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {
            # 真命中：主题里没有任何「后台 / 连不上」字样，BM25 捞不到
            "运维 RCON 连接失败排查": ("检查端口映射与密码", vec(1, 2)),
            "运维 备份策略": ("定期打包 world 目录", vec(3, 4)),
            "运维 内存调优": ("调整 Xmx 参数", vec(5, 6)),
        }
        q = "后台那边怎么都连不上了"
        _QUERY_VEC.clear()
        _QUERY_VEC[q] = vec(1, 2)          # 与第一条同向 → 余弦 1.0
        kb_off = build(tmp / "off", entries, enabled=False)
        kb_on = build(tmp / "on", entries, enabled=True)
        build_vectors(kb_on)

        off = [r["topic"] for r in kb_off.search(q, limit=6)]
        on = [r["topic"] for r in kb_on.search(q, limit=6, query_vec=_QUERY_VEC[q])]
        gold = kb_on._topic_key("运维 RCON 连接失败排查")   # 主题会被归一化成小写
        check("关：纯 BM25 拿不到该条（字面零命中）",
              all(kb_off._topic_key(t) != gold for t in off), f"→ {off}")
        check("开：语义通道把它送到榜首",
              on and kb_on._topic_key(on[0]) == gold, f"→ {on}")

    print("[2] 相似度门槛：整库都不相关的查询不该带出噪声")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {f"条目 {i}": (f"内容 {i}", vec(i)) for i in range(1, 6)}
        kb = build(tmp, entries, enabled=True)
        build_vectors(kb)
        # 方向与所有条目都正交 → 余弦全为 0，低于门槛
        far = vec()
        sims = kb._sem.matrix @ (KB._np.asarray(far, dtype="float32"))
        check("桩向量：与库内最高余弦为 0（必然低于门槛）",
              float(sims.max()) == 0.0, f"max={sims.max()}")
        hits = kb.search("今天午饭吃什么", limit=6, query_vec=far)
        check("门槛生效：无关查询返回 0 条", len(hits) == 0, f"→ {len(hits)} 条")
        sim = kb._sem.rank(far)
        check("语义通道本身也返回空（门槛拦在融合之前）",
              sim == [], f"→ {sim}")

    print("[3] 门槛与候选池的边界：刚好达标要进来、差一点要被挡")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # v1 = e1（余弦 1.0）；v2 = cos(θ) 恰好略低于门槛
        import math
        lo = SEMANTIC_MIN_SIM - 0.02
        hi = SEMANTIC_MIN_SIM + 0.02
        v_hi = [hi, math.sqrt(max(1 - hi * hi, 0.0))] + [0.0] * (DIM - 2)
        v_lo = [lo, math.sqrt(max(1 - lo * lo, 0.0))] + [0.0] * (DIM - 2)
        entries = {"达标条目": ("a", v_hi), "差一点条目": ("b", v_lo)}
        kb = build(tmp, entries, enabled=True)
        build_vectors(kb)
        kept = kb._sem.rank([1.0] + [0.0] * (DIM - 1))
        check(f"余弦 {hi:.2f}（>{SEMANTIC_MIN_SIM}）留在候选池里",
              "达标条目" in kept, f"→ {kept}")
        check(f"余弦 {lo:.2f}（<{SEMANTIC_MIN_SIM}）被门槛挡掉",
              "差一点条目" not in kept, f"→ {kept}")

    print("[4] 换嵌入模型（维度不同）→ 放弃语义通道，退化为纯 BM25，不报错")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {f"条目 {i}": (f"内容 {i}", vec(i)) for i in range(1, 6)}
        kb = build(tmp, entries, enabled=True)
        build_vectors(kb)
        wrong = [1.0] * 8                     # 维度对不上
        check("rank() 直接返回空，不抛异常", kb._sem.rank(wrong) == [])
        hits = kb.search("条目 1", limit=6, query_vec=wrong)
        topics = [r["topic"] for r in hits]
        check("检索整体不报错，且仍能给出 BM25 结果",
              bool(hits) and "条目 1" in topics, f"→ {topics}")

    print("[5] 关掉开关：不再加载向量、检索不碰语义通道")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        entries = {"运维 备份策略": ("定期打包", vec(1))}
        kb = build(tmp, entries, enabled=False)
        check("semantic_enabled=False", kb.semantic_enabled is False)
        check("向量索引未加载（_sem 为 None）", kb._sem is None)
        check("semantic_ready() 为 False", kb.semantic_ready() is False)
        hits = kb.search("备份", limit=6, query_vec=vec(1))
        check("带 query_vec 也不走语义通道",
              [r["topic"] for r in hits] == ["运维 备份策略"])

    print("[6] 批量写入性能：整批拼接，不是逐条 vstack")
    if KB._np is None:
        print("  - 跳过（环境无 numpy）")
    else:
        import time
        n = 2000
        kb = ModKnowledgeBase(str(Path(tempfile.mkdtemp())), server_id="s",
                              preset_id="p", semantic_enabled=True)
        entries = {f"t{i}": {"content": f"c{i}"} for i in range(n)}
        for i in range(n):
            kb._data["entries"][kb._topic_key(f"t{i}")] = {"content": f"c{i}"}
        kb._sem = KB.SemanticIndex()
        batch = [[float((i * 7 + j) % 11) for j in range(64)] for i in range(n)]
        t0 = time.perf_counter()
        for i in range(0, n, 16):
            kb._sem.set_vectors([f"t{k}" for k in range(i, i + 16)],
                                entries, batch[i:i + 16])
        dt = time.perf_counter() - t0
        check(f"{n} 条向量入库耗时 {dt:.2f}s（< 3s，逐条 vstack 需 10s+）",
              dt < 3.0 and kb._sem.matrix.shape == (n, 64), f"shape={kb._sem.matrix.shape}")

    print(f"\n{'✅ 全部通过' if FAIL == 0 else '❌ 有失败项'}：{PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
