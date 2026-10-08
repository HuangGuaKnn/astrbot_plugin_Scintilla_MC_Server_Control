"""Minecraft 服务器 Mod 物品词典构建器（整合包特化核心）。

从服务端 mods 目录解析各 mod 的 jar（zip）：
- META-INF/mods.toml           → modid / mod 显示名
- assets/<modid>/lang/*.json   → 物品/方块的中英文显示名 → 精确物品ID
- data/<modid>/recipes/*.json  → 配方（产出物品 / 合成原料）

v0.21.20：Paper / Spigot 系插件服务端没有 mods/，此时退化为「原版物品表」——
从服务端根目录的原生启动 jar（paper-x.jar / server.jar）读 assets/minecraft/lang/en_us.json，
至少让钻石剑、下界合金锭这类原版物品搜得到（模组物品、配方仍需 mods/）。
注意服务端 jar 里只有英文语言文件：原版物品**没有中文名**，中文关键词搜不到，
请用英文名或直接搜 ID 片段。插件本身不提供物品 / 配方数据。

v0.23.8：候选 jar 的**落点**补齐。1.18+ 的原版 ``server.jar`` 是 bundler，资产在内嵌的
``META-INF/versions/<版本>/server-<版本>.jar`` 里；Paper 把打过补丁的本体放在
``versions/<版本>/paper-<版本>.jar``、把下载的原版核留在 ``cache/mojang_*.jar``；
Fabric 启动器把原版核留在 ``.fabric/server/<版本>-server.jar``。此前只扫
``libraries/net/minecraft/server/`` 与**根目录**，这三类载体的原版表全部读成 0 条
（2026-10-06 Paper 1.21.11 / Fabric 1.21.1 真机实测）。

构建结果缓存到插件数据目录 items.json，供 mc_list_mods / mc_search_item
/ mc_get_recipes 等 LLM 工具查询，解决整合包海量物品时 LLM 猜错 ID 的问题。
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

_LANG_KEY_RE = re.compile(r"^(item|block)\.")

#: B5（2026-10-07 真机实证）：1.13 以下的语言文件是**文本 kv**、键名是 legacy camelCase，
#: 形如 ``item.shovelIron.name`` / ``tile.wood.oak.name``（变体在中间段）。
_LEGACY_LANG_KEY_RE = re.compile(r"^(?:item|tile)\.([A-Za-z0-9_]+)(?:\.([A-Za-z0-9_]+))?\.name$")

#: 预扁平化服务端 jar 里的语言文件落点（1.7.10 是大写 ``en_US.lang``，1.12.2 是小写）。
_LEGACY_LANG_TARGETS = ("assets/minecraft/lang/en_us.lang", "assets/minecraft/lang/en_US.lang")

#: 逐世代预扁平化数据表目录（随插件发布；也可放在缓存目录旁）。
_LEGACY_DATA_DIR = "legacy_items"

#: v0.24.2（GPT 全面复核 F09）：完整性只能由**完整来源**证明，不能由「有几个 namespace 条目」
#: 推断 —— 旧判据是「只要有一个 minecraft: 键就跳过原版核」，某个 mod 自带一个 minecraft:
#: 语言键（例如它自己的方块名），整张原版表就被跳过，diamond 这类基础物品彻底消失。
#: 于是改用**来源标识**：真的从原版 jar / 逐世代 .lang 读过一轮，才算「本次已读」。
_VANILLA_SRC_FLAG = "_vanilla_src_loaded"

#: 从 ``minecraft_server.1.12.2.jar`` 这类文件名里抠版本。
_JAR_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
_RECIPE_RESULT_KEYS = ("result",)
_RECIPE_INPUT_KEYS = ("ingredients", "key", "base", "addition", "input", "ingredient")

#: 服务端 jar 的常见落点（v0.23.8）—— 除开 ``libraries/net/minecraft/server/`` 与根目录。
#:
#: · ``versions/*/*.jar``  ：1.18+ 原版 bundler 首次启动的解包位置；Paper 打过补丁的本体；
#: · ``cache/*.jar``       ：paperclip 下载的**原版核**（``mojang_<版本>.jar``）；
#: · ``.fabric/server/*.jar``：Fabric 启动器下载的原版核。
_VANILLA_JAR_GLOBS = (
    "versions/*/*.jar",
    "cache/*.jar",
    ".fabric/server/*.jar",
)

#: bundler 内嵌的版本 jar；只下钻一层、只取前几个，避免病态包体拖垮扫描。
_NESTED_VERSION_RE = re.compile(r"^META-INF/versions/[^/]+/server-[^/]+\.jar$")
_NESTED_MAX_JARS = 2
_NESTED_MAX_BYTES = 128 * 1024 * 1024
_VANILLA_JAR_CANDIDATE_CAP = 16


class ItemDictionary:
    """Mod 物品词典：构建、缓存、检索、配方查询。"""

    def __init__(self, server_dir: str, cache_path: str | None = None):
        self.server_dir = Path(server_dir)
        self.cache_path = Path(cache_path) if cache_path else None
        self.mods: list[dict] = []          # [{"id": ..., "name": ...}]
        self.items: dict[str, dict] = {}    # {"modid:name": {"en","zh","type"}}
        self.recipes: dict[str, list] = {}  # {"output_id": [recipe...]}
        # ---- B5：预扁平化（1.13 以下）通道的状态 ----
        #: 生效的世代键（如 "1.12.2"）；空串表示走的是现代（1.13+）通道。
        self.legacy_generation: str = ""
        #: {族: {"domain_upper": n, "variants": {data值: 展示名}}} —— 生成层的**域判据**。
        self.legacy_variants: dict[str, dict] = {}
        #: {legacy 键: {"registry": 注册名, "display": 展示名}} —— 桥接表。
        self.legacy_bridge: dict[str, dict] = {}
        #: 可选版本提示（jar 文件名抠不出版本时用，由上层喂入；None = 不知道）。
        self.mc_hint: "tuple | None" = None
        #: **B5 诊断账本**：预扁平化通道为什么成 / 为什么不成（人类可读，随 build() 返回）。
        #: 静默失败是事故之母 —— 这条必须能一眼看出是「没有 .lang」「版本不在世代 range 内」
        #: 还是「缺 data/legacy_items 目录」。
        self.legacy_diag: str = ""

    # ================= 构建 =================

    def build(self) -> dict:
        """扫描 mods 目录构建词典。返回统计信息。

        v0.24.3（GPT v0.24.2 复核 N06）：**构建搬到全新对象上算完，再原子换入本对象**。

        旧实现直接在本对象上 ``self.items = {}`` 然后逐步填，两个真雷：

        1. **重扫丢原版表**：来源标志 ``_vanilla_src_loaded`` 不在重置之列，而
           ``mc_rescan_dictionary`` 恰恰是在**同一个** ``self._dictionary`` 上二次
           ``build()`` —— 第二次构建时 ``_parse_vanilla_lang()`` 一看标志还是 True
           就直接返回，原版 ``minecraft:`` 语言表整体缺席、词典凭空掉一大截，
           而日志还在报「本次已从完整来源读取（不重复）」（把人往反方向带）。
        2. **半成品外泄**：中途抛错时本对象只剩半张词典，而 ``_save_cache()`` /
           检索工具随时可能读到它。

        现在：算在子对象上（新对象天然状态干净，上面两个雷同时消掉），
        失败 = 本对象**保持上一次的完整结果**；成功 = ``_adopt()`` 就地换入。
        """
        fresh = ItemDictionary(
            str(self.server_dir), str(self.cache_path) if self.cache_path else None
        )
        fresh.mc_hint = self.mc_hint          # 版本提示是**构建输入**，必须先带过去
        stats = fresh._build_once()
        self._adopt(fresh)
        return stats

    def _build_once(self) -> dict:
        """在**干净状态**上跑一次完整构建（只应由 build() 调用）。

        这里把「每次构建都必须重置」的东西一次点全 —— 旧的 build() 只重置了
        前三项，漏掉的正是 N06 的病根。
        """
        self.mods, self.items, self.recipes = [], {}, {}
        self.legacy_generation = ""
        self.legacy_variants, self.legacy_bridge = {}, {}
        self.legacy_diag = ""
        setattr(self, _VANILLA_SRC_FLAG, False)
        mods_dir = self.server_dir / "mods"
        jar_files: list[Path] = []
        if mods_dir.exists():
            jar_files = [
                p for p in mods_dir.glob("*.jar")
                if not p.name.lower().endswith(".disabled")
            ]
        for jar in jar_files:
            self._parse_jar(jar)
        self._parse_vanilla_lang()
        if self.cache_path:
            self._save_cache()
        return {
            "mods": len(self.mods),
            "items": len(self.items),
            "recipes": len(self.recipes),
            "jars": len(jar_files),
            "legacy": self.legacy_generation or "-",
            "legacy_diag": self.legacy_diag or "-",
        }

    def _adopt(self, fresh: "ItemDictionary") -> None:
        """把子对象算好的结果**就地**换入本对象（v0.24.3 · N06）。

        就地（而不是 ``self.items = fresh.items``）的原因：``items`` / ``recipes``
        的**容器身份保持不变** —— 任何早先拿到 ``dic.items`` 引用的调用方，
        在这次 update 之后看到的是**新**词典，而不是抱着一本旧账。
        """
        self.mods[:] = fresh.mods
        self.items.clear()
        self.items.update(fresh.items)
        self.recipes.clear()
        self.recipes.update(fresh.recipes)
        self.legacy_generation = fresh.legacy_generation
        self.legacy_variants = fresh.legacy_variants
        self.legacy_bridge = fresh.legacy_bridge
        self.legacy_diag = fresh.legacy_diag
        setattr(self, _VANILLA_SRC_FLAG, bool(getattr(fresh, _VANILLA_SRC_FLAG, False)))

    def _vanilla_jar_candidates(self) -> list[Path]:
        """按「先准后广」收集候选服务端 jar：去重、限量、坏目录不牵连。"""
        out: list[Path] = []

        def _add(paths) -> None:
            for p in paths:
                if len(out) >= _VANILLA_JAR_CANDIDATE_CAP:
                    return
                try:
                    if p.is_file() and p not in out:
                        out.append(p)
                except OSError:
                    continue

        server_lib = self.server_dir / "libraries" / "net" / "minecraft" / "server"
        try:
            if server_lib.exists():
                _add(sorted(server_lib.glob("*/*.jar")))
        except OSError:
            pass
        for pattern in _VANILLA_JAR_GLOBS:
            try:
                _add(sorted(self.server_dir.glob(pattern))[:8])
            except OSError:
                pass
        # 根目录的启动 jar（原版 / Forge / 老式 paper-x.jar；限 8 个，防病态目录）
        try:
            _add(sorted(self.server_dir.glob("*.jar"))[:8])
        except OSError:
            pass
        return out

    def _load_vanilla_lang_from(self, jar: Path, target: str) -> bool:
        """从一个 jar 里读原版语言文件；外层没有就下钻一层内嵌版本 jar（bundler）。"""
        try:
            with zipfile.ZipFile(jar) as zf:
                names = zf.namelist()
                if target in names:
                    self._parse_lang(
                        zf.read(target).decode("utf-8", "replace"), "minecraft", "en"
                    )
                    return True
                nested = [n for n in names if _NESTED_VERSION_RE.match(n)][:_NESTED_MAX_JARS]
                for name in nested:
                    try:
                        if zf.getinfo(name).file_size > _NESTED_MAX_BYTES:
                            continue
                        with zipfile.ZipFile(io.BytesIO(zf.read(name))) as inner:
                            if target in inner.namelist():
                                self._parse_lang(
                                    inner.read(target).decode("utf-8", "replace"),
                                    "minecraft",
                                    "en",
                                )
                                return True
                    except Exception:
                        continue
        except Exception:
            return False
        return False

    def _parse_vanilla_lang(self) -> bool:
        """从服务端 jar 补充原版物品/方块英文名（minecraft: 命名空间）。

        v0.21.20：原版 / Forge 的原版 jar 在 ``libraries/net/minecraft/server/`` 下；
        Paper / Spigot 系（插件服务端）把服务端 jar 放在根目录（``paper-x.jar`` /
        ``server.jar``）—— 两边都试一遍，插件服务端也能查到原版物品。

        v0.23.8：再补 ``versions/`` / ``cache/`` / ``.fabric/server/`` 三处落点，
        并支持 bundler 的内嵌版本 jar（见 ``_VANILLA_JAR_GLOBS``）。
        """
        target = "assets/minecraft/lang/en_us.json"
        # v0.24.2（F09）：只认「完整来源读过」这个事实（见 _VANILLA_SRC_FLAG）。
        if getattr(self, _VANILLA_SRC_FLAG, False):
            self.legacy_diag = "原版表本次已从完整来源读取（不重复）"
            return True
        cands = self._vanilla_jar_candidates()
        # 读原版表时**不许覆盖**模组自己给的显示名（模组显示名优先保留），
        # 只补空缺 —— 否则「整合包的 Fancy Stick 覆盖」会被原版 Stick 抹掉。
        self._vanilla_lang_mode = True
        try:
            for jar in cands:
                if self._load_vanilla_lang_from(jar, target):
                    setattr(self, _VANILLA_SRC_FLAG, True)
                    self.legacy_diag = f"现代 JSON 语言表命中：{jar.name}"
                    return True
            # B5：现代 JSON 语言文件不存在（= 1.13 以下服务端）→ 走预扁平化通道。
            ok = self._parse_legacy_vanilla(cands)
            if ok:
                setattr(self, _VANILLA_SRC_FLAG, True)
            return ok
        finally:
            self._vanilla_lang_mode = False

    # ================= 预扁平化通道（1.13 以下）· B5 =================

    def _legacy_data_dir(self) -> "Path | None":
        """逐世代数据表目录：优先插件自带 ``data/legacy_items/``，其次缓存目录旁。"""
        cands = [Path(__file__).resolve().parent.parent / "data" / _LEGACY_DATA_DIR]
        if self.cache_path:
            cands.append(self.cache_path.parent / _LEGACY_DATA_DIR)
        for c in cands:
            try:
                if c.is_dir() and self._dir_has_generation_table(c):
                    return c
            except OSError:
                continue
        return None


    @staticmethod
    def _ver_of(text: str) -> "tuple | None":
        m = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", str(text).strip())
        return tuple(int(x) for x in m.groups(default="0")) if m else None

    @staticmethod
    def _is_generation_table(data) -> bool:
        """这份 JSON 是不是**逐世代运行表**（v0.24.3 · GPT v0.24.2 复核 N07）。

        发布目录 ``data/legacy_items/`` 只放**运行资产**；可旧实现对里面的 JSON
        一律照单全收，于是一旦混进别的东西（离线审计夹具、临时导出、改名备份），
        未知版本时会把它当成表用 —— 产出「0 桥接 + 0 变体」却照样写世代号、
        日志还报「通道命中」的**假成功**（实测：两代完整 jar 改名后都选中夹具）。

        判据取运行表**才有**的正面特征：``mc`` 存在，且 ``variants`` 或 ``bridge``
        至少有一个是非空字典。夹具（``registers`` 结构）与垃圾 JSON 一律落选。
        """
        if not isinstance(data, dict) or not data.get("mc"):
            return False
        for key in ("variants", "bridge"):
            val = data.get(key)
            if isinstance(val, dict) and val:
                return True
        return False

    @classmethod
    def _dir_has_generation_table(cls, d) -> bool:
        """目录里**至少有一张合格的世代运行表**才算「表在库」（v0.24.3 · N07）。

        只看 ``*.json`` 是否存在是不够的：一个只堆着夹具 / 临时导出的目录会让上层
        以为表齐了，然后落进「空桥接假成功」。这里直接按内容判。
        """
        try:
            for p in d.glob("*.json"):
                try:
                    if cls._is_generation_table(json.loads(p.read_text(encoding="utf-8"))):
                        return True
                except Exception:                                     # noqa: BLE001
                    continue
        except OSError:
            return False
        return False

    @classmethod
    def _as_mc(cls, value) -> "tuple | None":
        """版本提示 → ``(a, b, c)`` 或 ``None``（字符串与已解析元组都认，v0.24.3 · N07）。"""
        if isinstance(value, (tuple, list)) and len(value) >= 2:
            seq = [int(x) for x in list(value)[:3]]
            while len(seq) < 3:
                seq.append(0)
            return tuple(seq)
        return cls._ver_of(str(value or ""))

    def _pick_legacy_table(self, mc, files: list, require_match: bool = False) -> "Path | None":
        """挑最贴近的世代表。

        ``require_match=True``（唯一调用路径）：**必须**被表的 ``range`` 覆盖才返回，
        否则返回 ``None`` —— 版本不在任何 pre-1.13 世代里时宁可落空（fail-closed），
        绝不拿 1.12.2 的表去服务 1.21（域表不同，域外值会崩客户端）。

        v0.24.3（GPT v0.24.2 复核 N07）三处收口：

        * 候选先过 :meth:`_is_generation_table` —— 夹具 / 垃圾 JSON **不再进候选集**；
        * 修掉「推导式里引用上一轮循环残留的 ``p``」：未知版本时会因此把路径指到
          **上一个文件**（实测选中 ``registry_1.7.10.json``，items/bridge/variants 全 0）；
        * **版本未知时不再猜**：没有 ``mc`` / ``mc_hint`` 就诚实落空，
          由上层按「世代无法确定」拒绝生成，而不是随便挑一张表蒙过去。
        """
        mc = self._as_mc(mc)
        parsed = []
        for p in files:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:                                         # noqa: BLE001
                continue
            if not self._is_generation_table(data):
                continue
            path = Path(p)
            parsed.append((path, data, self._ver_of(data.get("mc", path.stem))))
        if not parsed:
            return None
        if not mc:
            # 版本未知 → 不猜（N07）。挑错世代的域表会直接崩客户端，比「词典 0 条」贵得多。
            return None
        for p, data, _v in parsed:
            rng = data.get("range") or []
            if len(rng) == 2:
                lo, hi = self._ver_of(rng[0]), self._ver_of(rng[1])
                if lo and hi and lo <= tuple(mc) < hi:
                    return p
        if require_match:
            return None
        le = [(v, p) for p, _d, v in parsed if v and v <= tuple(mc)]
        if le:
            return max(le, key=lambda t: t[0])[1]
        vv = [(v, p) for p, _d, v in parsed if v]
        return max(vv, key=lambda t: t[0])[1] if vv else parsed[0][0]


    def _read_legacy_lang(self, jar: Path) -> "str | None":
        """读 1.13 以下服务端 jar 里的 ``.lang`` 文本（含 bundler 内嵌一层）。"""
        try:
            with zipfile.ZipFile(jar) as zf:
                names = zf.namelist()
                for target in _LEGACY_LANG_TARGETS:
                    if target in names:
                        return zf.read(target).decode("utf-8", "replace")
                for name in [n for n in names if _NESTED_VERSION_RE.match(n)][:_NESTED_MAX_JARS]:
                    try:
                        if zf.getinfo(name).file_size > _NESTED_MAX_BYTES:
                            continue
                        with zipfile.ZipFile(io.BytesIO(zf.read(name))) as inner:
                            for target in _LEGACY_LANG_TARGETS:
                                if target in inner.namelist():
                                    return inner.read(target).decode("utf-8", "replace")
                    except Exception:
                        continue
        except Exception:
            return None
        return None

    def _parse_preflatten_lang(self, text: str, bridge: dict) -> int:
        """解析 ``.lang`` 文本表，返回注册条数。

        - 单键（``item.shovelIron.name``）→ 用桥接表换成注册名；
        - 变体键（``tile.wood.oak.name``）→ 用**内置域表的展示名**反查 (族, 数据值)，
          把该服务端自己的展示名刷进族表（比内置表更贴合这台服务器）。
        """
        vindex: dict[str, tuple] = {}
        for fam, fv in (self.legacy_variants or {}).items():
            for dmg, disp in (fv.get("variants") or {}).items():
                if disp:
                    vindex[str(disp).strip().lower()] = (fam, dmg)
        n = 0
        for raw in text.split("\n"):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, disp = line.partition("=")
            m = _LEGACY_LANG_KEY_RE.match(key.strip())
            if not m:
                continue
            base, var = m.group(1), m.group(2)
            disp = disp.strip()
            if var:
                hit = vindex.get(disp.lower())
                if hit:
                    fam, dmg = hit
                    cur = self.items.setdefault(
                        "minecraft:" + fam, {"en": fam, "zh": "", "type": "block"})
                    cur.setdefault("variants", {})[str(dmg)] = disp
                    n += 1
                continue
            ent = bridge.get(base)
            if not ent:
                continue
            rid = "minecraft:" + ent["registry"]
            cur = self.items.get(rid)
            if cur:
                cur["en"] = disp or cur.get("en") or rid
            else:
                self.items[rid] = {"en": disp or ent["registry"], "zh": "",
                                   "type": "item", "legacy_key": base}
            n += 1
        return n

    def _parse_legacy_vanilla(self, cands=None) -> bool:
        """1.13 以下：``.lang`` 文本表 + 内置逐世代域表（B5）。

        成功返回 True。失败（没有表 / 没有可用 jar）返回 False —— 词典条数保持 0，
        与旧行为一致（fail-closed 由生成层负责，不在这里猜）。
        """
        d = self._legacy_data_dir()
        if not d:
            self.legacy_diag = f"缺逐世代数据表目录（{_LEGACY_DATA_DIR}/）"
            return False
        cands = list(cands or self._vanilla_jar_candidates())
        if not cands:
            self.legacy_diag = f"服务端目录下找不到任何候选 jar（server_dir={self.server_dir}）"
            return False
        seen_lang = 0
        for jar in self._vanilla_jar_candidates():
            # ★ 硬判据（2026-10-07 回归修正）：**必须**在这台机器的 jar 里读到 legacy
            #   ``.lang`` 文本表，才认定它是「1.13 以下的预扁平化服务端」。
            #   否则（例如 1.13+ 但没找到原版核、或纯引导 jar）一律落空 ——
            #   绝不拿内置旧表去套现代服务端（域表不同，域外数据值会让客户端崩）。
            text = self._read_legacy_lang(jar)
            if not text:
                continue
            seen_lang += 1
            m = _JAR_VERSION_RE.search(jar.name)
            mc = self._as_mc(m.group(1)) if m else self._as_mc(getattr(self, "mc_hint", ""))
            if mc is None:
                # v0.24.3（N07）：**版本不知道就不猜**。挑错世代的域表会直接崩客户端，
                # 比「词典 0 条」贵得多；宁可诚实拒绝，也不给「假成功」。
                self.legacy_diag = (
                    f"{jar.name} 里有 legacy .lang（确实是 1.13 以下的预扁平化服务端），"
                    "但版本无法确定（文件名没有版本号，配置里的服务端版本也是空的）"
                    "→ 为避免用错世代的域表，拒绝猜表、不建词典"
                )
                continue
            path = self._pick_legacy_table(mc, sorted(d.glob("*.json")), require_match=True)
            if not path:
                self.legacy_diag = (
                    f"{jar.name} 里有 legacy .lang，但版本 {mc} 不在任何**世代运行表**的 "
                    "range 内（或目录里一张合格的运行表都没有）→ 落空"
                )
                continue

            try:
                table = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            self.legacy_generation = str(table.get("mc") or path.stem)
            self.legacy_variants = dict(table.get("variants") or {})
            self.legacy_bridge = dict(table.get("bridge") or {})
            # v0.24.3（N07）：空表**不许**当成功 —— 旧实现在未知版本下挑中夹具/垃圾文件，
            # 产出「0 桥接 + 0 变体」却照样写 legacy_generation，日志还报「通道命中」。
            if not self.legacy_bridge and not self.legacy_variants:
                self.legacy_diag = (f"{path.name} 解析后既无桥接也无变体族（空表 / 结构不符）"
                                    "→ 拒绝按成功处理")
                continue
            self._parse_preflatten_lang(text, self.legacy_bridge)

            # 表兜底：jar 里没有 lang（或键不全）时，用内置表把条目补齐
            for key, ent in self.legacy_bridge.items():
                rid = "minecraft:" + ent["registry"]
                self.items.setdefault(rid, {"en": ent.get("display") or ent["registry"],
                                            "zh": "", "type": "item", "legacy_key": key})
            for fam, fv in self.legacy_variants.items():
                rid = "minecraft:" + fam
                cur = self.items.setdefault(rid, {"en": fam, "zh": "", "type": "block"})
                # 合并而非覆盖：**服务端自己的展示名优先**（比内置表更贴合这台机器）
                merged = dict(fv.get("variants") or {})
                merged.update(cur.get("variants") or {})
                cur["variants"] = merged
                cur["domain_upper"] = int(fv.get("domain_upper", -1))
                cur["legacy_key"] = fam
            self.legacy_diag = (f"预扁平化通道命中：{jar.name} → 世代 {self.legacy_generation}"
                                f"（桥接 {len(self.legacy_bridge)} / 变体族 {len(self.legacy_variants)}）")
            return True
        self.legacy_diag = (f"候选 jar {len(cands)} 个，其中带 legacy .lang 的 {seen_lang} 个"
                            f"（server_dir={self.server_dir}）→ 未装载")
        return False

    def _parse_jar(self, jar: Path) -> None:
        try:
            with zipfile.ZipFile(jar) as zf:
                names = set(zf.namelist())
                # v0.24.2（GPT 全面复核 F10）：**不再依赖 Forge 的 META-INF/mods.toml
                # 推 mod_id** —— 标准 Fabric 模组只有 fabric.mod.json，老写法走到
                # `if not mod_id: return` 就返回了，整包物品展示名都进不了词典。
                # 现在按 jar 里**实际存在的** assets/<ns>/lang/ 与 data/<ns>/recipes/
                # 命名空间来解析（Forge 模组同样受益，且能处理多命名空间的 jar）。
                ns_set: set[str] = set()
                for n in names:
                    if n.startswith("assets/") and "/lang/" in n:
                        ns = n[len("assets/"):].split("/lang/", 1)[0]
                        if ns and "/" not in ns:
                            ns_set.add(ns)
                    elif n.startswith("data/") and "/recipes/" in n:
                        ns = n[len("data/"):].split("/recipes/", 1)[0]
                        if ns and "/" not in ns:
                            ns_set.add(ns)
                mod_id, mod_name = None, None
                if "fabric.mod.json" in names:
                    try:
                        fm = json.loads(zf.read("fabric.mod.json").decode("utf-8", "replace"))
                        fid = fm.get("id")
                        fname = fm.get("name")
                        mod_id = str(fid) if isinstance(fid, str) and fid else None
                        mod_name = str(fname) if isinstance(fname, str) and fname else None
                    except Exception:                                     # noqa: BLE001
                        pass
                if not mod_id and "META-INF/mods.toml" in names:
                    mod_id, mod_name = self._parse_mods_toml(
                        zf.read("META-INF/mods.toml").decode("utf-8", "replace")
                    )
                if not mod_id and not ns_set:
                    return                      # 既无元数据、又无语言/配方 → 不是内容模组
                if not mod_id:
                    mod_id = sorted(ns_set)[0]      # 兜底：用实际的资源命名空间
                # 元数据里的 mod_id 也要扫一遍：jar 没有语言文件时，它仍然是**一个模组**
                # （mc_list_mods 要能看到它）—— 这是老行为，不许回退。
                ns_set.add(mod_id)
                entry = {"id": mod_id, "name": mod_name or jar.stem}
                if entry not in self.mods:
                    self.mods.append(entry)
                # 本地化文件（en_us 英文名 + zh_cn 中文名）与配方：逐个命名空间解析
                for ns in sorted(ns_set):
                    for lang_name, lang_code in (
                        ("en_us.json", "en"),
                        ("zh_cn.json", "zh"),
                    ):
                        lang_path = f"assets/{ns}/lang/{lang_name}"
                        if lang_path in names:
                            self._parse_lang(
                                zf.read(lang_path).decode("utf-8", "replace"),
                                ns,
                                lang_code,
                            )
                    prefix = f"data/{ns}/recipes/"
                    for n in names:
                        if n.startswith(prefix) and n.endswith(".json"):
                            self._parse_recipe(
                                zf.read(n).decode("utf-8", "replace"), ns, n
                            )
        except Exception:
            # 单个 jar 解析失败不影响整体
            pass

    _TOML_KV_RE = re.compile(r'^\s*(\w+)\s*=\s*"([^"]*)"')

    @classmethod
    def _parse_mods_toml(cls, text: str) -> tuple[str | None, str | None]:
        mod_id = mod_name = None
        in_mods = False
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("[[mods]]"):
                in_mods = True
                continue
            if in_mods:
                if s.startswith("[") and not s.startswith("[[mods"):
                    break
                m = cls._TOML_KV_RE.match(s)
                if m:
                    k, v = m.group(1), m.group(2)
                    if k == "modId":
                        mod_id = v
                    elif k == "displayName":
                        mod_name = v
                if mod_id and mod_name:
                    break
        return mod_id, mod_name

    def _parse_lang(self, text: str, mod_id: str, lang: str) -> None:
        try:
            data = json.loads(text)
        except Exception:
            return
        for key, value in data.items():
            m = _LANG_KEY_RE.match(key)
            if not m:
                continue
            rest = key.split(".", 1)[1]
            parts = rest.split(".")
            if len(parts) != 2:
                # 跳过 tooltip / 子键等非物品键
                continue
            ns, name = parts
            item_id = f"{ns}:{name}"
            entry = self.items.setdefault(
                item_id, {"en": "", "zh": "", "type": m.group(1)}
            )
            # v0.24.2（F09）：原版表只补空缺、不覆盖模组显示名
            keep = bool(getattr(self, "_vanilla_lang_mode", False))
            if lang == "zh":
                if value and (not entry["zh"] or not keep):
                    entry["zh"] = str(value)
            else:
                if value and (not entry["en"] or not keep):
                    entry["en"] = str(value)

    def _parse_recipe(self, text: str, mod_id: str, name: str) -> None:
        try:
            data = json.loads(text)
        except Exception:
            return
        result = data.get("result") if "result" in data else data.get("output")
        output, count = None, 1
        if isinstance(result, dict):
            output = result.get("item") or result.get("id")
            count = result.get("count", 1)
        elif isinstance(result, str):
            output = result
        if not output:
            return
        inputs: list[str] = []
        for k in _RECIPE_INPUT_KEYS:
            if k not in data:
                continue
            v = data[k]
            if isinstance(v, dict):
                self._collect_id(v, inputs)
            elif isinstance(v, list):
                for sub in v:
                    if isinstance(sub, dict):
                        self._collect_id(sub, inputs)
        # pattern 中的 key 映射（合成配方）
        if "key" in data and isinstance(data["key"], dict):
            keymap = data["key"]
            for row in data.get("pattern", []) or []:
                for ch in row:
                    if ch in keymap and isinstance(keymap[ch], dict):
                        self._collect_id(keymap[ch], inputs)
        recipe = {
            "id": name,
            "type": str(data.get("type", "")).split(":")[-1],
            "output": output,
            "count": count,
            "inputs": list(dict.fromkeys(inputs)),
        }
        self.recipes.setdefault(output, []).append(recipe)

    @staticmethod
    def _collect_id(component: dict, into: list[str]) -> None:
        iid = component.get("item") or component.get("tag")
        if iid:
            into.append(iid)

    # ================= 缓存 =================

    def load_cache(self) -> bool:
        """从缓存文件加载词典。成功返回 True。"""
        if not self.cache_path or not self.cache_path.exists():
            return False
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.mods = data.get("mods", [])
            self.items = data.get("items", {})
            self.recipes = data.get("recipes", {})
            return True
        except Exception:
            return False

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "mods": self.mods,
                "items": self.items,
                "recipes": self.recipes,
            }
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.cache_path)
        except Exception:
            pass

    # ================= 检索 =================

    def search_items(self, keyword: str, limit: int = 15) -> list[dict]:
        """按中英文名/ID 片段模糊搜索物品，返回 [{id, en, zh, type}]。"""
        kw = keyword.strip().lower()
        if not kw:
            return []
        # 支持空格变体：iron ingot → iron_ingot / ironingot
        variants = {kw, kw.replace(" ", "_"), kw.replace(" ", "")}
        exact: list[tuple] = []
        prefix: list[tuple] = []
        contains: list[tuple] = []
        for item_id, entry in self.items.items():
            en = (entry.get("en") or "").lower()
            zh = entry.get("zh") or ""
            id_l = item_id.lower()
            # B5：预扁平化变体族 —— 展示名（如 Red Wool）也要能命中，并带回**数据值**
            vmap = {str(k): (v or "") for k, v in (entry.get("variants") or {}).items()}
            vlow = {k: v.lower() for k, v in vmap.items()}
            hit_exact = next((int(k) for k, v in vlow.items() if v in variants), None)
            hit_pre = next((int(k) for k, v in vlow.items()
                            if any(v.startswith(x) for x in variants)), None)
            hit_sub = next((int(k) for k, v in vlow.items()
                            if any(x in v for x in variants)), None)
            if id_l in variants or en in variants or zh in variants or hit_exact is not None:
                exact.append((item_id, hit_exact))
            elif any(id_l.startswith(v) or en.startswith(v) or zh.startswith(v)
                     for v in variants) or hit_pre is not None:
                prefix.append((item_id, hit_pre))
            elif any(v in id_l or v in en or v in zh for v in variants) or hit_sub is not None:
                contains.append((item_id, hit_sub))
        ranked = exact + prefix + contains
        out: list[dict] = []
        for i, dmg in ranked[:limit]:
            row = {"id": i, **self.items[i]}
            if dmg is not None:
                row["variant_damage"] = dmg
                row["variant_display"] = (self.items[i].get("variants") or {}).get(str(dmg))
            out.append(row)
        return out

    def get_recipes(self, item: str, direction: str = "forward") -> list[dict]:
        """配方查询。forward=该物品怎么造；reverse=该物品能用来造什么。"""
        if direction == "reverse":
            out = []
            for output, recipes in self.recipes.items():
                for r in recipes:
                    if item in r["inputs"]:
                        out.append(r)
            return out
        return self.recipes.get(item, [])
