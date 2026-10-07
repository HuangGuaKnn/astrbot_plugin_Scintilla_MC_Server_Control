# -*- coding: utf-8 -*-
"""v0.23.7 · B7：工作流区 6 个 Provider 位 = 下拉选择（对齐知识库模型先例）。

运行：
  <python> tests/test_v0237_wf_model_pick.py

背景（2026-10-05 立档）
========================
- 工作流区 6 个字段（主 Provider + 分类/判断/工程/实现/纠错）此前是**裸文本框**，
  用户得手敲 AstrBot 里的 Provider id；知识库区（嵌入/重排序）在 v0.21.43 已有
  「/kb/models + ensureProviderOption」的成熟下拉先例。
- 本单把这 6 个字段换成同款下拉：候选来自 /wf/models（AstrBot **已加载**的对话类
  Provider 实例，不读配置文件 —— 能选的就一定可用）；留空 = 运行时自动回退。

覆盖八件事：
  1. `_provider_insts("chat")` 取 provider_insts；embedding / rerank 老口径不变；
  2. `_provider_choices` 产出 [{id, model, type}]（字段缺失时给空串不崩）；
  3. AstrBot 侧异常 → 空列表（「没得选」而不是报错）；
  4. web_api：/wf/models 路由注册 + 复用 _provider_choices("chat") + 错误兜底；
  5. 前端：6 个字段已由 text input 变 select（旧 input 不得残留）；
  6. 前端：WF_MODEL_SEL_IDS 六件齐全 / loadWfModels 拉取 / ensureProviderOption
     兜底 / onchange 绑定 / 提示元素存在；
  7. CFG_FIELDS 六个绑定原样（键名不许漂）；
  8. schema：六个键仍在 workflow/items 且为 string（下拉化不改数据契约）。
"""
from __future__ import annotations

import ast
import json
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR  # noqa: E402

PASS = 0
FAIL = 0
ROOT = PLUGIN_DIR

WF_IDS = [
    "cfg_wf_provider", "cfg_wf_p_classifier", "cfg_wf_p_judge",
    "cfg_wf_p_engineer", "cfg_wf_p_implementer", "cfg_wf_p_corrector",
]
WF_KEYS = [
    "llm_provider_id", "agent_classifier_provider_id", "agent_judge_provider_id",
    "agent_engineer_provider_id", "agent_implementer_provider_id",
    "agent_corrector_provider_id",
]


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {extra}")


# ============ 从 main.py 抽出真实方法（AST 抽取，避开 astrbot 依赖） ============
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
TREE = ast.parse(MAIN_SRC)
PLUGIN_CLS = next(
    n for n in TREE.body
    if isinstance(n, ast.ClassDef) and n.name == "McControlPlugin"
)
WANTED = ("_inst_provider_id", "_provider_insts", "_provider_choices")
MOD = ast.Module(
    body=[m for m in PLUGIN_CLS.body
          if isinstance(m, ast.FunctionDef) and m.name in WANTED],
    type_ignores=[],
)
NS: dict = {"logging": logging, "logger": logging.getLogger("stub")}
exec(compile(ast.fix_missing_locations(MOD), "<main-extract>", "exec"), NS)

_inst_provider_id = getattr(NS["_inst_provider_id"], "__func__", NS["_inst_provider_id"])
_provider_insts = NS["_provider_insts"]
_provider_choices = NS["_provider_choices"]


def stub_inst(pid: str, model: str = "", ptype: str = "openai"):
    return SimpleNamespace(
        provider_config={"id": pid, "model": model or f"{pid}-model", "type": ptype}
    )


def make_self(chat=None, emb=None, rr=None, bomb=False):
    """按真实调用链拼桩：_provider_choices → self._provider_insts / _inst_provider_id。"""
    if bomb:
        ctx = SimpleNamespace(
            provider_manager=None,
            get_all_embedding_providers=lambda: (_ for _ in ()).throw(
                RuntimeError("AstrBot 侧爆炸")),
        )
    else:
        ctx = SimpleNamespace(
            provider_manager=SimpleNamespace(
                provider_insts=list(chat or []),
                rerank_provider_insts=list(rr or []),
            ),
            get_all_embedding_providers=lambda: list(emb or []),
        )
    s = SimpleNamespace(context=ctx)
    s._inst_provider_id = _inst_provider_id
    s._provider_insts = lambda kind: _provider_insts(s, kind)
    s._provider_choices = lambda kind, insts=None: _provider_choices(s, kind, insts)
    return s


def main() -> None:
    print("---- 一、_provider_insts：chat / embedding / rerank 三档 ----")
    chat = [stub_inst("openai-main", "gpt-4o"), stub_inst("claude-main", "claude-4-sonnet")]
    emb = [stub_inst("emb-1", "bge-m3", "embedding")]
    rr = [stub_inst("rr-1", "bge-reranker-v2", "rerank")]
    s = make_self(chat=chat, emb=emb, rr=rr)
    check("chat → provider_insts（2 条）", len(s._provider_insts("chat")) == 2)
    check("embedding 老口径不变（get_all_embedding_providers）",
          len(s._provider_insts("embedding")) == 1)
    check("rerank 老口径不变（rerank_provider_insts）",
          len(s._provider_insts("rerank")) == 1)

    print("---- 二、_provider_choices：下拉数据形态 ----")
    got = s._provider_choices("chat")
    check("chat choices = [{id, model, type}]（2 条）",
          got == [
              {"id": "openai-main", "model": "gpt-4o", "type": "openai"},
              {"id": "claude-main", "model": "claude-4-sonnet", "type": "openai"},
          ], str(got))
    check("choices 的 model 字段可用于「id · model」标签",
          all("model" in c and "id" in c for c in got))
    weird = SimpleNamespace()  # 无 provider_config、无 provider_id 的畸形实例
    check("畸形实例 → {id:'', model:'', type:''}（不崩）",
          s._provider_choices("chat", insts=[weird]) == [
              {"id": "", "model": "", "type": ""}], "")
    legacy = SimpleNamespace(provider_id="legacy-1")
    check("provider_config 缺失 → 回落 provider_id 属性",
          _inst_provider_id(legacy) == "legacy-1")
    check("都没有 → 空串", _inst_provider_id(object()) == "")

    print("---- 三、异常兜底：没得选 ≠ 报错 ----")
    b = make_self(bomb=True)
    check("provider_manager 异常 → chat 空列表", b._provider_insts("chat") == [])
    check("embedding 取数异常 → 空列表", b._provider_insts("embedding") == [])

    print("---- 四、web_api：/wf/models 路由 ----")
    api_src = (ROOT / "core" / "web_api.py").read_text(encoding="utf-8")
    check("路由已注册（reg ... /wf/models ... GET）",
          'reg(f"{PAGE_PREFIX}/wf/models", self.get_wf_models, ["GET"]' in api_src)
    check("处理器 get_wf_models 已定义", "async def get_wf_models" in api_src)
    check("★复用 _provider_choices(\"chat\")（与知识库同源）",
          '_provider_choices("chat")' in api_src)
    check("异常兜底：读不到就给 ok=False + error（不 500）",
          "读取模型列表失败" in api_src and '"ok": False' in api_src)
    check("返回 choices 键", '"choices": choices' in api_src)
    check("v0.23.7 · B7 来历注释在案", "v0.23.7 · B7" in api_src)

    print("---- 五、前端 HTML：6 个裸框 → 下拉 ----")
    html = (ROOT / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")
    for i in WF_IDS:
        check(f'{i} 已是 <select>', f'<select id="{i}"' in html)
        check(f'{i} 旧 text input 已清除', f'<input type="text" id="{i}"' not in html)
    check("提示元素 wf_model_tip 存在", 'id="wf_model_tip"' in html)
    _adv = re.search(r'<div class="hint adv" id="wf_model_advice">.*?</div>', html, re.S)
    adv = _adv.group(0) if _adv else ""
    check("★提速建议元素 wf_model_advice 存在（6 个 Provider 位下方）", bool(adv))
    # 文案与定稿逐字一致（去内联标签、空白归一后比对）—— 防日后被改写 / 漂移
    ADV_TEXT = ("提速建议：优先选择响应和速度快的模型（如 DeepSeek V4.1 Flash）。"
                "模型的思考强度由 AstrBot 模型服务商管理设置，"
                "推荐把推理强度设为 low 或 high，过高的思考强度会导致工作流处理速度过慢。")
    got = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", adv)).strip()
    check("★提速建议文案与定稿逐字一致", got == ADV_TEXT, repr(got[:60]))
    check("语气是「建议」而非「教做事」（无 请自行 / 必须 / 不要 / 禁止 / 不准）",
          not any(w in adv for w in ("请自行", "必须", "不要", "禁止", "不准")))
    check("显眼样式：class 带 adv + CSS 给色值字号 + 浅色主题单独取值",
          'class="hint adv"' in adv and ".hint.adv{" in html and "font-size:12px" in html
          and "color:var(--amber)" in html
          and ':root[data-theme="light"] .hint.adv{color:#8a5d06}' in html)
    check("位置：紧跟 5 个分工位之后、说明行之前",
          html.find('id="wf_model_advice"') > html.find('id="cfg_wf_p_corrector"')
          and html.find('id="wf_model_advice"') < html.find('id="wf_model_tip"'))

    print("---- 六、前端 JS：拉取 / 兜底 / 绑定 ----")
    m = re.search(r"const WF_MODEL_SEL_IDS = \[(.*?)\];", html, re.S)
    found = re.findall(r'"([^"]+)"', m.group(1)) if m else []
    check("★WF_MODEL_SEL_IDS 六件齐全", found == WF_IDS, str(found))
    check("loadWfModels 已定义", "async function loadWfModels" in html)
    check("★拉取 wf/models 接口", 'callGet("wf/models")' in html)
    check("fillWfModelSelects 已定义", "function fillWfModelSelects" in html)
    check("renderWfModelTip 已定义", "function renderWfModelTip" in html)
    check("★列表没回来也先补位（ensureProviderOption 兜底复用）",
          html.count("ensureProviderOption(sel, saved)") >= 2)
    check("空选项 = 自动回退（语义文案在案）",
          "回退会话默认 / 主 Provider" in html)
    check("★onchange 即时刷新提示", "el.onchange = renderWfModelTip" in html)
    check("loadSettings 里触发加载（≥2 处：缓存态 + 冷启动态）",
          html.count("loadWfModels();") >= 2, str(html.count("loadWfModels();")))
    check("v0.23.7 · B7 来历注释在案", "v0.23.7 · B7" in html)

    print("---- 七、CFG_FIELDS 绑定原样（键名不漂） ----")
    for i, k in zip(WF_IDS, WF_KEYS):
        check(f'{k} 绑定在册', f'["{i}","{k}","s"]' in html)

    print("---- 八、schema 契约不变 ----")
    sch = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    items = (sch.get("workflow") or {}).get("items") or {}
    for k in WF_KEYS:
        node = items.get(k)
        check(f"schema /workflow/items/{k} 仍为 string",
              isinstance(node, dict) and node.get("type") == "string",
              str(node)[:80])

    print()
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    if FAIL:
        print("失败清单见上 ✗")
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
