# -*- coding: utf-8 -*-
"""B4 · RCON 单包字节边界精测（二分 + 邻域复核 · 含原版逻辑 mock 自验）

背景（2026-10-05 CFR 反编译实锤 · 1.8.9→1.21.11 十版同构）
=========================================================
原版服务端每连接线程：
    byte[1460] 缓冲 → 单次 read(buf, 0, 1460)
    → 校验「包长字段 == 实读字节数 - 4」，不满足即 return（静默拔线）
⇒ 整包（4 包长 + 4 请求ID + 4 类型 + 命令体 + 2 填充）≤ 1460B 才存活
⇒ 命令体（UTF-8）理论边界 = **1446B**；1447B 起必死（本地直连理想链路）

本脚本对真实服务端实测：sanity（1400 过 / 1500 死）→ 二分定界 → 边界邻域复核，
最后与理论值对照出结论。`--mock` 用复刻原版读取逻辑的假服务器自验脚本本体。

用法：
    python tests/bench_rcon_packet_limit.py --port 25703 --password matrix-test
    python tests/bench_rcon_packet_limit.py --port 25703 --password matrix-test --lo 1400 --hi 1500
    python tests/bench_rcon_packet_limit.py --mock

退出码：0 = 与理论一致；2 = 偏差/抖动；1 = 运行错误。
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import threading

DEFAULT_THEORY = 1446  # 命令体（UTF-8）理论上限：整包 1460B - 14B 开销


# ---------------- RCON 极简客户端（仅本脚本用） ----------------

def _recv_exact(sock: socket.socket, n: int, timeout: float) -> bytes:
    sock.settimeout(timeout)
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("EOF")
        buf += chunk
    return buf


def _send_packet(sock: socket.socket, req_id: int, ptype: int, body: bytes) -> None:
    sock.sendall(struct.pack("<iii", len(body) + 10, req_id, ptype) + body + b"\x00\x00")


def _recv_packet(sock: socket.socket, timeout: float):
    head = _recv_exact(sock, 4, timeout)
    length = struct.unpack("<i", head)[0]
    if not (10 <= length <= 60000):
        raise ValueError(f"非法包长 {length}")
    rest = _recv_exact(sock, length, timeout)
    req_id, ptype = struct.unpack("<ii", rest[:8])
    return req_id, ptype, rest[8:-2]


def _connect_auth(host: str, port: int, password: str, timeout: float) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    _send_packet(sock, 1, 3, password.encode("utf-8"))
    req_id, _ptype, _body = _recv_packet(sock, timeout)
    if req_id == -1:
        sock.close()
        raise PermissionError("RCON 认证失败（密码不符？）")
    return sock


# ---------------- 单点测量 ----------------

def measure(host: str, port: int, password: str, cbytes: int,
            timeout: float = 4.0):
    """发一条命令体恰为 cbytes 字节的单包命令（`say ` + A×N），返回 (存活?, 死法/备注)。"""
    prefix = b"say "
    command = prefix + b"A" * (cbytes - len(prefix))
    try:
        sock = _connect_auth(host, port, password, timeout)
    except Exception as exc:  # noqa: BLE001
        return False, f"连接/认证失败：{exc}"
    try:
        _send_packet(sock, 101, 2, command)
        try:
            _recv_packet(sock, timeout)
        except ConnectionError:
            return False, "EOF（静默拔线特征）"
        except TimeoutError:
            return False, "读取超时"
        # 探针：连接仍可用才算真存活
        try:
            _send_packet(sock, 102, 2, b"list")
            _recv_packet(sock, timeout)
        except Exception:  # noqa: BLE001
            return False, "回执后探针失败（半死）"
        return True, "ok"
    finally:
        sock.close()


# ---------------- 原版读取逻辑复刻（mock 自验用） ----------------

class MockRcon(threading.Thread):
    """按反编译结果复刻：单次 recv(1460) + 校验 length == n-4，不满足即断开。"""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(16)
        self.port = self._srv.getsockname()[1]
        self._stop = False

    def run(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def stop(self) -> None:
        self._stop = True
        try:
            self._srv.close()
        except OSError:
            pass

    @staticmethod
    def _serve(conn: socket.socket) -> None:
        authed = False
        with conn:
            while True:
                try:
                    data = conn.recv(1460)          # ← 单次读取（byte[1460] 同构）
                except OSError:
                    return
                if len(data) < 10:
                    return
                length = struct.unpack("<i", data[:4])[0]
                if length != len(data) - 4:          # ← 原版校验：不匹配即静默断开
                    return
                req_id, ptype = struct.unpack("<ii", data[4:12])
                if ptype == 3:
                    authed = True
                resp = b""
                try:
                    conn.sendall(struct.pack("<iii", len(resp) + 10, req_id, 2)
                                 + resp + b"\x00\x00")
                except OSError:
                    return


# ---------------- 主流程 ----------------

def bisect_boundary(host, port, password, lo, hi, timeout, log) -> int:
    """不变式：lo 存活、hi 死亡；二分出最大存活命令体字节数。"""
    ok, mode = measure(host, port, password, lo, timeout)
    log(f"[sanity] {lo}B → {'✓ 过' if ok else '✗ 死'}（{mode}）")
    if not ok:
        log("✗ sanity 失败：下界应存活，请检查服务器/参数")
        raise SystemExit(2)
    ok, mode = measure(host, port, password, hi, timeout)
    log(f"[sanity] {hi}B → {'✓ 过' if ok else '✗ 死'}（{mode}）")
    if ok:
        log("✗ sanity 失败：上界应死亡（与反编译模型不符）")
        raise SystemExit(2)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        ok, mode = measure(host, port, password, mid, timeout)
        log(f"  二分 {mid}B → {'✓ 过' if ok else '✗ 死'}（{mode}）")
        if ok:
            lo = mid
        else:
            hi = mid
    return lo


def main() -> int:
    ap = argparse.ArgumentParser(description="RCON 单包字节边界精测（B4）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=25703)
    ap.add_argument("--password", default="")
    ap.add_argument("--lo", type=int, default=1400)
    ap.add_argument("--hi", type=int, default=1500)
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--theory", type=int, default=DEFAULT_THEORY)
    ap.add_argument("--mock", action="store_true", help="用原版逻辑复刻的本地假服务器自验")
    args = ap.parse_args()

    def log(msg: str) -> None:
        print(msg, flush=True)

    mock = None
    host, port, password = args.host, args.port, args.password
    if args.mock:
        mock = MockRcon()
        mock.start()
        host, port, password = "127.0.0.1", mock.port, "mock"
        log(f"—— mock 模式（复刻 byte[1460] 单读 + len==n-4 校验）端口 {port} ——")
    log(f"目标 {host}:{port}  sanity 区间 [{args.lo}, {args.hi}]  理论边界 {args.theory}B")

    try:
        max_alive = bisect_boundary(host, port, password, args.lo, args.hi, args.timeout, log)
        log("\n边界邻域复核（各连 2 次）：")
        final = {}
        for c in (max_alive - 1, max_alive, max_alive + 1, max_alive + 2):
            r1, m1 = measure(host, port, password, c, args.timeout)
            r2, m2 = measure(host, port, password, c, args.timeout)
            final[c] = (r1, r2)
            log(f"  {c}B → {'✓' if r1 else '✗'} / {'✓' if r2 else '✗'}   （{m1} / {m2}）")
        stable = all(final[max_alive]) and not any(final.get(max_alive + 1, (False, False)))
        log(f"\n结论：最大存活命令体 = {max_alive}B（理论 {args.theory}B）"
            + ("✓ 与反编译模型一致\n" if max_alive == args.theory else "✗ 与理论不一致，需记录偏差\n"))
        if not stable:
            log("⚠ 边界存在抖动（网络分段干扰），以理论 + 复测为准")
        return 0 if (max_alive == args.theory and stable) else 2
    finally:
        if mock:
            mock.stop()


if __name__ == "__main__":
    sys.exit(main())
