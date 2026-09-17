"""手动指定嵌入 / 重排序模型（knowledge_embed_provider_id / knowledge_rerank_provider_id）回归测试。

不联网、不需要 AstrBot 运行时：main.py 里那几个挑选方法用 AST 抽出来单独编译，
再喂桩 Provider 实例跑行为；其余接线用源码静态核对。

覆盖九件事：

  1. 未指定 → 取第一个（与 v0.21.42 之前的老行为完全一致）；
  2. 指定命中 → 取指定的那个，且不打回落 warning；
  3. 指定了但找不到（模型被删 / 停用 / 改名）→ 回落第一个 + 打一条 warning，不抛异常；
  4. 一个候选都没有 → 返回 None（缺模型时通道静默退化，不能让检索报错）；
  5. 候选实例的 id 取自 provider_config["id"]（AstrBot 实例就是这么挂的），
     provider_config 缺失 / 非 dict 时给空串而不是崩；
  6. kb_model_status 如实报告 active / configured / fallback；
  7. kb_effective_label 带上「指定的没找到已回落」的提醒（提示文案必须说实话）；
  8. 后端接线：两个配置键在白名单里、/kb/models 路由已注册、保存时换模型会重新注入
     且换嵌入模型会整库重算向量（旧模型算的向量不通用）；
  9. 前端接线：两个下拉 + CFG_FIELDS 绑定 + 列表接口调用 + 「值不在列表里也先补位」
     的兜底（否则列表没回来时保存会把主人的选择悄悄清空）。

运行：
  python tests\\test_kb_model_pick.py
"""
from __future__ import annotations

import ast
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR  # noqa: E402

PASS = 0
FAIL = 0
ROOT = PLUGIN_DIR


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


# ============ 从 main.py 抽出真实方法（不 import，避开 astrbot 依赖） ============
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
TREE = ast.parse(MAIN_SRC)
PLUGIN_CLS = next(
    n for n in TREE.body
    if isinstance(n, ast.ClassDef) and n.name == "McControlPlugin"
)
WANTED = ("_inst_provider_id", "_pick_provider", "kb_model_status", "kb_effective_label")
MOD = ast.Module(
    body=[m for m in PLUGIN_CLS.body if isinstance(m, ast.FunctionDef) and m.name in WANTED],
    type_ignores=[],
)
NS: dict = {"logging": logging, "logger": logging.getLogger("stub")}
exec(compile(ast.fix_missing_locations(MOD), "<main-extract>", "exec"), NS)

_inst_provider_id = NS["_inst_provider_id"]
_pick_provider = NS["_pick_provider"]
kb_model_status = NS["kb_model_status"]
kb_effective_label = NS["kb_effective_label"]


class WarnSpy(logging.Handler):
    """抓 warning 条数 —— 用来区分「指定命中」和「回落」两条路径。"""

    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def stub_inst(pid: str, model: str = "", ptype: str = "openai"):
    """造一个 AstrBot 风格的 Provider 实例桩（id 挂在 provider_config 里）。"""
    return SimpleNamespace(
        provider_config={"id": pid, "model": model or f"{pid}-model", "type": ptype}
    )


def as_choices(insts):
    """按真实 _provider_choices 的口径把实例列表转成 dict 列表（id/model/type）。"""
    out = []
    for i in insts or []:
        cfg = getattr(i, "provider_config", None)
        cfg = cfg if isinstance(cfg, dict) else {}
        out.append({
            "id": str(cfg.get("id") or ""),
            "model": str(cfg.get("model") or ""),
            "type": str(cfg.get("type") or ""),
        })
    return out


def make_self(cfg: dict | None = None, embed=None, rerank=None, spy: WarnSpy | None = None):
    cfg = cfg or {}
    logger = logging.getLogger("stub.self")
    logger.handlers = []
    if spy is not None:
        logger.addHandler(spy)
    logger.setLevel(logging.DEBUG)
    self = SimpleNamespace(
        logger=logger,
        _cfg=lambda key, default=None: cfg.get(key, default),
        # 抽出来的是未绑定方法，桩 self 上按各自签名补回
        _inst_provider_id=lambda inst: _inst_provider_id(inst),
    )
    self._pick_provider = lambda insts, want, cn: _pick_provider(self, insts, want, cn)
    self._provider_insts = lambda kind: list((rerank if kind == "rerank" else embed) or [])
    self._provider_choices = lambda kind, insts=None: as_choices(
        self._provider_insts(kind) if insts is None else insts
    )
    self.kb_model_status = lambda: kb_model_status(self)
    return self


# ============ 行为：_inst_provider_id ============
print("\n[1] 实例取 id")
check("provider_config['id'] 正常取出", _inst_provider_id(stub_inst("qwen3-embed")) == "qwen3-embed")
check("id 两侧空白被裁掉", _inst_provider_id(stub_inst("  pad-me  ")) == "pad-me")
check("provider_config 缺失 → 空串不崩", _inst_provider_id(SimpleNamespace()) == "")
check("provider_config 非 dict → 空串不崩", _inst_provider_id(SimpleNamespace(provider_config="oops")) == "")
check("id 为 None → 空串", _inst_provider_id(SimpleNamespace(provider_config={"id": None})) == "")

# ============ 行为：_pick_provider ============
print("\n[2] 挑模型（三条路径）")
a, b, c = stub_inst("alpha"), stub_inst("beta"), stub_inst("gamma")

spy = WarnSpy()
self_ = make_self(spy=spy)
check("未指定 → 取第一个", _pick_provider(self_, [a, b, c], "", "嵌入") is a)
check("未指定 → 不打 warning", not spy.records)

spy = WarnSpy()
self_ = make_self(spy=spy)
check("指定命中 → 取指定的那个", _pick_provider(self_, [a, b, c], "gamma", "嵌入") is c)
check("指定命中 → 不打 warning", not spy.records)

spy = WarnSpy()
self_ = make_self(spy=spy)
picked = _pick_provider(self_, [a, b, c], "ghost-model", "重排序")
check("指定失效 → 回落第一个", picked is a)
check("指定失效 → 打了一条 warning", len(spy.records) == 1, f"records={spy.records}")
check("warning 里点出失效的 id 与用途",
      "ghost-model" in spy.records[0] and "重排序" in spy.records[0], str(spy.records))

check("空候选 + 未指定 → None（不抛异常）", _pick_provider(make_self(), [], "", "嵌入") is None)
check("空候选 + 指定 → None（不抛异常）",
      _pick_provider(make_self(spy=WarnSpy()), [], "whoever", "嵌入") is None)
check("id 两侧空白 / None 一视同仁",
      _pick_provider(make_self(), [a, b], "  beta  ", "嵌入") is b
      and _pick_provider(make_self(), [a, b], None, "嵌入") is a)

# ============ 行为：kb_model_status / kb_effective_label ============
print("\n[3] 状态与展示名")
emb = [stub_inst("qwen3-embed", "Qwen3-Embedding-4B"), stub_inst("other-embed")]
rr = [stub_inst("qwen3-reranker", "Qwen3-Reranker-8B")]

st = kb_model_status(make_self(embed=emb, rerank=rr))
check("未指定：configured 空、active = 第一个、不是回落",
      st["embedding"] == {"choices": as_choices(emb), "configured": "",
                          "active": "qwen3-embed", "fallback": False},
      str(st["embedding"]))
check("两条通道各自独立（rerank 与 embedding 不串）", st["rerank"]["active"] == "qwen3-reranker")

st = kb_model_status(make_self({"knowledge_embed_provider_id": "other-embed"}, embed=emb, rerank=rr))
check("指定命中：active 跟上、fallback=False",
      st["embedding"]["active"] == "other-embed" and st["embedding"]["fallback"] is False)

st = kb_model_status(make_self({"knowledge_rerank_provider_id": "dead"}, embed=emb, rerank=rr))
check("指定失效：active = 实际用的第一个、fallback=True",
      st["rerank"]["active"] == "qwen3-reranker" and st["rerank"]["fallback"] is True)
check("同一时刻两条通道互不影响（embedding 仍照常）",
      st["embedding"]["active"] == "qwen3-embed" and st["embedding"]["fallback"] is False)

st = kb_model_status(make_self({"knowledge_embed_provider_id": "x"}, embed=[], rerank=[]))
check("一个候选都没有 → active 空、fallback=False（没得用，谈不上回落）",
      st["embedding"]["active"] == "" and st["embedding"]["fallback"] is False)

label = kb_effective_label(make_self({"knowledge_embed_provider_id": "other-embed"}, embed=emb), "embedding")
check("展示名带 id 与模型名", label == "other-embed · other-embed-model", label)
label = kb_effective_label(make_self({"knowledge_embed_provider_id": "dead"}, embed=emb), "embedding")
check("回落时展示名带上提醒", "已回落到第一个" in label, label)
check("没有可用模型 → 空串（提示文案不瞎写）",
      kb_effective_label(make_self(embed=[]), "embedding") == "")

# ============ 静态接线 ============
print("\n[4] 接线核对")
schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
items = schema["knowledge"]["items"]
for key in ("knowledge_embed_provider_id", "knowledge_rerank_provider_id"):
    node = items.get(key, {})
    check(f"schema 有 {key}（string / 默认空）",
          node.get("type") == "string" and node.get("default") == "", str(node))

web = (ROOT / "core" / "web_api.py").read_text(encoding="utf-8")
check("后端白名单收下两个配置键",
      '"knowledge_embed_provider_id", "knowledge_rerank_provider_id",' in web)
check("已注册 /kb/models 路由", "/kb/models" in web and "def get_kb_models" in web)
check("候选来自已加载实例（kb_model_status）", "self.plugin.kb_model_status()" in web)
check("保存后换重排序模型 → 重新注入",
      "knowledge_rerank_provider_id" in web and "_inject_rerank_fn" in web)
check("保存后换嵌入模型 → 重新注入 + 整库重算向量",
      "_inject_embed_fn" in web and "_kb_build_semantic(force=True)" in web)

check("main.py 读两个配置键（留空 = 自动）",
      '_cfg("knowledge_embed_provider_id", "")' in MAIN_SRC
      and '_cfg("knowledge_rerank_provider_id", "")' in MAIN_SRC)
check("两条 resolve 都走 _pick_provider（不再硬取 [0]）",
      MAIN_SRC.count("self._pick_provider(") >= 3)
check("候选列表来自 provider_manager.rerank_provider_insts",
      "provider_manager.rerank_provider_insts" in MAIN_SRC
      and "get_all_embedding_providers" in MAIN_SRC)
# 回归护栏：曾经把 _provider_choices 产出的 dict 列表喂给 _pick_provider，
# 结果 active 永远是空串（dict 上没有 provider_config）
check("挑模型走实例列表，不是给前端的 dict 列表",
      "insts = self._provider_insts(kind)" in MAIN_SRC
      and "self._pick_provider(insts, want, cn)" in MAIN_SRC)

html = (ROOT / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
check("前端有嵌入模型下拉", 'id="cfg_kb_embed_p"' in html and 'id="cfg_kb_rerank_p"' in html)
check("前端 CFG_FIELDS 已挂绑定",
      '["cfg_kb_embed_p","knowledge_embed_provider_id","s"]' in html
      and '["cfg_kb_rerank_p","knowledge_rerank_provider_id","s"]' in html)
check("前端拉取候选列表接口", 'callGet("kb/models")' in html)
check("「值不在列表里也先补位」的兜底存在（防静默清空）",
      "function ensureProviderOption" in html and "ensureProviderOption(el, val)" in html)
check("提示里区分「自动」与「当前生效」",
      "自动 · 用 AstrBot 里加载的第一个" in html and "当前生效" in html)

print(f"\n{'='*46}\n  通过 {PASS} · 失败 {FAIL}\n{'='*46}")
sys.exit(1 if FAIL else 0)
