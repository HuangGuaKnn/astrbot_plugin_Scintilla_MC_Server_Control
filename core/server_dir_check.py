# -*- coding: utf-8 -*-
"""服务端目录结构校验（v0.21.15，v0.21.20 补插件服务端）。

背景
====
v0.21.14 的异地部署审计暴露出一类高危误配：server_dir 填了一个「存在但不相干」的
目录（空壳目录、父级目录、客户端 .minecraft），插件照样初始化 —— 指纹退化成空集合
（d41d8cd98f00）、词典 0 mod、日志永远读不到，界面看着像配好了，实际全是空数据。

策略
====
* **硬校验**：结构不对 → 拒绝保存配置（WebUI 保存前 + 插件初始化时共同把关）；
* 判据只取「服务端天然会有」的特征，**特意不**把下面两类当必需项：
  - ``mods/``：原版服务端与客户端都不带这个目录；
  - ``world/``：首次开服前根本不存在。
  两者只作为提示（warnings），不参与「是否通过」的判定。
* 客户端目录（options.txt / saves/ / shaderpacks/ …）能被单独认出来并明确拒绝；
* 只填了「外壳目录」（真正服务端在下一级）时，给出**可直接复制的**正确路径建议；
* v0.21.20：内容可能在 ``mods/``（Forge/Fabric 整合包）也可能在 ``plugins/``
  （Paper/Spigot 插件服）—— 两者的 jar 数都统计，并在结论里点明内容装在哪
  （``content_kind`` = modpack / plugins / hybrid / vanilla），不再把
  「没有 mods/ 目录」一律说成「词典与知识库会是空的」。

返回结构（dict）
================
    {
      "path":        用户填的原始路径,
      "ok":          bool,          # 是否通过硬校验
      "level":       "ok"|"warn"|"error",
      "errors":      [str, ...],    # 致命问题（ok=False 时非空）
      "warnings":    [str, ...],    # 提示（不影响通过）
      "markers":     {特征: 值},     # 实际探测到的结构
      "suggest_path": str,          # 疑似正确的服务端根目录（无则 ""）
      "summary":     str,           # 一行结论（供 WebUI / 提示文案直接用）
    }
"""
from __future__ import annotations

import os
from pathlib import Path

# ---- 目录特征 -------------------------------------------------------------
DIR_MARKERS = (
    "logs", "libraries", "mods", "config", "defaultconfigs",
    "world", "world_nether", "world_the_end", "versions", "plugins",
)
# ---- 文件特征 -------------------------------------------------------------
FILE_MARKERS = (
    "server.properties", "eula.txt", "user_jvm_args.txt",
    "ops.json", "whitelist.json", "banned-ips.json", "banned-players.json",
    "usercache.json", "usernamecache.json",
)
# ---- 启动脚本 -------------------------------------------------------------
SCRIPT_MARKERS = (
    "run.bat", "run.sh", "start.bat", "start.sh",
    "start_server.bat", "start_server.sh",
    "startserver.bat", "startserver.sh",
)
# ---- 客户端（.minecraft）特征 --------------------------------------------
CLIENT_MARKERS = (
    "options.txt", "servers.dat", "saves", "shaderpacks", "resourcepacks",
)
# ---- 「证明这是服务端」的强特征 ------------------------------------------
# 命中任意一个即认为「是服务端根目录」；logs/world 之外的基本都进不来这个集合，
# 因为它们不是「服务端天然会有」的东西。
STRONG_MARKERS = (
    "logs", "libraries", "server.properties", "eula.txt",
    "user_jvm_args.txt", "ops.json", "whitelist.json",
    "run.bat", "run.sh", "start.bat", "start.sh",
    "start_server.bat", "start_server.sh", "startserver.bat", "startserver.sh",
    "server_jar",
)
# 扫描「外壳目录」下级时跳过的目录名（这些是服务端内部结构，不可能是服务端根）
_SKIP_CHILD = set(DIR_MARKERS) | {"backups", "backup", "crash-reports", "logs"}

_LABELS = {
    "logs": "logs/", "libraries": "libraries/", "mods": "mods/",
    "config": "config/", "defaultconfigs": "defaultconfigs/",
    "world": "world/", "world_nether": "world_nether/",
    "world_the_end": "world_the_end/", "versions": "versions/",
    "plugins": "plugins/", "server.properties": "server.properties",
    "eula.txt": "eula.txt", "user_jvm_args.txt": "user_jvm_args.txt",
    "ops.json": "ops.json", "whitelist.json": "whitelist.json",
    "banned-ips.json": "banned-ips.json",
    "banned-players.json": "banned-players.json",
    "usercache.json": "usercache.json", "usernamecache.json": "usernamecache.json",
    "run.bat": "run.bat", "run.sh": "run.sh", "start.bat": "start.bat",
    "start.sh": "start.sh", "start_server.bat": "start_server.bat",
    "start_server.sh": "start_server.sh", "startserver.bat": "startserver.bat",
    "startserver.sh": "startserver.sh", "server_jar": "服务端 jar",
}
_MAX_CHILDREN = 80          # 外壳目录扫描的宽度上限（防病态目录）


def _label(key: str) -> str:
    return _LABELS.get(key, key)


def _scan(root: Path) -> dict:
    """一次性扫描目录结构（只做 stat + 一次 *.jar 匹配）。"""
    found: dict = {}
    for name in DIR_MARKERS:
        try:
            if (root / name).is_dir():
                found[name] = True
        except OSError:
            pass
    for name in FILE_MARKERS + SCRIPT_MARKERS + CLIENT_MARKERS:
        try:
            if (root / name).is_file():
                found[name] = True
        except OSError:
            pass
    for name in CLIENT_MARKERS:
        try:
            if (root / name).is_dir():
                found[name] = True
        except OSError:
            pass
    jars: list[str] = []
    try:
        for p in root.glob("*.jar"):
            try:
                if p.is_file():
                    jars.append(p.name)
            except OSError:
                pass
    except OSError:
        pass
    if jars:
        found["server_jar"] = sorted(jars)[:5]
    mods_count = 0
    mods_dir = root / "mods"
    try:
        if mods_dir.is_dir():
            mods_count = sum(1 for p in mods_dir.glob("*.jar") if p.is_file())
    except OSError:
        mods_count = 0
    found["mods_count"] = mods_count
    # v0.21.20：Paper / Spigot 系服务端的内容在 plugins/（不是 mods/）
    plugins_count = 0
    plugins_dir = root / "plugins"
    try:
        if plugins_dir.is_dir():
            plugins_count = sum(1 for p in plugins_dir.glob("*.jar") if p.is_file())
    except OSError:
        plugins_count = 0
    found["plugins_count"] = plugins_count
    return found


def content_kind(found: dict) -> str:
    """按目录特征判断「内容装在哪」：modpack / plugins / hybrid / vanilla（v0.21.20）。"""
    has_mods = bool(found.get("mods_count"))
    has_plugins = bool(found.get("plugins_count"))
    if has_mods and has_plugins:
        return "hybrid"
    if has_plugins:
        return "plugins"
    if has_mods:
        return "modpack"
    return "vanilla"


CONTENT_KIND_LABELS = {
    "modpack": "整合包（mods/）",
    "plugins": "插件服务端（plugins/）",
    "hybrid": "混合服务端（mods/ + plugins/）",
    "vanilla": "原版 / 未装内容",
}


def _strong_hits(found: dict) -> list[str]:
    return [k for k in STRONG_MARKERS if found.get(k)]


def _looks_like_server(root: Path) -> bool:
    """轻量探测：这个目录自己像不像服务端根（用于给外壳目录找真身）。"""
    try:
        return bool(_strong_hits(_scan(root)))
    except Exception:
        return False


def _find_server_roots(parent: Path) -> list[Path]:
    """在「外壳目录」的下一级里找像服务端的子目录（不递归太深）。"""
    hits: list[Path] = []
    try:
        children = sorted(
            (c for c in parent.iterdir() if c.is_dir()),
            key=lambda p: p.name.lower(),
        )
    except OSError:
        return hits
    for child in children[:_MAX_CHILDREN]:
        if child.name.lower() in _SKIP_CHILD or child.name.startswith("."):
            continue
        if _looks_like_server(child):
            hits.append(child)
    return hits


def inspect_server_dir(path) -> dict:
    """校验一个路径是否像 Minecraft **服务端**根目录。"""
    raw = str(path or "").strip()
    res: dict = {
        "path": raw, "ok": False, "level": "error",
        "errors": [], "warnings": [], "markers": {},
        "suggest_path": "", "summary": "",
    }
    if not raw:
        res["errors"].append("未填写服务端目录")
        res["summary"] = "未填写服务端目录"
        return res

    try:
        root = Path(os.path.expanduser(raw))
    except Exception as e:                                     # noqa: BLE001
        res["errors"].append(f"路径无法解析：{e}")
        res["summary"] = "路径无法解析"
        return res

    if not root.exists():
        res["errors"].append(f"目录不存在：{root}")
        res["summary"] = f"目录不存在：{root}"
        return res
    if not root.is_dir():
        res["errors"].append(f"这不是一个目录（指到文件了？）：{root}")
        res["summary"] = "填的不是目录"
        return res
    try:
        os.listdir(root)                                       # 可读性探测
    except OSError as e:
        res["errors"].append(f"目录无法读取（权限不足？）：{e}")
        res["summary"] = "目录无法读取"
        return res

    found = _scan(root)
    res["markers"] = {
        k: v for k, v in found.items() if v not in (False, 0, [], None)
    }
    strong = _strong_hits(found)
    client = [k for k in CLIENT_MARKERS if found.get(k)]

    # ---------- 致命问题 ----------
    if not strong:
        if client:
            res["errors"].append(
                "这像是客户端 .minecraft 目录，不是服务端根目录"
                f"（发现 {('、'.join(_label(c) for c in client))}，"
                "却没有任何服务端特征）"
            )
        else:
            res["errors"].append(
                "目录里没有任何服务端特征（logs/ 、libraries/ 、server.properties 、"
                "eula.txt 、启动脚本、服务端 jar 一个都没找到）"
            )
        roots = _find_server_roots(root)
        if len(roots) == 1:
            res["suggest_path"] = str(roots[0])
            res["errors"].append(f"它看起来只是「外壳目录」，真正的服务端根目录应该是：{roots[0]}")
        elif len(roots) > 1:
            cand = "；".join(str(p) for p in roots[:3])
            res["errors"].append(f"它的下级有多个像服务端的目录，请指定其中一个：{cand}")

    # ---------- 提示（不影响通过；只在「确实是服务端」时才有意义） ----------
    if strong:
        if "logs" not in found:
            res["warnings"].append(
                "还没有 logs/ 目录（首次开服后才会生成）→ 服务器事件播报要等开服后才有效"
            )
        if "libraries" not in found:
            res["warnings"].append(
                "没有 libraries/（原版 / Forge 服务端都会有它；客户端目录没有）—— 请确认没有指到子目录"
            )
        if not found.get("mods") and not found.get("plugins"):
            res["warnings"].append(
                "既没有 mods/ 也没有 plugins/ 目录（原版服务端正常）→ 物品词典与知识库会是空的，"
                "内容指纹只能按「服务端形态」估算"
            )
        elif found.get("mods") and not found.get("mods_count") and not found.get("plugins_count"):
            res["warnings"].append(
                "mods/ 里一个 jar 都没有（原版服务端正常；若本该是整合包请确认目录指对了）"
                "→ 内容指纹会退化成「服务端形态」，物品词典与知识库都会是空的"
            )
        if found.get("plugins"):
            if found.get("plugins_count"):
                res["warnings"].append(
                    f"识别为插件服务端（Paper / Spigot 系）：plugins/ 里有 "
                    f"{found['plugins_count']} 个插件 jar，内容指纹按这份插件集合计算"
                    "（物品词典仍读 mods/，插件不自带可解析的物品注册信息）"
                )
            else:
                res["warnings"].append(
                    "plugins/ 目录是空的（插件服务端没装插件）：内容指纹会退化成「服务端形态」"
                )
        if "world" not in found:
            res["warnings"].append("没有 world/（首次开服前不存在，属正常）")

    if res["errors"]:
        res["level"] = "error"
        res["summary"] = res["errors"][0]
        return res

    res["ok"] = True
    res["level"] = "warn" if res["warnings"] else "ok"
    kind = content_kind(found)
    res["content_kind"] = kind            # v0.21.20：modpack / plugins / hybrid / vanilla
    seen = [_label(k) for k in ("libraries", "logs", "server.properties", "eula.txt", "server_jar")
            if found.get(k)]
    detail = "、".join(seen[:4]) if seen else "服务端特征"
    if kind == "plugins":
        content = f"plugins 内 {found.get('plugins_count', 0)} 个 jar（插件服务端）"
    elif kind == "hybrid":
        content = (f"mods 内 {found.get('mods_count', 0)} 个 jar + "
                   f"plugins 内 {found.get('plugins_count', 0)} 个 jar")
    else:
        content = f"mods 内 {found.get('mods_count', 0)} 个 jar"
    res["summary"] = (
        f"目录结构{'基本' if res['warnings'] else ''}正常（识别到 {detail}；{content}）"
    )
    return res


def format_check(check: dict, *, limit: int = 3) -> str:
    """把校验结果拼成一段可直接展示的文案。"""
    if not check:
        return ""
    lines: list[str] = []
    if check.get("ok"):
        lines.append(check.get("summary") or "目录结构正常")
    else:
        lines.append(f"服务端目录校验未通过：{check.get('path') or '（未填写）'}")
    lines += [f"· {e}" for e in (check.get("errors") or [])[:limit]]
    lines += [f"· 提示：{w}" for w in (check.get("warnings") or [])[:limit]]
    return "\n".join(lines)
