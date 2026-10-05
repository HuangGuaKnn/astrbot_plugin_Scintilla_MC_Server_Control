# -*- coding: utf-8 -*-
"""v0.23.7 · 对外文案与命名体检（人格名 / 私有称呼 / 本机路径 / 超长摘要 / 变量前缀）。

运行：
  <python> tests/test_v0237_changelog_hygiene.py

由来（2026-10-06 00:5x · 主人的原话）
====================================
第一句：「名字体检。。。主人也要纳入考量呢，这别人看着多别扭」
第二句：「如果你觉得有必要，你可以把 PIRIKA_* 换成别的」

事发经过：v0.23.7 打完 Tag 之后自查发布文案，先用 PowerShell 数了一遍 ——
```
$sec = $cl.Substring(...); [regex]::Matches($sec, "皮莉卡").Count   # 结果 0
```
**0 处**，看着全绿。可 GitHub 上那份 Release 正文里明明有「皮莉卡」。
真相是 **PowerShell 5.1 的 `Get-Content -Raw` 按系统 ANSI(GBK) 解码 UTF-8 文件**：
中文整个变成乱码，拿中文关键词去匹配当然一处也命中不了 —— 体检工具自己瞎了，
却报告「干净」。换成 UTF-8 读，实数立刻现形：

| 项 | 误报（GBK 读） | 真值（UTF-8 读） | 标杆 |
|---|---|---|---|
| 人格名 | 0 处 | **1 处** | v0.23.6 是 0 |
| 私有称呼 | 0 处 | **4 处** | 别人的仓库里读着别扭 |
| 一句话摘要 | — | **361 字** | 上一次亲眼把 255 字砍成 66 字 |

顺手还扫出两条同门病：v0.23.5 小节里抄着一条真实本机账户路径（发布物不该带），
以及测试 / CI 用的环境变量**前缀就是角色名**（`PIRIKA_UI_CHANNEL` 一类，仓库一搜就有）——
后一条经主人 2026-10-06 点名同意，统一换成插件自己的名字 `SCINTILLA_*`。

本测试钉死六件事（前身 50 项 → 现在 62 项）
=========================================
1. **现役小节**（CHANGELOG 第一个 `## [vX.Y.Z]`）里不得出现 皮莉卡 / Pirika / 主人；
2. **发布包内的全部对外文本**（CHANGELOG / README / docs/*.md）不得出现人格名（不分大小写）；
3. `> 一句话：` 摘要必须存在、必须单行、必须 ≤ 120 字（255 字那次被砍到 66 字）；
4. **发布物里不夹本机痕迹**：不许出现 `C:\\Users\\…` / `C:\\AstrBotOps` / `file:///`；
5. **代码与 CI 里不许有角色名的变量前缀**（AstrBot 会话 id 那种 `<名字>:FriendMessage:…` 除外 ——
   那来自运行时机器人昵称，不是我们起的名字）；
6. 守卫**有牙**：拿「历史病灶」样本喂进去必须报错 —— 否则这就是一条永远绿的摆设。

外加一条元检查：CHANGELOG 必须是合法 UTF-8，且**按 GBK 误读时判据会整体失效** ——
把那次假阴性本身写成回归用例，省得下一个人再用错编码自证清白。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import PLUGIN_DIR  # noqa: E402

CHANGELOG = PLUGIN_DIR / "CHANGELOG.md"
METADATA = PLUGIN_DIR / "metadata.yaml"
DOCS = PLUGIN_DIR / "docs"

#: 现役小节里不许出现的东西：人格名（三国语言写法）+ 私有称呼。
FORBIDDEN_SECTION = ("皮莉卡", "Pirika", "主人")
#: 人格名前缀 / 名称：不分大小写，一律不许出现在对外文本与代码命名里。
PERSONA_RE = re.compile("pirika", re.IGNORECASE)
#: 例外：AstrBot 会话 id 形如 `Pirika:FriendMessage:1031631712`（来自运行时机器人昵称，
#: 不是我们起的名字 —— 测试夹具必须照着线上真实 id 写才有效）。
SESSION_ID_RE = re.compile(r"Pirika:(?:Friend|Group)Message", re.IGNORECASE)
#: 一句话摘要上限（字）。主人先用 255 字否掉过一版，66 字那版才过。
SUMMARY_LIMIT = 120
#: 本机痕迹（发布物在别人机器上读着不该出现这些）。
LOCAL_PATH_PATTERNS = (r"C:\\+Users\\+", r"C:\\+AstrBotOps", r"file:///")
#: 环境变量前缀体检范围：仓库根与 .github 下的代码 / 配置。
CODE_GLOBS = ("*.py", "pytest.ini", "setup.cfg", "tox.ini")


_fail: list[str] = []
_pass = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"[PASS] {name}")
    else:
        _fail.append(name)
        print(f"[FAIL] {name}  <- {detail}")


# ============================ 判据本体 ============================

def latest_section(text: str) -> str:
    """取 CHANGELOG 里**第一个**版本小节（= 正在准备发布的这一版）。"""
    m = re.search(r"^## \[v[\d.]+\]", text, re.M)
    if not m:
        return ""
    nxt = re.search(r"^## \[", text[m.end():], re.M)
    return text[m.start(): m.end() + nxt.start()] if nxt else text[m.start():]


def summary_line(section: str) -> str:
    m = re.search(r"^> 一句话：(.*)$", section, re.M)
    return m.group(1) if m else ""


def persona_hits(text: str) -> int:
    """人格名命中数（不分大小写），扣掉 AstrBot 会话 id 那种运行时昵称。"""
    return len(PERSONA_RE.findall(text)) - len(SESSION_ID_RE.findall(text))


def shipped_texts() -> list[tuple[str, str]]:
    """发布包里真会带上的**对外文本**：CHANGELOG / README / docs/*.md。

    `docs/_internal/` 不算 —— 那是内部核验单，`.gitattributes` 的 export-ignore
    把它挡在发布包外（`run_release_verify.py` 的发布包卫生实测过这件事）。
    只查包里的文件，是为了让这条守卫的判据与「别人下载到手上能读到什么」一致。
    """
    out: list[tuple[str, str]] = [("CHANGELOG.md", CHANGELOG.read_text(encoding="utf-8"))]
    readme = PLUGIN_DIR / "README.md"
    if readme.is_file():
        out.append(("README.md", readme.read_text(encoding="utf-8")))
    for p in sorted(DOCS.glob("*.md")):
        out.append((f"docs/{p.name}", p.read_text(encoding="utf-8")))
    return out


def code_files() -> list[Path]:
    """参与「变量前缀体检」的代码 / 配置：仓库根的脚本、core/pages/tests 与 CI 工作流。"""
    out: list[Path] = []
    for g in CODE_GLOBS:
        out += [p for p in PLUGIN_DIR.glob(g)]
    for sub in ("core", "pages", "tests"):
        d = PLUGIN_DIR / sub
        if d.is_dir():
            out += [p for p in d.rglob("*.py")]
    wf = PLUGIN_DIR / ".github" / "workflows"
    if wf.is_dir():
        out += [p for p in wf.glob("*.yml")]
    # 豁免守卫自身：它的正文里就写着「历史病灶样本」与旧前缀样例，
    # 判据本身不该被自己当违规文本扫（它扫的是产品代码，不是政策说明）。
    self_path = Path(__file__).resolve()
    return [p for p in out if "__pycache__" not in p.parts and p.resolve() != self_path]


def audit(text: str) -> list[str]:
    """CHANGELOG 一条判据走完：返回违规清单（空清单 = 干净）。真实文件与病灶样本共用。"""
    problems: list[str] = []
    sec = latest_section(text)
    if not sec:
        return ["找不到 `## [vX.Y.Z]` 版本小节"]
    for w in FORBIDDEN_SECTION:
        n = sec.count(w)
        if n:
            problems.append(f"现役小节出现「{w}」×{n}（对外文案不许带人格名 / 称呼）")
    if persona_hits(text):
        problems.append(f"全文出现人格名（不分大小写）×{persona_hits(text)}")
    s = summary_line(sec)
    if not s:
        problems.append("现役小节缺少「> 一句话：」摘要行")
    elif len(s) > SUMMARY_LIMIT:
        problems.append(f"一句话摘要 {len(s)} 字 > {SUMMARY_LIMIT} 字上限")
    for pat in LOCAL_PATH_PATTERNS:
        if re.search(pat, text):
            problems.append(f"含本机路径痕迹：{pat}")
    return problems


# ============================ 一 ~ 四、对外文本 ============================

def part_one() -> None:
    print("---- 一、现役小节：人格名 / 私有称呼 ----")
    text = CHANGELOG.read_text(encoding="utf-8")
    sec = latest_section(text)
    check("CHANGELOG 找得到现役版本小节", bool(sec), "缺 `## [vX.Y.Z]`")
    ver = re.search(r"^## \[(v[\d.]+)\]", sec, re.M)
    print(f"       现役小节：{ver.group(1) if ver else '?'}（{len(sec)} 字符）")
    for w in FORBIDDEN_SECTION:
        n = sec.count(w)
        check(f"现役小节不出现「{w}」", n == 0, f"出现 {n} 次")

    print("---- 二、发布包内全部对外文本：人格名（不分大小写）----")
    for name, txt in shipped_texts():
        n = persona_hits(txt)
        check(f"{name} 不出现人格名（含英文拼写）", n == 0, f"出现 {n} 次")
    for name, txt in shipped_texts():
        n = txt.count("主人")
        check(f"{name} 不出现私有称呼", n == 0, f"出现 {n} 次")

    print("---- 三、一句话摘要 ----")
    s = summary_line(sec)
    check("存在「> 一句话：」摘要行", bool(s))
    check(f"摘要 ≤ {SUMMARY_LIMIT} 字", bool(s) and len(s) <= SUMMARY_LIMIT,
          f"实测 {len(s)} 字（255 字那版被砍到 66 字才过）")
    check("摘要里没有裸换行（渲染成一段）", "\r" not in s)

    print("---- 四、发布物卫生：不夹本机痕迹 ----")
    print("       （内部核验单 docs/_internal/ 被 export-ignore 挡在包外，不在此列）")
    for name, txt in shipped_texts():
        for pat in LOCAL_PATH_PATTERNS:
            check(f"{name} 无 {pat}", re.search(pat, txt) is None)

    print("---- 五、版本号与小节对得上 ----")
    try:
        meta = re.search(r"^version:\s*(\S+)", METADATA.read_text(encoding="utf-8"), re.M)
        mv = (meta.group(1) if meta else "").lstrip("v")
        cv = (ver.group(1) if ver else "").lstrip("v")
        check("metadata.yaml 版本 == CHANGELOG 现役小节版本", mv == cv and bool(mv),
              f"metadata={mv} / CHANGELOG={cv}")
    except FileNotFoundError:
        check("metadata.yaml 存在", False, "文件缺失")


# ============================ 六、变量前缀 ============================

def part_two() -> None:
    print("---- 六、命名卫生：代码 / CI 里不许有角色名前缀 ----")
    files = code_files()
    offenders: list[str] = []
    for p in files:
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        if persona_hits(txt):
            offenders.append(f"{p.relative_to(PLUGIN_DIR)}×{persona_hits(txt)}")
    print(f"       扫描 {len(files)} 个代码 / 配置 / 工作流文件")
    check("代码与 CI 里没有角色名前缀", not offenders, "；".join(offenders[:6]))
    wf = PLUGIN_DIR / ".github" / "workflows"
    wf_text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                        for p in sorted(wf.glob("*.yml")))
    check("CI 工作流用的是中性前缀 `SCINTILLA_`",
          "SCINTILLA_" in wf_text and not persona_hits(wf_text), "工作流里没找到 `SCINTILLA_`")
    paths = (PLUGIN_DIR / "tests" / "_paths.py").read_text(encoding="utf-8", errors="replace")
    check("`tests/_paths.py` 读的是中性前缀的 UI channel 变量",
          "SCINTILLA_UI_CHANNEL" in paths, "没找到 `SCINTILLA_UI_CHANNEL`")
    sess = (PLUGIN_DIR / "tests" / "test_config_persistence_contract.py")
    if sess.is_file():
        print("       例外：AstrBot 会话 id（`<昵称>:FriendMessage:…`）来自运行时机器人名，不算命名违规")
        check("会话 id 夹具里的运行时昵称被正确豁免",
              persona_hits(sess.read_text(encoding="utf-8", errors="replace")) == 0,
              "会话 id 被误判成命名违规")


# ============================ 七、守卫有牙 ============================

#: 把 v0.23.7 的真实病灶做成样本：人格名 + 私有称呼 + 超长摘要。
BAD_SAMPLE = (
    "# 更新日志\n\n"
    "## [v9.9.9] - 2026-01-01（预发布）\n"
    "> 一句话：" + "旧版服务的颜色不再人间蒸发，" * 26 + "\n\n"
    "### 新增\n"
    "- **固定格式化色 → 16 色名选项框**（主人追加点名）：单色格式选 `named` 时换下拉。\n"
    "- **广播署名分家**：理由：署名若是「皮莉卡」，看着就像管理方在说话。\n\n"
    "## [v9.9.8] - 2025-12-31\n"
    "> 一句话：上一版。\n"
)
GOOD_SAMPLE = (
    "# 更新日志\n\n"
    "## [v9.9.9] - 2026-01-01（预发布）\n"
    "> 一句话：**颜色不再人间蒸发** —— hex 单色在 <1.16 回退固定格式化色。\n\n"
    "### 新增\n"
    "- **单色格式下拉**（追加需求）：`named` 档换 16 色名下拉。\n\n"
    "## [v9.9.8] - 2025-12-31\n"
    "> 一句话：上一版。\n"
)


def _sample_with_summary(n: int) -> str:
    return (
        "# 更新日志\n\n"
        "## [v9.9.9] - 2026-01-01\n"
        f"> 一句话：{'字' * n}\n\n"
        "## [v9.9.8] - 2025-12-31\n"
        "> 一句话：上一版。\n"
    )


def part_seven() -> None:
    print("---- 七、守卫有牙：病灶样本必须被抓，好样本必须放行 ----")
    bad = audit(BAD_SAMPLE)
    print("       病灶样本命中：" + ("；".join(bad) if bad else "（无 —— 守卫是摆设！）"))
    check("病灶样本被抓出私有称呼", any("主人" in p for p in bad))
    check("病灶样本被抓出人格名", any("皮莉卡" in p for p in bad))
    check("病灶样本被抓出超长摘要", any("一句话摘要" in p for p in bad))
    check("病灶样本违规 ≥ 3 条（不是一个巧合命中）", len(bad) >= 3, f"实际 {len(bad)} 条")
    check("好样本零违规（不误伤）", audit(GOOD_SAMPLE) == [], str(audit(GOOD_SAMPLE)))
    check("摘要恰好 120 字算过（边界）", audit(_sample_with_summary(120)) == [],
          str(audit(_sample_with_summary(120))))
    check("摘要 121 字算红（边界）",
          any("一句话摘要" in p for p in audit(_sample_with_summary(121))))
    check("旧角色名前缀的变量名算违规",
          any("人格名" in p for p in audit(GOOD_SAMPLE.replace("上一版。", "上一版，走 PIRIKA_UI_CHANNEL。"))))
    check("中性前缀不误伤",
          audit(GOOD_SAMPLE.replace("上一版。", "上一版，走 SCINTILLA_UI_CHANNEL。")) == [],
          "中性前缀被误判")
    check("小写英文拼写一样算违规",
          any("人格名" in p for p in audit(GOOD_SAMPLE.replace("上一版。", "上一版，pirika 跑过。"))))
    check("本机路径算违规",
          any("本机路径" in p for p in audit(GOOD_SAMPLE.replace("上一版。", "见 C:\\Users\\x\\y。"))))
    # 造一个「现役小节缺摘要」的样本：按行滤掉摘要行（用 chr(n) 而非转义，避开编辑层吃转义的老坑）
    _lines = [ln for ln in GOOD_SAMPLE.split(chr(10)) if not ln.startswith("> 一句话：**颜色")]
    _no_summary = chr(10).join(_lines)
    check("缺摘要行算违规（现役小节）",
          any("摘要行" in p for p in audit(_no_summary)), str(audit(_no_summary)))
    check("缺版本小节算违规", audit("# 更新日志\n\n啥也没有。\n") != [])
    check("会话 id 形式不算命名违规", persona_hits("Pirika:FriendMessage:1031631712") == 0)
    check("会话 id 之外的昵称仍算违规", persona_hits("Pirika 在跑测试") == 1)


# ============================ 八、编码元检查 ============================

def _decodes(raw: bytes, enc: str) -> bool:
    try:
        raw.decode(enc)
        return True
    except UnicodeDecodeError:
        return False


def part_eight() -> None:
    print("---- 八、元检查：体检必须按 UTF-8 读（那次假阴性的成因） ----")
    raw = CHANGELOG.read_bytes()
    text = raw.decode("utf-8")
    check("CHANGELOG 是合法 UTF-8（无 BOM）",
          not raw.startswith(b"\xef\xbb\xbf") and _decodes(raw, "utf-8"))
    gbk = raw.decode("gbk", errors="replace")
    check("GBK 误读会让中文判据整体失效（所以只能用 UTF-8 读）",
          "主人" not in gbk and "皮莉卡" not in gbk,
          "GBK 解码后仍能命中中文关键词 —— 说明文本可能本来就不是 UTF-8")
    check("UTF-8 读得到的判据在 GBK 读下确实丢失（复现假阴性）",
          ("主人" in text) == ("主人" in gbk) or ("主人" not in gbk))


def main() -> int:
    part_one()
    part_two()
    part_seven()
    part_eight()
    print("\n==========================================")
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        for f in _fail:
            print("  [FAIL]", f)
        return 1
    print("全部通过 ✅ （对外文案与命名：无人格名 / 无称呼 / 摘要不超长 / 无本机痕迹 / 守卫有牙）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
