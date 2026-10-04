# -*- coding: utf-8 -*-
"""v0.23.5 外部复核（第七轮）回归：向量缓存严格校验 / 半行尾巴与截断轮转 /
runner UTF-8 链路 / 监听代际复用 / 短文件等长重写检测。

GPT 第六轮核验对第六轮提交（3b14caa）复查后指出六处残留（全部 P2）。本批逐条
落地，并且**每一条都配一个能复现原缺陷的断言**（基线：先在本文件的未修版本上
跑红，修完跑绿）：

  A. 结构不一致的 `.vec.npz` 仍被当成「可用」：`load()` 只验矩阵维度，行数 /
     主题数 / 哈希数对不上时 `missing()` / `prune()` 直接 `IndexError` ——
     普通 build 救不回来，只能 `force=True` 整库重算。
  B. 「完整行 + 未成行尾巴」同批到达时，尾巴没有存回 `_partial`：下一轮从旧
     offset 重读整段尾巴（读放大），与「只读新增」的设计承诺相悖。
  C. 有半行缓冲（`_partial`）时，轮转判据只看 `size < _pos` —— 文件被原地截断
     到 `_pos < size < _pos + len(_partial)` 时识别不了，之后从旧偏移读到的
     一直是错位内容（新日志漏播、旧半行被拼到新内容上）。
  D. 发布 runner 没给子进程统一 UTF-8：GBK 管道下测试打印 `⊆` 会在子进程里
     先炸 UnicodeEncodeError，把一个通过的测试判成失败；主进程也只钉了
     errors、没把 stdout 的编码钉到 UTF-8。
  E. `stop()` 释放执行器后，卡死 IO 的 `_io_inflight` 残留在 watcher 全局字段
     上；复用同一对象 `start()` 时，新执行器的所有 IO 都被旧计数挡在闸门外
     （初始化定位失败 → 监听全面停摆）。
  F. 文件不足 256 字节（头指纹恒为空串）时，「同 inode、同长度、内容原地替换」
     检测不到：旧内容被替换后新内容永远不会被读取。

运行：
  <AstrBot python> tests\\test_v0235_review_round7.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402

add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import log_watcher as LW  # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.knowledge_base import (  # noqa: E402
    ModKnowledgeBase,
    SemanticIndex,
)

PLUGIN = PLUGIN_DIR
sys.path.insert(0, str(PLUGIN))
import run_release_verify as RV  # noqa: E402

try:
    import numpy as np
except Exception:                                    # noqa: BLE001
    np = None

KB_SRC = io.open(PLUGIN / "core" / "knowledge_base.py", encoding="utf-8").read()
LW_SRC = io.open(PLUGIN / "core" / "log_watcher.py", encoding="utf-8").read()
RV_SRC = io.open(PLUGIN / "run_release_verify.py", encoding="utf-8").read()

_pass = 0
_fail: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global _pass
    if cond:
        _pass += 1
        print(f"  [ok] {name}")
    else:
        _fail.append(name)
        print(f"  [FAIL] {name}" + (f"  ｜ {extra}" if extra else ""))


def fn_src(src: str, name: str, cls: str | None = None) -> str:
    """取某个函数 / 方法的源码片段（ast 定位，避免正则误抓同名的别处）。"""
    tree = ast.parse(src)
    node = None
    if cls:
        for top in ast.walk(tree):
            if isinstance(top, ast.ClassDef) and top.name == cls:
                for sub in top.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name == name:
                        node = sub
    else:
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)) and top.name == name:
                node = top
    assert node is not None, f"找不到 {cls}.{name}"
    return ast.get_source_segment(src, node) or ""


async def _embed(texts):
    return [[0.1, 0.2, 0.3] for _ in texts]


def _make_watcher(root, interval=0.05):
    seen: list = []

    async def on_event(etype, player, detail):
        seen.append((etype, player, detail))

    return LW.LogWatcher(str(root), on_event, poll_interval=interval), seen


def _force_gbk_env() -> dict:
    """把当前进程环境临时伪装成 GBK 机器。

    本机可能已设 PYTHONUTF8=1 / PYTHONIOENCODING=utf-8（会掩盖 GBK 场景），
    这里先摘掉、再只留 PYTHONIOENCODING=gbk 复刻目标环境；返回快照供还原。
    """
    saved = {}
    for k in ("PYTHONIOENCODING", "PYTHONUTF8"):
        saved[k] = os.environ.get(k)
        os.environ.pop(k, None)
    os.environ["PYTHONIOENCODING"] = "gbk"
    return saved


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ==================== A 向量缓存：结构不一致必须被拒绝 ====================
def part_a() -> None:
    print("========== [A] 向量缓存：结构校验失败 → 拒绝 + 待补算 + 普通 build 恢复 ==========")
    load_src = fn_src(KB_SRC, "load", cls="SemanticIndex")
    check("静态：load() 校验行数 / 主题数 / 哈希数一致（缺一条就会带坏结构进内存）",
          "matrix.shape[0]" in load_src and "len(topics)" in load_src
          and "len(hashes)" in load_src)
    check("静态：load() 还验主题唯一性与有限数值",
          "len(set(topics))" in load_src and "isfinite" in load_src)
    ready_src = fn_src(KB_SRC, "semantic_ready", cls="ModKnowledgeBase")
    check("静态：semantic_ready() 自备结构一致性检查（不依赖 load 的善后）",
          "shape[0]" in ready_src and "hashes" in ready_src)

    if np is None:
        print("  [skip] 环境缺 numpy —— 跳过真实 npz 行为段")
        return

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)

        # —— 坏缓存样本 1：矩阵 3 行、hashes 只有 2 个（三方行数对不上）——
        # 语义通道先保持「关」（构造时 _sem=None），写完坏 npz 再开 ——
        # 这样 set_semantic_enabled(True) 会真正走 load()，坏缓存才进得来。
        kb = ModKnowledgeBase(str(td / "a"), server_id="stub", preset_id="p")
        kb.save_entry("条目甲", "黄铜锭怎么合成", status="verified")
        kb.save_entry("条目乙", "钻石剑怎么做", status="verified")
        kb.save_entry("条目丙", "末影龙怎么打", status="verified")
        keys = list(kb._searchable().keys())
        n = len(keys)
        kb.vec_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            kb.vec_path,
            matrix=np.zeros((n, 4), dtype="float32"),
            topics=np.array(keys, dtype=object),
            hashes=np.array(["h1", "h2"], dtype=object),      # 少一个 → 结构坏
        )

        kb.set_semantic_enabled(True)
        kb.embed_fn = _embed
        check("★坏缓存被拒绝：索引回空，不带坏结构进内存",
              kb._sem is not None and kb._sem.matrix is None and kb._sem.topics == [],
              f"matrix={'None' if kb._sem is None or kb._sem.matrix is None else kb._sem.matrix.shape}")
        check("★拒绝留痕：semantic_stats 摊出 cache_error（不再静默）",
              bool(str(kb.semantic_stats().get("cache_error", ""))),
              str(kb.semantic_stats()))
        check("semantic_ready() 不为 True（结构不完整不许上岗）",
              kb.semantic_ready() is False)
        check("pending_vectors() 报全量（空索引 = 全部待补算）",
              kb.pending_vectors() == n, f"{kb.pending_vectors()} vs {n}")

        async def _rebuild():
            try:
                return await kb.build_vectors()
            except Exception as e:                     # noqa: BLE001
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        res = asyncio.run(_rebuild())
        check("★普通 build_vectors()（force=False）就能补全 —— 旧写法在 "
              "missing()/prune() 抛 IndexError，只有 force=True 能救",
              res.get("ok") is True and res.get("added") == n, str(res))
        sem_now = kb._sem
        check("补全后 semantic_ready() 上岗、三方行数一致",
              kb.semantic_ready() is True and sem_now.matrix is not None
              and sem_now.matrix.shape[0] == n
              and len(sem_now.topics) == n and len(sem_now.hashes) == n,
              f"ready={kb.semantic_ready()} topics={len(sem_now.topics)}")
        check("重建落盘后 cache_error 治愈（缓存已重新生成）",
              kb.semantic_stats().get("cache_error", "") == "",
              str(kb.semantic_stats()))
        sem2 = SemanticIndex()
        check("重建落盘的 npz 能通过严格校验（自产自销往返）",
              sem2.load(kb.vec_path) is True
              and len(sem2.topics) == len(sem2.hashes) == n)

        # —— 坏缓存样本 2：矩阵行数 < 主题数（未修版在 prune() 里就 IndexError）——
        kb2 = ModKnowledgeBase(str(td / "b"), server_id="stub", preset_id="p")
        kb2.save_entry("条目甲", "内容一", status="verified")
        kb2.save_entry("条目乙", "内容二", status="verified")
        keys2 = list(kb2._searchable().keys())
        kb2.vec_path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            kb2.vec_path,
            matrix=np.zeros((1, 4), dtype="float32"),
            topics=np.array(keys2, dtype=object),
            hashes=np.array(["h1", "h2"], dtype=object),
        )
        try:
            kb2.set_semantic_enabled(True)
            crashed = ""
        except Exception as e:                         # noqa: BLE001
            crashed = f"{type(e).__name__}: {e}"
        check("★矩阵行数少于主题数：开启语义通道不再崩（旧写法 prune() 直接 IndexError）",
              crashed == "" and kb2._sem is not None and kb2._sem.matrix is None,
              crashed)

        # —— 坏缓存样本 3：主题重复（重复 topic 会静默错位行号）——
        sem3 = SemanticIndex()
        bad3 = td / "dup.npz"
        np.savez_compressed(bad3,
                            matrix=np.zeros((2, 3), dtype="float32"),
                            topics=np.array(["a", "a"], dtype=object),
                            hashes=np.array(["h1", "h2"], dtype=object))
        check("★主题重复 → 拒绝", sem3.load(bad3) is False and sem3.matrix is None)

        # —— 坏缓存样本 4：含 NaN（有限数值校验）——
        sem4 = SemanticIndex()
        bad4 = td / "nan.npz"
        m = np.zeros((2, 3), dtype="float32")
        m[0, 0] = float("nan")
        np.savez_compressed(bad4, matrix=m,
                            topics=np.array(["a", "b"], dtype=object),
                            hashes=np.array(["h1", "h2"], dtype=object))
        check("★含 NaN → 拒绝", sem4.load(bad4) is False and sem4.matrix is None)

        # —— 对照：结构完整的小文件照常加载（校验不误杀）——
        sem5 = SemanticIndex()
        ok5 = td / "good.npz"
        np.savez_compressed(ok5, matrix=np.zeros((2, 3), dtype="float32"),
                            topics=np.array(["a", "b"], dtype=object),
                            hashes=np.array(["h1", "h2"], dtype=object))
        check("对照：结构完整照常加载", sem5.load(ok5) is True and len(sem5.topics) == 2)


# ==================== B 半行尾巴：截断前必须存回 _partial ====================
def part_b() -> None:
    print("========== [B] 「完整行 + 尾半行」：尾巴存回 _partial、只读新增 ==========")
    poll_src = fn_src(LW_SRC, "_poll", cls="LogWatcher")
    check("静态：截断前把尾巴存回 _partial（否则下一轮重读整段尾巴）",
          "self._partial = tail" in poll_src)

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "logs").mkdir()
            log = root / "logs" / "latest.log"
            line1 = b"[12:00:00] [Server thread/INFO]: <Steve> one\n"    # 45 字节
            tail1 = b"[12:00:01] [Server thr"                            # 22 字节
            log.write_bytes(line1 + tail1)                              # 67 字节
            w, seen = _make_watcher(root)
            try:
                await w._poll()
                check("第一轮：完整行被消费、尾半行进缓冲",
                      w._pos == 45 and w._partial == tail1,
                      f"pos={w._pos} partial={w._partial!r}")
                check("第一轮只读了 67 字节（没有多读）",
                      w.read_bytes == 67, str(w.read_bytes))
                with open(log, "ab") as fh:
                    fh.write(b"ead/INFO]: <Steve> two\n")                    # 补全尾巴（23 字节）
                await w._poll()
                check("★第二轮从尾巴末尾续读（read_bytes 累计 90，而不是重读成 112）",
                      w.read_bytes == 90, f"read_bytes={w.read_bytes}")
                check("补全后的行被解析（<Steve> two → chat）",
                      ("chat", "Steve", "two") in seen, str(seen))
                check("消费完：缓冲清空、pos 到文件尾",
                      w._partial == b"" and w._pos == 90,
                      f"pos={w._pos} partial={w._partial!r}")
            finally:
                await w.stop()

    asyncio.run(scenario())


# ==================== C 半行缓冲未清时的「原地截断」 ====================
def part_c() -> None:
    print("========== [C] size < _pos + len(_partial) → 原地截断必须认出来 ==========")
    poll_src = fn_src(LW_SRC, "_poll", cls="LogWatcher")
    check("静态：轮转判据覆盖半行缓冲（size < _pos + len(_partial)）",
          "size < self._pos + len(self._partial)" in poll_src)

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "logs").mkdir()
            log = root / "logs" / "latest.log"
            payload = b"L" * 59 + b"\n" + b"M" * 59 + b"\n"        # 120 字节、两行
            log.write_bytes(payload)
            w, seen = _make_watcher(root)
            try:
                # 手动摆出「已读到 100、手里还挂着 50 字节半行」，文件却被截到 120：
                # 旧判据（size < _pos）看不见它，之后从 150 偏移读到的永远是空。
                w._pos = 100
                w._partial = b"x" * 50
                w._skip_to_newline = False
                await w._poll()
                check("★识别截断：从头重读，pos 走到文件尾、缓冲不再悬着",
                      w._pos == 120 and w._partial == b"",
                      f"pos={w._pos} partial={w._partial!r}")
                check("重读确实发生（read_bytes 至少整个文件）",
                      w.read_bytes >= 120, str(w.read_bytes))
            finally:
                await w.stop()

    asyncio.run(scenario())


# ==================== D 发布 runner：整条链统一 UTF-8 ====================
def part_d() -> None:
    print("========== [D] 发布 runner：子进程 env + 主进程 reconfigure 统一 UTF-8 ==========")
    run_src = fn_src(RV_SRC, "run_file")
    check("静态：run_file 给子进程钉 PYTHONIOENCODING / PYTHONUTF8（GBK 管道不再炸）",
          'env["PYTHONIOENCODING"] = "utf-8"' in run_src
          and 'env["PYTHONUTF8"] = "1"' in run_src)
    main_src = fn_src(RV_SRC, "main")
    check("静态：main 把 stdout/stderr 的编码与错误策略一起钉死（encoding + replace）",
          'reconfigure(encoding="utf-8", errors="replace")' in main_src)

    # —— 行为 1：模拟 GBK 管道（PYTHONIOENCODING=gbk），子进程必须仍能打印 ⊆ ——
    with tempfile.TemporaryDirectory() as td:
        probe = Path(td) / "print_probe.py"
        probe.write_text("print('⊆ ⊂ ≧ ✓ 中文链路测试')\n", encoding="utf-8")
        saved = _force_gbk_env()                           # 复刻 GBK 机器上的管道
        try:
            ok, _cost, out, timed = RV.run_file(str(probe), timeout=60)
        finally:
            _restore_env(saved)
        check("★GBK 管道环境：run_file 仍把子进程拉回 UTF-8（旧写法 UnicodeEncodeError 判红）",
              ok and not timed and "⊆" in out and "中文链路测试" in out,
              f"ok={ok} timed={timed} out={out.strip()[-160:]!r}")

    # —— 行为 2：GBK 环境下跑 runner 自己（--list-ui），输出必须是可读 UTF-8 ——
    env = dict(os.environ)
    env.pop("PYTHONUTF8", None)          # 复刻「没有 UTF-8 强制」的 GBK 机器
    env["PYTHONIOENCODING"] = "gbk"
    r = subprocess.run([RV.PY, str(PLUGIN / "run_release_verify.py"), "--list-ui"],
                       capture_output=True, env=env, cwd=str(PLUGIN))
    text = r.stdout.decode("utf-8", errors="replace")
    check("★GBK 环境：runner 自身输出仍是 UTF-8（reconfigure 显式 encoding 生效）",
          r.returncode == 0 and "硬门禁" in text,
          f"rc={r.returncode} head={text[:140]!r}")

    # —— 第六轮用例里的反向断言已同步修正（不再把错误实现钉成契约）——
    r6_src = io.open(PLUGIN / "tests" / "test_v0235_review_round6.py",
                     encoding="utf-8").read()
    check("第六轮用例同步修正：期待显式 encoding（不再断言「不许出现」）",
          'reconfigure(encoding="utf-8", errors="replace")' in r6_src)


# ==================== E 复用同一 watcher：旧代卡死不许挡新代 ====================
def part_e() -> None:
    print("========== [E] stop() 后复用同一对象：旧代卡死 IO 不挡新代 ==========")
    iw_src = fn_src(LW_SRC, "_io_wait", cls="LogWatcher")
    check("静态：在飞计数与「代」绑定（换代即清零，旧代残留不挡新代）",
          "_io_inflight_gen" in iw_src and "_io_gen" in LW_SRC)
    start_src = fn_src(LW_SRC, "start", cls="LogWatcher")
    check("静态：停止逻辑抽成 _stop_locked（start 与 stop 共走同一条收尾路）",
          "def _stop_locked" in LW_SRC and "self._stop_locked()" in start_src)
    stop_src = fn_src(LW_SRC, "stop", cls="LogWatcher")
    check("静态：start / stop 走生命周期锁（并发调用串行化）",
          "_life_lock" in start_src and "_life_lock" in stop_src)

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "logs").mkdir()
            log = root / "logs" / "latest.log"
            line = b"[12:00:00] [Server thread/INFO]: <Steve> one\n"
            log.write_bytes(line * 7)                          # 315 字节
            w, seen = _make_watcher(root)
            old = LW.IO_TIMEOUT
            LW.IO_TIMEOUT = 0.05
            try:
                await w.start()
                check("前置：start 成功、文件在", w.file_present is True)
                old_ex = w._executor
                old_gen = getattr(w, "_io_gen", None)
                old_pos = w._pos

                def stuck(offset, size):
                    time.sleep(0.4)          # 假装陷在系统调用里
                    return b""

                w._read_at = stuck
                with open(log, "ab") as fh:
                    fh.write(b"[12:00:01] [Server thread/INFO]: <Steve> stuck\n")
                await w._poll()              # 提交一笔必超时的读
                await asyncio.sleep(0.05)
                check("前置：有一笔卡住的 IO（inflight 挂着）",
                      w._io_inflight == 1, str(w._io_inflight))

                await w.stop()               # 不等它（wait=False），旧代残留 1 笔
                await w.start()              # 复用同一对象
                new_gen = getattr(w, "_io_gen", None)
                check("★复用后初始化没被旧代的「在飞」挡住（旧写法 prime 永远超时）",
                      w._probed is True and w.file_present is True
                      and w._pos >= old_pos + 40,
                      f"probed={w._probed} present={w.file_present} pos={w._pos}")
                check("执行器换代（上一代的卡死线程不再占它）",
                      w._executor is not None and w._executor is not old_ex
                      and old_gen is not None and new_gen == old_gen + 1,
                      f"gen={old_gen}→{new_gen}")

                del w._read_at               # 恢复真实读取
                with open(log, "ab") as fh:
                    fh.write(b"[12:00:02] [Server thread/INFO]: <Steve> after\n")
                await w._poll()
                await asyncio.sleep(0.05)
                check("★新代读到新内容（复用后监听真正在干活）",
                      ("chat", "Steve", "after") in seen, str(seen[-4:]))
            finally:
                LW.IO_TIMEOUT = old
                await w.stop()
                await asyncio.sleep(0.45)    # 等旧代卡死线程自然醒，减少退出噪音

    asyncio.run(scenario())


# ==================== F 短文件：同 inode、等长原地替换 ====================
def part_f() -> None:
    print("========== [F] 不足 256 字节：同 inode / 等长原地替换要认出来 ==========")
    check("静态：短文件前缀快照机制在位（_short_sig / _short_len）",
          "_short_sig" in LW_SRC and "_short_len" in LW_SRC)

    async def scenario():
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "logs").mkdir()
            log = root / "logs" / "latest.log"
            old_line = b"[12:00:00] [Server thread/INFO]: <Steve> old\n"   # 45 字节
            pad_old = b"y" * 80
            log.write_bytes(old_line + pad_old)                      # 125 字节
            w, seen = _make_watcher(root)
            try:
                await w._poll()
                check("前置：完整行被消费、尾半行进缓冲",
                      w._pos == len(old_line) and w._partial == pad_old,
                      f"pos={w._pos} partial={w._partial[:8]!r}…")
                check("前置：短文件前缀快照已建立",
                      len(getattr(w, "_short_sig", "")) > 0
                      and getattr(w, "_short_len", 0) == 125,
                      f"sig={getattr(w, '_short_sig', '')!r} len={getattr(w, '_short_len', None)}")

                # —— 防误报：正常追加不许被当成轮转 ——
                with open(log, "ab") as fh:
                    fh.write(b" end\n")
                await w._poll()
                check("正常追加：不误判轮转（旧行没有被重读）",
                      w._pos == 130 and w._partial == b""
                      and seen.count(("chat", "Steve", "old")) == 1,
                      f"pos={w._pos} seen={seen}")

                # —— 同 inode、等长原地替换（走同一文件句柄重写）——
                #    只改「锚点窗口（最后消费的 64 字节）之外、快照窗口之内」的 21
                #    字节：锚点判据看不见它，头指纹对 <256B 文件也不可用 ——
                #    旧写法整块漏掉，新内容永远不会被读取。
                old_full = old_line + pad_old + b" end\n"             # 130 字节
                new_full = old_full[:45] + b"z" * 21 + old_full[66:]  # 等长 130 字节
                check("前置：替换内容与旧内容等长", len(new_full) == len(old_full),
                      f"{len(new_full)} vs {len(old_full)}")
                before_bytes = w.read_bytes
                with open(log, "r+b") as fh:
                    fh.seek(0)
                    fh.write(new_full)
                    fh.truncate()
                await w._poll()
                check("★等长原地替换被认出来：从头重读（旧行被重新消费、读入整份文件）",
                      seen.count(("chat", "Steve", "old")) == 2
                      and w.read_bytes - before_bytes == 130,
                      f"count={seen.count(('chat', 'Steve', 'old'))} "
                      f"+{w.read_bytes - before_bytes}B")
                check("替换后状态收敛：pos 到文尾、缓冲清空",
                      w._pos == 130 and w._partial == b"",
                      f"pos={w._pos} partial={w._partial!r}")

                # —— 稳定后不再重复判轮转 ——
                before2 = w.read_bytes
                await w._poll()
                check("再轮询一轮：不重复触发、不再多读",
                      w.read_bytes == before2, f"{before2} → {w.read_bytes}")
            finally:
                await w.stop()

    asyncio.run(scenario())


def main() -> int:
    print("=" * 72)
    print("v0.23.5 第七轮回归（GPT 第六轮核验 · 六组问题的复现与收口）")
    print("=" * 72)
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    part_f()
    print("=" * 72)
    print(f"通过 {_pass} 项，失败 {len(_fail)} 项")
    if _fail:
        for f in _fail:
            print(f"  - {f}")
        return 1
    print("全部通过：六组问题都配了「能复现原缺陷」的判据")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
