"""模组知识库引擎（学习型动态知识库）。

设计目标：
- 代码通用：不硬编码任何整合包特定知识，工具跨整合包可用
- 知识动态：每个整合包一份知识库文件，换整合包自动切换/重建
- LLM 智能路由：简单任务不查库直接执行；重型任务由 LLM 决定查询
- 学习闭环：硬推理成功后通过 mc_save_knowledge 沉淀
- 纠错闭环：结果出错时通过 mc_correct_knowledge 修正，条目带验证状态

存储结构（JSON）：
{
  "server_id": "...",            # 当前整合包指纹（mods目录文件哈希）
  "entries": {
    "topic/关键词": {
      "content": "知识内容",
      "status": "verified|untested|corrected",
      "created_at": "...",
      "updated_at": "...",
      "source": "auto_scan|llm_learn|user_correct"
    }
  }
}
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

try:                      # numpy 随 AstrBot（faiss 依赖）一起提供；缺了就让语义通道自动失效
    import numpy as _np
except Exception:         # pragma: no cover
    _np = None

VALID_STATUS = ("verified", "untested", "corrected", "pending")

# ================= 检索引擎 =================
# v0.21.40：检索引擎双轨制（配置项 knowledge.search_engine）。
#
# - "bm25"（默认）：中文二元切分 + BM25 + 倒排索引 + 最低分门槛。
#   中文切成二元组（bigram）保留了「字序」信息——「黄铜」与「铜黄」不再等价；
#   BM25 的 idf 会自动把「通用 / 方案 / 规则」这类高频套话降权，长度归一化
#   则避免长条目靠字数多而霸榜。
#   离线评测（26 条语料 + 17 组带标准答案查询）：返回结果精确率 13.9% → 38.5%、
#   无关查询误召回 17 条 → 3 条、5000 条规模单次检索 5.7ms → 1.0ms。
#
# - "legacy"：旧版「中文按单字 + 英文按整段」的子串命中计数，保留为可选项。
#   中文单字匹配会连锁误召回（查「满配一把枪」能命中一堆含「配 / 方 / 案」的条目），
#   仅建议「需要与历史检索结果完全一致」的场景使用。
DEFAULT_SEARCH_ENGINE = "bm25"
SEARCH_ENGINES = ("bm25", "legacy")

# ================= 语义增强检索（可选 · v0.21.41） =================
# 配置项 knowledge.semantic_search（**默认关闭**，见 _conf_schema.json）：
# 开启后知识库除 BM25 词法通道外，再走一条「嵌入向量」语义通道，两通道用
# RRF（Reciprocal Rank Fusion，倒数排名融合）合并。适合条目多、且用户爱用
# 口语改写提问的大型服务器；条目少时 BM25 已经够准，收益有限。
#
# ── 为什么是 RRF，而不是「归一化加权 + 硬门槛」 ──
# 两条通道分数尺度完全不同（BM25 无上界、余弦 0~1）。v1 评测用 min-max 归一化
# 加权 + 0.35 硬门槛，语义型查询 Hit@1 直接归零：gold 只在单通道强（BM25 零命中、
# dense 排在很后面），归一化后融合分过不了门槛，被整条砍掉。RRF 只看名次、不看
# 分数，天然免疫尺度问题，是业界混合检索的标准做法。
#
# ── 关于余弦门槛（v0.21.41 定稿时的两难，现已被 SEMANTIC_MIN_SIM 解决）──
# 一开始**刻意不做**绝对门槛，理由是实测过两种嵌入模型的余弦分布差异很大：
#   · Qwen3-Embedding-4B：无关查询 top1 最高 0.460，相关查询最低 0.505 → 可分
#   · Qwen3-VL-Embedding-8B（多模态）：无关 0.568、真命中可低至 0.257 → 重叠
# 门槛一旦按某个模型标定，换模型就可能整条通道失效（要么全被切掉、要么放进噪声）。
# 但「不做门槛」也有代价：RRF 只看名次，无关提问照样会被塞进候选池并加分，
# 实测 3 条无关查询的返回条数从 0 涨到 18。最终定案是「门槛 + 候选池」双保险，
# 因为默认/推荐的嵌入模型就是 4B 文本模型，该场景下 0.50 是最优点。
# 选多模态模型时门槛会把语义通道压到接近失效，但**退化方向是安全的**
# （实测仍 ≥ 纯 BM25，不会把命中挤掉），所以不必为它调参。
#
# ── 本机实测（438 条语料 / 24 组带标准答案查询 / 参考槽位 K=6）──
#   纯 BM25                         Hit@1 75.0%  Hit@6  79.2%  MRR 0.771  噪声 0
#   BM25 + 语义（RRF，无门槛）      Hit@1 83.3%  Hit@6  91.7%  MRR 0.868  噪声 18
#   BM25 + 语义（RRF + 门槛 0.50）  Hit@1 87.5%  Hit@6 100.0%  MRR 0.931  噪声 0  ← 当前
#     └ 拆分：字面型 Hit@1 91.7%→100%；语义型 Hit@1 58.3%→75.0%、Hit@6 58.3%→100%
#   换多模态嵌入模型（同参数）：Hit@1 37.5% / Hit@6 83.3%（高于它自身无门槛的
#   75.0%，也高于纯 BM25 的 79.2% —— 只把语义通道压弱，不会把命中挤掉）
# 代价：建库时每条约一次嵌入调用（后台分批、带落盘缓存、只算新增/改动条目）；
# 检索时多一次查询嵌入调用，约 300~500ms 延迟。这是它「只建议大型服务器启用」
# 的原因——小库用 BM25 就够了，不值这份延迟与 API 开销。
SEMANTIC_POOL = 60            # 嵌入通道参与融合的候选池上限（名次越靠后越像噪声）
RRF_K = 60                    # RRF 平滑常数（业界默认 60）
SEMANTIC_BATCH = 16           # 建向量索引时的批大小（一次嵌入调用算几条）

# ================= 重排序精排（可选 · v0.21.42） =================
# 配置项 knowledge.rerank（**默认关闭**）：
# 开启后检索变成「召回 → 精排」两段式 —— 先用 BM25（+可选语义通道 RRF）召回
# 一个候选池，再把这个池子交给 Rerank 模型（Cross-Encoder）逐条打分重排，
# 最后取前 limit 条。召回阶段看的是「词/向量像不像」，精排阶段看的是
# 「查询与条目真实相关性」，能把召回到了但排不前的 gold 提上来。
#
# 为什么单独做一个开关：rerank 是**纯附加延迟**（多一次 API 往返），
# 小库（几十条）BM25 已经够准，不值这份开销；条目多、问法杂时才划算。
# 与语义通道相互独立：没有嵌入模型也能单独开 rerank（只对 BM25 召回池精排）。
#
# 候选池大小：池子太小会漏掉召回阶段排名靠后的 gold（rerank 救不回来），
# 太大则单次 rerank 的 token 成本与延迟上升。20 是评测脚本
# （tests/bench_kb_engines.py，RERANK_POOL）用的值，对齐它。
RERANK_POOL = 20
# 精排后是否保留「模板补召」：rerank 已经在池内做了全局比较，补召会绕过精排
# 顺序追加条目，因此在精排结果不足 limit 时才补（与普通检索同口径）。

# 余弦相似度门槛：低于它的候选不参与融合。
#
# 为什么需要它：RRF 只看名次、不看分数，所以哪怕提问跟整库毫无关系
# （「今天午饭吃什么」），嵌入通道也一定会给出 60 个「最像的」候选并按名次加分，
# 于是凭空多出一批噪声（实测 3 条无关查询从 0 条变成 18 条）。
# 余弦相似度则是有绝对尺度的——本机实测（438 条语料）：
#   相关查询 top1 余弦 最低 0.505 / 中位 0.644；无关查询 top1 余弦 最高 0.460
# 取 0.50 卡在两者之间：噪声归零，且指标反而更高
#   （Hit@1 75%→87.5%、Hit@6 79.2%→100%、语义型 Hit@6 83.3%→100%）。
# 注意：这条门槛只挡「整库都不相关」的查询，不影响正常的跨说法召回
#   （「游戏里连不上后台控制台了」余弦远高于 0.5）。
SEMANTIC_MIN_SIM = 0.50
VECTOR_CACHE_SUFFIX = ".vec.npz"

# BM25 最低分门槛：低于「本条最高分 × 该比例」的候选直接丢弃。
# 0.35 是评测出的甜点——命中数不减，返回的无关条目砍掉约 44%（省 token）。
BM25_MIN_SCORE_RATIO = 0.35
_BM25_K1 = 1.2
_BM25_B = 0.75
_BM25_TOPIC_BOOST = 2.0   # 主题字段的词频权重（主题比正文更能代表条目）


def _tokenize_raw(text: str) -> list[str]:
    """旧版切分：中文按单字、英文/数字/下划线按整段（legacy 引擎用）。"""
    return re.findall(r"[\u4e00-\u9fff]|[a-z0-9_]+", text.lower())


def _bigrams(text: str) -> list[str]:
    """中文二元切分：CJK 连续段切 bigram（单字段保留原字），英文/数字按整段。

    英文仍按词边界整段（`sig_mcx_spear` 是一个整体），中文则保留相邻两字的组合，
    比单字切分多一层字序约束。
    """
    out: list[str] = []
    for m in re.finditer(r"[\u4e00-\u9fff]+|[a-z0-9_]+", text.lower()):
        seg = m.group(0)
        if re.fullmatch(r"[a-z0-9_]+", seg) or len(seg) == 1:
            out.append(seg)
        else:
            out.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return out


class BM25Index:
    """倒排索引版 BM25（v0.21.40 起的默认检索引擎）。

    文档侧 TF / 长度 / idf 在构造时算一次，查询只做倒排查表，
    因此语料涨到几千条也不拖慢（实测 5000 条 ≈ 1ms，旧版线性扫描 5.7ms）。
    """

    def __init__(self, entries: dict, topic_boost: float = _BM25_TOPIC_BOOST):
        self.k1 = _BM25_K1
        self.b = _BM25_B
        self.topic_boost = topic_boost
        self.n = 0
        self.avgdl = 0.0
        self.dl: dict[str, int] = {}
        self.tf: dict[str, dict[str, float]] = {}
        self.idf: dict[str, float] = {}
        self.postings: dict[str, list[str]] = {}
        self._build(entries)

    def _build(self, entries: dict) -> None:
        postings: dict[str, list[str]] = defaultdict(list)
        for topic, e in entries.items():
            tf_all = Counter(_bigrams(f"{topic} {e.get('content', '')}"))
            tf_topic = Counter(_bigrams(topic))
            merged = {
                tk: c + self.topic_boost * tf_topic.get(tk, 0)
                for tk, c in tf_all.items()
            }
            self.tf[topic] = merged
            self.dl[topic] = sum(merged.values())
            for tk in merged:
                postings[tk].append(topic)
        self.postings = dict(postings)
        self.n = len(self.tf)
        self.avgdl = (sum(self.dl.values()) / self.n) if self.n else 0.0
        self.idf = {
            tk: math.log((self.n - len(p) + 0.5) / (len(p) + 0.5) + 1.0)
            for tk, p in self.postings.items()
        }

    def score(self, query: str) -> dict[str, float]:
        """返回 {topic: BM25 分}（只含至少命中一个词的条目）。"""
        qt = _bigrams(query)
        if not qt or not self.n or not self.avgdl:
            return {}
        out: dict[str, float] = defaultdict(float)
        for tk in qt:
            posting = self.postings.get(tk)
            if not posting:
                continue
            idf = self.idf[tk]
            for topic in posting:
                tf = self.tf[topic][tk]
                out[topic] += idf * (tf * (self.k1 + 1)) / (
                    tf + self.k1 * (1 - self.b + self.b * self.dl[topic] / self.avgdl)
                )
        return dict(out)


class SemanticIndex:
    """嵌入向量索引（v0.21.41，可选通道）。

    与 BM25Index 的分工：BM25 管「字面命中」，本类管「说法不同但意思一样」——
    用户问「游戏里连不上后台控制台了」，库里存的是「运维 RCON 连接失败排查」，
    字面一个词都不重合，只有向量能拉得上。

    设计约束（重要）：嵌入调用是 async 的，而 search() 是同步方法（被多处同步调用）。
    所以本类**只存向量与做点积**，真正的 API 调用由外部注入的 embed_fn 完成：
    查询向量由 asearch() 在进入同步检索前先算好，建库向量由 build() 在后台分批算好。
    这样同步检索路径上永远不会出现 await，调用方不必改造成异步。
    """

    def __init__(self, dim: int = 0):
        self.dim = dim
        self.topics: list[str] = []          # 行号 → topic
        self.hashes: list[str] = []          # 行号 → 内容哈希（判复用/失效）
        self.matrix = None                   # (N, dim) float32，已 L2 归一化
        self._pos: dict[str, int] = {}       # topic → 行号

    # ---------- 构建 ----------

    @staticmethod
    def content_hash(topic: str, entry: dict) -> str:
        """条目内容指纹：topic + 正文 + 启停状态。

        禁用/待审批条目不该留在向量池里（BM25 侧同样排除），所以状态进哈希，
        重启条目的开关会触发这一条重算，不用整库重建。
        """
        raw = f"{topic}\x00{entry.get('content', '')}\x00{int(bool(entry.get('enabled', True)))}"
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def needs(self, topic: str, entry: dict) -> bool:
        """该条目是否需要（重）算向量。"""
        return self._pos.get(topic) is None or self.hashes[self._pos[topic]] != self.content_hash(topic, entry)

    def missing(self, entries: dict) -> list[str]:
        """尚未建向量 / 内容已变动的 topic 列表。"""
        return [t for t, e in entries.items() if self.needs(t, e)]

    def has(self, topic: str) -> bool:
        return topic in self._pos

    def set_vectors(self, topics: list[str], entries: dict, vectors) -> None:
        """把一批新算的向量并入索引（同 topic 覆盖旧行）。

        性能注意：新增行先攒在列表里、最后**一次性 vstack**。
        早期写法是每行都 vstack 一次，建 4000 条时等于复制矩阵 4000 次
        （O(n²)，实测要好几秒）；批量建库正是「大型服务器」的典型场景，
        所以这里必须整批拼接。
        """
        if _np is None or not topics or vectors is None:
            return
        mat = _np.asarray(vectors, dtype="float32")
        if mat.ndim == 1:
            mat = mat.reshape(1, -1)
        if mat.shape[0] != len(topics):
            return
        norms = _np.linalg.norm(mat, axis=1, keepdims=True)
        mat = mat / _np.maximum(norms, 1e-9)
        self.dim = int(mat.shape[1])
        fresh: list = []
        for i, topic in enumerate(topics):
            h = self.content_hash(topic, entries.get(topic) or {})
            pos = self._pos.get(topic)
            if pos is None:
                self._pos[topic] = len(self.topics)
                self.topics.append(topic)
                self.hashes.append(h)
                fresh.append(mat[i])
            else:
                self.hashes[pos] = h
                self.matrix[pos] = mat[i]
        if fresh:
            block = _np.stack(fresh) if len(fresh) > 1 else fresh[0].reshape(1, -1)
            self.matrix = block if self.matrix is None else _np.vstack([self.matrix, block])

    def prune(self, entries: dict) -> int:
        """剔除已删除条目的向量（保持矩阵与条目表一致），返回剔除条数。"""
        if _np is None or self.matrix is None:
            return 0
        keep = [i for i, t in enumerate(self.topics) if t in entries]
        drop = len(self.topics) - len(keep)
        if drop:
            self.matrix = self.matrix[keep] if keep else None
            self.topics = [self.topics[i] for i in keep]
            self.hashes = [self.hashes[i] for i in keep]
            self._pos = {t: i for i, t in enumerate(self.topics)}
            self.dim = int(self.matrix.shape[1]) if self.matrix is not None else self.dim
        return drop

    # ---------- 查询 ----------

    def rank(self, query_vec) -> list[str]:
        """按余弦相似度返回降序 topic 列表（截到候选池上限，并过相似度门槛）。"""
        if _np is None or self.matrix is None or query_vec is None or not self.topics:
            return []
        q = _np.asarray(query_vec, dtype="float32").ravel()
        if q.shape[0] != self.matrix.shape[1]:
            # 换过嵌入模型（维度不同）→ 本次直接放弃语义通道，别拿旧向量硬算
            return []
        q = q / max(float(_np.linalg.norm(q)), 1e-9)
        sims = self.matrix @ q
        order = _np.argsort(-sims)[:SEMANTIC_POOL]
        if SEMANTIC_MIN_SIM > 0:
            # 门槛之外的候选一律不参与融合：RRF 不看分数尺度，没有这道闸
            # 无关提问也会被塞进 60 个"最像的"候选、平白招来噪声。
            order = [i for i in order if float(sims[int(i)]) >= SEMANTIC_MIN_SIM]
        return [self.topics[int(i)] for i in order]

    # ---------- 落盘 ----------

    def save(self, path: Path) -> None:
        if _np is None or self.matrix is None:
            return
        try:
            _np.savez_compressed(
                path, matrix=self.matrix,
                topics=_np.array(self.topics, dtype=object),
                hashes=_np.array(self.hashes, dtype=object),
            )
        except Exception:
            pass

    def load(self, path: Path) -> bool:
        if _np is None or not path.exists():
            return False
        try:
            with _np.load(path, allow_pickle=True) as z:
                self.matrix = _np.asarray(z["matrix"], dtype="float32")
                self.topics = [str(x) for x in z["topics"]]
                self.hashes = [str(x) for x in z["hashes"]]
                self.dim = int(self.matrix.shape[1]) if self.matrix.ndim == 2 else 0
            self._pos = {t: i for i, t in enumerate(self.topics)}
            return True
        except Exception:
            self.matrix, self.topics, self.hashes, self._pos = None, [], [], {}
            return False


# 老算法「mods 目录里一个 jar 都没有」时的指纹（= md5 空串）。这种「空整合包指纹」
# 无法区分不同服务端：两个都没装 mod（或 server_dir 指到空壳目录）的服务端算出来
# 一模一样，指纹比对与轮次记账都会失效 —— 必须点出来，不能装作正常。
#
# v0.21.20：指纹引擎改成分层来源（mods/ → plugins/ → 服务端形态），已经不会再产出
# 这个值；常量保留只为兼容老 registry（预设可能仍绑着它），weak 判定改由引擎显式给出
# （见 KnowledgePresetManager.server_weak）。
EMPTY_SERVER_FP = hashlib.md5(b"").hexdigest()[:12]   # d41d8cd98f00

# ================= 模组分类 / 模板识别 =================
# 已知模组别名表（用于 topic/content 里没有命名空间时的兜底识别）
KNOWN_MOD_ALIASES = {
    "tac": ["tac", "枪械", "配件", "附件"],
    "create": ["create", "机械动力"],
    "ftbquests": ["ftbquests", "ftb quests", "任务书", "任务"],
    "deceasedcraft": ["deceasedcraft", "幸存者", "村民职业"],
    "minecraft": ["原版", "vanilla"],
    "jei": ["jei"],
}
_TEMPLATE_WORDS = ("通用", "规则", "格式", "机制", "系统", "槽位", "结构", "原理", "流程", "命令", "配置", "枚举", "模板")
_INSTANCE_WORDS = ("满改", "方案", "攻略", "做法", "发放", "发给", "给.*发")


class KnowledgeState:
    """知识库全局状态（热生效，WebUI 可即时修改）。

    - enabled: 知识库能力总开关（关=完全走硬路线，不查不学不写）
    - learning: 学习开关（关=不写入知识库，但可查询）
    - auto_apply: 自动应用开关（开=学习即应用；关=进入 pending 待管理员审批）
    """

    def __init__(self, data_dir: str):
        self.file_path = Path(data_dir) / "knowledge_state.json"
        self.enabled = True
        self.learning = True
        self.auto_apply = True
        self.load()

    def load(self) -> None:
        if self.file_path.exists():
            try:
                data = json.loads(self.file_path.read_text(encoding="utf-8"))
                self.enabled = bool(data.get("enabled", True))
                self.learning = bool(data.get("learning", True))
                self.auto_apply = bool(data.get("auto_apply", True))
            except Exception:
                pass

    def save(self) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "enabled": self.enabled,
                "learning": self.learning,
                "auto_apply": self.auto_apply,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.file_path)
        except Exception:
            pass

    def set(self, enabled: bool | None = None, learning: bool | None = None,
            auto_apply: bool | None = None) -> dict:
        if enabled is not None:
            self.enabled = bool(enabled)
        if learning is not None:
            self.learning = bool(learning)
        if auto_apply is not None:
            self.auto_apply = bool(auto_apply)
        self.save()
        return self.as_dict()

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "learning": self.learning,
            "auto_apply": self.auto_apply,
        }


class ModKnowledgeBase:
    """模组知识库（预设槽位制）：检索、沉淀、纠错。

    v0.18.0 起每个预设 = 一个独立文件 kb_preset_<预设ID>.json：
    - 预设自带 fingerprint（绑定的服务端整合包指纹），**可以为 None = 未绑定**
    - 未绑定的预设只在「第一次写入知识」时绑定到当时运行的服务端指纹
    - 服务端指纹变化时本类不做任何自动切换/继承（由 KnowledgePresetManager 通知用户）

    v0.21.40 起检索引擎双轨制（search_engine）：
    - "bm25"（默认）：中文 bigram + BM25 + 倒排索引 + 最低分门槛
    - "legacy"：旧版单字/整段子串命中计数（保留为可选项）
    索引只在 load()/save() 时重建——所有写操作都会走到 save()，所以内存索引
    与磁盘内容始终一致，不需要在每个写方法里各挂一次钩子。

    v0.21.41 起可叠加语义通道（semantic_enabled，默认关；配置项 knowledge.semantic_search）：
    BM25 与嵌入向量两条通道各自排名，再用 RRF 融合。嵌入调用是异步的，而本类
    检索是同步的——所以向量在这里「只算一次、存起来」：查询向量由 asearch()
    提前算好传进来，库侧向量由 build_vectors() 后台分批算好并落盘缓存。
    """

    def __init__(
        self,
        data_dir: str,
        server_id: str | None = None,
        preset_id: str | None = None,
        preset_name: str | None = None,
        fingerprint: str | None = None,
        on_first_write=None,
        search_engine: str = DEFAULT_SEARCH_ENGINE,
        semantic_enabled: bool = False,
        rerank_enabled: bool = False,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.server_id = server_id or "default"   # 当前运行服务端的整合包指纹（只读）
        self.preset_id = preset_id or f"legacy_{self.server_id}"
        self.preset_name = preset_name or f"知识库 {self.preset_id}"
        self.fingerprint = fingerprint           # 本预设绑定的指纹，None=未绑定
        self.on_first_write = on_first_write     # 首次写入时回调（用于绑定指纹）
        # 检索引擎：非法值一律回落到默认（配置写错也不能让检索崩掉）
        self.search_engine = (
            search_engine if search_engine in SEARCH_ENGINES else DEFAULT_SEARCH_ENGINE
        )
        self._index: BM25Index | None = None     # bm25 引擎的倒排索引（惰性/随 save 重建）
        self.file_path = self.data_dir / f"kb_preset_{self.preset_id}.json"
        self.state = KnowledgeState(data_dir)
        self._data: dict = {"preset_id": self.preset_id, "entries": {}}
        # 语义通道（默认关）：向量索引 + 嵌入调用函数（由插件注入，可能为 None）
        self.semantic_enabled = bool(semantic_enabled)
        self._sem: SemanticIndex | None = None
        self.embed_fn = None                     # async (list[str]) -> list[list[float]]
        self.vec_path = self.file_path.with_suffix(VECTOR_CACHE_SUFFIX)
        # 重排序精排（默认关，v0.21.42）：候选池交给 Rerank 模型重排
        self.rerank_enabled = bool(rerank_enabled)
        self.rerank_fn = None                    # async (query, docs) -> [(index, score)]
        self.load()

    # ================= 语义通道 =================

    def set_semantic_enabled(self, on: bool) -> bool:
        """开关语义通道（WebUI 即时生效）。返回实际状态。"""
        self.semantic_enabled = bool(on)
        if self.semantic_enabled and self._sem is None:
            sem = SemanticIndex()
            if not sem.load(self.vec_path):
                self._sem = sem          # 缓存不存在/损坏 → 用空索引，等 build_vectors 补
            else:
                self._sem = sem
        if self._sem is not None:
            self._sem.prune(self._searchable())
        return self.semantic_enabled

    def semantic_ready(self) -> bool:
        """语义通道是否真正可用（开关开着 + 有向量 + 维度对得上）。"""
        return bool(
            self.semantic_enabled and self._sem is not None
            and self._sem.matrix is not None and self._sem.topics
        )

    def semantic_stats(self) -> dict:
        ready = self.semantic_ready()
        pending = len(self._sem.missing(self._searchable())) if self._sem is not None else 0
        return {
            "enabled": self.semantic_enabled,
            "ready": ready,
            "vectors": len(self._sem.topics) if self._sem is not None else 0,
            "pending": pending,
            "dim": self._sem.dim if self._sem is not None else 0,
            "has_embed_fn": self.embed_fn is not None,
        }

    # ================= 重排序精排（v0.21.42） =================

    def set_rerank_enabled(self, on: bool) -> bool:
        """开关重排序精排（WebUI 即时生效）。返回实际状态。"""
        self.rerank_enabled = bool(on)
        return self.rerank_enabled

    def rerank_stats(self) -> dict:
        """精排通道状态（用于 WebUI / 诊断）。"""
        return {
            "enabled": self.rerank_enabled,
            "has_rerank_fn": self.rerank_fn is not None,
            "pool": RERANK_POOL,
        }

    def rerank_ready(self) -> bool:
        """精排通道是否真正可用（开关开着 + 有 rerank 调用）。"""
        return bool(self.rerank_enabled and self.rerank_fn is not None)

    async def build_vectors(self, force: bool = False) -> dict:
        """为「新增 / 内容有变」的条目补算向量（后台调用，可安全重复执行）。

        force=True 时整库重算（换嵌入模型、怀疑缓存脏了时用）。
        返回统计信息；嵌入调用失败不抛异常，交由上层提示。
        """
        if not self.semantic_enabled:
            return {"ok": False, "reason": "语义通道未开启"}
        if self.embed_fn is None:
            return {"ok": False, "reason": "未注入嵌入调用（缺少可用的嵌入模型 Provider）"}
        entries = self._searchable()
        if self._sem is None:
            self._sem = SemanticIndex()
        if force:
            self._sem = SemanticIndex()
        self._sem.prune(entries)
        todo = list(entries.keys()) if force else self._sem.missing(entries)
        if not todo:
            return {"ok": True, "added": 0, "total": len(self._sem.topics), "skipped": True}
        added, failed = 0, 0
        for i in range(0, len(todo), SEMANTIC_BATCH):
            batch = todo[i:i + SEMANTIC_BATCH]
            texts = [f"{t} {entries[t].get('content', '')}" for t in batch]
            try:
                vectors = await self.embed_fn(texts)
                if not vectors or len(vectors) != len(batch):
                    failed += len(batch)
                    continue
                # 换过模型会改变维度 → 旧行与新行维度不一致，整库重算更稳妥
                if self._sem.dim and self._sem.matrix is not None and \
                        len(vectors[0]) != self._sem.matrix.shape[1]:
                    self._sem = SemanticIndex()
                self._sem.set_vectors(batch, entries, vectors)
                added += len(batch)
            except Exception:
                failed += len(batch)
                continue
        self._sem.save(self.vec_path)
        return {
            "ok": True, "added": added, "failed": failed,
            "total": len(self._sem.topics), "dim": self._sem.dim,
        }

    async def asearch(self, query: str, limit: int = 10) -> list[dict]:
        """search() 的异步入口：先算查询向量（语义通道），再走检索。

        调用方（async 上下文）应优先用它；同步调用 search() 仍然可用，只是
        带不上语义通道与精排（都依赖异步 API 调用），自动退化为纯 BM25。

        v0.21.42：开启重排序精排时走「召回 → 精排」两段式（见 _search_with_rerank）。
        """
        qvec = None
        if self.semantic_ready() and self.embed_fn is not None and query.strip():
            try:
                out = await self.embed_fn([query.strip()])
                if out:
                    qvec = out[0]
            except Exception:
                qvec = None          # 嵌入失败不该让整次检索失败，退化为纯 BM25
        if self.rerank_ready() and query.strip():
            return await self._search_with_rerank(query, limit=limit, query_vec=qvec)
        return self.search(query, limit=limit, query_vec=qvec)

    async def _search_with_rerank(self, query: str, limit: int = 10,
                                  query_vec=None) -> list[dict]:
        """「召回 → 精排」检索（v0.21.42，精排开关打开时由 asearch 调用）。

        召回：复用 _rank_candidates（BM25 或 BM25+语义 RRF），取候选池前
        RERANK_POOL 条；精排：把池内条目文档交给 Rerank 模型逐条打分，按分数
        降序取前 limit 条。任何异常都退回普通 search() —— 精排是锦上添花，
        不该让整次检索失败。
        """
        pool = self._rank_candidates(query, query_vec)[:RERANK_POOL]
        if not pool:
            return []
        entries = self._searchable()
        # 文档口径与建向量一致（topic + 正文）：topic 是最强的相关性信号
        docs = [f"{t} {entries.get(t, {}).get('content', '')}" for t in pool]
        try:
            scored = await self.rerank_fn(query, docs)
        except Exception:
            return self.search(query, limit=limit, query_vec=query_vec)
        # rerank 只回「它给过分的那些」：没回的候选按原召回顺序兜底排在其后
        order: dict[int, float] = {}
        for item in scored or []:
            try:
                idx, score = item[0], item[1]
            except Exception:
                continue
            if isinstance(idx, int) and 0 <= idx < len(pool):
                order[idx] = float(score)
        ranked = sorted(range(len(pool)),
                        key=lambda i: (-order.get(i, float("-inf")), i))
        out, seen = [], set()
        for i in ranked:
            topic = pool[i]
            if topic in seen:
                continue
            seen.add(topic)
            out.append(self._entry_view(topic))
            if len(out) >= limit:
                break
        self._template_fill(query, out, seen, limit)
        return out

    def set_search_engine(self, engine: str) -> str:
        """切换检索引擎（WebUI 即时生效）。返回实际生效的引擎名。"""
        self.search_engine = engine if engine in SEARCH_ENGINES else DEFAULT_SEARCH_ENGINE
        # 无论切到哪个引擎都走一次重建：bm25 会建索引、legacy 会释放索引
        self._rebuild_index()
        return self.search_engine

    def bind(self, fingerprint: str | None) -> None:
        """更新本预设绑定的指纹（None=解绑）。"""
        self.fingerprint = fingerprint

    def matches_server(self) -> bool | None:
        """预设指纹与服务端指纹是否匹配：None=未绑定（不警告）。"""
        if not self.fingerprint:
            return None
        return self.fingerprint == self.server_id

    # ================= 基础 =================

    def load(self) -> None:
        if self.file_path.exists():
            try:
                self._data = json.loads(
                    self.file_path.read_text(encoding="utf-8")
                )
                self._data.setdefault("entries", {})
            except Exception:
                self._data = {"preset_id": self.preset_id, "entries": {}}
        self._data.setdefault("preset_id", self.preset_id)
        self._data.setdefault("deleted", [])  # 墓碑：被删除的 topic（防旧文件继承复活）
        self._migrate()
        self._load_semantic()
        self._rebuild_index()

    def _load_semantic(self) -> None:
        """加载向量缓存（语义通道用）。缺 numpy / 缓存不存在都静默跳过。"""
        if not self.semantic_enabled:
            self._sem = None
            return
        sem = SemanticIndex()
        if sem.load(self.vec_path):
            sem.prune(self._searchable())
        self._sem = sem

    def reload(self) -> None:
        """从磁盘重新读取（外部改动后刷新内存）。"""
        self.load()

    def entry_count(self) -> int:
        return len(self._data.get("entries", {}))

    def _migrate(self) -> None:
        """旧条目惰性迁移：自动补 mod/kind 字段。"""
        changed = False
        for topic, e in self._data["entries"].items():
            if "mod" not in e:
                e["mod"] = self._detect_mod(topic, e.get("content", ""))
                changed = True
            if "kind" not in e:
                e["kind"] = self._detect_kind(topic, e.get("content", ""))
                changed = True
        if changed:
            self.save()

    def save(self) -> None:
        try:
            self._data["preset_id"] = self.preset_id
            self._data["updated_at"] = self._now()
            tmp = self.file_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.file_path)
        except Exception:
            pass
        # 落盘后重建索引：所有写操作（沉淀/纠错/启停/删除）都会走到这里，
        # 索引因此始终与内存条目一致，不必在每个写方法里各挂一次钩子
        self._rebuild_index()

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _topic_key(self, topic: str) -> str:
        return topic.strip().lower()

    # ================= 模组分类 / 模板识别 =================

    # 常见非模组命名空间（如 NBT 字段名），识别时跳过
    _NON_MOD_NS = {
        "id", "item", "items", "value", "data", "json", "nbt", "tag", "tags",
        "type", "name", "count", "slot", "key", "enchantments", "attachments",
        "scope", "barrel", "stock", "under_barrel", "side_rail", "ir_device",
        "extended_mag", "pistolscope", "gunskin", "damage", "size", "color",
        "display", "text", "component", "block", "entity", "biome", "en", "zh",
    }

    def _detect_mod(self, topic: str, content: str = "") -> str:
        """从 topic/content 识别所属模组。

        策略：1) mod id 以词边界出现（最可靠，如 tac、ftbquests）；
        2) 收集 `xxx:` 命名空间，跳过 NBT 字段黑名单；3) 别名表兜底
        （按别名长度降序，减少「枪械师」撞「枪械」类误捕）；4) general。
        """
        text = f"{topic} {content}".lower()
        # 1) mod id 词边界直接出现
        for mod_id in KNOWN_MOD_ALIASES:
            if re.search(rf"(?<![a-z0-9_]){re.escape(mod_id)}(?![a-z0-9_])", text):
                return mod_id
        # 2) 命名空间候选（跳过 NBT 字段黑名单）
        for m in re.finditer(r"(?<![a-z0-9_])([a-z0-9_]+):", text):
            ns = m.group(1)
            if ns in self._NON_MOD_NS:
                continue
            if ns in KNOWN_MOD_ALIASES:
                return ns
            return ns
        # 3) 别名表兜底（别名长的优先，降低误捕）
        for mod_id, aliases in sorted(
            KNOWN_MOD_ALIASES.items(),
            key=lambda kv: -max((len(a) for a in kv[1]), default=0),
        ):
            if any(a in text for a in aliases):
                return mod_id
        return "general"

    def _detect_kind(self, topic: str, content: str = "") -> str:
        """判断条目类型：template=通用规则（可泛化套用）；instance=具体实例。"""
        text = f"{topic} {content}".lower()
        if any(w in text for w in _INSTANCE_WORDS):
            return "instance"
        if any(w in text for w in _TEMPLATE_WORDS):
            return "template"
        return "instance"

    def _entry_view(self, topic: str) -> dict:
        e = self._data["entries"][topic]
        return {
            "topic": topic,
            "content": e.get("content", ""),
            "status": e.get("status", "untested"),
            "enabled": e.get("enabled", True),
            "updated_at": e.get("updated_at", ""),
            "mod": e.get("mod", "general"),
            "kind": e.get("kind", "instance"),
        }

    # ================= 检索 =================

    def _searchable(self) -> dict:
        """可参与检索的条目：排除「手工禁用」与「待审批」。"""
        return {
            k: e
            for k, e in self._data["entries"].items()
            if e.get("enabled", True) and e.get("status") != "pending"
        }

    def _rebuild_index(self) -> None:
        """重建 BM25 倒排索引（load()/save() 之后调用）。"""
        if self.search_engine != "bm25":
            self._index = None
            return
        try:
            self._index = BM25Index(self._searchable())
        except Exception:
            # 建索引失败不该让检索整体瘫痪：置空后 _rank 会自动退化到 legacy 打分
            self._index = None

    def _rank(self, query: str) -> list[str]:
        """候选排序（不含模板补召）。返回按相关度降序的 topic 列表。"""
        if self.search_engine == "bm25":
            if self._index is None:
                self._rebuild_index()
            if self._index is not None:
                scores = self._index.score(query)
                if not scores:
                    return []
                cut = max(scores.values()) * BM25_MIN_SCORE_RATIO
                kept = {k: v for k, v in scores.items() if v >= cut}
                # 同分时模板(template)优先置顶，保证泛化模板不被实例条目挤出
                return sorted(
                    kept,
                    key=lambda k: (
                        kept[k],
                        self._data["entries"][k].get("kind") == "template",
                    ),
                    reverse=True,
                )
            # 索引不可用 → 退化到 legacy（宁可慢一点，也不能查不出来）
        return self._rank_legacy(query)

    def _rrf_fuse(self, lexical: list[str], semantic: list[str]) -> list[str]:
        """RRF（倒数排名融合）合并词法排名与语义排名。

        RRF 只看名次、不看分数，所以天然免疫「两通道分数尺度不同」的问题
        （BM25 无上界、余弦 0~1）。侧信道处理：语义通道命中但词法一个词都没
        命中的条目（正是口语改写的典型情形）不会被丢掉 —— 只要它在语义池
        里（pool 上限内），就会以自己名次对应的 1/(k+rank) 参与融合。

        同分时的次序：词法名次优先（可解释性更好——字面命中的条目更容易被
        人理解和验证），其次模板优先，最后按词法排名兜底。
        """
        fused: dict[str, float] = {}
        vrank: dict[str, int] = {}
        for r, t in enumerate(lexical, 1):
            fused[t] = fused.get(t, 0.0) + 1.0 / (RRF_K + r)
            vrank.setdefault(t, r)
        for r, t in enumerate(semantic, 1):
            fused[t] = fused.get(t, 0.0) + 1.0 / (RRF_K + r)
            vrank.setdefault(t, len(lexical) + r)     # 只在语义通道出现的排到后面
        entries = self._data["entries"]
        return sorted(
            fused,
            key=lambda k: (
                fused[k],
                entries.get(k, {}).get("kind") == "template",
                -vrank.get(k, 10 ** 9),
            ),
            reverse=True,
        )

    def _rank_legacy(self, query: str) -> list[str]:
        """旧版打分：中文按单字、英文/数字按整段，统计命中 token 数。"""
        tokens = _tokenize_raw(query)
        if not tokens:
            return []
        exact, partial, scores = [], [], {}
        for topic, entry in self._searchable().items():
            haystack = f"{topic} {entry.get('content', '')}".lower()
            score = sum(1 for t in tokens if t in haystack)
            scores[topic] = score
            if score >= len(tokens):
                exact.append(topic)
            elif score > 0:
                partial.append(topic)
        # 部分命中排序：匹配词数降序 → 模板(template)优先置顶
        partial.sort(
            key=lambda t: (
                scores.get(t, 0),
                self._data["entries"][t].get("kind") == "template",
            ),
            reverse=True,
        )
        return exact + partial

    def _rank_candidates(self, query: str, query_vec=None) -> list[str]:
        """召回排序：词法排名（BM25/legacy），语义通道就绪时与 RRF 融合。

        返回**完整有序**的 topic 列表（不截断到 limit、不含模板补召）——
        普通检索取前 limit 条；精排检索取前 RERANK_POOL 条当候选池。
        """
        q = query.strip().lower()
        if not q:
            return []
        ranked = self._rank(q)
        if self.semantic_ready() and query_vec is not None:
            ranked = self._rrf_fuse(ranked, self._sem.rank(query_vec))
        return ranked

    def _template_fill(self, query: str, out: list[dict], seen: set, limit: int) -> None:
        """模板补召：query 含模组且结果不足时，追加该模组 template（可泛化）+ 少量实例。

        原地修改 out / seen（普通检索与精排检索共用，保证两条路径口径一致）。
        """
        if len(out) >= limit:
            return
        qmod = self._detect_mod(query, "")
        if qmod == "general":
            return
        for topic, e in self._searchable().items():
            if topic in seen:
                continue
            if e.get("mod") != qmod:
                continue
            # template 优先补（≤2），instance 少量补（≤2）
            if e.get("kind") == "template":
                if sum(1 for x in out if x.get("mod") == qmod and x.get("kind") == "template") >= 2:
                    continue
            else:
                if sum(1 for x in out if x.get("mod") == qmod and x.get("kind") == "instance") >= 2:
                    continue
            seen.add(topic)
            out.append(self._entry_view(topic))
            if len(out) >= limit:
                break

    def search(self, query: str, limit: int = 10, query_vec=None) -> list[dict]:
        """检索知识条目（引擎由 search_engine 决定：bm25 / legacy）。

        举一反三：直接命中优先；命中不足时自动补召查询中涉及模组的
        template 模板条目（可泛化套用到同类物品），并附带少量同模组实例。
        v0.21.41：语义通道开着且传入了 query_vec 时，与词法排名做 RRF 融合。
        v0.21.42：召回与补召分别抽到 _rank_candidates / _template_fill（与精排共用）。
        返回 [{topic, content, status, mod, kind}]。
        """
        q = query.strip().lower()
        if not q:
            return []
        ranked = self._rank_candidates(q, query_vec)
        out, seen = [], set()
        for topic in ranked:
            if topic in seen:
                continue
            seen.add(topic)
            out.append(self._entry_view(topic))
            if len(out) >= limit:
                break
        self._template_fill(query, out, seen, limit)
        return out

    # ================= 沉淀 =================

    def save_entry(
        self,
        topic: str,
        content: str,
        source: str = "llm_learn",
        status: str = "untested",
        rename_from: str = "",
        manual: bool = False,
    ) -> dict:
        """写入/更新一条知识（学习沉淀）。返回条目信息。

        自动应用关闭时（auto_apply=False），status 会被强制改为 pending，
        进入 WebUI 审批队列，管理员批准后才生效。

        rename_from（v0.21.11）：把旧主题的条目整体搬成新主题（WebUI 详情页改名用）——
        沿用旧的 created_at / mod / kind，旧主题进墓碑防止被旧文件复活。
        manual=True：主人亲手动笔（WebUI）→ 不再因 auto_apply 关闭而强制转 pending，
        也不会莫名其妙把「已验证」降级成「未验证」。
        """
        key = self._topic_key(topic)
        old_key = self._topic_key(rename_from) if rename_from else key
        now = self._now()
        # 手动重新写入 = 主人想复活这条知识 → 移出墓碑
        deleted = self._data.setdefault("deleted", [])
        if key in deleted:
            deleted.remove(key)
        old = self._data["entries"].get(old_key, {})
        if old_key != key and old_key in self._data["entries"]:
            # 改名：旧键搬走（不留残影），旧主题立墓碑
            self._data["entries"].pop(old_key, None)
            self._tombstone(old_key)
        if status not in VALID_STATUS:
            status = "untested"
        # 待审批状态：旧条目若已应用，降级为 pending 等待重新审批（主人手动编辑除外）
        if not manual and not self.state.auto_apply and old.get("status") not in ("pending",):
            status = "pending"
        entry = {
            "content": content,
            "status": status,
            "enabled": old.get("enabled", True),
            "created_at": old.get("created_at", now),
            "updated_at": now,
            "source": source,
            "mod": old.get("mod") or self._detect_mod(topic, content),
            "kind": old.get("kind") or self._detect_kind(topic, content),
        }
        self._data["entries"][key] = entry
        self.save()
        # v0.18.0：未绑定预设的「首次写入」→ 此刻绑定到当前服务端指纹
        bound = None
        if not self.fingerprint and callable(self.on_first_write):
            try:
                bound = self.on_first_write(self)
            except Exception:
                bound = None
        out = {"topic": key, **entry}
        if bound:
            out["bound_fingerprint"] = bound
        return out

    # ================= 纠错 =================

    def correct_entry(self, topic: str, correction: str) -> dict | None:
        """纠错：覆盖已有知识并标记 corrected。topic 不存在时返回 None。"""
        key = self._topic_key(topic)
        if key not in self._data["entries"]:
            return None
        now = self._now()
        entry = self._data["entries"][key]
        entry["content"] = correction
        entry["status"] = "corrected"
        entry["updated_at"] = now
        entry["source"] = "user_correct"
        self.save()
        return {"topic": key, **entry}

    # ================= 条目管理（WebUI） =================

    @staticmethod
    def _public_entry(topic: str, e: dict) -> dict:
        """条目对外字段（WebUI 列表与详情共用）。"""
        return {
            "topic": topic,
            "content": e.get("content", ""),
            "status": e.get("status", "untested"),
            "enabled": e.get("enabled", True),
            "source": e.get("source", ""),
            "created_at": e.get("created_at", ""),
            "updated_at": e.get("updated_at", ""),
            "mod": e.get("mod", "general"),
            "kind": e.get("kind", "instance"),
        }

    def get_entry(self, topic: str) -> dict | None:
        """取单条条目（v0.21.11：详情弹窗 / 改名时校验原条目是否还在）。"""
        key = self._topic_key(topic)
        e = self._data["entries"].get(key)
        return self._public_entry(key, e) if e is not None else None

    def rename_exists(self, old_topic: str, new_topic: str) -> bool:
        """改名目标是否撞到别的已有主题（撞了就该拒绝，别静默覆盖）。"""
        old_key, new_key = self._topic_key(old_topic), self._topic_key(new_topic)
        return new_key != old_key and new_key in self._data["entries"]

    def list_entries(self, keyword: str = "", include_disabled: bool = True) -> list[dict]:
        """列出全部条目（WebUI 用，含禁用与 pending）。"""
        kw = keyword.strip().lower()
        out = []
        for topic, e in self._data["entries"].items():
            if kw and kw not in topic.lower() and kw not in e.get("content", "").lower():
                continue
            if not include_disabled and not e.get("enabled", True):
                continue
            out.append(self._public_entry(topic, e))
        out.sort(key=lambda x: x["updated_at"], reverse=True)
        return out

    def set_enabled(self, topic: str, enabled: bool) -> dict | None:
        """启用/禁用条目。"""
        key = self._topic_key(topic)
        if key not in self._data["entries"]:
            return None
        self._data["entries"][key]["enabled"] = bool(enabled)
        self.save()
        return {"topic": key, "enabled": bool(enabled)}

    def delete_entry(self, topic: str) -> bool:
        """删除条目（含墓碑，防旧文件继承复活）。

        v0.18.0 起不再联动清理其它预设文件——预设之间互相独立，
        跨预设同步交给用户在知识库页手动「复制/移动」。
        """
        key = self._topic_key(topic)
        if key in self._data["entries"]:
            del self._data["entries"][key]
            self._tombstone(key)
            self.save()
            return True
        return False

    def _tombstone(self, key: str) -> None:
        """把 topic key 记入墓碑列表（继承时跳过）。"""
        deleted = self._data.setdefault("deleted", [])
        if key not in deleted:
            deleted.append(key)

    def _purge_from_legacy_files(self, key: str) -> None:
        """同步删除所有旧指纹知识库文件里的同 topic 条目，彻底断源。"""
        for f in self.data_dir.glob("mod_knowledge_*.json"):
            if f.name == self.file_path.name:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if key in data.get("entries", {}):
                del data["entries"][key]
                try:
                    f.write_text(
                        json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8",
                    )
                except Exception:
                    pass

    def approve_entry(self, topic: str, approve: bool = True) -> dict | None:
        """审批 pending 条目：批准→verified（应用）；拒绝→删除。"""
        key = self._topic_key(topic)
        if key not in self._data["entries"]:
            return None
        if approve:
            self._data["entries"][key]["status"] = "verified"
            self._data["entries"][key]["source"] = "admin_approved"
            self._data["entries"][key]["updated_at"] = self._now()
            self.save()
            return {"topic": key, "status": "verified", "approved": True}
        del self._data["entries"][key]
        self.save()
        return {"topic": key, "approved": False}

    def pending_entries(self) -> list[dict]:
        """返回待审批条目。"""
        return [e for e in self.list_entries() if e["status"] == "pending"]

    def inherit_legacy_knowledge(self) -> dict:
        """【v0.18.0 起不再自动调用】继承旧整合包指纹的知识库条目。

        新流程：指纹变化不再自动继承/切换，改由用户在 WebUI 知识库页手动
        选择预设、复制/移动知识。本方法仅保留给「手动找回旧文件」的场景。

        扫描数据目录下所有 mod_knowledge_*.json（排除当前指纹），把当前库
        没有的条目合并进来，source 标记为 inherited。已有条目保留不动。
        """
        inherited = 0
        for f in sorted(self.data_dir.glob("mod_knowledge_*.json")):
            if f.name == self.file_path.name:
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            for topic, entry in data.get("entries", {}).items():
                if topic in self._data["entries"]:
                    continue  # 当前库已有，保留当前版本
                if topic in self._data.setdefault("deleted", []):
                    continue  # 墓碑：主人明确删除过，不复活
                entry = dict(entry)
                entry["source"] = f"inherited:{data.get('server_id', '?')}"
                self._data["entries"][topic] = entry
                inherited += 1
        if inherited:
            self.save()
        return {
            "inherited": inherited,
            "total": self.stats()["total"],
        }

    # ================= 统计/管理 =================

    def stats(self) -> dict:
        entries = self._data["entries"]
        match = self.matches_server()
        return {
            "server_id": self.server_id,
            "preset_id": self.preset_id,
            "preset_name": self.preset_name,
            "fingerprint": self.fingerprint or "",
            "bound": bool(self.fingerprint),
            "match": match,        # True=匹配 / False=不匹配 / None=未绑定
            "total": len(entries),
            "verified": sum(1 for e in entries.values() if e.get("status") == "verified"),
            "untested": sum(1 for e in entries.values() if e.get("status") == "untested"),
            "corrected": sum(1 for e in entries.values() if e.get("status") == "corrected"),
            "pending": sum(1 for e in entries.values() if e.get("status") == "pending"),
            "disabled": sum(1 for e in entries.values() if not e.get("enabled", True)),
            "template": sum(1 for e in entries.values() if e.get("kind") == "template"),
            "instance": sum(1 for e in entries.values() if e.get("kind") != "template"),
            "file": str(self.file_path),
            "state": self.state.as_dict(),
            "semantic": self.semantic_stats(),
        }

    def clear(self) -> dict:
        """清空本预设（换整合包/重开时使用）：条目清空并记墓碑防复活。

        v0.18.0 起只影响当前预设，不动其它预设文件（其它预设由用户在页面上管理）。
        """
        keys = list(self._data["entries"].keys())
        for k in keys:
            self._tombstone(k)
        self._data["entries"] = {}
        self.save()
        return {"cleared": len(keys)}


def compute_server_id(mods_dir: str) -> str:
    """计算整合包指纹：mods 目录全部 jar 的名称+大小+修改时间的哈希。"""
    try:
        h = hashlib.md5()
        entries = []
        for p in sorted(Path(mods_dir).glob("*.jar")):
            if p.name.lower().endswith(".disabled"):
                continue
            st = p.stat()
            entries.append(f"{p.name}|{st.st_size}|{int(st.st_mtime)}")
        h.update("|".join(entries).encode("utf-8"))
        return h.hexdigest()[:12]
    except Exception:
        return "unknown"


# ===================== 预设管理器（v0.18.0） =====================


def _preset_id() -> str:
    import secrets

    return "p" + secrets.token_hex(4)


class KnowledgePresetManager:
    """知识库预设槽位管理器。

    设计（v0.18.0）：
    - 每个预设 = 一个知识库文件 kb_preset_<id>.json，互不干扰
    - 预设指纹（fingerprint）由用户决定，**新建预设默认不绑定**；未绑定的预设在
      第一次写入知识时才绑定当时运行的服务端指纹
    - 服务端指纹变化时**不再自动切换/继承知识库**，只把差异记为「待提醒」，
      由 WebUI 在用户进入时弹全屏提示，用户在知识库页手动选择/绑定
    - 支持把某个预设整体「复制 / 移动」到另一个预设（冲突 topic 默认保留目标版本）
    """

    REG_NAME = "kb_presets.json"

    def __init__(self, data_dir: str, server_id: str, server_dir: str = "",
                 search_engine: str = DEFAULT_SEARCH_ENGINE,
                 semantic_enabled: bool = False,
                 rerank_enabled: bool = False):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.server_id = server_id or "unknown"
        # v0.21.9：记住「当前服务端目录」——指纹算不出区别（mods 空目录）时靠它认出换了服务端
        self.server_dir = (server_dir or "").strip()
        # v0.21.20：指纹是否「认不出服务端」（mods 与 plugins 都没有内容）——由指纹引擎给定
        self.server_weak: bool | None = None
        # v0.21.40：检索引擎（bm25=默认 / legacy=旧版），随预设实例下发
        self.search_engine = (
            search_engine if search_engine in SEARCH_ENGINES else DEFAULT_SEARCH_ENGINE
        )
        # v0.21.41：语义通道开关（默认关），随预设实例下发
        self.semantic_enabled = bool(semantic_enabled)
        self.embed_fn = None                     # async 嵌入调用，由插件注入
        # v0.21.42：重排序精排开关（默认关），随预设实例下发
        self.rerank_enabled = bool(rerank_enabled)
        self.rerank_fn = None                    # async 重排序调用，由插件注入
        self.reg_path = self.data_dir / self.REG_NAME
        self.reg: dict = self._load_registry()
        self.kb: ModKnowledgeBase | None = None
        self.reload_active()

    def set_search_engine(self, engine: str) -> str:
        """切换检索引擎并即时生效（当前内存 KB 直接换引擎，无需重建实例）。"""
        self.search_engine = engine if engine in SEARCH_ENGINES else DEFAULT_SEARCH_ENGINE
        if self.kb is not None:
            self.kb.set_search_engine(self.search_engine)
        return self.search_engine

    def set_semantic_enabled(self, on: bool) -> bool:
        """开关语义通道并即时生效（当前内存 KB 直接换，无需重建实例）。"""
        self.semantic_enabled = bool(on)
        if self.kb is not None:
            self.kb.embed_fn = self.embed_fn
            self.kb.set_semantic_enabled(self.semantic_enabled)
        return self.semantic_enabled

    def set_rerank_enabled(self, on: bool) -> bool:
        """开关重排序精排并即时生效（当前内存 KB 直接换，无需重建实例）。"""
        self.rerank_enabled = bool(on)
        if self.kb is not None:
            self.kb.rerank_fn = self.rerank_fn
            self.kb.set_rerank_enabled(self.rerank_enabled)
        return self.rerank_enabled

    # ---------------- 注册表 ----------------

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _load_registry(self) -> dict:
        if self.reg_path.exists():
            try:
                data = json.loads(self.reg_path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("presets"), list):
                    data.setdefault("active", None)
                    migrated = self._migrate_notice(data)
                    if self._recover_orphans(data) or migrated:
                        self._write_registry_raw(data)
                    return data
            except Exception:
                pass
        return self._migrate_legacy()

    def _migrate_notice(self, data: dict) -> bool:
        """把老 registry 的 notice 字段（永久 suppressed + 单个 ack）迁移为「轮次列表」（v0.21.8）。

        轮次 = (激活预设 × 服务端指纹)，弹窗与「本轮不再提醒」都按轮次记账 ——
        同一轮只弹一次，服务端指纹下次变动（轮次翻新）时重新获得一次弹窗机会。
        """
        n = data.get("notice")
        if not isinstance(n, dict):
            n = data["notice"] = {}
        migrated = False
        if not isinstance(n.get("popup_keys"), list):
            n["popup_keys"] = [str(n["ack"])] if n.get("ack") else []
            migrated = True
        if not isinstance(n.get("suppress_keys"), list):
            # 老的 suppressed=True 是「永久不再提醒」→ 迁移为「本轮不再提醒」：
            # 保留主人这次的选择，但指纹下次变动时会重新提示。
            n["suppress_keys"] = (
                [f"{data.get('active')}|{self.server_id}"] if n.get("suppressed") else []
            )
            migrated = True
        for legacy in ("suppressed", "ack"):
            if legacy in n:
                n.pop(legacy, None)
                migrated = True
        # 老记账（ack / suppressed）对应的就是当前服务端指纹那一轮，别让新一轮判定把它清掉
        n.setdefault("last_fp", self.server_id)
        return migrated

    def _recover_orphans(self, data: dict) -> bool:
        """注册表里缺失、但磁盘上存在的预设文件 → 补登记（防注册表丢失）。"""
        found = False
        known = {p.get("id") for p in data["presets"]}
        for f in sorted(self.data_dir.glob("kb_preset_*.json")):
            pid = f.stem[len("kb_preset_"):]
            if pid in known:
                continue
            fp = ""
            try:
                fp = str(json.loads(f.read_text(encoding="utf-8")).get("fingerprint") or "")
            except Exception:
                pass
            data["presets"].append({
                "id": pid,
                "name": f"恢复的预设 {pid}",
                "fingerprint": fp,
                "created_at": self._now(),
                "updated_at": self._now(),
            })
            found = True
        return found

    def _stamp_fp(self, pid: str, fp: str) -> None:
        """把指纹同时写进预设文件本身（文件自描述，便于迁移/恢复）。"""
        p = self.preset_path(pid)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            d["fingerprint"] = fp or ""
            p.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def _migrate_legacy(self) -> dict:
        """首次运行：把旧 mod_knowledge_<指纹>.json 逐个导入为预设（原文件保持不动）。"""
        presets: list[dict] = []
        for f in sorted(self.data_dir.glob("mod_knowledge_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            fp = str(data.get("server_id") or f.stem.replace("mod_knowledge_", ""))
            pid = _preset_id()
            body = {
                "preset_id": pid,
                "fingerprint": fp,
                "entries": data.get("entries", {}),
                "deleted": data.get("deleted", []),
                "migrated_from": f.name,
            }
            try:
                (self.data_dir / f"kb_preset_{pid}.json").write_text(
                    json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8"
                )
            except Exception:
                continue
            presets.append({
                "id": pid,
                "name": f"旧知识库 {fp}",
                "fingerprint": fp,
                "created_at": self._now(),
                "updated_at": self._now(),
            })
        if not presets:
            # 全新安装：建一个「默认知识库」预设，**不预设指纹**（首次写入时才绑定）
            pid = _preset_id()
            (self.data_dir / f"kb_preset_{pid}.json").write_text(
                json.dumps({"preset_id": pid, "fingerprint": "",
                            "entries": {}, "deleted": []},
                           ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            presets.append({
                "id": pid, "name": "默认知识库", "fingerprint": "",
                "created_at": self._now(), "updated_at": self._now(),
            })
        # 激活项：优先绑定当前服务端指纹的那个，否则最新的非空预设
        exact = next((p for p in presets if p["fingerprint"] == self.server_id), None)
        if exact is None:
            ranked = sorted(
                presets,
                key=lambda p: (self._file_entries(p["id"]) > 0, p["updated_at"]),
                reverse=True,
            )
            exact = ranked[0]
        reg = {
            "active": exact["id"],
            "presets": presets,
            "notice": {"popup_keys": [], "suppress_keys": []},
        }
        self._write_registry_raw(reg)
        return reg

    def _write_registry_raw(self, reg: dict) -> None:
        try:
            tmp = self.reg_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.reg_path)
        except Exception:
            pass

    def save_registry(self) -> None:
        self._write_registry_raw(self.reg)

    # ---------------- 预设读写 ----------------

    def preset_path(self, pid: str) -> Path:
        return self.data_dir / f"kb_preset_{pid}.json"

    def _file_entries(self, pid: str) -> int:
        p = self.preset_path(pid)
        if not p.exists():
            return 0
        try:
            return len(json.loads(p.read_text(encoding="utf-8")).get("entries", {}))
        except Exception:
            return 0

    def get(self, pid: str) -> dict | None:
        return next((p for p in self.reg["presets"] if p.get("id") == pid), None)

    def active_preset(self) -> dict | None:
        return self.get(self.reg.get("active") or "")

    def reload_active(self) -> ModKnowledgeBase | None:
        """按当前激活预设重建 KB 实例（切换预设后调用）。"""
        p = self.active_preset()
        if p is None:
            self.kb = None
            return None
        old_state = self.kb.state if self.kb is not None else None
        kb = ModKnowledgeBase(
            str(self.data_dir),
            server_id=self.server_id,
            preset_id=p["id"],
            preset_name=p.get("name") or p["id"],
            fingerprint=(p.get("fingerprint") or None),
            on_first_write=self.bind_active_if_needed,
            search_engine=self.search_engine,
            semantic_enabled=self.semantic_enabled,
            rerank_enabled=self.rerank_enabled,
        )
        kb.embed_fn = self.embed_fn
        kb.rerank_fn = self.rerank_fn
        if old_state is not None:
            kb.state = old_state
        self.kb = kb
        return kb

    def set_server_id(self, server_id: str, server_dir: str | None = None,
                      weak: bool | None = None) -> ModKnowledgeBase | None:
        """就地更新「当前服务端」基准（v0.21.7：换服务端不必重载插件）。

        只刷新比对基准与内存 KB 的 server_id，**不切换激活预设**——与 v0.18.0
        「指纹变化不自动切换、只提醒」的策略一致。

        v0.21.9：可一并更新服务端目录。指纹没变（例如两个服务端都是空 mods）但目录
        换了，也要算「换了服务端」，弹窗轮次要翻新，否则提醒永远不会再出现。

        v0.21.20：weak 由指纹引擎给出（mods/ 与 plugins/ 都没有内容 → 指纹认不出
        服务端），带给 notice() 用于弹窗额外警告。
        """
        sid = server_id or "unknown"
        ndir = self.server_dir if server_dir is None else (server_dir or "").strip()
        nweak = self.server_weak if weak is None else bool(weak)
        if sid == self.server_id and ndir == self.server_dir and nweak == self.server_weak:
            return self.kb
        self.server_id = sid
        self.server_dir = ndir
        self.server_weak = nweak
        return self.reload_active()

    # ---------------- 首次写入绑定 ----------------

    def bind_active_if_needed(self, kb: ModKnowledgeBase) -> str | None:
        """未绑定预设的首次知识写入 → 绑定当前服务端指纹。返回新指纹或 None。"""
        p = self.active_preset()
        if p is None or p.get("fingerprint"):
            return None
        p["fingerprint"] = self.server_id
        p["updated_at"] = self._now()
        kb.bind(self.server_id)
        self._stamp_fp(p["id"], self.server_id)
        self.save_registry()
        return self.server_id

    # ---------------- 预设操作 ----------------

    def create(self, name: str = "", copy_from: str | None = None) -> dict:
        """新建预设（默认不预设指纹）。copy_from 可指定从哪个预设复制条目。"""
        pid = _preset_id()
        entries, deleted = {}, []
        if copy_from:
            src = self.preset_path(copy_from)
            if src.exists():
                try:
                    d = json.loads(src.read_text(encoding="utf-8"))
                    entries = d.get("entries", {})
                except Exception:
                    entries = {}
        (self.data_dir / f"kb_preset_{pid}.json").write_text(
            json.dumps({"preset_id": pid, "fingerprint": "",
                        "entries": entries, "deleted": deleted},
                       ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        preset = {
            "id": pid,
            "name": (name or "").strip() or f"新预设 {len(self.reg['presets']) + 1}",
            "fingerprint": "",          # ← 不预设指纹
            "created_at": self._now(),
            "updated_at": self._now(),
        }
        self.reg["presets"].append(preset)
        self.save_registry()
        return preset

    def rename(self, pid: str, name: str) -> dict | None:
        p = self.get(pid)
        if p is None or not (name or "").strip():
            return None
        p["name"] = name.strip()
        p["updated_at"] = self._now()
        self.save_registry()
        if self.kb is not None and pid == (self.active_preset() or {}).get("id"):
            self.kb.preset_name = p["name"]
        return p

    def remove(self, pid: str) -> dict:
        """删除预设（至少保留一个）。删除激活预设时自动切到第一个。"""
        p = self.get(pid)
        if p is None:
            return {"ok": False, "error": "预设不存在"}
        if len(self.reg["presets"]) <= 1:
            return {"ok": False, "error": "至少要保留一个预设"}
        self.reg["presets"] = [x for x in self.reg["presets"] if x.get("id") != pid]
        try:
            self.preset_path(pid).unlink(missing_ok=True)
        except Exception:
            pass
        if self.reg.get("active") == pid:
            self.reg["active"] = self.reg["presets"][0]["id"]
            self.reload_active()
        self.save_registry()
        return {"ok": True, "removed": pid}

    def switch(self, pid: str) -> dict:
        p = self.get(pid)
        if p is None:
            return {"ok": False, "error": "预设不存在"}
        self.reg["active"] = pid
        self.save_registry()
        self.reload_active()
        return {"ok": True, "active": pid}

    def bind(self, pid: str, fingerprint: str | None) -> dict | None:
        """手动绑定/解绑预设指纹（fingerprint=None 表示解绑）。"""
        p = self.get(pid)
        if p is None:
            return None
        p["fingerprint"] = (fingerprint or "").strip()
        p["updated_at"] = self._now()
        self._stamp_fp(p["id"], p["fingerprint"])
        self.save_registry()
        if self.kb is not None and pid == self.reg.get("active"):
            self.kb.bind(p["fingerprint"] or None)
        return p

    def transfer(self, src_id: str, dst_id: str, mode: str = "copy") -> dict:
        """把 src 预设的条目整体复制/移动到 dst 预设（冲突 topic 默认保留目标版本）。"""
        if src_id == dst_id:
            return {"ok": False, "error": "源与目标不能是同一个预设"}
        src_p, dst_p = self.preset_path(src_id), self.preset_path(dst_id)
        sp, dp = self.get(src_id), self.get(dst_id)
        if sp is None or dp is None or not src_p.exists() or not dst_p.exists():
            return {"ok": False, "error": "预设文件不存在"}
        try:
            sd = json.loads(src_p.read_text(encoding="utf-8"))
            dd = json.loads(dst_p.read_text(encoding="utf-8"))
        except Exception as e:
            return {"ok": False, "error": f"读取失败: {e}"}
        s_entries, d_entries = sd.get("entries", {}), dd.setdefault("entries", {})
        moved = conflict = 0
        for topic, entry in list(s_entries.items()):
            if topic in d_entries:
                conflict += 1
                continue
            e = dict(entry)
            e["source"] = f"imported:{sp.get('name')}"
            d_entries[topic] = e
            moved += 1
        dd.setdefault("deleted", [])
        try:
            dst_p.write_text(json.dumps(dd, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            return {"ok": False, "error": f"写入失败: {e}"}
        if mode == "move":
            sd["entries"] = {}
            sd.setdefault("deleted", [])
            try:
                src_p.write_text(json.dumps(sd, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:
                pass
        # 刷新受影响的内存实例
        if self.reg.get("active") in (src_id, dst_id):
            self.reload_active()
        return {"ok": True, "count": moved, "conflicts": conflict,
                "mode": mode, "from": sp.get("name"), "to": dp.get("name")}

    # ---------------- 指纹不匹配提醒（v0.21.8 重做 / v0.21.9 补轮次口径） ----------------
    #
    # 规则：检测到「激活预设的指纹 ≠ 当前服务端指纹」就弹一次全屏大弹窗；
    #   这个弹窗以 (预设, 服务端) 为一个「轮次」，**同一轮只弹一次** ——
    #   服务端下次再变（或换到别的预设），轮次更新，弹窗重新获得一次机会。
    #   弹窗只要在 WebUI 里真的显示过就算数（不要求主人点按钮），
    #   所以刷新页面、切页签都不会反复骚扰。
    #
    #   v0.21.9：「服务端」的判据 = 指纹 + 服务器目录。只比指纹会漏掉一种情况：
    #   两个服务端都没装 mod（或 server_dir 指到了没有 mods 的空壳目录）时指纹算出来
    #   一模一样（d41d8cd98f00），于是「换服务端」被判成「同一轮」→ 弹窗再也不出现。

    _NOTICE_DEFAULT = {
        "popup_keys": [],      # 本轮（一个服务端世代）里已经弹过的预设，最多记 20 条
        "suppress_keys": [],   # 本轮里被「本轮不再提醒」静音的预设
        "last_fp": "",         # 记账时的服务端指纹 —— 它一变就作废旧记账（重新给一次机会）
        "last_dir": "",        # 记账时的服务端目录（v0.21.9）—— 指纹撞车时靠它认出换服
    }
    _MAX_NOTICE_KEYS = 20

    def _notice_state(self) -> dict:
        n = self.reg.setdefault("notice", {})
        if not isinstance(n, dict):
            n = self.reg["notice"] = {}
        for k, v in self._NOTICE_DEFAULT.items():
            if k in ("last_fp", "last_dir"):
                n.setdefault(k, "")
            elif not isinstance(n.get(k), list):
                n[k] = list(v)
        return n

    def _roll_round_if_needed(self) -> dict:
        """服务端一变（指纹或目录变了）→ 开新的一轮：作废上一轮的记账。

        这样「服务端下次变动之前只弹一次」才成立：同一服务端世代里每个预设只弹一次，
        服务端一变（哪怕变回旧值）就重新获得一次弹窗机会。
        """
        n = self._notice_state()
        changed = (n.get("last_fp") != self.server_id
                   or n.get("last_dir") != self.server_dir)
        if changed:
            # 指纹变了 → 换了整合包；指纹没变但目录变了 → 十有八九是两个服务端都没装 mod，
            # 指纹撞在一起了（此时弹窗里还会额外警告「mods 目录是空的」）
            n["last_fp"] = self.server_id
            n["last_dir"] = self.server_dir
            n["popup_keys"] = []
            n["suppress_keys"] = []
            self.save_registry()
        return n

    def notice_key(self, p: dict | None = None) -> str:
        """当前轮次里该预设的记账 key：预设 × 服务端指纹。"""
        p = p if p is not None else (self.active_preset() or {})
        return f"{p.get('id')}|{self.server_id}"

    def _remember(self, field: str, key: str) -> None:
        """记下本轮某个预设（列表去重 + 保留最近若干条，避免无限膨胀）。"""
        n = self._roll_round_if_needed()
        keys = [k for k in n[field] if k != key]
        keys.append(key)
        n[field] = keys[-self._MAX_NOTICE_KEYS:]

    def notice(self) -> dict:
        """是否需要弹「整合包指纹不匹配」全屏提示。"""
        p = self.active_preset()
        n = self._roll_round_if_needed()
        key = self.notice_key(p)
        preset_fp = (p or {}).get("fingerprint") or ""
        server_fp = self.server_id
        mismatch = bool(p) and bool(preset_fp) and preset_fp != server_fp
        suppressed = key in n["suppress_keys"]
        popped = key in n["popup_keys"]
        return {
            "show": bool(mismatch and not suppressed and not popped),
            "changed": mismatch,          # 兼容旧字段名：语义就是「不匹配」
            "mismatch": mismatch,         # 概览页常驻标记用它（不随弹窗消失）
            "suppressed": suppressed,
            "popped": popped,
            "key": key,
            "server_fp": server_fp,
            # v0.21.9：空 mods 目录 → 指纹恒为 d41d8cd98f00，认不出不同服务端，弹窗要额外警告
            # v0.21.20：优先用指纹引擎给出的 weak（mods/ 与 plugins/ 都没内容）；
            #          引擎缺席时（老路径 / 测试直接构造）退回旧判据
            "server_fp_weak": (server_fp == EMPTY_SERVER_FP if self.server_weak is None
                               else bool(self.server_weak)),
            "server_dir": self.server_dir,
            "preset_id": (p or {}).get("id"),
            "preset_name": (p or {}).get("name"),
            "preset_fp": preset_fp,
        }

    def mark_notice_shown(self) -> dict:
        """WebUI 把弹窗真正显示出来时回调：记下「这一轮已经弹过了」。

        只认「显示过」，不要求主人点确认按钮 —— 这就是「同一轮只弹一次」的实现。
        """
        self._remember("popup_keys", self.notice_key())
        self.save_registry()
        return self.notice()

    def ack_notice(self, suppress: bool = False) -> dict:
        """主人点了弹窗里的按钮：记本轮已弹；suppress=True 表示本轮不再提醒。"""
        key = self.notice_key()
        self._remember("popup_keys", key)
        if suppress:
            self._remember("suppress_keys", key)   # 只对本轮生效，指纹变动后自动恢复提示
        self.save_registry()
        return self.notice()

    def set_suppress(self, on: bool) -> dict:
        """开启/关闭「指纹不匹配提醒」（关闭=本轮静音；重新开启会立刻恢复本次提示）。"""
        n = self._roll_round_if_needed()
        key = self.notice_key()
        if on:
            self._remember("suppress_keys", key)
        else:
            n["suppress_keys"] = []
            n["popup_keys"] = [k for k in n["popup_keys"] if k != key]  # 本轮重新给一次弹窗
        self.save_registry()
        return self.notice()

    # ---------------- 展示 ----------------

    def list_presets(self) -> list[dict]:
        active = self.reg.get("active")
        out = []
        for p in self.reg["presets"]:
            fp = p.get("fingerprint") or ""
            out.append({
                "id": p.get("id"),
                "name": p.get("name") or p.get("id"),
                "fingerprint": fp,
                "bound": bool(fp),
                "active": p.get("id") == active,
                "entries": self._file_entries(p.get("id", "")),
                "match": (fp == self.server_id) if fp else None,
                "updated_at": p.get("updated_at", ""),
                "created_at": p.get("created_at", ""),
            })
        return out

    def status(self) -> dict:
        return {
            "server_fingerprint": self.server_id,
            "active": self.reg.get("active"),
            "presets": self.list_presets(),
            "notice": self.notice(),
        }

