# -*- coding: utf-8 -*-
"""出站文本兼容层（v0.23.7 · B2 + B6 合流）。

所有「插件 → 服务器」的文本类命令（tellraw / title）都经过这里的两层兜底，
二者都源于 2026-10-05 版本矩阵实测（小本本 §十二 / §十九）：

B6 · 旧版字符洗白（「（」吞字案）
================================
MC 1.8.x 官方客户端字体表 ``glyph_sizes.bin`` 里「（」的宽度被算成 **-6**，
被文本系统当成 § 格式码哨兵，**吞掉其后整句**并整体偏左（§十九 卷宗）。
全角括号族里实用雷共 4 颗：（）［］｛｝｜ 。
把这几颗洗成半角即可绕开（1.9 起官方原生正常，实测 1.9.4 / 1.14.4 /
1.16.5 / 1.20.x / 1.21.11 全干净）。

版本门（2026-10-05 终局实测）：**已知版本 < 1.9 → 洗；≥ 1.9 → 豁免**。
版本未知 → 保守洗（worst case 只是括号变了半角，而漏洗的 worst case 是
整句被吞 —— 两边不对称，取安全侧；配置开关可关）。

B2 · RCON 单包字节预算
======================
原版 RCON 读取缓冲 = byte[1460]（**全世代同构** · 2026-10-05 CFR 反编译
1.8.9→1.21.11 十版逐验、含 1.15.2 补刀）：单次 read(buf,0,1460) + 长度
校验 len==n-4，不通过即 return —— 整包 >1460B 服务端**静默拔线**
（read 阶段 0 bytes）—— 命令不送达、无日志、无回执。
命令体（UTF-8）理论边界 = 1446B（1447B 起必死；1.13.2 实测 ≈1.1KB 过 /
≈1.7KB 死）。JSON 渐变 ≈35B/字 极易超限（实测 ≈1.7KB 即炸）。
策略：渲染后按 UTF-8 字节做预算 —— 超限先降级单色，仍超则按字符切段
多次发送；title 类不允许切段（多段会互相顶掉）→ 截断兜底。

本模块是**纯函数**，不依赖 AstrBot 运行时，可直接单测。
"""
from __future__ import annotations

# ===================== B6：旧版字符洗白 =====================

#: 全角 → 半角替换表（只需这 4 对；官方字体表全表 50 颗里其余是外星字符）
LEGACY_CHAR_MAP = {
    "（": "(", "）": ")",
    "［": "[", "］": "]",
    "｛": "{", "｝": "}",
    "｜": "|",
}

#: 洗白分水岭：已知版本 ≥ 此版本 → 豁免（2026-10-05 实测 1.9+ 原生正常）
LEGACY_WASH_CUTOVER = (1, 9)

#: translate 表只建一次（性能无关紧要，图干净）
_LEGACY_TRANS = str.maketrans(LEGACY_CHAR_MAP)


def needs_legacy_wash(mc: tuple[int, ...] | None) -> bool:
    """是否需要洗白。

    已知版本 < 1.9 → True；≥ 1.9 → False；**版本未知 → True（保守侧）**：
    漏洗的代价是「整句被客户端吞掉」，误洗的代价只是「括号变半角」——
    两边不对称，未知时取安全侧（配置开关可整体关闭）。
    """
    if mc is None:
        return True
    return tuple(mc) < LEGACY_WASH_CUTOVER


def wash_legacy_text(text: str) -> str:
    """把全角括号族洗成半角（B6）。空值原样返回。"""
    if not text:
        return text
    return text.translate(_LEGACY_TRANS)


# ===================== B2：RCON 单包字节预算 =====================

#: 单条 RCON 命令（UTF-8 编码后）的目标上限。
#: 依据：原版读取缓冲 byte[1460]（1.8.9→1.21.11 十版反编译同构，含 1.15.2）；
#: 命令体理论边界 1446B（整包 1460B），取 1400 = 留 46B 余量（兼容远端网络分段等）。
RCON_SAFE_BYTES = 1400

#: 「疑似单包超限」诊断提示的触发门槛：出错时命令已达此字节数才追加提示。
SUSPECT_SINGLE_PACKET_BYTES = 1300


def utf8_len(text: str) -> int:
    """UTF-8 编码后的字节数（RCON 按字节算，不是字符数）。"""
    return len(text.encode("utf-8"))


def split_text_by_bytes(text: str, budget: int) -> list[str]:
    """按 UTF-8 字节预算把文本切成若干块（不切坏字符、不丢字符）。

    预算 ≤ 0 或整段已在预算内 → 原样单块返回；空文本 → 空列表。
    """
    if not text:
        return []
    if budget <= 0 or utf8_len(text) <= budget:
        return [text]
    chunks: list[str] = []
    cur: list[str] = []
    cur_b = 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        if cur and cur_b + n > budget:
            chunks.append("".join(cur))
            cur, cur_b = [ch], n
        else:
            cur.append(ch)
            cur_b += n
    if cur:
        chunks.append("".join(cur))
    return chunks


def clip_text_to_bytes(text: str, budget: int) -> str:
    """截断到不超过预算的**完整字符边界**（budget ≤ 0 → 空串）。

    供「不可切段」场景（title 类）的截断兜底使用。
    """
    if budget <= 0 or not text:
        return ""
    out: list[str] = []
    used = 0
    for ch in text:
        n = len(ch.encode("utf-8"))
        if used + n > budget:
            break
        out.append(ch)
        used += n
    return "".join(out)


# ===================== B2③：旧版 hex 单色门控 =====================

#: JSON 文本组件里的 hex 颜色（``"color": "#RRGGBB"``）是 **1.16+** 客户端特性。
#: 已知 < 1.16 时下发 hex：命令**不报错、客户端静默忽略**（渲染成默认色）——
#: 2026-10-05 活体实测：1.13.2 四条 tellraw（hex 大写 / 小写 / 无井号 / 色名对照）
#: 全部静默成功、零错误回执，说明服务端压根不校验、而旧客户端根本认不出 hex。
#: 策略：已知 < 1.16 → 把 hex 单色**降级为最接近的原版 16 色名**（可见 > 精确）；
#: ≥ 1.16 → 原样精确下发；版本未知 → 不猜（与渐变门控同策略，原样下发）。
HEX_COLOR_CUTOVER = (1, 16)

#: 原版 16 色名 → 客户端调色板 RGB（MC 官方值；「最近色」近似的锚点）
VANILLA_COLORS: dict[str, tuple[int, int, int]] = {
    "black": (0, 0, 0),
    "dark_blue": (0, 0, 170),
    "dark_green": (0, 170, 0),
    "dark_aqua": (0, 170, 170),
    "dark_red": (170, 0, 0),
    "dark_purple": (170, 0, 170),
    "gold": (255, 170, 0),
    "gray": (170, 170, 170),
    "dark_gray": (85, 85, 85),
    "blue": (85, 85, 255),
    "green": (85, 255, 85),
    "aqua": (85, 255, 255),
    "red": (255, 85, 85),
    "light_purple": (255, 85, 255),
    "yellow": (255, 255, 85),
    "white": (255, 255, 255),
}


def needs_hex_color_downgrade(mc: tuple[int, ...] | None) -> bool:
    """hex 单色是否需要降级：已知 < 1.16 → True；≥ 1.16 → False；未知 → False。"""
    if mc is None:
        return False
    return tuple(mc) < HEX_COLOR_CUTOVER


def parse_hex_color(color: str) -> tuple[int, int, int] | None:
    """``#RRGGBB``（或 ``RRGGBB``）→ (r, g, b)；不是 hex → None。"""
    if not color:
        return None
    v = str(color).strip().lstrip("#")
    if len(v) != 6:
        return None
    try:
        n = int(v, 16)
    except ValueError:
        return None
    return (n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF


def _rgb_to_hsl(r: int, g: int, b: int) -> tuple[float, float, float]:
    """(r,g,b) 0-255 → (h 0-360, s 0-1, l 0-1)（标准 HSL，非色相饱和 = 0）。"""
    fr, fg, fb = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(fr, fg, fb), min(fr, fg, fb)
    l = (mx + mn) / 2.0
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == fr:
        h = ((fg - fb) / d) % 6.0
    elif mx == fg:
        h = (fb - fr) / d + 2.0
    else:
        h = (fr - fg) / d + 4.0
    return h * 60.0, s, l


def _rgb_proximity(rgb_a: tuple[int, int, int], rgb_b: tuple[int, int, int]) -> float:
    """绝对邻近度（归一化加权 RGB 距离，0..1；2/4/3 = 红2·绿4·蓝3 的经典权重）。"""
    dr, dg, db = rgb_a[0] - rgb_b[0], rgb_a[1] - rgb_b[1], rgb_a[2] - rgb_b[2]
    return (2 * dr * dr + 4 * dg * dg + 3 * db * db) / (9 * 255 * 255)


#: 最近色度量的三个权重（改这里 = 改「像不像」的口味，测试同步钉死）
_HUE_WEIGHT = 4.0        # Δh² 的最高权重（再 × 目标饱和度）
_LIGHT_WEIGHT = 2.0      # Δl² 权重（明暗错档比色相偏一点更刺眼）
_PROXIMITY_WEIGHT = 0.15  # 绝对邻近度权重：给「色相打平」提供真实的距离裁决


def nearest_vanilla_color(color: str) -> str | None:
    """把 hex 颜色近似成最接近的原版色名（色相感知 + 邻近兜底）；非 hex → None。

    度量（HSL 空间，用户视角的「像不像」）::

        d = 4 · s_target · Δh²  +  Δs²  +  2 · Δl²  +  0.15 · 邻近度

    - **Δh**：环形色相差（0..180° 归一化）。权重随目标饱和度线性放大 ——
      用户挑的色越鲜艳就越保色相；灰阶目标（s≈0）自动不吃色相，纯比明暗。
    - **Δl** 权重 2：明暗错档比色相偏一点更刺眼。
    - **邻近度**（归一化加权 RGB 距离）：色相项天然会「打平」——
      dodgerblue（209.6°）到 blue（240°）与 aqua（180°）的色相隔几乎相等，
      只看色相会把湛蓝判给青色；这一项按真实距离把它拉回 blue。
    - 同分裁决：主距离量化到 1e-6 后比「邻近度」，再按声明顺序兜底 ——
      浮点噪声不许改写同分结果，同一输入永远同一输出。

    为什么不用朴素 RGB 距离：它会为了「数值近」把深靛蓝 #123456、哑绿 #2E8B57
    这类低饱和深色判给 gray 一档 —— 数值近了、**色相没了**，用户看到的就是
    「我的颜色没生效」。16 色盘再粗，也不该把彩色判成灰。
    """
    rgb = parse_hex_color(color)
    if rgb is None:
        return None
    th, ts, tl = _rgb_to_hsl(*rgb)
    best_name: str | None = None
    best_key: tuple[float, float] | None = None
    for name, (cr, cg, cb) in VANILLA_COLORS.items():
        ch, cs, cl = _rgb_to_hsl(cr, cg, cb)
        dh = abs(th - ch) % 360.0
        dh = min(dh, 360.0 - dh) / 180.0
        ds = abs(ts - cs)
        dl = abs(tl - cl)
        prox = _rgb_proximity(rgb, (cr, cg, cb))
        d = (_HUE_WEIGHT * ts * dh * dh + ds * ds
             + _LIGHT_WEIGHT * dl * dl + _PROXIMITY_WEIGHT * prox)
        key = (round(d, 6), prox)
        if best_key is None or key < best_key:
            best_name, best_key = name, key
    return best_name


# ===================== B2④：格式化符号色码（§ / & 简写） =====================

#: MC 格式化符号色码 ↔ 原版 16 色名（``§0``-``§f``；``&`` 为插件侧同义写法）。
#: 旧版客户端只认这 16 色 —— 直接敲符号是「最清楚地保留自定义颜色」的写法，
#: 全世代通用、零近似（2026-10-05 用户点名：种类少点也没关系，清楚最重要）。
SECTION_COLOR_CODES: dict[str, str] = {
    "0": "black", "1": "dark_blue", "2": "dark_green", "3": "dark_aqua",
    "4": "dark_red", "5": "dark_purple", "6": "gold", "7": "gray",
    "8": "dark_gray", "9": "blue", "a": "green", "b": "aqua",
    "c": "red", "d": "light_purple", "e": "yellow", "f": "white",
}


def parse_section_color(value) -> str | None:
    """``§6`` / ``&6`` → ``gold``；不是格式化色码 → None。

    只认「符号 + 一位码」两字符形式（大小写不敏感、两端空白忽略）：
    裸 ``6`` 会被 ``_parse_color`` 当十进制 RGB —— 绝不能在这里抢，否则十进制颜色全废。
    """
    if not value:
        return None
    v = str(value).strip().lower()
    if len(v) == 2 and v[0] in ("§", "&"):
        return SECTION_COLOR_CODES.get(v[1])
    return None
