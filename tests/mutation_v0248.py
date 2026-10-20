# -*- coding: utf-8 -*-
"""v0.24.8 判据变异凭据：25 处关键边界逐条翻错，看专项用例会不会红。

为什么要有这支脚本
------------------
「用例跑绿」不等于「用例能拦」。v0.24.7 的 11 处变异凭据已经证明过一次；
v0.24.8 新增的专项断言同样需要证明自己不是摆设 —— 尤其是这轮
有两处是**判据自己漏扫**才留下的洞（`bool(self.plugin._cfg(`、留空提交 0），
第七轮又实测出三条**变异存活**的判据（前端 `parseCfgBool` 那三条）。

口径
----
* 每处只改一个锚点（``old`` 必须恰好出现 1 次），改完立刻跑指定用例；
* 期望用例**非 0 退出且输出里有判据失败行**（KILLED）；仍然全绿 = SURVIVED，说明判据没牙；
* 「非 0 退出」本身不算数：真崩（traceback + 异常名）、或退非 0 却看不到失败行，
  都记 INFRA-ERR，不冒充 KILLED；
* 无论结果如何都在 finally 里按原字节还原，并复核 sha256；
* 每处改动前把「原字节」落一份现场记录（系统临时目录，不落仓库），
  被 Ctrl+C 打断时下次启动自动还原 —— 半改不改的文件留在工作区比跑红更危险；
* 只碰工作区文件，不改版本号 / CHANGELOG / tag。

用法：``python tests/mutation_v0248.py``（可加 ``--only M3`` 单跑一条）
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
    """现场记录的位置：系统临时目录（**不落仓库** —— 根目录多一个 .json 会被门禁判红）。"""
    tag = hashlib.sha1(str(ROOT).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"scintilla_mutation_{tag}.json"


def restore_inflight() -> str:
    """上次被打断（Ctrl+C / 进程被杀）时留下的现场：把文件按原字节放回去。

    没有这一层的话，中断会留下一个**半改不改**的工作区文件：文件是坏的、脚本没跑完、
    没有任何记录说「这里动过」。实测踩过一次（core/web_api.py 停在被改写的状态），
    靠 `git status` 才发现 —— 换成没接版本控制的新文件就查不出来了。
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


CASES = [
    Case(
        "M1", "core/log_watcher.py",
        "self._max_workers = max(1, int(max_workers))",
        "self._max_workers = 1  # 变异：退回「名字叫单线程」的假上限",
        "tests/test_v0248_review_round3.py",
        "执行器并发上限退回 1 → P1 必须发现「并发不是 4」",
    ),
    Case(
        "M2", "core/log_watcher.py",
        "victims = set(self._callback_inflight) | set(self._callback_stuck)",
        "victims = set(self._callback_inflight)  # 变异：漏掉卡死僵尸",
        "tests/test_v0248_review_round3.py",
        "stop() 只取消在飞、不取消僵尸 → P2 必须发现僵尸仍活着",
    ),
    Case(
        "M3", "core/web_api.py",
        '_BROADCAST_UNCONFIRMED_STATUSES = frozenset({"inferred_success", "dispatched_unconfirmed"})',
        "_BROADCAST_UNCONFIRMED_STATUSES = frozenset()  # 变异：未确认态当作失败",
        "tests/test_v0248_review_round3.py",
        "未确认态不再单列 → P3 必须发现 ok / confirmed 的组合变了",
    ),
    Case(
        "M4", "core/strict_bool.py",
        """        (_log if logger is None else logger).warning(
            "配置项 %s 的值无法解析为布尔（%r），已按默认值 %s 处理",
            key or "(未命名)", value, bool(default),
        )
        return bool(default)""",
        """        (_log if logger is None else logger).warning(
            "配置项 %s 的值无法解析为布尔（%r），已按默认值 %s 处理",
            key or "(未命名)", value, bool(default),
        )
        return True  # 变异：无法判定时 fail-open""",
        "tests/test_v0248_review_round3.py",
        'strict_bool("maybe", False) 回落 True → P4 必须发现 fail-open',
    ),
    Case(
        "M5", "core/web_api.py",
        "        if required and not val:",
        "        if False and required and not val:  # 变异：required 空串放行",
        "tests/test_v0248_review_round3.py",
        "required 但全空白不再报错 → P5 必须发现空名字进了注册表",
    ),
    Case(
        "M6", "pages/mc_control/index.html",
        'else if(ty==="n"){ const x = parseFloat(el.value); if(!isNaN(x)) s[key] = x; }',
        'else if(ty==="n"){ const x = parseFloat(el.value); s[key] = isNaN(x) ? 0 : x; }  // 变异：留空提交 0',
        "tests/test_v0248_collect_settings.py",
        "留空不再跳过、改成提交 0 → B6 必须发现默认值被覆盖",
    ),
    # ---------------- GPT 第四轮复核（2057）：队列 / 播报 / 请求体 / 空服 / 长度 ----------------
    Case(
        "M7", "core/log_watcher.py",
        'self._queue: "Queue" = Queue(maxsize=self._max_queue)',
        "self._queue: \"Queue\" = Queue()  # 变异：排队无界",
        "tests/test_v0248_review_round4.py",
        "队列改回无界 → 第四轮 [1] 必须发现「灌 20 笔一个都不拒」",
    ),
    Case(
        "M8", "core/log_watcher.py",
        "            if cancel_futures:",
        "            if False:  # 变异：cancel_futures 只投哨兵、队列照跑",
        "tests/test_v0248_review_round4.py",
        "关闭不再取消排队任务 → 第四轮 [1] 必须发现「被取消的活儿还是跑了」",
    ),
    Case(
        "M9", "core/log_watcher.py",
        "        if fut.cancel():",
        "        if False and fut.cancel():  # 变异：超时只「不再等」，活儿留在队列里",
        "tests/test_v0248_review_round4.py",
        "run_blocking 超时不再撤回 → 第四轮 [1b] 必须发现那笔活儿白跑了一次",
    ),
    Case(
        "M10", "main.py",
        '        elif etype == EVENT_JOIN and self._cfg_bool("notify_join_leave", True):',
        '        elif etype == EVENT_JOIN and self._cfg("notify_join_leave", True):  # 变异：裸真值',
        "tests/test_v0248_review_round4.py",
        'notify_join_leave="false" 照发 → 第四轮 [2] 必须发现进服播报没关掉',
    ),
    Case(
        "M11", "core/web_api.py",
        '''        if not isinstance(data, dict):
            return {}, f"请求体需要 JSON 对象（收到 {type(data).__name__}）"''',
        '''        if not isinstance(data, dict):
            return {} if data else {}, ""  # 变异：非对象体静默当空对象''',
        "tests/test_v0248_review_round4.py",
        "数组体不再点名类型 → 第四轮 [3] 必须发现回包说的是「必填」而不是「需要对象」",
    ),
    Case(
        "M12", "core/command_result.py",
        "        if boundary_confirmed is False:",
        "        if False:  # 变异：无接收者无条件算成功",
        "tests/test_v0248_review_round4.py",
        "空服回执绕过边界 → 第四轮 [4] 必须发现「未确认被报成成功」",
    ),
    Case(
        "M13", "core/knowledge_base.py",
        "MAX_CONTENT_CHARS = 20000",
        "MAX_CONTENT_CHARS = 10 ** 9  # 变异：正文上限形同虚设",
        "tests/test_v0248_review_round4.py",
        "正文上限放大到 10 亿 → 第四轮 [5] 必须发现上限口径变了",
    ),
    Case(
        "M14", "core/knowledge_base.py",
        "    if c_len > MAX_CONTENT_CHARS:",
        "    if False and c_len > MAX_CONTENT_CHARS:  # 变异：正文超长不判",
        "tests/test_v0248_review_round4.py",
        "正文超长不判 → 第四轮 [5] 必须发现超长条目被放进库里",
    ),
    Case(
        "M15", "core/knowledge_base.py",
        'self.enabled = strict_bool(enabled, False, key="knowledge.set.enabled")',
        'self.enabled = bool(enabled)  # 变异：退回裸 bool()',
        "tests/test_v0248_review_round4.py",
        'set(enabled="false") 退回裸 bool() → 第四轮 [7] 必须发现「关」被读成「开」',
    ),
    Case(
        "M16", "core/knowledge_base.py",
        'if entry_enabled(e) and e.get("status") != "pending"',
        'if e.get("enabled", True) and e.get("status") != "pending"  # 变异：条目级退回真值',
        "tests/test_v0248_review_round4.py",
        '条目级 enabled 退回真值 → 第四轮 [8] 必须发现「禁用条目仍进检索池」',
    ),
    Case(
        "M17", "core/knowledge_base.py",
        "        changed = self._normalize_enabled_flags()",
        "        changed = False  # 变异：删掉 load() 的收口",
        "tests/test_v0248_review_round4.py",
        "删掉 load() 收口 → 第四轮 [8] 必须发现「内存里还是字符串」（这条此前是假绿：下游各自又解析了一遍）",
    ),
    Case(
        "M18", ".gitignore",
        "/*.log",
        "#/*.log 变异：拿掉兜底，只留按名字补的那几条",
        "tests/test_v0248_review_round4.py",
        "拿掉根目录 .log 兜底 → 第四轮 [9] 必须发现「随手起名的取证日志会被 git add -A 带走」",
    ),
    # ---------------- 第七轮（第六轮复核 B6 / B7 / B8 / B9 的靶子） ----------------
    Case(
        "M19", "core/knowledge_base.py",
        "        return parse_bool(raw)",
        "        return strict_bool(raw, True)  # 变异：读不懂退回 fail-open",
        "tests/test_v0248_kb_enabled_single_source.py",
        "口径回退成 fail-open（读不懂 → True）→ 新口径表必须发现「手改坏的值照常生效」",
    ),
    Case(
        "M20", "core/knowledge_base.py",
        "        changed = self._normalize_enabled_flags()\n"
        "        if self._migrate():\n"
        "            changed = True\n",
        "        if self._normalize_enabled_flags():\n"
        "            self.save()  # 变异：两处各写一次盘\n"
        "        if self._migrate():\n"
        "            self.save()\n"
        "        changed = False\n",
        "tests/test_v0248_kb_enabled_single_source.py",
        "收敛与迁移各写一次盘 → 新用例必须发现「同一批内容排了两次向量补算」",
    ),
    Case(
        "M21", "pages/mc_control/index.html",
        'const CFG_TRUE_TOKENS = new Set(["true", "1", "yes", "on", "y", "t"]);',
        'const CFG_TRUE_TOKENS = new Set(["true", "1", "yes", "on"]);  // 变异：词表靠人列',
        "tests/test_v0248_same_source_bool.py",
        "前端词表少掉单字母 y/t → 新逐格用例必须发现「输入表靠人列就会漏格」",
    ),
    Case(
        "M22", "run_release_verify.py",
        "    # 仓库根\n"
        '    return name.startswith("_") or name.lower().endswith(FORENSIC_SUFFIXES)',
        "    # 仓库根\n"
        "    return False  # 变异：根目录不再认取证产物",
        "tests/test_v0248_untracked_forensics.py",
        "门禁不再认根目录的取证产物 → 新用例必须发现「未跟踪的取证日志会被放行」",
    ),
    # ---------------- 第八轮（GPT 第七轮复核 B10 / B9 的靶子） ----------------
    Case(
        "M23", "pages/mc_control/index.html",
        'if(ty==="b") el.checked = (v===undefined||v===null) ? !!dft : parseCfgBool(v, dft);',
        'if(ty==="b") el.checked = !!v;  // 变异：整页回退成 !!v',
        "tests/test_v0248_same_source_bool.py",
        "设置页渲染入口整句换回 !!v → 新渲染判据必须发现「开关画错 53/122 格」"
        "（函数本体没动、文本哨兵也照样命中，只有这一层能抓）",
    ),
    Case(
        "M24", "run_release_verify.py",
        "    return sorted(bad)",
        "    return []  # 变异：发布包不再看已跟踪的取证产物",
        "tests/test_v0248_untracked_forensics.py",
        "发布包不再看已跟踪取证产物 → 第五节必须发现「误提交的探针跟着进包了」",
    ),
    # ---------------- 第九轮（GPT 第八轮复核 P2-1 / P2-2 的靶子） ----------------
    Case(
        "M25", "run_release_verify.py",
        "    return not any(dims.get(k) for k in HYGIENE_DIMENSIONS)",
        "    return True  # 变异：卫生守卫永远放行",
        "tests/test_v0243_asset_boundary.py",
        "卫生守卫永远放行 → v0.24.3 第四节必须发现「有失败维度也照样打 PASS」",
    ),
]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_case(case: Case) -> tuple[str, str]:
    path = ROOT / case.rel
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    old, new = case.old, case.new
    # 仓库里的文件是 CRLF（Windows 检出）——多行锚点按 LF 写，这里自动适配；
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
    # （用例正文里提到 AttributeError 之类是正常的 —— 早先按关键词裸查，把 [3] 那一节
    #   的正常输出也判成了「变异把文件写坏」。）
    infra = ("syntaxerror", "modulenotfounderror", "importerror", "nameerror",
             "attributeerror", "typeerror", "indentationerror")
    if "traceback (most recent call last)" in low and any(k in low for k in infra):
        return "INFRA-ERR", "变异把文件写坏（真崩了：traceback + 异常名），不算 KILLED"
    # 摘一条**真**失败行当证据：优先 `[FAIL]` 开头的那行；找不到才退回含「失败/FAIL」的行
    # （早先不分主次，把 `[ok] … 空服广播不是失败` 这种也摘出来当证据 —— 看着像判据没红）。
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
    print(f"v0.24.8 判据变异：{len(picked)} 处，解释器 {PY}")
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
