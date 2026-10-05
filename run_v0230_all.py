# -*- coding: utf-8 -*-
"""本机「一键复现」：**全部**用例都跑（回归 + UI 全部，含观察期与取证脚本）。

跑法：<python> run_v0230_all.py

与 `run_release_verify.py` 的分工：
  · run_release_verify.py = **门禁**（CI / 发版同款）：静态 + 回归 + UI 契约类（硬门禁），
    任何一项红就挡住发版；逐文件超时，超时 = 失败并连子进程树一起收掉。
  · 本脚本 = **全量复现**（本机排查用）：连观察期的渲染 / 度量类与取证脚本一起跑。

两者共用同一套跑测循环与清单（`run_release_verify` + `tests/_ui_manifest.py`），
不再各自维护一份 —— v0.23.5 第五轮：旧版本是一次性 `subprocess.run()` 且**没有超时**，
UI 用例挂死时整条命令就再也出不来了（GPT 复核）。

UI 用例需要真实浏览器：本机走系统 Edge（默认），CI 上设 SCINTILLA_UI_CHANNEL=bundled。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_release_verify as RV  # noqa: E402


if __name__ == "__main__":
    sys.exit(RV.main(["--all-ui"]))
