"""v0.21.20 回归：「插件服务端也能算出指纹」+ 服务端形态兜底。

主人提的问题：知识库指纹要读 mods/ 目录，可 Paper 这类插件服务端根本没有
mods/，内容在 plugins/ —— 老算法在它们身上恒得 md5("") = d41d8cd98f00，
也就是「认不出任何服务端」。本测试钉住新契约：

  [1] 整合包（mods/*.jar）指纹与老算法**逐字节一致**（既有预设不会失效）
  [2] 插件服务端（plugins/*.jar）：读 plugin.yml / paper-plugin.yml / bungee.yml /
      velocity-plugin.json 算出插件 ID 集合指纹，不再退化成空指纹
  [3] 插件版本更新（同 ID、改内容）→ 指纹不变；增删插件 → 指纹变
  [4] 原版 / 空插件目录 → 退化到「服务端形态」（paper/spigot + MC 版本）
  [5] 空壳目录 → kind=unknown、weak=True、指纹不再是那个撞车值
  [6] 混合端（mods + plugins）→ kind=hybrid，指纹与「只算 mods」不同
  [7] 目录结构校验：plugins/ 被正确识别（content_kind / 文案不再误导）
  [8] 解析缓存：插件 jar 第一次解析、第二次命中缓存（不重复读 zip）
  [9] .disabled 跳过；认不出 ID 的 jar 计入 unidentified

不需要 AstrBot 运行时。运行：
  python tests\\test_server_identity.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/ → _paths
from _paths import PLUGIN_DIR, add_sys_paths  # noqa: E402
add_sys_paths()

from astrbot_plugin_Scintilla_MC_Server_Control.core import mod_fingerprint as mf          # noqa: E402
from astrbot_plugin_Scintilla_MC_Server_Control.core.server_dir_check import (             # noqa: E402
    inspect_server_dir,
)

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  ✓ " if ok else "  ✗ ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def make_jar(path: Path, files: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
        zf.writestr("dummy.txt", name_stub(path))


def name_stub(p: Path) -> str:
    return f"stub-{p.name}"


def mod_jar(path: Path, mod_id: str, extra: str = "") -> None:
    make_jar(path, {"META-INF/mods.toml": f'[[mods]]\nmodId="{mod_id}"\n{extra}'})


def plugin_jar(path: Path, plugin_name: str, meta: str = "plugin.yml",
               extra_body: str = "") -> None:
    make_jar(path, {meta: f"name: {plugin_name}\nmain: com.example.{plugin_name}.Main\n"
                          f"{extra_body}"})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mcid_"))
    cache = tmp / "mod_ids_cache.json"

    # ---------------- [1] 整合包指纹与老算法一致 ----------------
    print("[1] 整合包：新算法与老算法指纹逐字节一致（既有预设不会失效）")
    pack = tmp / "pack"
    mod_jar(pack / "mods" / "a-1.0.jar", "alpha")
    mod_jar(pack / "mods" / "b-1.0.jar", "beta")
    legacy_fp, legacy_ids = mf.compute_stable_server_id(str(pack / "mods"), str(cache))
    ident = mf.compute_server_identity(str(pack), str(cache))
    check("指纹相同", ident["fingerprint"] == legacy_fp, f"{ident['fingerprint']} vs {legacy_fp}")
    check("kind=modpack", ident["kind"] == "modpack", ident["kind"])
    check("mod ID 集合一致", ident["mods"] == legacy_ids, str(ident["mods"]))
    check("非 weak", ident["weak"] is False)
    check("summary 提到 mods/", "mods/" in ident["summary"], ident["summary"])
    check("mods 有内容时哈希输入不含盐", ident["fingerprint"] == legacy_fp)

    # ---------------- [2] 插件服务端 ----------------
    print("[2] Paper 插件服务端：plugins/*.jar → 插件 ID 集合指纹")
    paper = tmp / "paper"
    (paper / "plugins").mkdir(parents=True)
    (paper / "logs").mkdir(parents=True)
    (paper / "server.properties").write_text("motd=x\n", encoding="utf-8")
    (paper / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    (paper / "paper-1.20.1-100.jar").write_bytes(b"")
    (paper / "plugins" / "EssentialsX.jar").write_bytes(b"")
    plugin_jar(paper / "plugins" / "EssentialsX.jar", "EssentialsX")
    plugin_jar(paper / "plugins" / "Vault.jar", "Vault", meta="paper-plugin.yml")
    plugin_jar(paper / "plugins" / "WorldEdit.jar", "WorldEdit", meta="bungee.yml")
    make_jar(paper / "plugins" / "VelocityPlugin.jar",
             {"velocity-plugin.json": json.dumps({"id": "MyVelocityThing"})})
    p_ident = mf.compute_server_identity(str(paper), str(cache))
    check("kind=plugins", p_ident["kind"] == "plugins", p_ident["kind"])
    check("算出 4 个插件 ID", len(p_ident["plugins"]) == 4, str(p_ident["plugins"]))
    check("ID 全部小写规范",
          p_ident["plugins"] == sorted(x.lower() for x in p_ident["plugins"]),
          str(p_ident["plugins"]))
    check("指纹不再是空指纹 d41d8cd98f00",
          p_ident["fingerprint"] != mf._fingerprint(""), p_ident["fingerprint"])
    check("weak=False（有内容可识别）", p_ident["weak"] is False)
    check("summary 说明内容在 plugins/", "plugins/" in p_ident["summary"], p_ident["summary"])

    # ---------------- [3] 稳定性：版本更新不变，增删插件才变 ----------------
    print("[3] 插件版本更新不改指纹；增删插件才改指纹")
    plugin_jar(paper / "plugins" / "Vault.jar", "Vault", meta="paper-plugin.yml",
               extra_body="version: 9.9.9\n" + "x" * 500)
    p2 = mf.compute_server_identity(str(paper), str(cache))
    check("同 ID、改内容（版本更新）→ 指纹不变",
          p2["fingerprint"] == p_ident["fingerprint"],
          f"{p2['fingerprint']} vs {p_ident['fingerprint']}")
    plugin_jar(paper / "plugins" / "NewPlugin.jar", "BrandNew")
    p3 = mf.compute_server_identity(str(paper), str(cache))
    check("新增插件 → 指纹变化", p3["fingerprint"] != p_ident["fingerprint"])
    (paper / "plugins" / "NewPlugin.jar").unlink()
    p4 = mf.compute_server_identity(str(paper), str(cache))
    check("移除插件后又回到原指纹", p4["fingerprint"] == p_ident["fingerprint"])

    # ---------------- [4] 原版 / 空插件目录 → 服务端形态 ----------------
    print("[4] 原版服务端 / 装了空的 plugins/ → 按「服务端形态」算指纹")
    vanilla = tmp / "vanilla"
    (vanilla / "logs").mkdir(parents=True)
    (vanilla / "server.properties").write_text("motd=x\n", encoding="utf-8")
    (vanilla / "server.jar").write_bytes(b"")
    (vanilla / "version_history.json").write_text(
        json.dumps({"currentVersion": "1.20.1"}), encoding="utf-8")
    v_ident = mf.compute_server_identity(str(vanilla), str(cache))
    check("kind=server", v_ident["kind"] == "server", v_ident["kind"])
    check("读出版本 1.20.1", v_ident["shape"]["mc"] == "1.20.1", str(v_ident["shape"]))
    check("weak=True（指纹认不出同形态的两台服务端）", v_ident["weak"] is True)
    check("指纹不等于 d41d8cd98f00", v_ident["fingerprint"] != mf._fingerprint(""))
    (vanilla / "version_history.json").write_text(
        json.dumps({"currentVersion": "1.21.1"}), encoding="utf-8")
    v2 = mf.compute_server_identity(str(vanilla), str(cache))
    check("换版本 → 指纹变化（形态变了）", v2["fingerprint"] != v_ident["fingerprint"])
    check("summary 讲到「按服务端形态」", "服务端形态" in v_ident["summary"], v_ident["summary"])

    print("[4b] Paper：只有 bukkit.yml / logs，没有 jar 与 version_history")
    paper2 = tmp / "paper2"
    (paper2 / "logs").mkdir(parents=True)
    (paper2 / "bukkit.yml").write_text("settings: {}\n", encoding="utf-8")
    (paper2 / "logs" / "latest.log").write_text(
        "[12:00:00] [Server thread/INFO]: Starting minecraft server version 1.20.4\n",
        encoding="utf-8")
    pb = mf.compute_server_identity(str(paper2), str(cache))
    check("kind=server", pb["kind"] == "server", pb["kind"])
    check("从日志读出版本 1.20.4", pb["shape"]["mc"] == "1.20.4", str(pb["shape"]))
    check("识别出 Bukkit 家族（bukkit.yml）", pb["shape"]["sw"] == "bukkit", str(pb["shape"]))

    print("[4c] Paper 版本的 version_history.json（git-Paper-196 (MC: 1.20.1)）")
    (paper2 / "version_history.json").write_text(
        json.dumps({"oldVersion": [], "currentVersion": "git-Paper-196 (MC: 1.20.1)"}),
        encoding="utf-8")
    pc = mf.compute_server_identity(str(paper2), str(cache))
    check("软件识别为 paper", pc["shape"]["sw"] == "paper", str(pc["shape"]))
    check("版本识别为 1.20.1", pc["shape"]["mc"] == "1.20.1", str(pc["shape"]))

    # ---------------- [5] 空壳目录 ----------------
    print("[5] 空壳目录：kind=unknown、weak=True、指纹不再是撞车值")
    shell = tmp / "shell"
    shell.mkdir()
    s_ident = mf.compute_server_identity(str(shell), str(cache))
    check("kind=unknown", s_ident["kind"] == "unknown", s_ident["kind"])
    check("weak=True", s_ident["weak"] is True)
    check("指纹不再是 d41d8cd98f00（老算法在这里撞车）",
          s_ident["fingerprint"] != "d41d8cd98f00", s_ident["fingerprint"])
    check("summary 说明是占位值", "占位值" in s_ident["summary"], s_ident["summary"])

    # ---------------- [6] 混合端 ----------------
    print("[6] 混合端（Mohist/Arclight 之类）：mods + plugins 一起算")
    hybrid = tmp / "hybrid"
    (hybrid / "logs").mkdir(parents=True)
    (hybrid / "server.properties").write_text("motd=x\n", encoding="utf-8")
    mod_jar(hybrid / "mods" / "a.jar", "alpha")
    plugin_jar(hybrid / "plugins" / "LuckPerms.jar", "LuckPerms")
    h_ident = mf.compute_server_identity(str(hybrid), str(cache))
    check("kind=hybrid", h_ident["kind"] == "hybrid", h_ident["kind"])
    only_mods = mf.compute_server_identity(str(tmp / "pack"), str(cache))["fingerprint"]
    check("指纹与「只算 mods」不同（插件也算进身份）",
          h_ident["fingerprint"] != only_mods)
    check("summary 同时提到 mods/ 与 plugins/",
          "mods/" in h_ident["summary"] and "plugins/" in h_ident["summary"],
          h_ident["summary"])

    # ---------------- [7] 目录结构校验 ----------------
    print("[7] server_dir_check：plugins/ 被识别为插件服务端")
    chk = inspect_server_dir(paper)
    check("校验通过", chk["ok"] is True, str(chk.get("errors")))
    check("content_kind=plugins", chk.get("content_kind") == "plugins", str(chk.get("content_kind")))
    check("markers 里有 plugins_count", chk["markers"].get("plugins_count") == 4,
          str(chk["markers"].get("plugins_count")))
    check("结论提到 plugins 与「插件服务端」",
          "plugins 内 4 个 jar" in chk["summary"] and "插件服务端" in chk["summary"],
          chk["summary"])
    check("不再说「物品词典与知识库会是空的」（插件服不算错）",
          not any("物品词典与知识库会是空的" in w for w in chk["warnings"]),
          str(chk["warnings"]))
    check("插件服务端提示里点明指纹按 plugins 算",
          any("内容指纹按这份插件集合计算" in w for w in chk["warnings"]),
          str(chk["warnings"]))
    chk_v = inspect_server_dir(vanilla)
    check("原版目录 content_kind=vanilla", chk_v.get("content_kind") == "vanilla",
          str(chk_v.get("content_kind")))
    check("原版目录仍提示词典会是空的",
          any("物品词典与知识库会是空的" in w for w in chk_v["warnings"]),
          str(chk_v["warnings"]))
    chk_h = inspect_server_dir(hybrid)
    check("混合端 content_kind=hybrid", chk_h.get("content_kind") == "hybrid",
          str(chk_h.get("content_kind")))

    # ---------------- [8] 缓存 ----------------
    print("[8] 解析缓存：第二次不再重新读 zip")
    cache2 = tmp / "c2.json"
    calls = {"n": 0}
    real = mf.extract_plugin_ids

    def counting(path: str):
        calls["n"] += 1
        return real(path)

    mf.extract_plugin_ids = counting
    try:
        f1 = mf.compute_server_identity(str(paper), str(cache2))["fingerprint"]
        first = calls["n"]
        f2 = mf.compute_server_identity(str(paper), str(cache2))["fingerprint"]
        second = calls["n"] - first
    finally:
        mf.extract_plugin_ids = real
    check("首次解析了 4 个插件 jar", first == 4, str(first))
    check("第二次全部命中缓存（0 次解析）", second == 0, str(second))
    check("两次指纹一致", f1 == f2, f"{f1} vs {f2}")
    saved = json.loads(Path(cache2).read_text(encoding="utf-8"))
    check("缓存里插件条目带 plugin: 前缀（与 mods 条目不撞车）",
          any(k.startswith("plugin:") for k in saved), str(list(saved)[:3]))

    # ---------------- [9] 边角 ----------------
    print("[9] 边角：.disabled 跳过、认不出 ID 的 jar 计入 unidentified、引号与注释")
    edge = tmp / "edge"
    (edge / "plugins").mkdir(parents=True)
    plugin_jar(edge / "plugins" / "Off.jar.disabled", "ShouldNotCount")
    (edge / "plugins" / "Mystery.jar").parent.mkdir(parents=True, exist_ok=True)
    make_jar(edge / "plugins" / "Mystery.jar", {"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n"})
    make_jar(edge / "plugins" / "Quoted.jar",
             {"plugin.yml": 'name: "Quoted Plugin"  # 行内注释\nmain: a.B\n'})
    make_jar(edge / "plugins" / "Commented.jar",
             {"plugin.yml": "main: a.B\nname: PlainName # 尾巴注释\n"})
    make_jar(edge / "plugins" / "Nested.jar",
             {"plugin.yml": "main: a.B\ndescription:\n  name: NestedNotThis\n"})
    e_ident = mf.compute_server_identity(str(edge), str(cache))
    check(".disabled 的 jar 被跳过",
          all(x != "shouldnotcount" for x in e_ident["plugins"]), str(e_ident["plugins"]))
    check("带引号的 name 去引号", "quoted plugin" in e_ident["plugins"], str(e_ident["plugins"]))
    check("行内注释被剥掉", "plainname" in e_ident["plugins"], str(e_ident["plugins"]))
    check("嵌套的 name: 不当作插件名",
          "nestednotthis" not in e_ident["plugins"], str(e_ident["plugins"]))
    check("认不出 ID 的 jar 计入 unidentified（Mystery + 没有顶格 name 的 Nested）",
          e_ident["unidentified"] == 2, str(e_ident["unidentified"]))
    check("4 个 jar 计数正确（.disabled 不算）", e_ident["plugin_jars"] == 4,
          str(e_ident["plugin_jars"]))

    # ---------------- [10] 插件服务端 → 词典兜底 ----------------
    print("[10] 插件服务端（无 mods/）→ 物品词典退化为原版物品表")
    from astrbot_plugin_Scintilla_MC_Server_Control.core.item_dictionary import ItemDictionary  # noqa: E402
    pv = tmp / "paper_dict"
    (pv / "plugins").mkdir(parents=True)
    (pv / "logs").mkdir(parents=True)
    (pv / "server.properties").write_text("motd=x\n", encoding="utf-8")
    plugin_jar(pv / "plugins" / "LuckPerms.jar", "LuckPerms")
    make_jar(pv / "paper-1.20.1-100.jar", {
        "assets/minecraft/lang/en_us.json": json.dumps({
            "item.minecraft.diamond_sword": "Diamond Sword",
            "block.minecraft.diamond_block": "Block of Diamond",
            "item.minecraft.enchanted_book": "Enchanted Book",
            "gui.something.else": "不该进词典",
        }, ensure_ascii=False),
    })
    dic = ItemDictionary(str(pv), str(tmp / "items_paper.json"))
    stat = dic.build()
    check("mods 为空（插件不提供物品数据）", stat["mods"] == 0 and dic.mods == [], str(stat))
    check("从根目录服务端 jar 读到原版物品",
          "minecraft:diamond_sword" in dic.items, str(list(dic.items)[:3]))
    check("item / block 类型分得清",
          dic.items["minecraft:diamond_sword"]["type"] == "item"
          and dic.items["minecraft:diamond_block"]["type"] == "block",
          str(dic.items.get("minecraft:diamond_block")))
    check("非物品键（gui.*）不进词典", all("gui" not in k for k in dic.items))
    check("英文名可检索到精确 ID",
          any(x["id"] == "minecraft:diamond_sword" for x in dic.search_items("diamond sword")),
          str(dic.search_items("diamond sword")[:2]))
    check("直接搜 ID 片段也能命中",
          any(x["id"] == "minecraft:diamond_sword" for x in dic.search_items("diamond_sword")))
    check("原版物品无中文名（服务端 jar 只有 en_us）—— 中文搜不到，属已知边界",
          dic.search_items("钻石剑") == [], str(dic.search_items("钻石剑")[:2]))

    print()
    if FAILED:
        print("✗ 失败项:")
        for f in FAILED:
            print("  -", f)
        return 1
    print("✓ 全部通过（插件服务端指纹 + 形态兜底 + 词典兜底 10 组）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
