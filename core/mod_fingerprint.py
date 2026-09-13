"""服务端内容指纹引擎。

背景：早期用「jar 文件名+大小+修改时间」做指纹，但 mod 更新版本/增删 mod
都会改变快照，导致知识库失联。本模块改用 **内容 ID 集合** 做指纹：

- 稳定指纹 = 排序后的 ID 集合哈希 → 内容版本更新不影响；换整合包/换插件集合才变
- Mod 来源：``META-INF/mods.toml``(Forge/NeoForge) > ``fabric.mod.json``(Fabric) > ``mcmod.info``
- **插件来源（v0.21.20 新增）**：``plugins/*.jar`` 里的 ``plugin.yml`` /
  ``paper-plugin.yml`` / ``bungee.yml`` / ``velocity-plugin.json``。
  Paper / Spigot / Bukkit 系服务端根本没有 ``mods/`` 目录，老算法在它们身上
  恒得 ``md5("")`` = ``d41d8cd98f00`` —— 也就是「认不出任何服务端」。
- 两边都没有内容时退化到「服务端形态」：服务端软件（paper/spigot/forge…）+
  MC 版本，从根目录 jar 名 / ``version_history.json`` / ``logs/latest.log`` 里读。
- 解析结果按 ``(jar名|大小|mtime)`` 缓存，只有变化的 jar 才重新解析（快）。

**兼容性**：mods 有内容、plugins 为空时，哈希输入与老算法逐字节一致
（``"|".join(mod_ids)``）—— 既有预设绑定的指纹不会因为这次改造而失效。

v0.18.0 起指纹只用于「比对与提示」：指纹变化时不再自动继承/切换知识库，
改由 KnowledgePresetManager 弹窗提醒用户手动选择/绑定预设。
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

# ===================== Mod（mods/*.jar） =====================
_MODID_RE = re.compile(r'modId\s*=\s*"([^"]+)"')
_JAR_META_FILES = ("META-INF/mods.toml", "fabric.mod.json", "mcmod.info")

# ===================== 插件（plugins/*.jar，v0.21.20） =====================
# Bukkit 系：plugin.yml（最普遍）/ paper-plugin.yml（Paper 新式清单）/ bungee.yml
# Velocity：velocity-plugin.json
_PLUGIN_META_FILES = ("plugin.yml", "paper-plugin.yml", "bungee.yml")
_VELOCITY_META_FILE = "velocity-plugin.json"
# 只认顶格（无缩进）的 name: —— 避免匹配到 plugin.yml 里嵌套结构的子键
_PLUGIN_NAME_RE = re.compile(r'^name[ \t]*:[ \t]*(\S.*?)[ \t]*$', re.M)

# ===================== 服务端形态（内容为空时的兜底） =====================
_SOFTWARE_PATTERNS = (
    ("paper", r"paper"),
    ("purpur", r"purpur"),
    ("folia", r"folia"),
    ("spigot", r"spigot"),
    ("bukkit", r"craftbukkit|bukkit"),
    ("neoforge", r"neoforge"),
    ("forge", r"forge"),
    ("fabric", r"fabric"),
    ("velocity", r"velocity"),
    ("vanilla", r"minecraft_server|minecraft server"),
)
# "MC: 1.20.1" / "minecraft server version 1.18.2" / "version 1.20.1"
_MC_VER_RE = re.compile(
    r"(?:mc[: ]|minecraft(?: server)? (?:version )?)(\d+\.\d+(?:\.\d+)?)", re.I
)
# 兜底：裸版本号（version_history.json 里常常只是 "1.20.1"）
_BARE_VER_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+)?)\b")
# 根目录 jar 名里的版本：paper-1.20.1-100.jar / server-1.18.2.jar / forge-1.18.2-40.2.4…
_JAR_VER_RE = re.compile(r"[-_](\d+\.\d+(?:\.\d+)?)(?:[-_.]|$)")
# Bukkit 系家族特征文件（有其一即说明是插件服务端，即使 plugins/ 是空的）
_FAMILY_MARKERS = (
    "bukkit.yml", "spigot.yml", "paper.yml", "purpur.yml",
    "config/paper-global.yml", "config/paper-world-defaults.yml",
)

KIND_MODPACK = "modpack"     # 只有 mod（Forge / Fabric / NeoForge 整合包）
KIND_PLUGINS = "plugins"     # 只有插件（Paper / Spigot / Bukkit 系）
KIND_HYBRID = "hybrid"       # 两者都有（Mohist / Arclight / CatServer 等混合端）
KIND_SERVER = "server"       # 都没有，但认得出服务端形态（原版 / 插件服空插件目录）
KIND_UNKNOWN = "unknown"     # 空壳目录：连服务端形态都读不到

KIND_LABELS = {
    KIND_MODPACK: "整合包（mods/）",
    KIND_PLUGINS: "插件服务端（plugins/）",
    KIND_HYBRID: "混合服务端（mods/ + plugins/）",
    KIND_SERVER: "无内容服务端（原版 / 未装插件）",
    KIND_UNKNOWN: "认不出（空壳目录？）",
}


# ===================== Mod ID 解析 =====================

def extract_mod_ids(jar_path: str) -> list[str]:
    """从 jar 中提取 mod ID 列表（多来源兜底）。失败返回空列表。"""
    ids: list[str] = []
    try:
        with zipfile.ZipFile(jar_path) as zf:
            names = set(zf.namelist())
            for probe in _JAR_META_FILES:
                if probe not in names:
                    continue
                raw = zf.read(probe).decode("utf-8", errors="replace")
                if probe == "fabric.mod.json":
                    try:
                        data = json.loads(raw)
                        mid = data.get("id") or data.get("name") or ""
                        if mid:
                            ids.append(str(mid).strip().lower())
                        for entry in data.get("depends", {}):
                            if isinstance(entry, str):
                                ids.append(entry.strip().lower())
                        break
                    except Exception:
                        continue
                if probe == "META-INF/mods.toml":
                    ids.extend(_MODID_RE.findall(raw))
                elif probe == "mcmod.info":
                    try:
                        data = json.loads(raw)
                        if isinstance(data, list):
                            ids.extend(
                                str(m.get("modid", "")).strip().lower()
                                for m in data if isinstance(m, dict) and m.get("modid")
                            )
                    except Exception:
                        continue
                if ids:
                    break
    except Exception:
        pass
    return _normalize(ids)


def _normalize(ids) -> list[str]:
    """规范化并去重（保持稳定顺序）。"""
    seen, out = set(), []
    for mid in ids:
        mid = str(mid).strip().lower()
        if mid and mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


# ===================== 插件 ID 解析 =====================

def _yaml_scalar(raw: str) -> str:
    """取 plugin.yml 里 ``name: xxx`` 的值：去引号、去行内注释。"""
    v = (raw or "").strip()
    if not v:
        return ""
    if v[0] in "\"'":
        q = v[0]
        end = v.find(q, 1)
        return (v[1:end] if end > 0 else v[1:]).strip()
    for mark in (" #", "\t#"):
        if mark in v:
            v = v.split(mark, 1)[0]
    return v.strip()


def extract_plugin_ids(jar_path: str) -> list[str]:
    """从插件 jar 中提取插件 ID（plugin.yml / paper-plugin.yml / bungee.yml / velocity）。

    插件 jar 里**没有** mods.toml 那种清单，所以老算法在这里一律返回空 —— 这正是
    Paper 系服务端指纹退化成 ``d41d8cd98f00`` 的原因。失败返回空列表。
    """
    try:
        with zipfile.ZipFile(jar_path) as zf:
            names = set(zf.namelist())
            for probe in _PLUGIN_META_FILES:
                if probe not in names:
                    continue
                raw = zf.read(probe).decode("utf-8", errors="replace")
                m = _PLUGIN_NAME_RE.search(raw)
                if m:
                    pid = _yaml_scalar(m.group(1)).lower()
                    if pid:
                        return [pid]
            if _VELOCITY_META_FILE in names:
                try:
                    data = json.loads(
                        zf.read(_VELOCITY_META_FILE).decode("utf-8", errors="replace")
                    )
                    pid = str(data.get("id") or data.get("name") or "").strip().lower()
                    if pid:
                        return [pid]
                except Exception:
                    pass
    except Exception:
        pass
    return []


# ===================== 服务端形态探测 =====================

def _shape_from_text(text: str, *, allow_generic: bool = True) -> tuple[str, str]:
    """从一段文本里猜「服务端软件 + MC 版本」。

    allow_generic=False 时，「泛原版」信号（如日志里的 ``Starting minecraft server
    version x``）不算软件名 —— 它只说明这是个 MC 服务端，说不清是 Paper 还是 Spigot，
    不能盖过 bukkit.yml / 服务端 jar 名这类更具体的证据。
    """
    low = (text or "").lower()
    sw = ""
    for name, pat in _SOFTWARE_PATTERNS:
        if re.search(pat, low):
            sw = name
            break
    if sw == "vanilla" and not allow_generic:
        sw = ""
    m = _MC_VER_RE.search(text or "")
    if not m:
        m = _BARE_VER_RE.search(text or "")
    return sw, (m.group(1) if m else "")


def detect_server_shape(server_dir: str) -> dict:
    """探测「服务端软件 + MC 版本」—— 内容为空时用它兜底算指纹。

    来源优先级（都是廉价的本地读，读不到就跳过）：
      软件：家族特征文件 > version_history.json / logs/latest.log > 根目录 jar 名
      版本：version_history.json > logs/latest.log > 根目录 jar 名
    """
    root = Path(server_dir)
    ev: list[str] = []
    sw_cfg = sw_vh = sw_log = sw_jar = ""
    mc_vh = mc_log = mc_jar = ""

    for marker in _FAMILY_MARKERS:
        try:
            if (root / marker).is_file():
                sw_cfg = ("paper" if "paper" in marker else
                          "purpur" if "purpur" in marker else
                          "spigot" if "spigot" in marker else "bukkit")
                ev.append(marker)
                break
        except OSError:
            pass

    try:
        vh = root / "version_history.json"
        if vh.is_file():
            raw = vh.read_text(encoding="utf-8", errors="replace")[:4096]
            sw_vh, mc_vh = _shape_from_text(raw)
            ev.append("version_history.json")
    except Exception:
        pass

    try:
        log = root / "logs" / "latest.log"
        if log.is_file():
            # 只读开头（"Starting minecraft server version x" 在最前面）
            with log.open("r", encoding="utf-8", errors="replace") as f:
                head = f.read(32768)
            sw_log, mc_log = _shape_from_text(head, allow_generic=False)
            if sw_log or mc_log:
                ev.append("logs/latest.log")
    except Exception:
        pass

    try:
        for p in sorted(root.glob("*.jar")):
            low = p.name.lower()
            for name, pat in _SOFTWARE_PATTERNS:
                if name != "vanilla" and re.search(pat, low):
                    sw_jar = sw_jar or name
                    break
            if not mc_jar:
                m = _JAR_VER_RE.search(low)
                if m:
                    mc_jar = m.group(1)
    except OSError:
        pass
    if sw_jar or mc_jar:
        ev.append("根目录 jar 名")

    # 软件：能说出具体名字的优先（version_history > 家族特征文件 > 日志 > jar 名）
    # 版本：version_history > 日志 > jar 名
    return {
        "sw": sw_vh or sw_cfg or sw_log or sw_jar or "",
        "mc": mc_vh or mc_log or mc_jar or "",
        "evidence": ev,
    }


def describe_shape(shape: dict) -> str:
    """把形态写成一行，例：``paper 1.20.1``；只有版本时写 ``MC 1.20.1``。"""
    sw = (shape or {}).get("sw") or ""
    mc = (shape or {}).get("mc") or ""
    if not sw and mc:
        sw = "MC"
    return " ".join(x for x in (sw, mc) if x)


# ===================== 扫描 + 缓存 =====================

def _load_cache(cache_path: str | None) -> dict:
    if not cache_path:
        return {}
    try:
        data = json.loads(Path(cache_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(cache_path: str | None, cache_new: dict) -> None:
    if not cache_path or not cache_new:
        return
    try:
        p = Path(cache_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache_new, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _scan_dir_ids(
    folder: Path, cache: dict, cache_new: dict, *, prefix: str, parser
) -> tuple[list[str], int, int]:
    """扫描一个目录里的 jar，解析出 ID 集合。

    返回 ``(排序去重后的 ID 列表, jar 数量, 解析不出 ID 的 jar 数量)``。
    缓存键 = ``prefix + "jar名|大小|mtime"``（mods 的 prefix 为空串，
    与老版本缓存格式保持一致，老缓存继续命中）。
    """
    ids: set[str] = set()
    jars = 0
    unidentified = 0
    try:
        paths = sorted(folder.glob("*.jar"))
    except OSError:
        paths = []
    for p in paths:
        if p.name.lower().endswith(".disabled"):
            continue
        try:
            st = p.stat()
        except Exception:
            continue
        jars += 1
        key = f"{prefix}{p.name}|{st.st_size}|{int(st.st_mtime)}"
        if key in cache:
            got = cache[key]
        else:
            got = parser(str(p))
            cache_new[key] = got
        if not got:
            unidentified += 1
        ids.update(got)
    return sorted(ids), jars, unidentified


def _fingerprint(parts: str) -> str:
    return hashlib.md5(parts.encode("utf-8")).hexdigest()[:12]


# ===================== 对外主入口 =====================

def compute_server_identity(server_dir: str, cache_path: str | None = None) -> dict:
    """算出「当前服务端」的内容指纹与形态。

    返回::

        {
          "fingerprint": "e38d99c65853",
          "kind":        "modpack" | "plugins" | "hybrid" | "server" | "unknown",
          "mods":        [...mod id...],
          "plugins":     [...plugin id...],
          "mod_jars": n, "plugin_jars": n, "unidentified": n,
          "shape":       {"sw": "paper", "mc": "1.20.1", "evidence": [...]},
          "shape_text":  "paper 1.20.1",
          "weak":        bool,      # 指纹认不出服务端（两边都没有内容）
          "summary":     "mods/ 185 个 jar → 214 个 mod（forge 1.18.2）",
        }
    """
    root = Path(server_dir)
    cache = _load_cache(cache_path)
    cache_new: dict = {}
    mod_ids, mod_jars, mod_unknown = _scan_dir_ids(
        root / "mods", cache, cache_new, prefix="", parser=extract_mod_ids
    )
    plugin_ids, plugin_jars, plugin_unknown = _scan_dir_ids(
        root / "plugins", cache, cache_new, prefix="plugin:", parser=extract_plugin_ids
    )
    _save_cache(cache_path, cache_new)

    shape = detect_server_shape(server_dir)
    if mod_ids and plugin_ids:
        kind = KIND_HYBRID
    elif plugin_ids:
        kind = KIND_PLUGINS
    elif mod_ids:
        kind = KIND_MODPACK
    elif describe_shape(shape):
        kind = KIND_SERVER
    else:
        kind = KIND_UNKNOWN

    # ---- 哈希输入（mods 有内容时与老算法逐字节一致） ----
    if mod_ids and not plugin_ids:
        parts = "|".join(mod_ids)
    elif plugin_ids and not mod_ids:
        parts = "plugins|" + "|".join(plugin_ids)
    elif mod_ids and plugin_ids:
        parts = "mods+plugins|" + "|".join(mod_ids) + "||" + "|".join(plugin_ids)
    else:
        parts = "shape|" + (shape.get("sw") or "") + "|" + (shape.get("mc") or "")

    ident = {
        "fingerprint": _fingerprint(parts),
        "kind": kind,
        "mods": mod_ids,
        "plugins": plugin_ids,
        "mod_jars": mod_jars,
        "plugin_jars": plugin_jars,
        "unidentified": mod_unknown + plugin_unknown,
        "shape": shape,
        # 两边都没有内容 → 指纹只能反映「服务端形态」，两台同形态的服务端会撞车
        "weak": not mod_ids and not plugin_ids,
    }
    ident["shape_text"] = describe_shape(shape)      # 短形态，如 "paper 1.20.1"
    ident["summary"] = describe_identity(ident)
    return ident


def describe_identity(ident: dict) -> str:
    """把身份写成人话（WebUI / 日志 / 提示文案共用）。"""
    if not ident:
        return ""
    shape = describe_shape(ident.get("shape") or {})
    kind = ident.get("kind")
    mods = len(ident.get("mods") or [])
    plugins = len(ident.get("plugins") or [])
    tail = f"（{shape}）" if shape else ""
    if kind == KIND_MODPACK:
        txt = f"mods/ {ident.get('mod_jars', 0)} 个 jar → {mods} 个 mod{tail}"
    elif kind == KIND_PLUGINS:
        txt = f"plugins/ {ident.get('plugin_jars', 0)} 个 jar → {plugins} 个插件{tail}"
    elif kind == KIND_HYBRID:
        txt = (f"mods/ {ident.get('mod_jars', 0)} 个 + plugins/ "
               f"{ident.get('plugin_jars', 0)} 个 → {mods} 个 mod / {plugins} 个插件{tail}")
    elif kind == KIND_SERVER:
        txt = f"没有 mod 也没有插件 → 按服务端形态「{shape}」计指纹"
    else:
        txt = "既没有 mod 也没有插件，版本线索也读不到 → 指纹只是占位值"
    if ident.get("unidentified"):
        txt += f"（{ident['unidentified']} 个 jar 认不出 ID）"
    return txt


def compute_stable_server_id(
    mods_dir: str, cache_path: str | None = None
) -> tuple[str, list[str]]:
    """老接口（v0.18.0）：只扫 ``mods`` 目录算指纹，返回 ``(指纹, mod ID 列表)``。

    保留给既有调用方与回归测试；`server_dir` 版本请用 compute_server_identity()。
    """
    cache = _load_cache(cache_path)
    cache_new: dict = {}
    ids, _jars, _unknown = _scan_dir_ids(
        Path(mods_dir), cache, cache_new, prefix="", parser=extract_mod_ids
    )
    _save_cache(cache_path, cache_new)
    return _fingerprint("|".join(ids)), ids
