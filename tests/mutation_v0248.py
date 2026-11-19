# -*- coding: utf-8 -*-
"""v0248 mutation catalog. Runs in the shared isolated runner."""
from __future__ import annotations

import argparse
import sys


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
        # 第九轮：实现改了（多了 deep 分支），锚点跟着回写 —— 锚点失配会记 INFRA-ERR，
        # 那是「工具要维护」，不是「用例没牙」，两者不能混为一谈。
        '        if len(parts) == 2 and (parts[0] != "tests" or name in FORENSIC_ALLOW):\n'
        "            return False\n"
        '        return name.startswith("_") or name.lower().endswith(FORENSIC_SUFFIXES)',
        '        if len(parts) == 2 and (parts[0] != "tests" or name in FORENSIC_ALLOW):\n'
        "            return False\n"
        "        return False  # 变异：根目录不再认取证产物",
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
    # ---------------- 第十轮（GPT 第九轮复核 P2 残余的靶子） ----------------
    Case(
        "M26", "run_release_verify.py",
        "    if absent:\n"
        '        raise ValueError(f"漏传的卫生维度 {absent}（清单见 HYGIENE_DIMENSIONS）")',
        "    if False:  # 变异：漏传维度不再报错\n"
        '        raise ValueError(f"漏传的卫生维度 {absent}（清单见 HYGIENE_DIMENSIONS）")',
        "tests/test_v0243_asset_boundary.py",
        "漏传维度不再报错 → 第四节必须发现「少传一个维度照样放行」",
    ),
    Case(
        "M27", "run_release_verify.py",
        '    re.compile(r"^data/legacy_items/\\d+(?:\\.\\d+)*\\.json$"),\n'
        '    re.compile(r"^data/legacy_registry/items_\\d+(?:\\.\\d+)*\\.json$"),',
        '    re.compile(r"^data/"),  # 变异：data/ 整片放行\n'
        '    re.compile(r"^data/legacy_registry/items_\\d+(?:\\.\\d+)*\\.json$"),',
        "tests/test_v0248_untracked_forensics.py",
        "运行数据目录整片放行 → 第五节必须发现「data/_probe.json 与目录内的 _probe.json 都溜过去」",
    ),
    # ---------------- 第十二轮（GPT 第十一轮复核的最后一条收紧） ----------------
    Case(
        "M28", "run_release_verify.py",
        "        if norm in ARCHIVE_FORENSIC_ALLOW_PATHS:",
        '        if norm.rsplit("/", 1)[-1] in ARCHIVE_FORENSIC_ALLOW_PATHS:  # 变异：退回 basename',
        "tests/test_v0248_untracked_forensics.py",
        '路径白名单退回 basename → 第五节必须发现「docs/_conf_schema.json / tools/requirements.txt 溜过去」',
    ),
]


def main(argv: list[str]) -> int:
    from mutation_all import entry, registered_cases

    parser = argparse.ArgumentParser(description=__doc__)
    cases = registered_cases(sys.modules[__name__])
    parser.add_argument("--only", choices=[c.criterion for c in cases])
    args = parser.parse_args(argv)
    selected = [c for c in cases if not args.only or c.criterion == args.only]
    return entry({"v0248": selected}, legacy=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
