# -*- coding: utf-8 -*-
"""发布通道判定：**tag 是唯一来源**（v0.24.8 · GPT 第三轮复核 B7）。

为什么要有这支脚本
------------------
`release.yml` 原先靠「CHANGELOG 小节标题里有没有『预发布』三个字」决定
`gh release create --prerelease`，而同一条流水线里「上一个正式版」的基线
却是查 GitHub Release 的真实 `prerelease == false` 状态 —— 同一件事（这个版本
是不是预发布）有两处判据、两个来源：

* 改标题文字能改变发布性质，却没有任何校验拦着（v0.24.7 的标题没写「预发布」，
  靠人事后手动改，流程本身给不出正确答案）；
* 标题是给人看的文案，不是机器判据 —— 文案一改（去掉前缀、换措辞、加注），
  发布性质就跟着漂。

现在把判定收成一处：**tag 后缀**。tag 是不可变的、跟着 commit 走的、发出去就
改不了的事实：

    v0.24.8          → 正式版
    v0.24.8-rc.1     → 预发布（Release 标 pre-release，不占「Latest」）

`metadata.yaml` 的版本号始终是 `v0.24.8`（不带 -rc.N）—— 预发布只是**发布通道**，
不是另一个版本号；`--expect` 会把 tag 的 rc 后缀摘掉再比，避免「打了个 rc 标签
结果校验说版本不一致」。

用法（workflow 里就这么调）
--------------------------
    python run_release_channel.py --tag v0.24.8 --expect v0.24.8
    python run_release_channel.py --tag v0.24.8 --github-env >> "$GITHUB_ENV"
    ver="$(python run_release_channel.py --tag v0.24.8 --print version)"

退出码：0 = 判定成功；2 = tag 形状不合法；3 = 与 metadata 版本不一致。
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import NamedTuple

#: 只认 `vX.Y.Z` 与 `vX.Y.Z-rc.N` 两种形状。
#: 刻意不接受任意 semver 预发布后缀（beta / alpha / preview）——
#: 这个项目只用 rc 通道，多一种写法就多一条没人维护的路径。
TAG_RE = re.compile(r"^v(?P<ver>\d+\.\d+\.\d+)(?P<pre>-rc\.(?P<rc>\d+))?$")


class Channel(NamedTuple):
    tag: str
    version: str        # 用于比 metadata / 取 CHANGELOG 小节（不带 v、不带 -rc.N）
    prerelease: bool
    rc: int | None


def release_channel(tag: str) -> Channel:
    """把一个 tag 解析成发布通道；形状不合法抛 ValueError（绝不猜）。"""
    m = TAG_RE.match((tag or "").strip())
    if not m:
        raise ValueError(
            f"tag 形状不合法：{tag!r} —— 只接受 vX.Y.Z 或 vX.Y.Z-rc.N"
        )
    rc = int(m.group("rc")) if m.group("rc") else None
    return Channel(tag=tag.strip(), version=m.group("ver"),
                   prerelease=rc is not None, rc=rc)


def _norm_ver(v: str) -> str:
    v = (v or "").strip().strip('"').strip("'")
    return v[1:] if v.startswith("v") else v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="发布通道判定（tag 是唯一来源）")
    ap.add_argument("--tag", required=True, help="形如 v0.24.8 或 v0.24.8-rc.1")
    ap.add_argument("--expect", help="metadata.yaml 里的版本号（可带 v / 引号）")
    ap.add_argument("--github-env", action="store_true",
                    help="输出 PRERELEASE=--prerelease / PRERELEASE=（供 $GITHUB_ENV）")
    ap.add_argument("--print", dest="what", choices=["version", "prerelease", "tag", "rc"],
                    help="只打印某个字段，便于 shell 取值")
    args = ap.parse_args(argv)

    try:
        ch = release_channel(args.tag)
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    if args.expect is not None:
        want = _norm_ver(args.expect)
        if ch.version != want:
            print(f"[FAIL] 标签与 metadata.yaml 版本不一致（{ch.version} != {want}）",
                  file=sys.stderr)
            return 3

    if args.what:
        print({"version": ch.version,
               "prerelease": "true" if ch.prerelease else "false",
               "tag": ch.tag,
               "rc": "" if ch.rc is None else str(ch.rc)}[args.what])
        return 0

    if args.github_env:
        print(f"PRERELEASE={'--prerelease' if ch.prerelease else ''}")
        print(f"RELEASE_VERSION={ch.version}")
        return 0

    print(f"tag={ch.tag} version={ch.version} "
          f"channel={'预发布 (rc.%s)' % ch.rc if ch.prerelease else '正式版'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
