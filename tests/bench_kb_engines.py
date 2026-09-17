"""知识库检索引擎评测 v2：legacy / bm25 / dense(嵌入) / RRF混合 / RRF+rerank。

v1 教训（已记录，避免重蹈覆辙）：
  第一版混合用「min-max 归一化加权 + 0.35 硬门槛」，结果语义型查询 Hit@1 = 0%。
  验尸结论：gold 只在单通道强（BM25 零命中、dense 第 439 名），归一化后融合分
  远低于「最高分 × 0.35」的门槛，被整条砍掉。**加权融合 + 硬门槛**对
  「单通道强项」有系统性误杀；改用 RRF（倒数排名融合）——只看名次不看分数尺度，
  天然免疫归一化问题，是业界混合检索的标准做法。

本版评测口径：
  语料：442 条（20 把枪满配 / 机械动力配方 / 运维知识 / 300 条套话噪声 / 异模组）。
  查询：12 条字面型（问法含条目关键词）+ 12 条语义型（口语改写，gold 唯一可辨）。
  指标：Hit@1 / Hit@6（对齐 KB_RESULT_LIMIT=6）、MRR、精确率@6、延迟、误召回。

运行：
  python tests\\bench_kb_engines.py
"""
from __future__ import annotations

import json
import os
import random
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (  # noqa: E402
    ModKnowledgeBase,
)

FP = "bench0000fp"
PROXY = os.environ.get("PIRIKA_PROXY", "http://127.0.0.1:7897")
CFG = Path(os.environ.get("ASTRBOT_CFG", r"C:\Users\10316\.astrbot\data\cmd_config.json"))
K = 6          # 与 workflow.KB_RESULT_LIMIT 对齐
RRF_K = 60     # RRF 平滑常数（业界默认）
RERANK_POOL = 20


# ==================== 语料 ====================

GUNS = ["ak47", "m4a1", "sig_mcx_spear", "hk_mp5a5", "scar_h", "aug", "m249",
        "vector", "ump45", "p90", "famas", "g36c", "mp7", "ak74", "m16a4",
        "deagle", "glock17", "m1911", "awp", "m700"]
SLOTS = ["Scope", "Muzzle", "Barrel", "Grip", "Magnum"]
PARTS = ["eotech_holographic", "muzzle_brake", "vertical_grip", "extended_mag",
         "tactical_stock", "laser_sight", "sniper_scope", "angled_grip"]

CREATE_RECIPES = [
    ("铜锭 + 锌锭", "混合器", "黄铜锭"),
    ("黄铜锭 + 铁锭", "压力机", "精密构件"),
    ("安山岩 + 铁粒", "混合器", "安山合金"),
    ("木板 + 铁锭", "机械臂", "传送带"),
    ("铜锭 + 铁板", "压力机", "铜齿轮"),
    ("岩浆 + 水", "液体混合", "黑曜石"),
    ("小麦 + 水", "搅拌器", "面团"),
    ("石头 + 烈焰粉", "混合器", "焦炭"),
]
OPS = [
    ("RCON 连接失败排查", "依次确认 enable-rcon、rcon.port、rcon.password 与防火墙端口放行；改完必须重启服务端"),
    ("服务器白名单配置", "whitelist.json 增删后执行 /whitelist reload；online-mode 必须为 true"),
    ("整合包指纹计算方式", "对 mods/ 目录下所有 jar 的文件名与大小做哈希，换整合包指纹自动变化"),
    ("TPS 低排查顺序", "先看 /forge tps 定位维度，再用 spark profiler 采样；常见元凶是刷怪塔与漏斗链"),
    ("备份策略", "world 与 config 目录按每日增量、每周全量；备份前先 /save-all flush"),
    ("玩家数据回档", "从 usercache.json 定位 UUID，替换 playerdata/<uuid>.dat 后重启"),
    ("权限组配置", "LuckPerms 中 default 组默认只给 essential 权限；管理员用 op 组继承"),
    ("内存调优", "user_jvm_args.txt 设置 Xmx 为物理内存 60%；Xms 与 Xmx 相同可减少 GC 抖动"),
    ("模组冲突定位", "看 latest.log 首个 Caused by 栈帧；二分法禁用一半模组可快速定位"),
    ("世界边界设置", "worldborder set 直径 后再用 center 调整中心点"),
]
FILLER = ["通用 指令 格式 规则", "通用 方案 参考 模板", "通用 注意事项 说明",
          "常见问题 通用 处理 方式", "标准 流程 参考 规范", "通用 排查 思路 规则"]
NOISE_TEMPLATES = [
    "本条为通用说明，给出常见书写格式与参考思路，第 {n} 条示例，具体数值以实际为准",
    "通用提醒：不同版本间字段名可能变化，遇到解析失败请先核对版本号与文档（#{n}）",
    "本条目记录一次通用排查的经验：先定位现象、再缩小范围、最后逐项验证（序号 {n}）",
    "写作规范：命令中的占位符用尖括号标注，玩家名一律取自在线名单（样例 {n}）",
    "注意事项汇总：注意大小写、注意空格、注意引号转义，三条都过一遍再执行（{n}）",
    "通用参考模板：这类任务通常是「查 → 试 → 校验」三步走，可直接套用（{n}）",
    "历史遗留记录：早期版本的处理方式与现在不同，仅作参考，不建议直接照抄（{n}）",
    "综合说明条目：涉及多个模组时请分步处理，避免一次性堆叠复杂 NBT（{n}）",
]
OTHER_MODS = [
    ("farmersdelight 厨锅 配方", "厨锅需要下方热源，放入食材后等待烹饪进度条满"),
    ("mekanism 富集仓 用法", "富集仓通入能量后处理矿石，产出两份矿粉"),
    ("ae2 存储总线 配置", "存储总线面向 ME 驱动器时优先级需高于默认值"),
    ("botania 魔力池 合成", "魔力池需花药台提供魔力，投掷物品触发合成"),
    ("jei 隐藏物品", "在 JEI 界面按 Ctrl+O 可隐藏当前物品"),
    ("ftbquests 任务奖励", "在章节编辑器中右键任务可配置奖励物品与经验"),
    ("kubejs 自定义配方", "在 server_scripts 中调用 ServerEvents.recipes 注册"),
    ("curios 饰品栏", "饰品槽位由 curios 数据包定义，非代码硬编码"),
]

# 字面型：问法含条目关键词
QUERIES_LITERAL = [
    ("ak47 满配 槽位 怎么装", "tac 满配 ak47 方案"),
    ("m4a1 的 Barrel 用什么", "tac 满配 m4a1 方案"),
    ("黄铜锭 配方 是什么", "create 黄铜锭 配方"),
    ("精密构件 怎么造", "create 精密构件 配方"),
    ("安山合金 材料", "create 安山合金 配方"),
    ("RCON 连不上 怎么办", "运维 RCON 连接失败排查"),
    ("服务器 TPS 低 查哪里", "运维 TPS 低排查顺序"),
    ("白名单 怎么加人", "运维 服务器白名单配置"),
    ("备份 怎么做", "运维 备份策略"),
    ("内存 怎么调", "运维 内存调优"),
    ("厨锅 怎么用", "farmersdelight 厨锅 配方"),
    ("富集仓 怎么处理矿石", "mekanism 富集仓 用法"),
]

# 语义型：口语改写，gold 唯一可辨（异模组/运维知识，不与 20 把枪混淆）
QUERIES_SEMANTIC = [
    ("游戏里连不上后台控制台了", "运维 RCON 连接失败排查"),
    ("服务器卡顿掉帧去哪看", "运维 TPS 低排查顺序"),
    ("想让指定玩家才能进服", "运维 服务器白名单配置"),
    ("存档怕丢要怎么防", "运维 备份策略"),
    ("开服吃内存 参数怎么给", "运维 内存调优"),
    ("换个整合包 以前攒的经验会不会串", "运维 整合包指纹计算方式"),
    ("玩家的数据文件在哪个目录", "运维 玩家数据回档"),
    ("权限怎么分才安全", "运维 权限组配置"),
    ("装了一堆模组开服就崩 怎么找是哪个", "运维 模组冲突定位"),
    ("想限制玩家能走多远", "运维 世界边界设置"),
    ("烹饪锅怎么烧东西", "farmersdelight 厨锅 配方"),
    ("矿石处理能多出粉的机器", "mekanism 富集仓 用法"),
]
QUERIES_NONE = ["zzz_never_exists_qqq", "今天午饭吃什么", "帮我写一首诗"]


def build_corpus(n_filler: int = 300):
    rng = random.Random(20260917)
    out = []
    for g in GUNS:
        slots = rng.sample(SLOTS, 3)
        body = "；".join(f"{s}={rng.choice(PARTS)}" for s in slots)
        out.append((f"tac 满配 {g} 方案",
                    f"命令 /give @s tac:{g}{{Attachments:{{{body}}}}}；槽位键名大写驼峰",
                    "tac", "instance"))
    for name in [r[2] for r in CREATE_RECIPES]:
        ing, mach, _ = next(r for r in CREATE_RECIPES if r[2] == name)
        out.append((f"create {name} 配方",
                    f"{name} 由 {ing} 在 {mach} 产出；注意连续生产时传送带需供料稳定",
                    "create", "instance"))
    # 噪声：长期服务器会积累大量「模板/说明」类套话条目。
    # 刻意做成多样化文本——若内容全同，向量会互相 0.88 相似度，评测会失真。
    for i in range(n_filler):
        t = FILLER[i % len(FILLER)]
        body = NOISE_TEMPLATES[i % len(NOISE_TEMPLATES)].format(n=i)
        out.append((f"{t} #{i}", body, "general", "template"))
    for topic, content in OPS:
        out.append((f"运维 {topic}", content, "general", "instance"))
    # 异模组条目：每个模组第一条用「正题」（无 #i 后缀）便于作为 gold，
    # 其余作为噪声副本；否则评测的 gold 根本不在语料里（v2 首跑的踩坑点）
    for i in range(n_filler // 3):
        topic, content = OTHER_MODS[i % len(OTHER_MODS)]
        t = topic if i < len(OTHER_MODS) else f"{topic} #{i}"
        out.append((t, content, topic.split()[0], "instance"))
    return out


def build_kb(tmp: Path, corpus, engine: str):
    kb = ModKnowledgeBase(str(tmp), server_id=FP, preset_id="p1",
                          preset_name="bench", fingerprint=FP, search_engine=engine)
    for topic, content, mod, kind in corpus:
        kb.save_entry(topic, content, source="seed", status="verified", manual=True)
        key = kb._topic_key(topic)
        kb._data["entries"][key]["mod"] = mod
        kb._data["entries"][key]["kind"] = kind
    kb.save()
    return kb


# ==================== Provider 调用 ====================

def _providers():
    cfg = json.loads(CFG.read_text(encoding="utf-8-sig"))
    return {p["id"]: p for p in cfg["provider"]}


def _opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"https": PROXY, "http": PROXY}))


EMBED_MODEL = os.environ.get("PIRIKA_EMBED_MODEL", "Qwen/Qwen3-Embedding-4B")
# Qwen3-Embedding 系列是指令感知模型：查询侧加 instruct 前缀有小幅增益
INSTRUCT = ("Instruct: Given a Chinese question about a Minecraft modded server, "
            "retrieve the knowledge entry that answers it.\nQuery: ")


def embed_texts(texts: list[str], batch: int = 16) -> list[list[float]]:
    p = dict(_providers()["siliconflow_embedding"])
    p["embedding_model"] = EMBED_MODEL
    url = p["embedding_api_base"].rstrip("/") + "/embeddings"
    op = _opener()
    out = []
    for i in range(0, len(texts), batch):
        req = urllib.request.Request(
            url, data=json.dumps({"model": p["embedding_model"],
                                  "input": texts[i:i + batch],
                                  "encoding_format": "float"}).encode(),
            headers={"Authorization": f"Bearer {p['embedding_api_key']}",
                     "Content-Type": "application/json"})
        res = json.loads(op.open(req, timeout=120).read())
        out.extend(r["embedding"] for r in sorted(res["data"], key=lambda r: r["index"]))
    return out


def rerank_scores(query: str, docs: list[str]) -> list[float]:
    p = _providers()["siliconflow_rerank"]
    url = p["rerank_api_base"].rstrip("/") + p["rerank_api_suffix"]
    req = urllib.request.Request(
        url, data=json.dumps({"model": p["rerank_model"], "query": query,
                              "documents": docs, "top_n": len(docs)}).encode(),
        headers={"Authorization": f"Bearer {p['rerank_api_key']}",
                 "Content-Type": "application/json"})
    res = json.loads(_opener().open(req, timeout=60).read())
    scores = [0.0] * len(docs)
    for item in res.get("results", []):
        scores[item["index"]] = float(item["relevance_score"])
    return scores


def cosine_top(qv, mat) -> list[float]:
    import numpy as np
    q = np.asarray(qv, dtype="float32")
    m = np.asarray(mat, dtype="float32")
    q /= (np.linalg.norm(q) + 1e-9)
    m /= (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)
    return (m @ q).tolist()


# ==================== 评测 ====================

def evaluate(name: str, rank_fn, queries) -> dict:
    hit1 = hitk = 0
    rr = 0.0
    prec = []
    lat = []
    for q, gold in queries:
        t0 = time.perf_counter()
        ranked = rank_fn(q)
        lat.append((time.perf_counter() - t0) * 1000)
        top = ranked[:K]
        if top and top[0] == gold:
            hit1 += 1
        if gold in top:
            hitk += 1
            rr += 1.0 / (top.index(gold) + 1)
        prec.append(1.0 / len(top) if top else 0.0)
    n = len(queries)
    return {"name": name, "hit1": hit1 / n, "hitk": hitk / n, "mrr": rr / n,
            "prec": sum(prec) / n, "lat": sum(lat) / len(lat)}


def main() -> int:
    corpus = build_corpus()
    topics = [t for t, *_ in corpus]
    docs = [f"{t} {c}" for t, c, *_ in corpus]

    print(f"语料 {len(corpus)} 条 | 查询：字面型 {len(QUERIES_LITERAL)} + 语义型 "
          f"{len(QUERIES_SEMANTIC)} | 返回槽位 k={K} | 融合 RRF(k={RRF_K})")
    print(f"嵌入模型：{EMBED_MODEL} | 重排模型：siliconflow_rerank\n")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        kb_leg = build_kb(tmp / "leg", corpus, "legacy")
        kb_bm = build_kb(tmp / "bm", corpus, "bm25")
        keys = [kb_bm._topic_key(t) for t in topics]

        import hashlib
        sig = hashlib.md5("\n".join(docs).encode("utf-8")).hexdigest()[:10]
        cache = Path(tempfile.gettempdir()) / f"pirika_bench_vec_{len(corpus)}_{sig}.json"
        t0 = time.perf_counter()
        if cache.exists():
            mat = json.loads(cache.read_text(encoding="utf-8"))
            cost = 0.0
            print(f"→ 语料向量命中缓存（{len(mat)} 条）")
        else:
            print(f"→ 计算语料向量（{EMBED_MODEL}）...")
            mat = embed_texts(docs)
            cache.write_text(json.dumps(mat), encoding="utf-8")
            cost = time.perf_counter() - t0
            print(f"   完成 {len(mat)} 条，耗时 {cost:.1f}s")
        if len(mat) != len(keys):
            print(f"   ⚠ 缓存条数({len(mat)})与语料({len(keys)})不符，重建")
            mat = embed_texts(docs)
            cache.write_text(json.dumps(mat), encoding="utf-8")

        qvec: dict[str, list[float]] = {}

        def qv(q):
            if q not in qvec:
                qvec[q] = embed_texts([INSTRUCT + q])[0]
            return qvec[q]

        def ranks_from_scores(scores: dict) -> list[str]:
            return sorted(scores, key=lambda k: -scores[k])

        def rank_legacy(q):
            return kb_leg._rank(q.lower())

        def rank_bm25(q):
            return kb_bm._rank(q.lower())

        def rank_dense(q):
            return ranks_from_scores(dict(zip(keys, cosine_top(qv(q), mat))))

        def rank_rrf(q):
            """RRF：只看名次，天然免疫分数尺度差异（v1 加权融合的修正）。"""
            r_bm = rank_bm25(q)
            r_dn = rank_dense(q)
            fused: dict[str, float] = {}
            for rank, k in enumerate(r_bm[:200], 1):
                fused[k] = fused.get(k, 0.0) + 1.0 / (RRF_K + rank)
            for rank, k in enumerate(r_dn[:200], 1):
                fused[k] = fused.get(k, 0.0) + 1.0 / (RRF_K + rank)
            return ranks_from_scores(fused)

        def rank_rrf_rerank(q):
            pool = rank_rrf(q)[:RERANK_POOL]
            if not pool:
                return []
            id2doc = {kb_bm._topic_key(t): d for t, d in zip(topics, docs)}
            sc = rerank_scores(q, [id2doc[k] for k in pool])
            order = sorted(range(len(pool)), key=lambda i: -sc[i])
            return [pool[i] for i in order]

        engines = (("legacy（旧版）", rank_legacy),
                   ("bm25（当前默认）", rank_bm25),
                   ("dense（纯嵌入）", rank_dense),
                   ("RRF 混合", rank_rrf),
                   ("RRF + rerank", rank_rrf_rerank))

        # gold 必须归一化成 topic key（_topic_key 会 strip+lower）——
        # 否则含大写字母的 gold（RCON/TPS）与检索结果永远对不上，指标全部失真
        def to_key(qs):
            return [(q, kb_bm._topic_key(g)) for q, g in qs]

        all_q = to_key(QUERIES_LITERAL + QUERIES_SEMANTIC)
        rows = []
        for nm, fn in engines:
            print(f"→ {nm} ...")
            rows.append(evaluate(nm, fn, all_q))

        print("\n" + "=" * 80)
        print(f"{'引擎':<20}{'Hit@1':>8}{'Hit@6':>8}{'MRR':>8}{'精确率@6':>10}{'延迟ms':>10}")
        print("-" * 80)
        for r in rows:
            print(f"{r['name']:<20}{r['hit1']:>8.1%}{r['hitk']:>8.1%}"
                  f"{r['mrr']:>8.3f}{r['prec']:>10.1%}{r['lat']:>10.0f}")
        print("=" * 80)

        for label, qs in (("语义型（BM25 词表鸿沟场景）", to_key(QUERIES_SEMANTIC)),
                          ("字面型（关键词直给）", to_key(QUERIES_LITERAL))):
            print(f"\n【{label}】")
            print(f"{'引擎':<20}{'Hit@1':>8}{'Hit@6':>8}{'MRR':>8}")
            print("-" * 44)
            for nm, fn in engines:
                r = evaluate(nm, fn, qs)
                print(f"{r['name']:<20}{r['hit1']:>8.1%}{r['hitk']:>8.1%}{r['mrr']:>8.3f}")

        print("\n【噪声污染诊断】语义型查询里 gold 落榜时，榜首被谁占了")
        for q, gold in to_key(QUERIES_SEMANTIC)[:4]:
            top = rank_dense(q)[:3]
            print(f"  「{q}」→ " + " / ".join(top))

        print("\n【无关查询误召回】3 条无关查询共返回条数")
        for nm, fn in engines:
            print(f"  {nm:<20} {sum(len(fn(q)[:K]) for q in QUERIES_NONE)}")

        print(f"\n建索引成本：{cost:.1f}s / {len(corpus)} 条 = {cost / len(corpus) * 1000:.0f}ms 每条（仅首次）")
        print(f"索引体积：{len(mat) * len(mat[0]) * 4 / 1024 / 1024:.1f} MB"
              f"（{len(mat[0])} 维 float32，约 {len(mat[0]) * 4 / 1024:.1f} KB/条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
