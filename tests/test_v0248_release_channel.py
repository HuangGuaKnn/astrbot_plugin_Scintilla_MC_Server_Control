# -*- coding: utf-8 -*-
"""发布通道判定的行为判据（v0.24.8 · GPT 第三轮复核 B7）。

判什么
------
1. **tag → 通道**：`vX.Y.Z` 是正式版，`vX.Y.Z-rc.N` 是预发布；其余形状一律拒绝
   （宁可红，也不许把看不懂的 tag 当正式版发出去）。
2. **rc 后缀不参与版本比对**：metadata 写 `v0.24.8`，tag 可以是 `v0.24.8-rc.3`。
3. **CLI 输出可用**：`--github-env` 写进 `$GITHUB_ENV` 的那两行格式正确；
   `--print version` 给 CHANGELOG 取小节用。
4. **单一来源**：`release.yml` 里不再有「按 CHANGELOG 文案判预发布」的判据，
   且校验与判定都调同一个脚本 —— 防止下一次有人把 `grep 预发布` 加回来。
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

import run_release_channel as rc  # noqa: E402

_PASS = 0
_FAIL = 0
_FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"[PASS] {name}")
    else:
        _FAIL += 1
        _FAILED.append(name)
        print(f"[FAIL] {name}" + (f"  →  {detail}" if detail else ""))


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PLUGIN / "run_release_channel.py"), *args],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60)


def part_channel() -> None:
    print("\n-- ① tag → 通道 --")
    c = rc.release_channel("v0.24.8")
    check("v0.24.8 → 正式版", (c.version, c.prerelease, c.rc) == ("0.24.8", False, None), str(c))
    for tag, n in (("v0.24.8-rc.1", 1), ("v0.24.8-rc.12", 12)):
        c = rc.release_channel(tag)
        check(f"{tag} → 预发布（rc={n}）",
              (c.version, c.prerelease, c.rc) == ("0.24.8", True, n), str(c))
    c = rc.release_channel("  v10.0.3  ")
    check("首尾空白被容忍（CI 变量偶尔带空格）", (c.version, c.tag) == ("10.0.3", "v10.0.3"), str(c))

    for bad in ("0.24.8", "v0.24", "v0.24.8.1", "v0.24.8-rc", "v0.24.8-rc.x",
                "v0.24.8-beta.1", "v0.24.8-rc.1.2", "", "v"):
        try:
            rc.release_channel(bad)
        except ValueError:
            check(f"非法 tag 被拒：{bad!r}", True)
        else:
            check(f"非法 tag 被拒：{bad!r}", False, "没有抛 ValueError")


def part_cli() -> None:
    print("\n-- ② CLI：与 workflow 的接线面 --")
    r = run_cli("--tag", "v0.24.8", "--expect", 'v0.24.8"')
    check("--expect 能容忍 v 前缀 / 引号（metadata 的写法）", r.returncode == 0,
          r.stderr.strip()[:200])
    r = run_cli("--tag", "v0.24.8-rc.2", "--expect", "v0.24.8")
    check("rc 标签与 metadata 的正式版本号一致（rc 不参与比对）", r.returncode == 0,
          r.stderr.strip()[:200])
    r = run_cli("--tag", "v0.24.9", "--expect", "v0.24.8")
    check("版本真的不一致时退出码非 0", r.returncode == 3, f"rc={r.returncode}")
    r = run_cli("--tag", "v0.24.9-rc.1", "--expect", "v0.24.8")
    check("rc 标签也只比版本号本身", r.returncode == 3, f"rc={r.returncode}")

    r = run_cli("--tag", "v0.24.8", "--print", "version")
    check("--print version 给出版本号（CHANGELOG 取小节用）", r.stdout.strip() == "0.24.8",
          repr(r.stdout))
    r = run_cli("--tag", "v0.24.8-rc.3", "--print", "prerelease")
    check("--print prerelease 输出 true", r.stdout.strip() == "true", repr(r.stdout))

    r = run_cli("--tag", "v0.24.8", "--github-env")
    lines = r.stdout.splitlines()
    check("正式版写进 $GITHUB_ENV 的 PRERELEASE 是空值",
          lines == ["PRERELEASE=", "RELEASE_VERSION=0.24.8"], repr(lines))
    r = run_cli("--tag", "v0.24.8-rc.1", "--github-env")
    lines = r.stdout.splitlines()
    check("预发布写进 $GITHUB_ENV 的是 --prerelease",
          lines == ["PRERELEASE=--prerelease", "RELEASE_VERSION=0.24.8"], repr(lines))

    r = run_cli("--tag", "v24.8")
    check("非法 tag 的退出码是 2（与版本不一致的 3 区分开）", r.returncode == 2,
          f"rc={r.returncode}")


def part_workflow() -> None:
    print("\n-- ③ release.yml：判据只剩一处 --")
    yml = io.open(PLUGIN / ".github" / "workflows" / "release.yml",
                  encoding="utf-8").read()

    check("校验步骤调 run_release_channel.py --expect",
          re.search(r'run_release_channel\.py --tag "\$GITHUB_REF_NAME" --expect', yml) is not None)
    check("发布通道步骤调同一支脚本的 --github-env",
          'run_release_channel.py --tag "$GITHUB_REF_NAME" --github-env' in yml)
    check("Release 说明按版本号取小节（--print version），不按 rc tag",
          '--print version' in yml and 'index($0, "## [" v "]")' in yml)

    # 反向：老的「按文案判」写法不许回来
    bad = [p for p in ('grep -q "预发布"',
                       'grep -m1 -F "## [${GITHUB_REF_NAME}]"',
                       'title="$(grep -m1')
           if p in yml]
    check("★没有任何一处再按 CHANGELOG 文案判预发布", not bad, str(bad))

    # 判据脚本里可以**解释**「以前按 CHANGELOG 判」，但可执行代码不许碰文案：
    # 去掉注释与字符串字面量后，token 流里不应再出现 CHANGELOG / open(。
    import tokenize
    code_tokens: list[str] = []
    with io.open(PLUGIN / "run_release_channel.py", encoding="utf-8") as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING, tokenize.NL):
                continue
            code_tokens.append(tok.string)
    code = " ".join(code_tokens)
    check("★判定脚本的可执行代码不读 CHANGELOG（判据不是文案）",
          "CHANGELOG" not in code, code[:200])
    check("判定脚本的可执行代码不做任何文件 IO（纯 tag 解析）",
          "open" not in code_tokens and "io" not in code_tokens, code[:200])

    r = run_cli("--tag", "v0.24.8-rc.1", "--expect", "v0.24.8")
    check("CI 会走的这条命令组合本身能跑通", r.returncode == 0, r.stderr.strip()[:200])


def main() -> int:
    print("=" * 68)
    print("发布通道：tag 是唯一来源（v0.24.8 · B7）")
    print("=" * 68)
    part_channel()
    part_cli()
    part_workflow()
    print(f"\n结果：{_PASS} 通过 / {_FAIL} 失败")
    if _FAIL:
        print("失败项：")
        for n in _FAILED:
            print("  -", n)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
