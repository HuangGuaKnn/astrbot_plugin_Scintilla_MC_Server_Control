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

构建结果缓存到插件数据目录 items.json，供 mc_list_mods / mc_search_item
/ mc_get_recipes 等 LLM 工具查询，解决整合包海量物品时 LLM 猜错 ID 的问题。
"""
from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

_LANG_KEY_RE = re.compile(r"^(item|block)\.")
_RECIPE_RESULT_KEYS = ("result",)
_RECIPE_INPUT_KEYS = ("ingredients", "key", "base", "addition", "input", "ingredient")


class ItemDictionary:
    """Mod 物品词典：构建、缓存、检索、配方查询。"""

    def __init__(self, server_dir: str, cache_path: str | None = None):
        self.server_dir = Path(server_dir)
        self.cache_path = Path(cache_path) if cache_path else None
        self.mods: list[dict] = []          # [{"id": ..., "name": ...}]
        self.items: dict[str, dict] = {}    # {"modid:name": {"en","zh","type"}}
        self.recipes: dict[str, list] = {}  # {"output_id": [recipe...]}

    # ================= 构建 =================

    def build(self) -> dict:
        """扫描 mods 目录构建词典。返回统计信息。"""
        self.mods, self.items, self.recipes = [], {}, {}
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
        }

    def _parse_vanilla_lang(self) -> None:
        """从服务端 jar 补充原版物品/方块英文名（minecraft: 命名空间）。

        v0.21.20：原版 / Forge 的原版 jar 在 ``libraries/net/minecraft/server/`` 下；
        Paper / Spigot 系（插件服务端）只把服务端 jar 放在根目录（``paper-x.jar`` /
        ``server.jar``）—— 两边都试一遍，插件服务端也能查到原版物品。
        """
        target = "assets/minecraft/lang/en_us.json"
        candidates: list[Path] = []
        server_lib = self.server_dir / "libraries" / "net" / "minecraft" / "server"
        try:
            if server_lib.exists():
                candidates += sorted(server_lib.glob("*/*.jar"))
        except OSError:
            pass
        if not any(k.startswith("minecraft:") for k in self.items):
            # 根目录的服务端 jar（插件服务端走这条路；限 8 个，防病态目录）
            try:
                candidates += [p for p in sorted(self.server_dir.glob("*.jar"))[:8] if p.is_file()]
            except OSError:
                pass
        for jar in candidates:
            try:
                with zipfile.ZipFile(jar) as zf:
                    if target in zf.namelist():
                        self._parse_lang(
                            zf.read(target).decode("utf-8", "replace"),
                            "minecraft",
                            "en",
                        )
                        return
            except Exception:
                continue

    def _parse_jar(self, jar: Path) -> None:
        try:
            with zipfile.ZipFile(jar) as zf:
                names = set(zf.namelist())
                mod_id, mod_name = None, None
                if "META-INF/mods.toml" in names:
                    mod_id, mod_name = self._parse_mods_toml(
                        zf.read("META-INF/mods.toml").decode("utf-8", "replace")
                    )
                if not mod_id:
                    return
                entry = {"id": mod_id, "name": mod_name or jar.stem}
                if entry not in self.mods:
                    self.mods.append(entry)
                # 本地化文件（en_us 英文名 + zh_cn 中文名）
                for lang_name, lang_code in (
                    ("en_us.json", "en"),
                    ("zh_cn.json", "zh"),
                ):
                    lang_path = f"assets/{mod_id}/lang/{lang_name}"
                    if lang_path in names:
                        self._parse_lang(
                            zf.read(lang_path).decode("utf-8", "replace"),
                            mod_id,
                            lang_code,
                        )
                # 配方
                prefix = f"data/{mod_id}/recipes/"
                for n in names:
                    if n.startswith(prefix) and n.endswith(".json"):
                        self._parse_recipe(
                            zf.read(n).decode("utf-8", "replace"), mod_id, n
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
            if lang == "zh":
                entry["zh"] = str(value)
            else:
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
        exact: list[str] = []
        prefix: list[str] = []
        contains: list[str] = []
        for item_id, entry in self.items.items():
            en = (entry.get("en") or "").lower()
            zh = entry.get("zh") or ""
            id_l = item_id.lower()
            if id_l in variants or en in variants or zh in variants:
                exact.append(item_id)
            elif any(
                id_l.startswith(v) or en.startswith(v) or zh.startswith(v)
                for v in variants
            ):
                prefix.append(item_id)
            elif any(
                v in id_l or v in en or v in zh for v in variants
            ):
                contains.append(item_id)
        ranked = exact + prefix + contains
        return [
            {"id": i, **self.items[i]} for i in ranked[:limit]
        ]

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
