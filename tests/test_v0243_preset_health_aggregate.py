"""v0.24.3 回归：预设侧**聚合**落盘健康度必须能被前端持续看见（GPT v0.24.2 复核 N10）。

复核原文（P2）：**前端未消费预设聚合健康度，非活动预设错误不能持续显示** ——
`core/web_api.py:258` 的 state 只返回当前 KB 健康；`pages/mc_control/index.html`
页面加载走 state，两处重建 KBP 时又丢弃聚合 save_health；显示函数只读 state 的健康。
真实 API 场景：A 新建文件失败、B 成功且活动 KB 正常 —— `state.save_health.ok=True`
而 `kb/presets.save_health.ok=False`；首次错误提示条只活 3.5 秒，重载知识库页
**没有任何持续显示通道**。验收：非活动 A 失败 → B 成功 → 重新加载页面仍展示 A；
A 修复/删除后消失。

本文件钉住四组契约（全部走真实代码路径，不做白盒记账）：

  [1] 管理器：某个预设文件写失败时，聚合账为红、**失败那一笔不被后面的成功覆盖**，
      且 `parts_detail` 把部件键翻译成**预设名称/ID**（界面要能点名）
  [2] 修复 / 删除之后红要消失（不是「红得没有尽头」）
  [3] `GET /state` 要把预设侧聚合账一起给出去（页面加载就走这条路）
  [4] 前端静态契约：常驻横幅 + 三本账合并渲染 + 三处重建点都不许丢聚合

运行：
  python tests\\test_v0243_preset_health_aggregate.py
判据健壮性口径（v0.24.3 变异测试补齐）：被测对象坏掉时，**判据只许变红、不许自己崩** ——
所有 `parts_detail` / `parts[key]` 一律走 `parts_of()` / `part_of()` 安全取值。判据崩掉等于
「到底抓没抓住」说不清，而变异测试（F15）恰好会撞在这个坑上。

"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import knowledge_base as kbmod   # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.web_api import (                  # noqa: E402
    McControlWebApi,
)

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [XX] {name}" + (f"  ← {extra}" if extra else ""))


class _FakePlugin:
    """GET /state 只用到 _kb() / _kbman()，其余路径不碰。"""


def body(resp) -> dict:
    return json.loads(bytes(resp.body).decode("utf-8"))


def block_preset_file(man, pid: str) -> Path:
    """把某个预设文件**换成同名非空目录** —— 之后对它的读写必定失败。

    用真实文件系统造「写不进去」，不去打桩记账函数：这条缺陷的要害正是
    「写失败能不能被持续看见」，桩掉写入就等于把被测对象替换掉了。
    """
    p = man.preset_path(pid)
    backup = p.with_suffix(".json.real")
    shutil.copyfile(p, backup)
    p.unlink()
    p.mkdir()
    (p / "occupied.txt").write_text("occupied", encoding="utf-8")
    return backup


def unblock_preset_file(man, pid: str, backup: Path) -> None:
    p = man.preset_path(pid)
    shutil.rmtree(p)
    shutil.copyfile(backup, p)


def bad_parts(h: dict) -> list[dict]:
    """失败项（`parts_detail` 取不到就当空表 —— 见 `parts_of`）。"""
    return [x for x in parts_of(h) if x.get("ok") is False]


def parts_of(h: dict) -> list[dict]:
    """`parts_detail` 的安全取值（v0.24.3 · 变异测试补齐）。

    判据的**健壮性口径**：被测对象坏掉时，判据只许变红，不许自己崩 ——
    字段一消失就 KeyError 会打断整份判据（后面的组一条都不跑，「谁抓住了 bug」
    也就说不清）。变异测试 M1（摘掉 parts_detail）正是撞在这个坑上。
    """
    v = (h or {}).get("parts_detail")
    return v if isinstance(v, list) else []


def part_of(h: dict, key: str) -> dict:
    """`parts[key]` 的安全取值（同一口径）。

    返回值**不参与真假判断** —— 调用处一律写 `is True` / `is False`：
    缺失时得到 None，`not None` 会假绿，`is False` 才是真红。
    """
    v = ((h or {}).get("parts") or {}).get(key)
    return v if isinstance(v, dict) else {}


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        man = kbmod.KnowledgePresetManager(str(root / "kb"), "sid", str(root))

        print("================ [1] 一个预设写失败 → 聚合为红且能点名 ================")
        a = man.create("预设A")
        b = man.create("预设B")
        check("A / B 都建好了（各有自己的文件）",
              [p.get("id") for p in man.reg["presets"]][-2:] == [a["id"], b["id"]]
              and man.preset_path(a["id"]).is_file() and man.preset_path(b["id"]).is_file())
        check("此刻聚合账是健康的", man.save_health().get("ok") is True)

        backup_a = block_preset_file(man, a["id"])
        rb = man.bind(a["id"], "fp-aaa")          # 指纹回写 = 真实写入预设文件
        h1 = man.save_health()
        check("A 写失败 → 聚合 ok=False（不被后面任何成功抹掉）", h1.get("ok") is False,
              str(h1))
        check("A 自己的那一笔被记下来了",
              part_of(h1, f"preset_file:{a['id']}").get("ok") is False)
        bad1 = bad_parts(h1)
        check("parts_detail 点名到 A（一个失败项）", len(bad1) == 1, str(bad1))
        check("失败项带预设**名称**（界面不用去猜内部编码）",
              bad1 and bad1[0].get("preset_name") == "预设A", str(bad1))
        check("失败项带预设 ID 与类别",
              bad1 and bad1[0].get("preset_id") == a["id"]
              and bad1[0].get("kind") == "preset_file", str(bad1))
        check("失败项带原因（不是空话）", bool(bad1 and bad1[0].get("error")), str(bad1))
        check("B 那一笔仍是绿的（按文件记账，互不覆盖）",
              part_of(h1, f"preset_file:{b['id']}").get("ok") is True)

        rb_ok = man.bind(b["id"], "fp-bbb")        # B 成功 —— 不能把 A 的红盖掉
        h2 = man.save_health()
        check("B 随后写成功，A 的红依然在（这正是 N10 的场景）", h2.get("ok") is False
              and len(bad_parts(h2)) == 1, str(h2))

        print("\n--- 同一本账也随 kb/presets 系列接口出去（status() 口径）---")
        st = man.status()
        check("status() 带 save_health", isinstance(st.get("save_health"), dict))
        check("status() 口径与 save_health() 一致（同源一处实现）",
              (st.get("save_health") or {}).get("ok") is False
              and len(bad_parts(st.get("save_health") or {})) == 1)
        check("老字段 registry_save_ok 口径已改为聚合（保持兼容不改语义）",
              st.get("registry_save_ok") is False)

        print("\n--- 条目账与预设账要分得清（state.save_health 不该被预设失败带红）---")
        kb = kbmod.ModKnowledgeBase(str(root / "kb_entry"), server_id="sid", preset_id="p")
        check("知识库条目账仍然是健康的（两个失败域互不冒充）",
              kb.save_health().get("ok") is True)
        check("条目账字段形状未变（老消费者不受影响）",
              set(kb.save_health()) == {"ok", "error", "at"})
        check("预设账新增 parts_detail 但 parts 原样保留（老消费者不受影响）",
              "parts" in h2 and isinstance(h2["parts"], dict)
              and "parts_detail" in h2)
        _reg = [x for x in parts_of(h2) if x.get("kind") == "registry"]
        check("注册表那一项也有归属（不是只翻预设文件）",
              bool(_reg) and _reg[0].get("ok") is True, str(_reg))

        print("================ [2] 修复 / 删除之后，红要消失 ================")
        unblock_preset_file(man, a["id"], backup_a)
        man.bind(a["id"], "fp-aaa2")               # 同一个文件这次写成功
        h3 = man.save_health()
        check("A 修好后聚合回到健康（不是一次失败永久钉红）", h3["ok"] is True, str(h3))
        _a3 = part_of(h3, f"preset_file:{a['id']}")
        check("A 那一笔的 ok 变 True，error 清空",
              _a3.get("ok") is True and _a3.get("error") == "")

        print("\n--- 删除成功后消失（GPT 验收的后半句）---")
        man_del = kbmod.KnowledgePresetManager(str(root / "kb_del"), "sid", str(root))
        c = man_del.create("预设C")
        backup_c = block_preset_file(man_del, c["id"])
        man_del.bind(c["id"], "fp-c")              # 先弄出一条红
        _c_bad = [x for x in bad_parts(man_del.save_health()) if x.get("preset_id") == c["id"]]
        check("C 的红记上了（点名到「预设C」）",
              bool(_c_bad) and _c_bad[0].get("preset_name") == "预设C", str(_c_bad))
        unblock_preset_file(man_del, c["id"], backup_c)
        man_del.remove(c["id"])                    # 文件真的删掉了
        h_del = man_del.save_health()
        check("预设删掉之后聚合回到健康", h_del.get("ok") is True, str(h_del))
        check("parts_detail 里不再有 C（那一笔账整体撤掉）",
              not [x for x in parts_of(h_del) if x.get("preset_id") == c["id"]],
              str(h_del.get("parts_detail")))

        print("\n--- 删除**失败**时那一笔必须留着（文件还在磁盘上，红就该在）---")
        man_bad = kbmod.KnowledgePresetManager(str(root / "kb_bad"), "sid", str(root))
        d = man_bad.create("预设D")
        block_preset_file(man_bad, d["id"])
        man_bad.bind(d["id"], "fp-d")
        r_del = man_bad.remove(d["id"])
        h_bad = man_bad.save_health()
        check("删除失败如实回报（save_ok=False，不假报成功）", r_del.get("save_ok") is False,
              str(r_del))
        check("那一笔账仍留着（界面看得出来「还没删干净」）", h_bad.get("ok") is False, str(h_bad))
        check("注册表里它已经没了（内存动作照旧生效，界面不该装作没删）",
              not [p for p in man_bad.reg["presets"] if p.get("id") == d["id"]])
        _d_bad = [x for x in bad_parts(h_bad) if x.get("preset_id") == d["id"]]
        check("名字如实退回 ID 形态（预设已不在注册表，不编一个名字）",
              bool(_d_bad) and _d_bad[0].get("preset_name") == f"预设 {d['id']}",
              str(h_bad.get("parts_detail")))

        print("================ [3] GET /state 要带预设侧聚合账 ================")
        man2 = kbmod.KnowledgePresetManager(str(root / "kb2"), "sid", str(root))
        a2 = man2.create("非活动预设A")
        b2 = man2.create("活动预设B")
        man2.switch(b2["id"])                       # B 是活动预设，A 不是
        block_preset_file(man2, a2["id"])
        man2.bind(a2["id"], "fp-x")                 # A（非活动）写失败
        man2.bind(b2["id"], "fp-y")                 # B（活动）写成功

        api = McControlWebApi(_FakePlugin())
        kb_api = kbmod.ModKnowledgeBase(str(root / "kb_api"), server_id="sid", preset_id="p")
        api._kb = lambda: kb_api                     # noqa: E731
        api._kbman = lambda: man2                    # noqa: E731
        payload = body(asyncio.run(api.get_state()))

        check("回包仍然 ok", payload.get("ok") is True)
        check("状态 shape 未被破坏（老字段都在）",
              all(k in payload for k in
                  ("ok", "state", "stats", "entries", "pending", "save_health",
                   "state_save_warning", "presets", "active_preset", "notice")))
        check("条目账如实为绿（GPT 场景：state.save_health.ok=True）",
              (payload.get("save_health") or {}).get("ok") is True,
              str(payload.get("save_health")))
        ph = payload.get("preset_save_health")
        check("state 带上了预设侧聚合账（页面的持续通道就靠它）",
              isinstance(ph, dict) and ph.get("ok") is False, str(ph))
        badph = bad_parts(ph or {})
        check("聚合账里能点名到非活动预设 A",
              len(badph) == 1 and badph[0].get("preset_name") == "非活动预设A", str(badph))
        check("活动预设 B 不在失败项里",
              all(x.get("preset_id") != b2["id"] for x in badph), str(badph))

        print("--- 拿不到账本的老对象不许把 GET /state 炸了 ---")

        class _Boom:
            def save_health(self):
                raise RuntimeError("账本炸了")

        class _Weird:
            save_health = "不是可调用"

        check("save_health() 抛异常 → 退回空账（接口不炸）",
              McControlWebApi._preset_save_health(_Boom()) == {})
        check("save_health 不是可调用 → 退回空账", McControlWebApi._preset_save_health(_Weird()) == {})
        check("正常对象原样透传（含 parts_detail 兜底）",
              McControlWebApi._preset_save_health(man2).get("ok") is False)

        print("\n--- 静态：get_state 的源码里确实挂着这一项 ---")
        src = (PLUGIN_DIR / "core" / "web_api.py").read_text(encoding="utf-8")
        seg = src.split("async def get_state", 1)[1].split("async def ", 1)[0]
        check('get_state 里写入了 "preset_save_health"', '"preset_save_health"' in seg)
        check("且用的是同一本账（_preset_save_health(man)），不是另抄一份判据",
              "self._preset_save_health(man)" in seg)
        check("老口径 save_health (条目账) 一字未动（旧用例仍成立）",
              '"save_health": kb.save_health()' in seg)

    print("\n================ [4] 前端：持续通道 + 三本账合并渲染 ================")
    html = (PLUGIN_DIR / "pages" / "mc_control" / "index.html").read_text(encoding="utf-8")

    check("有常驻横幅元素（复用不自动消失的 .banner 原语）",
          'class="banner" id="kb_health"' in html)
    check("横幅不是 setNotice 管的（那条 3.5 秒就消失，正是 N10 的病根）",
          'setNotice($("kb_health")' not in html
          and 'setNotice($("kb_health"), ' not in html)

    # 健康度这一族（healthErrLine / presetBadParts / saveHealthLines / renderKbSaveHealth）
    # 从第一个助手函数取到下一个无关函数之前 —— 判据盯的是「整族合起来的口径」，
    # 只截 renderKbSaveHealth 一个函数会漏掉它调用的助手（第一版判据就是这么错的）
    fn = html.split("function healthErrLine(", 1)[1].split("function collectSettings(", 1)[0]
    fnbody = fn.split("function renderKbSaveHealth(", 1)[1]
    check("渲染函数体里不再有「读到一本红就 return」的短路（旧实现在这里短路）",
          "return;" not in fnbody, fnbody[:400])
    check("三本账都摊开：条目账 + 开关状态账 + 预设聚合账",
          "KB.save_health" in fn and "state_save_warning" in fn and "KBP.save_health" in fn)
    check("预设的账要按名称/ID 点名（消费后端 parts_detail）",
          "parts_detail" in fn and "preset_name" in fn)
    check("横幅是持续显示的那一支（banner show bad）", "banner show bad" in fn)
    check("横幅写入在提示条之前（持续性不吃提示条的顺序）",
          fn.index("banner show bad") < fn.index("setNotice("), fn[:600])
    check("横幅内容做 HTML 转义（预设名是用户输入，不许注入）",
          "lines.map(l => esc(l))" in fn)

    check("loadKnowledge 接住 state 里的预设聚合账（页面加载这条路）",
          "r.preset_save_health || KBP.save_health || null" in html)
    n_keep = html.count("save_health:r.save_health || KBP.save_health || null")
    check(f"三处 KBP 重建点统统不许丢聚合（实际 {n_keep} 处）", n_keep >= 3, str(n_keep))
    n_redraw = html.count("renderKbSaveHealth();")
    check(f"重建之后都要重绘健康条（实际 {n_redraw} 处）", n_redraw >= 3, str(n_redraw))
    check("KBP 声明里带 save_health 字段（否则重建时就地丢失）",
          re.search(r"let KBP = \{[^}]*save_health: null", html) is not None)
    check("三个重建点分别在 presetOp / presetNotice / checkFingerprintNotice 里",
          all(f"async function {name}(" in html
              for name in ("presetOp", "presetNotice", "checkFingerprintNotice")))
    check("正常时整条隐藏（不打扰）",
          'bar.className = "banner"; bar.innerHTML = ""' in fn)

    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        print("\n[XX] 失败项:")
        for f in _fail:
            print("  -", f)
        return 1
    print("\n[ok] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
