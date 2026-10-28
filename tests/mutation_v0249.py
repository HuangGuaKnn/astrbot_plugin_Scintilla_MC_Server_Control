# -*- coding: utf-8 -*-
"""v0.24.9 判据变异凭据：5 处关键边界逐条翻错，看专项用例会不会红。

为什么要有这支脚本
-----------------
「用例跑绿」不等于「用例能拦」。v0.24.7 的 11 处、v0.24.8 的 28 处变异凭据
已经证明过两次；v0.24.9 新增的两栏栏级总开关（`tools.enable_all_tools` /
`knowledge.enable_all_knowledge`）同样需要证明自己不是摆设 ——
这一版的判据最容易变成「断言还在、但已经不可能失败」：
两栏语义、回执措辞、白名单契约都只差一两个字符。

口径（照抄 tests/mutation_v0248.py，不自己发明）
---------------------------------------------
* 每处只改一个锚点（``old`` 必须恰好出现 1 次），改完立刻跑指定用例；
* 期望用例**非 0 退出且输出里有判据失败行**（KILLED）；仍然全绿 = SURVIVED，说明判据没牙；
* 「非 0 退出」本身不算数：真崩（traceback + 异常名）、或退非 0 却看不到失败行，
  都记 INFRA-ERR，不冒充 KILLED；
* 无论结果如何都在 finally 里按原字节还原，并复核 sha256；
* 每处改动前把「原字节」落一份现场记录（系统临时目录，不落仓库 ——
  根目录多一个 .json 会被门禁判红），被 Ctrl+C 打断时下次启动自动还原；
* 只碰工作区文件，不改版本号 / CHANGELOG / tag，也不改任何用例本身。

用法：``python tests/mutation_v0249.py``（可加 ``--only M3`` 单跑一条）
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def _inflight_path() -> Path:
    """现场记录的位置：系统临时目录（**不落仓库** —— 根目录多一个 .json 会被门禁判红）。

    与 tests/mutation_v0248.py 用同一个文件名：两支脚本互斥跑，
    任一支被打断留下的现场，另一支启动时也会先还原。
    """
    tag = hashlib.sha1(str(ROOT).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"scintilla_mutation_{tag}.json"


def restore_inflight() -> str:
    """上次被打断（Ctrl+C / 进程被杀）时留下的现场：把文件按原字节放回去。

    没有这一层的话，中断会留下一个**半改不改**的工作区文件：文件是坏的、脚本没跑完、
    没有任何记录说「这里动过」。
    """
    p = _inflight_path()
    if not p.exists():
        return ""
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
        raw = base64.b64decode(rec["raw_b64"])
        target = ROOT / rec["rel"]
        target.write_bytes(raw)
        p.unlink()
        got = hashlib.sha256(target.read_bytes()).hexdigest()
        ok = "sha256 与现场一致" if got == rec["sha256"] else "⚠ sha256 与现场不一致，请复核"
        return f"已还原上次被打断留下的 {rec['rel']}（{ok}）"
    except Exception as exc:  # noqa: BLE001 —— 现场读不出来也不能拦住整轮
        return f"⚠ 现场记录 {p} 读不出来（{exc}），已丢弃 —— 请自行 git status 复核"


class Case:
    def __init__(self, cid: str, rel: str, old: str, new: str, test: str, note: str):
        self.cid = cid
        self.rel = rel
        self.old = old
        self.new = new
        self.test = test
        self.note = note


# v0.24.9 的 5 处锚点。多行锚点一律按 **LF** 写（仓库文件是纯 LF），
# run_case() 里保留了「LF 找不到就试 CRLF」的自动适配，无害。
CASES = [
    Case(
        "M1", "main.py",
        # 带上下文切片：单行 `        if name in _KNOWLEDGE_TOOLS:` 在 main.py 里出现 2 次
        # （_tool_master_off_reason 与 _tool_enabled），只贴单行会直接判「锚点不唯一」。
        "        # 这里直接读配置键（不调兄弟方法）：测试与降级装配里常只伪造 `_cfg_bool` / `_cfg`，\n"
        "        # 少一层跨方法依赖就少一处崩点。\n"
        "        if name in _KNOWLEDGE_TOOLS:",
        "        # 这里直接读配置键（不调兄弟方法）：测试与降级装配里常只伪造 `_cfg_bool` / `_cfg`，\n"
        "        # 少一层跨方法依赖就少一处崩点。\n"
        "        if False:",
        "tests/test_v0249_master_switch.py",
        "_tool_enabled 里知识库那一栏的分派失效 → 两栏各管各栏的判据必须红",
    ),
    Case(
        "M2", "main.py",
        '        elif not self._cfg_bool("enable_all_tools", True):',
        "        elif False:",
        "tests/test_v0249_master_switch.py",
        "工具总开关不再掐工具 → 「13 个工具全部不可用」必须红",
    ),
    Case(
        "M3", "main.py",
        # v0.24.9 后续：回执文案收成模块级常量（单一来源），锚点跟着挪到常量定义那一行
        '    "「LLM 工具」总开关已关闭（WebUI → 插件设置 → AI 能力 → LLM 工具开关）："',
        '    "「词典与知识库」总开关已关闭（WebUI → 插件设置 → AI 能力 → LLM 工具开关）："',
        "tests/test_v0249_master_switch.py",
        "回执把「LLM 工具」写错成另一栏 → 「回执点名「LLM 工具」」必须红",
    ),
    Case(
        "M4", "main.py",
        # 单行 `        if not self._knowledge_master_enabled():` 的 str.count 会被
        # 12 空格那处的**尾部**也算进去（=2），必须带上下文。
        "        归因错了，用户就会跑去改一个没问题的配置项（这正是真机/真人交互才照得出来的洞）。\n"
        '        """\n'
        "        if not self._knowledge_master_enabled():",
        "        归因错了，用户就会跑去改一个没问题的配置项（这正是真机/真人交互才照得出来的洞）。\n"
        '        """\n'
        "        if False:",
        "tests/test_v0249_master_switch.py",
        "词典不可用的归因不再看总开关 → 「报总开关已关闭」必须红",
    ),
    Case(
        "M5", "core/web_api.py",
        '        "enable_all_tools", "enable_all_knowledge",',
        '        "enable_all_knowledge",',
        "tests/test_settings_whitelist_contract.py",
        "后端保存白名单漏登记 enable_all_tools → 「没有『提交了但后端不认』的键」必须红",
    ),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_case(case: Case) -> tuple[str, str]:
    path = ROOT / case.rel
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    old, new = case.old, case.new
    # 仓库里的文件是 LF（Windows 检出可能变 CRLF）——多行锚点按 LF 写，这里自动适配；
    # 若按 LF 找不到、按 CRLF 正好一处，就用 CRLF 版本（LF 检出同样能跑）。
    if text.count(old) != 1 and text.count(old.replace("\n", "\r\n")) == 1:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    if text.count(old) != 1:
        return "INFRA-ERR", f"锚点出现 {text.count(old)} 次（应为 1）"
    before = sha(path)
    inflight = _inflight_path()
    inflight.write_text(json.dumps({
        "rel": case.rel,
        "cid": case.cid,
        "sha256": before,
        "raw_b64": base64.b64encode(raw).decode("ascii"),
    }, ensure_ascii=False), encoding="utf-8")
    try:
        path.write_bytes(text.replace(old, new).encode("utf-8"))
        res = subprocess.run(
            [PY, case.test], cwd=str(ROOT), capture_output=True, timeout=300,
            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
        )
        out = (res.stdout + res.stderr).decode("utf-8", "replace")
    finally:
        path.write_bytes(raw)
        inflight.unlink(missing_ok=True)
    after = sha(path)
    if after != before:
        return "INFRA-ERR", f"还原后 sha256 不一致（{before[:12]} -> {after[:12]}）"
    if res.returncode == 0:
        return "SURVIVED", "用例仍然全绿 —— 判据没拦住这处变异"
    low = out.lower()
    # 只有**真的崩了**才算 INFRA-ERR：必须同时出现 traceback 与异常名。
    # （用例正文里提到 AttributeError 之类是正常的，按关键词裸查会误判。）
    infra = ("syntaxerror", "modulenotfounderror", "importerror", "nameerror",
             "attributeerror", "typeerror", "indentationerror")
    if "traceback (most recent call last)" in low and any(k in low for k in infra):
        return "INFRA-ERR", "变异把文件写坏（真崩了：traceback + 异常名），不算 KILLED"
    # 摘一条**真**失败行当证据：优先 `[FAIL]` 开头的那行；找不到才退回含「失败/FAIL」的行
    # （v0.24.9 的 test_settings_whitelist_contract.py 用的是 `✗` + `FAILED n 项：` 汇总，
    #   所以这条退路这一版是真会用到的）。
    lines = [ln.strip() for ln in out.splitlines()]
    first = next((ln for ln in lines if ln.startswith("[FAIL]")), "")
    if not first:
        first = next((ln for ln in lines if "失败" in ln or "FAIL" in ln), "")
    if not first:
        # 非 0 退出、也没有 traceback、又看不到任何失败行 —— 可能是静默退出 / 超时 / 环境问题。
        # 「进程退非 0」本身不是判据红了，不能拿它冒充 KILLED。
        return "INFRA-ERR", f"非 0 退出（rc={res.returncode}）但输出里没有判据失败行，看不出是哪条判据红的"
    return "KILLED", first[:160]


def main(argv: list[str]) -> int:
    only = next((a.split("=", 1)[1] for a in argv if a.startswith("--only=")), "")
    if "--only" in argv:
        i = argv.index("--only")
        if i + 1 < len(argv):
            only = argv[i + 1]
    picked = [c for c in CASES if not only or c.cid == only]
    if not picked:
        print(f"没有匹配的变异：{only!r}")
        return 2
    print("=" * 72)
    print(f"v0.24.9 判据变异：{len(picked)} 处，解释器 {PY}")
    print("=" * 72)
    leftover = restore_inflight()
    if leftover:
        print(f"[现场还原] {leftover}")
    bad = 0
    for c in picked:
        verdict, detail = run_case(c)
        if verdict != "KILLED":
            bad += 1
        print(f"[{verdict:8s}] {c.cid}  {c.note}")
        print(f"           {c.rel} -> {c.test}")
        if detail:
            print(f"           {detail}")
    print("-" * 72)
    print(f"KILLED {len(picked) - bad}/{len(picked)}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
