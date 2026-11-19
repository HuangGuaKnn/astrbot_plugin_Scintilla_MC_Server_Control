"""v0.24.7 second-round mutation catalog, run in the shared isolated runner."""

from __future__ import annotations

import sys

TEST = "tests/test_v0247_review_round2_p2.py"

HTML = "pages/mc_control/index.html"
SCHEMA = "_conf_schema.json"
KB = "core/knowledge_base.py"
WA = "core/web_api.py"
LW = "core/log_watcher.py"
WF = "core/workflow.py"
MF = "core/mod_fingerprint.py"

MUTATIONS = [
    dict(
        group="[1]", rel=KB, name="非法 status 的回落说明被吞掉（note 静默）",
        old='        return "untested", (\n'
            '            f"status「{status}」不是合法取值（合法：{\' / \'.join(VALID_STATUS)}）"\n'
            '            "，已按 untested 处理"\n'
            '        )',
        new='        return "untested", ""',
        expect="非法值必须",
    ),
    dict(
        group="[2]", rel=WA, name="非字符串字段强转 str（不再结构化拒绝）",
        old='            return "", f"{key} 需要字符串（收到 {type(raw).__name__}）"',
        new='            raw = str(raw)   # MUTATED：静默强转',
        expect="content=dict",
    ),
    dict(
        group="[3]", rel=KB, name="防穿越放宽：允许 id 里出现 /",
        old=r'            or any(ch in raw for ch in ("/", "\\", "\x00"))):',
        new=r'            or any(ch in raw for ch in ("\\", "\x00"))):',
        expect="防穿越的那一层",
    ),
    dict(
        group="[4]", rel=LW, name="阻塞 IO 线程改成非 daemon",
        old="daemon=True)",
        new="daemon=False)",
        expect="跑在 daemon 线程里",
    ),
    dict(
        group="[5]", rel=LW, name="stop() 不再取消在飞回调",
        old="        self._cancel_callbacks()\n"
            "        # v0.23.5 第八轮：先失效旧代、再取消",
        new="        # v0.23.5 第八轮：先失效旧代、再取消",
        expect="stop() 取消在飞回调任务",
    ),
    dict(
        group="[6]", rel=WF, name="被拒的那一条也计入字节额度",
        old="        if self.used_bytes + nbytes > self.max_bytes:\n",
        new="        if self.used_bytes + nbytes > self.max_bytes:\n"
            "            self.used_bytes += nbytes   # MUTATED：拒了也记账\n",
        expect="被拒的那一条",
    ),
    dict(
        group="[7]", rel=MF, name="缓存键退回秒级 mtime",
        old='key = f"{prefix}{p.name}|{st.st_size}|{st.st_mtime_ns}"',
        new='key = f"{prefix}{p.name}|{st.st_size}|{int(st.st_mtime)}"',
        expect="秒级缓存键不再命中",
    ),
    dict(
        group="[8]-a", rel=HTML, name="设置页 CFG_FIELDS 键名打错",
        old='["cfg_wf_max_bytes","agent_max_task_bytes","n"]',
        new='["cfg_wf_max_bytes","agent_max_task_bytes_typo","n"]',
        expect="设置页 CFG_FIELDS 登记了 agent_max_task_bytes",
    ),
    dict(
        group="[8]-b", rel=SCHEMA, name="schema 默认值与代码常量差 1",
        old='"default": 262144',
        new='"default": 262145',
        expect="schema 的默认值与代码常量 DEFAULT_MAX_TASK_BYTES 一致",
    ),
    dict(
        group="[8]-c", rel=WA, name="后端 INT_RANGES 上限与源码差 1",
        old='"agent_max_task_bytes": (512, 67108864),',
        new='"agent_max_task_bytes": (512, 67108865),',
        expect="前后端上下限同源",
    ),
    dict(
        group="[8]-d", rel=HTML, name="设置页 S_DEFAULTS 塞入假默认值",
        old="const S_DEFAULTS = {\n",
        new='const S_DEFAULTS = {\n  "cfg_wf_max_bytes": "262144",\n',
        expect="三键没有被塞进 S_DEFAULTS",
    ),
]


def mutation_cases():
    from mutation_all import Case

    python = (sys.executable, "-B", "-X", "utf8")
    command = (*python, TEST)
    daemon_check = (*python, "tests/mutation_v0247_round2.py", "--check-daemon")
    return [Case(m["group"], m["name"], m["rel"], m["old"], m["new"],
                 daemon_check if m["group"] == "[4]" else command, m["expect"],
                 baseline=daemon_check if m["group"] == "[4]" else None)
            for m in MUTATIONS]


def check_daemon_mutation() -> int:
    import test_v0247_review_round2_p2 as checks

    try:
        return checks.main()
    finally:
        # 非 daemon 变异会让解释器等待常驻 worker，必须在断言执行后关闭执行器。
        executor = checks.lw._DAEMON_RUNNER
        if executor is not None:
            executor.shutdown()


if __name__ == "__main__":
    if sys.argv[1:] == ["--check-daemon"]:
        raise SystemExit(check_daemon_mutation())

    from mutation_all import entry

    raise SystemExit(entry({"v0247_round2": mutation_cases()}, legacy=True))
