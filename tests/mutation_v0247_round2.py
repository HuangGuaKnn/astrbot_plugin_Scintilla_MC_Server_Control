"""v0.24.7 · 第二轮复核判据的**变异测试**（判据强度自检，内部用）。

判据「写了」不等于判据「能拦」。这个脚本把
``tests/test_v0247_review_round2_p2.py`` 里的 11 条关键断言逐条"翻掉"，
看对应测试会不会红：

  * KILLED   —— 变异让测试失败，且失败行落在**为该变异设计的那条判据**上；
  * SURVIVED —— 变异后测试照样全绿（判据空转，等于没写）。

覆盖：[1] 状态口径 / [2] 写接口硬化 / [3] 预设 ID 防穿越 / [4] daemon 线程 /
[5] 回调取消 / [6] 发送预算 / [7] 纳秒缓存键 / [8] 三键同源（4 处）。

安全约定：
  * 每个变异都是**单点文本替换**，锚点必须唯一（``count == 1``），否则中止；
  * 无论测试怎样结束，都在 ``finally`` 里按**原始字节**还原，并用 sha256 复核；
  * 全程只动源码，不产生提交、不改版本号、不碰 CHANGELOG。

用法（在仓库根目录）::

    python tests/mutation_v0247_round2.py

退出码 0 = 全部 KILLED；1 = 有 SURVIVED / 有还原失败。
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


def run_tests() -> tuple[int, list[str], str]:
    r = subprocess.run(
        [sys.executable, TEST], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (r.stdout or "") + (r.stderr or "")
    fails = [ln.strip()[2:] for ln in out.splitlines() if ln.strip().startswith("- ")]
    return r.returncode, fails, out


def main() -> int:
    print(f"变异测试目标：{TEST}（共 {len(MUTATIONS)} 处）\n")
    rows: list[tuple] = []
    for m in MUTATIONS:
        path = ROOT / m["rel"]
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8")
        # 工作区可能是 CRLF（.gitattributes 只管仓库内的入库形态）——
        # 多行锚点必须按文件自身的换行风格归一化，否则 count 恒为 0。
        nl = chr(13) + chr(10) if chr(13) + chr(10) in text else chr(10)
        old = m["old"].replace(chr(10), nl)
        new = m["new"].replace(chr(10), nl)
        n = text.count(old)
        if n != 1:
            print(f"  !! {m['group']} 锚点不唯一/缺失（count={n}）：{m['name']} —— 跳过")
            rows.append((m["group"], m["name"], "ANCHOR", 0, False))
            continue
        try:
            path.write_bytes(text.replace(old, new, 1).encode("utf-8"))
            rc, fails, out = run_tests()
            hit = [f for f in fails if m["expect"] in f]
            infra = "Traceback" in out and not fails
            verdict = "KILLED" if (rc != 0 and hit and not infra) else "SURVIVED"
            if infra:
                verdict = "INFRA-ERR"
        finally:
            path.write_bytes(raw)
        restored = hashlib.sha256(path.read_bytes()).hexdigest() == digest
        rows.append((m["group"], m["name"], verdict, len(fails), restored))
        print(f"  {m['group']:>6}  {verdict:<11} 失败{len(fails):>2} 还原{'✓' if restored else '✗'}  {m['name']}")
        for f in hit[:2]:
            print(f"           ↳ {f[:96]}")

    print("\n================ 结论 ================")
    bad = [r for r in rows if r[2] != "KILLED" or not r[4]]
    for group, name, verdict, n, restored in rows:
        mark = "✓" if (verdict == "KILLED" and restored) else "✗"
        print(f"{mark} {group:>6}  {verdict:<11}  {name}")
    print(f"\nKILLED {sum(1 for r in rows if r[2] == 'KILLED')}/{len(rows)}"
          f"   异常 {len(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
