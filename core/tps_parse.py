"""解析 TPS/MSPT 命令已知的回包格式。"""
from __future__ import annotations

import re

_COLOR_CODE = re.compile(r"§[^\r\n]")
_NUMBER = r"[0-9]{1,19}(?:\.[0-9]+)?"
_TPS_WINDOWS = ("tps_1m", "tps_5m", "tps_15m")
_TPS_LINE = re.compile(
    r"^(?:server )?tps(?: from last 1m\s*,\s*5m\s*,\s*15m)?\s*[:：]\s*(.+)$",
    re.I,
)
_FORGE_VALUES = rf"Mean tick time:\s*({_NUMBER})\s*ms\.\s*Mean TPS:\s*({_NUMBER})"
_FORGE_OVERALL = re.compile(rf"(?<![A-Za-z])Overall:\s*{_FORGE_VALUES}[ \t]*$", re.I | re.M)
_PAPER_MSPT = re.compile(
    r"^Server tick times\s*\(avg/min/max\)\s*from last 5s\s*,\s*10s\s*,\s*1m\s*[:：]\s*"
    rf"◴?\s*(?:{_NUMBER}/{_NUMBER}/{_NUMBER}\s*,\s*){{2}}"
    rf"({_NUMBER})/{_NUMBER}/{_NUMBER}\s*$",
    re.I,
)
_AVERAGE_TICK = re.compile(rf"\bAverage time per tick:\s*({_NUMBER})\s*ms\b", re.I)
_BEHIND_TICKS = re.compile(
    r"\brunning\s+([0-9]{1,19})\s+ticks?\s+behind(?=\b|Target tick rate:)", re.I
)
_SPARK_HEADER = re.compile(r"^TPS from last 5s\s*,\s*10s\s*,\s*1m\s*,\s*5m\s*,\s*15m\s*:", re.I)


def strip_color_codes(text: str) -> str:
    """去掉 Minecraft 的 § 颜色和格式码。"""
    return _COLOR_CODE.sub("", str(text or ""))


def normalize_reply(raw: str) -> str:
    return strip_color_codes(raw).replace("\r\n", "\n").replace("\r", "\n").replace(r"\n", "\n").strip()


def _tps_columns(text: str, size: int) -> list[float]:
    tokens = re.split(r"(?:\s*[,，]\s*|\s+)", text.strip())
    if not 1 <= len(tokens) <= size:
        return []
    if any(not re.fullmatch(rf"\*?{_NUMBER}", token) for token in tokens):
        return []
    values = [float(token.lstrip("*")) for token in tokens]
    return values if all(value <= 1000.0 for value in values) else []


def parse_tps_output(command: str, raw: str) -> dict:
    """按命令解释数字，只返回有实测值的字段。"""
    text = normalize_reply(raw)
    if command == "forge tps":
        # 多维度回包只使用 Overall。旧格式也可能只给一行总值。
        match = _FORGE_OVERALL.search(text) or re.fullmatch(_FORGE_VALUES, text, re.I)
        if match:
            return {"tps_1m": float(match[2]), "mspt": float(match[1])}
    elif command == "mspt":
        match = _PAPER_MSPT.fullmatch(text)
        if match:
            return {"mspt": float(match[1])}
    elif command == "tick query":
        match = _AVERAGE_TICK.search(text)
        if match:
            result = {"mspt": float(match[1])}
            behind = _BEHIND_TICKS.search(text)
            if behind:
                result["behind_ticks"] = int(behind[1])
            elif "the game is running normally" in text.lower():
                result["behind_ticks"] = 0
            _, marker, percentiles = text.partition("Percentiles:")
            if marker and percentiles.strip():
                result["percentiles"] = marker + percentiles.rstrip()
            return result
    elif command == "spark tps":
        # 供离线解析回包/日志使用。探针不经 RCON 发送这条命令。
        lines = [line.split("[⚡]", 1)[-1].strip() for line in text.splitlines()]
        for i, line in enumerate(lines):
            header = _SPARK_HEADER.match(line)
            if header:
                tail = line[header.end():].strip()
                if not tail and i + 1 < len(lines):
                    tail = lines[i + 1]
                values = _tps_columns(tail, 5)
                return dict(zip(_TPS_WINDOWS, values[2:])) if len(values) == 5 else {}
        star_lines = [line[2:] for line in lines if line.startswith("*:")]
        if star_lines:
            values = _tps_columns(star_lines[0], 5)
            result = dict(zip(_TPS_WINDOWS, values[2:] if len(values) == 5 else values[:1]))
            if len(star_lines) > 1:
                avg = re.match(rf"^\s*({_NUMBER})\s*/", star_lines[1])
                if avg:
                    result["mspt"] = float(avg[1])
            return result
    elif command == "tps":
        for line in text.splitlines():
            match = _TPS_LINE.fullmatch(line.strip())
            if match:
                return dict(zip(_TPS_WINDOWS, _tps_columns(match[1], 3)))
        # 有些探针只回一行裸数字。多行回包需要明确的 TPS 标签。
        if "\n" not in text:
            return dict(zip(_TPS_WINDOWS, _tps_columns(text, 3)))
    return {}
