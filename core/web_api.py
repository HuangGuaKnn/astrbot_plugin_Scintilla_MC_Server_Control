"""MC控制插件 WebUI 后端 API。

页面位于 pages/mc_control/，通过 AstrBot 插件页面机制（PluginPageService）
自动挂载到左侧导航。前端经 bridge (window.AstrBotPluginPage) 调用本模块
注册的接口（route 前缀: astrbot_plugin_Scintilla_MC_Server_Control/page/...）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from astrbot.api.web import request, json_response

from .agent_prompts import AGENT_ORDER, AGENT_SPECS, describe_agents
from .rcon import IDLE_PROBE
from .server_dir_check import format_check, inspect_server_dir

PAGE_PREFIX = "astrbot_plugin_Scintilla_MC_Server_Control/page"

# 插件展示信息（与 metadata.yaml 保持一致）
# v0.21.22 开源卫生：# _cfg_path 不再写死本机路径（与 main.py 同款自适应写法），
# 个人文案（昵称 / QQ 号示例）已换成中性示例，_backup/ 与测试产物已清理。
def _metadata_version(fallback: str = "0.21.40") -> str:
    """v0.21.14：version 直接从 metadata.yaml 读，不再手抄。

    以前这里硬编码 "v0.21.11"，metadata 都升到 0.21.13 了它还是旧的 ——
    顶栏与「插件信息」卡片就会显示过期版本号。页面自己会补 "v" 前缀，故此处不带前缀。
    """
    try:
        path = Path(__file__).resolve().parents[1] / "metadata.yaml"
        m = re.search(r"^version:\s*(\S+)", path.read_text(encoding="utf-8"), re.M)
        if m:
            return m.group(1).strip().lstrip("v")
    except Exception:
        pass
    return fallback


PLUGIN_INFO = {
    "name": "astrbot_plugin_Scintilla_MC_Server_Control",
    "display_name": "Minecraft Server 智控台",
    "version": _metadata_version(),
    "author": "HuangGuaKnn",
    "desc": "通过 RCON 用自然语言操控 Minecraft 服务器并具备高度完备的 WebUI：内置多 Agent 工作流处理整合包复杂任务；转发玩家进出/聊天/死亡/成就/对话/指令；平台指令组 mcs 支持绑定、喊话、状态查询与踢人封禁（管理员）。",
}


class McControlWebApi:
    """WebUI 后端：状态开关、知识库管理、基础设置。"""

    def __init__(self, plugin) -> None:
        self.plugin = plugin

    # ================= 注册 =================

    def register(self) -> None:
        reg = self.plugin.context.register_web_api
        reg(f"{PAGE_PREFIX}/state", self.get_state, ["GET"], "获取知识库状态")
        reg(f"{PAGE_PREFIX}/state/set", self.set_state, ["POST"], "修改知识库开关")
        reg(f"{PAGE_PREFIX}/settings", self.get_settings, ["GET"], "读取基础设置")
        reg(f"{PAGE_PREFIX}/settings/save", self.save_settings, ["POST"], "保存基础设置")
        reg(f"{PAGE_PREFIX}/kb/entry", self.save_entry, ["POST"], "新增/编辑知识条目")
        reg(f"{PAGE_PREFIX}/kb/toggle", self.toggle_entry, ["POST"], "启用/禁用条目")
        reg(f"{PAGE_PREFIX}/kb/delete", self.delete_entry, ["POST"], "删除条目")
        reg(f"{PAGE_PREFIX}/kb/approve", self.approve_entry, ["POST"], "审批条目")
        reg(f"{PAGE_PREFIX}/kb/clear", self.clear_kb, ["POST"], "清空知识库")
        reg(f"{PAGE_PREFIX}/kb/presets", self.get_presets, ["GET"], "知识库预设列表")
        reg(f"{PAGE_PREFIX}/kb/presets/switch", self.switch_preset, ["POST"], "切换激活预设")
        reg(f"{PAGE_PREFIX}/kb/presets/create", self.create_preset, ["POST"], "新建预设")
        reg(f"{PAGE_PREFIX}/kb/presets/rename", self.rename_preset, ["POST"], "重命名预设")
        reg(f"{PAGE_PREFIX}/kb/presets/delete", self.delete_preset, ["POST"], "删除预设")
        reg(f"{PAGE_PREFIX}/kb/presets/bind", self.bind_preset, ["POST"], "绑定/解绑预设指纹")
        reg(f"{PAGE_PREFIX}/kb/presets/transfer", self.transfer_preset, ["POST"], "复制/移动预设知识")
        reg(f"{PAGE_PREFIX}/kb/presets/notice", self.preset_notice, ["POST"], "指纹变化提醒操作")
        reg(f"{PAGE_PREFIX}/kb/models", self.get_kb_models, ["GET"], "知识库可选的嵌入/重排序模型")
        reg(f"{PAGE_PREFIX}/rescan", self.rescan, ["POST"], "重建词典与知识库")
        reg(f"{PAGE_PREFIX}/workflow/status", self.get_workflow_status, ["GET"], "多Agent工作流状态")
        reg(f"{PAGE_PREFIX}/prompts", self.get_prompts, ["GET"], "读取多Agent提示词")
        reg(f"{PAGE_PREFIX}/prompts/save", self.save_prompts, ["POST"], "保存多Agent提示词")
        reg(f"{PAGE_PREFIX}/prompts/reset", self.reset_prompt, ["POST"], "恢复内置默认提示词")
        reg(f"{PAGE_PREFIX}/overview", self.get_overview, ["GET"], "概览信息")
        reg(f"{PAGE_PREFIX}/rcon/test", self.test_rcon, ["POST"], "测试RCON连接")
        reg(f"{PAGE_PREFIX}/rcon/reset", self.reset_rcon, ["POST"], "重建RCON连接（清除自动降级）")
        reg(f"{PAGE_PREFIX}/events", self.get_events, ["GET"], "最近服务器事件")
        reg(f"{PAGE_PREFIX}/server/status", self.get_server_status, ["GET"], "服务器实时状态")
        reg(f"{PAGE_PREFIX}/server/broadcast", self.broadcast, ["POST"], "服务器广播")
        reg(f"{PAGE_PREFIX}/colors", self.get_colors, ["GET"], "读取文本颜色")
        reg(f"{PAGE_PREFIX}/colors/save", self.save_colors, ["POST"], "保存文本颜色")

    # ================= 工具 =================

    def _kb(self):
        kb = getattr(self.plugin, "_knowledge", None)
        if kb is None:
            return None
        return kb

    def _cfg_path(self) -> Path:
        """插件配置文件路径（随 AstrBot 数据目录走，不写死本机路径）。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            return Path(get_astrbot_data_path()) / "config" / "astrbot_plugin_Scintilla_MC_Server_Control_config.json"
        except Exception:
            data_dir = Path(StarTools.get_data_dir("astrbot_plugin_Scintilla_MC_Server_Control"))
            return data_dir.parent.parent / "config" / "astrbot_plugin_Scintilla_MC_Server_Control_config.json"

    def _cfg(self, key: str, default=None):
        return self.plugin._cfg(key, default)

    @staticmethod
    def _parse_list_output(out: str) -> dict:
        """解析 RCON list 输出 → {online, max, players:[...]}。"""
        players: list[str] = []
        online = maxp = 0
        m = re.search(r"(\d+)\s+of\s+a\s+max(?:imum)?\s+of\s+(\d+)", out or "")
        if m:
            online, maxp = int(m.group(1)), int(m.group(2))
        colon = (out or "").rfind(":")
        if colon != -1:
            tail = out[colon + 1:].strip()
            players = [p.strip() for p in tail.split(",") if p.strip()]
        return {"online": online, "max": maxp, "players": players}

    # ================= 状态 =================

    async def get_kb_models(self):
        """设置页用：列出可指定的嵌入 / 重排序模型 + 当前实际生效的那个。

        v0.21.43 新增。choices 直接来自 AstrBot **已加载**的 Provider 实例（不读配置文件），
        所以「配置里有、但没启用 / 加载失败」的模型不会出现在下拉里 —— 下拉能选的一定可用。
        semantic_stats / rerank_stats 是两条通道的运行态（向量是否就绪、待补算多少条）。
        """
        try:
            status = self.plugin.kb_model_status()
        except Exception as e:                            # noqa: BLE001
            return json_response({"ok": False, "error": f"读取模型列表失败：{e}"})
        kb = self._kb()
        return json_response({
            "ok": True,
            **status,
            "semantic_stats": kb.semantic_stats() if kb is not None else {},
            "rerank_stats": kb.rerank_stats() if kb is not None else {},
        })

    async def get_state(self):
        kb = self._kb()
        if kb is None:
            return json_response(self._kb_uninit())
        man = self._kbman()
        payload = {
            "ok": True,
            "state": kb.state.as_dict(),
            "stats": kb.stats(),
            "entries": kb.list_entries(),
            "pending": kb.pending_entries(),
            "server_id": kb.server_id,
        }
        if man is not None:
            payload["presets"] = man.list_presets()
            payload["active_preset"] = man.reg.get("active")
            payload["notice"] = man.notice()
        return json_response(payload)

    async def set_state(self):
        kb = self._kb()
        if kb is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        st = kb.state.set(
            enabled=data.get("enabled"),
            learning=data.get("learning"),
            auto_apply=data.get("auto_apply"),
        )
        return json_response({"ok": True, "state": st})

    # ================= 基础设置 =================

    async def get_settings(self):
        """返回全部设置项（v0.14.0：配置按分组存储，此处统一摊平给前端）。"""
        try:
            flat = self.plugin._cfg_flat()
        except Exception:
            flat = {}
        if not flat:
            cfg = self._cfg_path()
            if not cfg.exists():
                return json_response({"ok": False, "error": "配置文件不存在"})
            try:
                flat = self._flatten(json.loads(cfg.read_text(encoding="utf-8-sig")))
            except Exception as e:
                return json_response({"ok": False, "error": f"配置读取失败: {e}"})
        # 多 Agent 提示词正文由「提示词」页单独读取（v0.15.0），
        # 这里剔除，避免设置页一次性搬运近 2 万字提示词。
        flat = {k: v for k, v in flat.items() if not str(k).startswith("agent_prompt_")}
        # mcs 指令组的前缀跟随 AstrBot 唤醒词设置，一并给前端用于文案展示
        # （纯反斜杠唤醒词会被 _wake_prefix() 归一成空串：展示成 \mcs 只会误导人）
        try:
            wake_prefix = self.plugin._wake_prefix()
        except Exception:
            wake_prefix = ""
        # 会话目标选择器需要平台列表（v0.16.1）：前端据此生成「平台ID:消息类型:会话ID」
        try:
            platforms = self.plugin._platforms()
        except Exception:
            platforms = []
        return json_response({
            "ok": True,
            "settings": flat,
            "platforms": platforms,
            "wake_prefix": wake_prefix,
        })

    @staticmethod
    def _flatten(raw: dict) -> dict:
        """把分组配置摊平成扁平字典（仅展开一层 object 分组）。

        含嵌套结构的键（如 notify_target_events 的 会话→列表 映射）作为
        独立键整体保留，不会被误当作分组展开。
        """
        out: dict = {}
        if not isinstance(raw, dict):
            return out
        for k, v in raw.items():
            if isinstance(v, dict) and v and not any(
                isinstance(x, dict) for x in v.values()
            ):
                out.update(v)
            elif isinstance(v, dict):
                for kk, vv in v.items():
                    out[kk] = vv
            else:
                out[k] = v
        return out

    # ---- 设置项分类型白名单（与 _conf_schema.json 全量对齐） ----
    BOOL_SETTING_KEYS = (
        "enable_event_listener", "notify_join_leave", "notify_chat",
        "notify_death", "notify_advancement", "notify_command",
        "chat_bridge_enabled", "chat_bridge_receipt",
        "chat_bridge_case_sensitive",
        "enable_mc_execute_command", "enable_mc_list_players",
        "enable_mc_server_status", "enable_mc_broadcast",
        "enable_mc_give_item", "enable_mc_kick", "enable_mc_ban",
        "enable_mc_connection_status", "enable_mc_search_item",
        "enable_mc_get_recipes", "enable_mc_list_mods",
        "enable_bind_command", "enable_say_command", "say_command_public",
        "enable_status_command", "enable_kick_command", "enable_ban_command",
        "enable_unban_command", "enable_banlist_command",
        "enable_help_command", "enable_title_command",
        "feedback_tellraw", "dictionary_enabled", "knowledge_enabled",
        "knowledge_semantic_search",
        "knowledge_rerank",
        "enable_mc_search_knowledge", "enable_mc_save_knowledge",
        "enable_mc_correct_knowledge", "agent_workflow_enabled",
        "enable_mc_workflow", "gradient_enabled",
        "permission_hint_injection", "permission_latch",
        # v0.21.15：异地 RCON 模式（开关本身也是设置项，务必进白名单）
        "remote_rcon_mode",
    )
    INT_RANGES = {
        "rcon_port": (1, 65535),
        "agent_max_implement_rounds": (1, 10),
        "agent_max_correct_rounds": (0, 10),
        "permission_latch_ttl": (10, 3600),
    }
    FLOAT_RANGES = {"rcon_timeout": (0.1, 300.0), "rcon_idle_probe": (0.05, 5.0)}
    LIST_SETTING_KEYS = ("admin_ids", "notify_targets", "chat_bridge_targets")
    # 会话 → 事件组列表（v0.17.0：每个会话单独配置播报内容）
    DICT_SETTING_KEYS = ("notify_target_events",)
    # 会话目标类列表：允许「私聊:123456789 / 群聊:987654321」简写，落盘前统一规范化
    SESSION_TARGET_KEYS = ("notify_targets", "chat_bridge_targets")
    STR_SETTING_KEYS = (
        "rcon_host", "rcon_end_mode", "rcon_probe_command", "rcon_password", "server_dir", "chat_bridge_prefix",
        "chat_bridge_format", "ban_default_reason", "feedback_name",
        "llm_provider_id", "agent_classifier_provider_id",
        "agent_judge_provider_id", "agent_engineer_provider_id",
        "agent_implementer_provider_id", "agent_corrector_provider_id",
        # v0.21.43：知识库两条可选通道各自指定用哪个 Provider（留空 = 自动取第一个）
        "knowledge_embed_provider_id", "knowledge_rerank_provider_id",
    )
    ENUM_SETTING_KEYS = {
        # 与 _conf_schema.json / main.py 的闸门实现保持一致
        "danger_command_policy": ("whitelist", "blacklist"),
        # v0.21.40：知识库检索引擎（bm25=默认 / legacy=旧版兼容）
        "knowledge_search_engine": ("bm25", "legacy"),
    }

    def _validate_settings(self, settings: dict):
        """校验 + 规范化全部设置项。返回 (parsed, error)。"""
        parsed: dict = {}
        for k in self.BOOL_SETTING_KEYS:
            if k in settings:
                parsed[k] = bool(settings[k])
        for k, (lo, hi) in self.INT_RANGES.items():
            if k in settings:
                try:
                    v = int(settings[k])
                except (TypeError, ValueError):
                    return None, f"{k} 需要整数（收到 {settings[k]!r}）"
                if not (lo <= v <= hi):
                    return None, f"{k} 超出范围（{lo} ~ {hi}）"
                parsed[k] = v
        for k, (lo, hi) in self.FLOAT_RANGES.items():
            if k in settings:
                try:
                    v = float(settings[k])
                except (TypeError, ValueError):
                    return None, f"{k} 需要数字（收到 {settings[k]!r}）"
                if not (lo <= v <= hi):
                    return None, f"{k} 超出范围（{lo} ~ {hi}）"
                parsed[k] = v
        for k in self.LIST_SETTING_KEYS:
            if k in settings:
                raw = settings[k]
                if isinstance(raw, str):
                    raw = re.split(r"[,，]", raw)
                if not isinstance(raw, (list, tuple)):
                    return None, f"{k} 需要列表"
                items = [str(x).strip() for x in raw if str(x).strip()]
                if k in self.SESSION_TARGET_KEYS:
                    # 简写 → 标准标识（群聊:123 → GroupMessage:123），状态与格式统一
                    items = [self.plugin._normalize_umo(x) for x in items]
                    items = [x for x in items if x]
                parsed[k] = items
        for k in self.DICT_SETTING_KEYS:
            if k in settings:
                raw = settings[k]
                if isinstance(raw, str):
                    try:
                        raw = json.loads(raw or "{}")
                    except Exception:
                        return None, f"{k} 需要 JSON 对象"
                if not isinstance(raw, dict):
                    return None, f"{k} 需要对象（会话 → 列表）"
                allowed_groups = {g for g, _ in self.plugin.NOTIFY_EVENT_GROUPS}
                clean: dict = {}
                for umo_key, groups in raw.items():
                    if isinstance(groups, str):
                        groups = [x.strip() for x in groups.replace("，", ",").split(",")]
                    if not isinstance(groups, (list, tuple, set)):
                        return None, f"{k} 的 {umo_key!r} 需要列表"
                    umo = self.plugin._normalize_umo(str(umo_key)) or str(umo_key)
                    picked = [str(x).strip() for x in groups]
                    picked = [x for x in picked if x in allowed_groups]
                    clean[umo] = picked
                parsed[k] = clean
        for k in self.STR_SETTING_KEYS:
            if k in settings:
                parsed[k] = str(settings[k] or "").strip()
        for k, allowed in self.ENUM_SETTING_KEYS.items():
            if k in settings:
                v = str(settings[k] or "").strip().lower()
                if v not in allowed:
                    return None, f"{k} 取值无效（可选：{' / '.join(allowed)}）"
                parsed[k] = v
        if "gradient_format" in settings:
            fmt = str(settings["gradient_format"] or "").strip().lower()
            if fmt not in self.plugin.GRADIENT_FORMATS:
                return None, (
                    f"未知的渐变输出格式：{fmt!r}，可选："
                    f"{', '.join(self.plugin.GRADIENT_FORMATS)}"
                )
            parsed["gradient_format"] = fmt
        for k in self.COLOR_KEYS:
            if k in settings:
                pv = self.plugin._parse_color(settings.get(k), "")
                if not pv:
                    return None, (
                        f"{k} 颜色无法识别：{settings.get(k)!r}，"
                        "支持 #RRGGBB / RGB(255,0,0) / 十进制 / 色名"
                    )
                parsed[k] = pv
        for k in self.GRADIENT_COLOR_KEYS:
            if k in settings:
                clean = []
                for it in self._split_stops(settings.get(k)):
                    c = self.plugin._parse_color(it, "")
                    if not c:
                        return None, f"{k} 中存在无法识别的颜色：{it!r}"
                    clean.append(c)
                if len(clean) < 2:
                    return None, f"{k} 至少需要 2 个渐变颜色（当前 {len(clean)} 个）"
                parsed[k] = ",".join(clean)
        return parsed, None

    async def save_settings(self):
        """保存全部设置：校验 → 写盘 → 热应用到运行中的插件。"""
        data = await request.json() or {}
        settings = data.get("settings")
        if not isinstance(settings, dict):
            return json_response({"ok": False, "error": "参数错误"})
        parsed, err = self._validate_settings(settings)
        if err:
            return json_response({"ok": False, "error": err})
        if not parsed:
            return json_response({"ok": False, "error": "没有可保存的设置项"})

        # ---------- v0.21.15 硬校验：server_dir 必须真的像「服务端根目录」 ----------
        # 误配（空壳目录 / 父级目录 / 客户端 .minecraft）会让指纹退化成空集合、
        # 词典 0 mod、日志永远读不到 —— 界面看着配好了，实际全是空数据。
        # 于是在保存这一步直接拦住：宁可报错，也不写盘、不给「看起来成功」的假象。
        remote_mode = (
            bool(parsed["remote_rcon_mode"]) if "remote_rcon_mode" in parsed
            else bool(self.plugin._cfg("remote_rcon_mode", False))
        )
        eff_dir = (
            str(parsed["server_dir"] or "").strip() if "server_dir" in parsed
            else str(self.plugin._cfg("server_dir", "") or "").strip()
        )
        if not remote_mode and eff_dir:
            try:
                chk = inspect_server_dir(eff_dir)
            except Exception as e:                       # noqa: BLE001
                chk = {"ok": False, "errors": [f"目录校验异常：{e}"],
                       "warnings": [], "suggest_path": "", "summary": ""}
            if not chk.get("ok"):
                lines = [format_check(chk)]
                if chk.get("suggest_path"):
                    lines.append(f"→ 建议「服务器目录」改填：{chk['suggest_path']}")
                lines.append(
                    "已拒绝保存（配置保持原样）。若服务端确实在另一台机器上、本机只映射了 "
                    "RCON 端口，请打开「异地 RCON 模式」开关：该模式下可以不填目录，"
                    "但物品词典 / 知识库 / 服务器事件播报 / 版本与进程指标会一并禁用。"
                )
                return json_response({
                    "ok": False, "error": chr(10).join(lines),
                    "server_dir_check": chk,
                })
        # v0.17.1：前端已更新、插件却没热重载时，新版键会被旧代码静默忽略
        # （保存看着成功、实际不落盘），这里把「后端不认识的键」回传给前端提示。
        ignored = sorted(k for k in settings.keys() if k not in parsed)
        # 仅对「真正发生变化的键」触发副作用，避免无变化的保存白重启监听器
        try:
            current = self.plugin._cfg_flat()
        except Exception:
            current = {}
        changed = {k for k, v in parsed.items() if current.get(k) != v}
        # v0.14.0：按分组写回内存并落盘（AstrBotConfig.save_config）
        try:
            self.plugin._set_cfg_batch(parsed)
        except Exception as e:
            return json_response({"ok": False, "error": f"配置保存失败: {e}"})

        # ---------- 热应用（无需重启） ----------
        effects: list[str] = []
        if "admin_ids" in changed:
            try:
                self.plugin._admins = set(str(x) for x in parsed["admin_ids"])
                effects.append("管理员列表已刷新")
            except Exception:
                pass
        if {"rcon_host", "rcon_port", "rcon_password", "rcon_timeout",
                "rcon_end_mode", "rcon_probe_command", "rcon_idle_probe"} & changed:
            try:
                # v0.22.5：走带锁的 reset_rcon（旧实例显式退役并关闭，避免 socket 泄漏）
                await self.plugin.reset_rcon()
                effects.append("RCON 连接已重置（下次调用自动重建）")
            except Exception as e:
                effects.append(f"RCON 连接重置失败：{e}")
        if {"enable_event_listener", "server_dir"} & changed:
            try:
                msg = await self.plugin._restart_event_listener()
                effects.append(msg)
            except Exception as e:
                effects.append(f"服务器事件转发重启失败：{e}")

        # v0.21.7：换服务端 / 词典·知识库启停 → 就地重算指纹、重建词典、刷新知识库基准
        # （此前只在插件初始化时算一次，改 server_dir 后必须重载插件才更新）
        if {"server_dir", "dictionary_enabled", "knowledge_enabled"} & changed:
            try:
                msg = await self.plugin._sync_server_context(rebuild_dictionary=True)
                effects.append(msg)
            except Exception as e:
                effects.append(f"服务端识别刷新失败：{e}")

        if remote_mode and ({"remote_rcon_mode", "server_dir"} & changed):
            effects.append(
                "异地 RCON 模式已开启：只走 RCON —— 物品词典 / 知识库 / 服务器事件播报 / "
                "版本探测 / 进程指标已按设计禁用"
            )

        # v0.21.40：切换知识库检索引擎 → 立即换引擎（不必重建库实例、不必重载插件）
        if "knowledge_search_engine" in changed:
            try:
                eng = self.plugin._kb_engine()
                if self.plugin._kbman is not None:
                    self.plugin._kbman.set_search_engine(eng)
                effects.append(
                    f"知识库检索引擎已切换为「{'BM25（推荐）' if eng == 'bm25' else '旧版（兼容）'}」"
                )
            except Exception as e:
                effects.append(f"检索引擎切换失败：{e}")

        # v0.21.41：开关语义增强检索 → 立即切换通道，并把缺向量后台补齐
        if "knowledge_semantic_search" in changed:
            try:
                on = self.plugin._kb_semantic()
                if self.plugin._kbman is not None:
                    self.plugin._kbman.set_semantic_enabled(on)
                if on:
                    ok = self.plugin._inject_embed_fn()
                    if not ok:
                        effects.append(
                            "⚠ 语义增强检索已开启，但没有找到可用的嵌入模型 —— "
                            "请先在 AstrBot「服务提供商」里添加一个 Embedding 模型，"
                            "否则本通道会安静退化为纯 BM25"
                        )
                    else:
                        asyncio.create_task(self.plugin._kb_build_semantic())
                        effects.append("语义增强检索已开启：向量索引正在后台补齐（新增条目才会重算）")
                else:
                    effects.append("语义增强检索已关闭：检索回到纯 BM25")
            except Exception as e:
                effects.append(f"语义增强检索切换失败：{e}")

        # v0.21.42：开关重排序精排 → 立即生效（注入/撤销 rerank 调用）
        if "knowledge_rerank" in changed:
            try:
                on = self.plugin._kb_rerank()
                if self.plugin._kbman is not None:
                    self.plugin._kbman.set_rerank_enabled(on)
                if on:
                    ok = self.plugin._inject_rerank_fn()
                    if not ok:
                        effects.append(
                            "⚠ 重排序精排已开启，但没有找到可用的 Rerank 模型 —— "
                            "请先在 AstrBot「服务提供商」里添加一个 Rerank 模型，"
                            "否则检索会安静退化为召回顺序"
                        )
                    else:
                        effects.append("重排序精排已开启：检索升级为「召回 → 精排」两段式")
                else:
                    effects.append("重排序精排已关闭：检索回到召回顺序")
            except Exception as e:
                effects.append(f"重排序精排切换失败：{e}")

        # v0.21.43：改「指定嵌入 / 重排序模型」→ 立即重新注入调用
        # 注意：换了嵌入模型，旧向量是**别的模型**算出来的（维度与语义空间都不通用），
        # 必须整库重算，不能留着串味 —— 所以这里用 force 重建而不是增量补齐。
        if {"knowledge_embed_provider_id", "knowledge_rerank_provider_id"} & changed:
            try:
                if "knowledge_rerank_provider_id" in changed:
                    if self.plugin._inject_rerank_fn():
                        label = self.plugin.kb_effective_label("rerank")
                        effects.append(
                            f"重排序模型已切换为「{label}」" if label
                            else "重排序模型已切换"
                        )
                    elif self.plugin._kb_rerank():
                        effects.append(
                            "⚠ 重排序精排开着，但没有可用的 Rerank 模型 —— "
                            "检索会安静退化为召回顺序"
                        )
                if "knowledge_embed_provider_id" in changed:
                    if self.plugin._inject_embed_fn():
                        label = self.plugin.kb_effective_label("embedding")
                        if self.plugin._kb_semantic():
                            asyncio.create_task(
                                self.plugin._kb_build_semantic(force=True)
                            )
                            effects.append(
                                f"嵌入模型已切换为「{label or '（未指明）'}」："
                                "向量索引正在后台整库重算（旧模型算的向量不通用）"
                            )
                        else:
                            effects.append(
                                f"嵌入模型已切换为「{label or '（未指明）'}」"
                                "（语义增强检索未开启，暂时用不上）"
                            )
                    elif self.plugin._kb_semantic():
                        effects.append(
                            "⚠ 语义增强检索开着，但没有可用的嵌入模型 —— "
                            "本通道会安静退化为纯 BM25"
                        )
            except Exception as e:                        # noqa: BLE001
                effects.append(f"模型切换失败：{e}")

        notice = f"已保存 {len(parsed)} 项并即时生效"
        if effects:
            notice += "（" + "；".join(effects) + "）"
        if ignored:
            notice = (
                f"⚠ 有 {len(ignored)} 项后端未识别：{'、'.join(ignored[:4])}"
                "（多为插件未重载所致，请到 AstrBot「插件管理」重载本插件后重新保存）；" + notice
            )
        return json_response({
            "ok": True, "notice": notice, "applied": parsed, "ignored": ignored,
        })

    # ================= 多Agent工作流 =================

    async def get_workflow_status(self):
        wf = getattr(self.plugin, "_workflow", None)
        if wf is None:
            return json_response({"ok": False, "error": "工作流未初始化"})
        return json_response({
            "ok": True,
            "enabled": bool(self._cfg("agent_workflow_enabled", True)),
            "tool_enabled": bool(self._cfg("enable_mc_workflow", True)),
            "main_provider": self._cfg("llm_provider_id", ""),
            "agents": {
                "classifier": self._cfg("agent_classifier_provider_id", ""),
                "judge": self._cfg("agent_judge_provider_id", ""),
                "engineer": self._cfg("agent_engineer_provider_id", ""),
                "implementer": self._cfg("agent_implementer_provider_id", ""),
                "corrector": self._cfg("agent_corrector_provider_id", ""),
            },
            "rounds": {
                "implement": self._cfg("agent_max_implement_rounds", 3),
                "correct": self._cfg("agent_max_correct_rounds", 2),
            },
            "logs": wf.recent_logs(15),
        })

    # ================= 多Agent提示词（v0.15.0） =================
    #
    # 与 _conf_schema.json 的「⑩ 多 Agent 提示词」分组共用同一份配置：
    # 原生插件配置页与本 WebUI 任改其一，另一处刷新后即可看到。

    MAX_PROMPT_LEN = 40000

    async def get_prompts(self):
        """返回五个 Agent 的当前生效提示词 + 内置默认（编辑器数据源）。"""
        try:
            agents = describe_agents(self.plugin._cfg)
        except Exception as e:
            return json_response({"ok": False, "error": f"提示词读取失败: {e}"})
        return json_response({
            "ok": True,
            "agents": agents,
            "custom_count": sum(1 for a in agents if a.get("custom")),
            "total": len(agents),
        })

    async def save_prompts(self):
        """保存提示词。支持单条 {role, content} 或批量 {values: {role: content}}。"""
        data = await request.json() or {}
        payload: dict = {}
        if isinstance(data.get("values"), dict):
            payload = {str(k): v for k, v in data["values"].items()}
        elif data.get("role"):
            payload = {str(data["role"]): data.get("content", "")}
        else:
            return json_response({"ok": False, "error": "参数错误：缺少 role / content"})

        updates: dict = {}
        warn: list[str] = []
        for role, raw in payload.items():
            spec = AGENT_SPECS.get(role)
            if not spec:
                return json_response({"ok": False, "error": f"未知的 Agent 角色：{role}"})
            text = str(raw if raw is not None else "")
            if not text.strip():
                # 留空 = 回退内置默认（写回内置正文，保证原生配置页也能看到内容）
                text = spec["default"]
            if len(text) > self.MAX_PROMPT_LEN:
                return json_response({
                    "ok": False,
                    "error": f"{spec['name']} 提示词过长（{len(text)} 字符，上限 {self.MAX_PROMPT_LEN}）",
                })
            if "json" not in text.lower():
                warn.append(spec["short"])
            updates[spec["cfg_key"]] = text

        try:
            self.plugin._set_cfg_batch(updates)
        except Exception as e:
            return json_response({"ok": False, "error": f"配置写入失败: {e}"})

        notice = f"已保存 {len(updates)} 个 Agent 的提示词，下一次任务即刻生效"
        if warn:
            notice += f"（注意：{'、'.join(warn)} 的提示词里没有出现 JSON 输出约定，可能解析失败）"
        return json_response({"ok": True, "notice": notice})

    async def reset_prompt(self):
        """把某个 Agent（或全部）的提示词恢复为内置默认。"""
        data = await request.json() or {}
        role = str(data.get("role") or "").strip()
        if data.get("all"):
            roles = list(AGENT_ORDER)
        elif role:
            if role not in AGENT_SPECS:
                return json_response({"ok": False, "error": f"未知的 Agent 角色：{role}"})
            roles = [role]
        else:
            return json_response({"ok": False, "error": "参数错误：需要 role 或 all=true"})

        updates = {AGENT_SPECS[r]["cfg_key"]: AGENT_SPECS[r]["default"] for r in roles}
        try:
            self.plugin._set_cfg_batch(updates)
        except Exception as e:
            return json_response({"ok": False, "error": f"配置写入失败: {e}"})
        names = "、".join(AGENT_SPECS[r]["short"] for r in roles)
        return json_response({
            "ok": True,
            "notice": f"已把「{names}」恢复为内置默认提示词（下次任务生效）",
        })

    # ================= 概览 / 服务器 =================

    async def get_overview(self):
        """概览页：插件信息 + 关键模块状态 + 最近事件。"""
        plugin = self.plugin

        # 词典
        dict_info = None
        d = getattr(plugin, "_dictionary", None)
        if d is not None:
            try:
                dict_info = {
                    "enabled": bool(self._cfg("dictionary_enabled", True)),
                    "mods": len(d.mods),
                    "items": len(d.items),
                    "recipes": len(d.recipes),
                }
            except Exception:
                dict_info = {"enabled": True, "error": "词典数据读取失败"}

        # 知识库
        kb = self._kb()
        kb_info = None
        if kb is not None:
            try:
                st = kb.stats()
                kb_info = {
                    "enabled": bool(self._cfg("knowledge_enabled", True)),
                    "server_id": kb.server_id,
                    "state": kb.state.as_dict(),
                    "stats": st,
                    "preset_name": st.get("preset_name"),
                    "fingerprint": st.get("fingerprint"),
                    "match": st.get("match"),
                    "server_content": self.plugin.server_content_text(),
                }
                man = self._kbman()
                if man is not None:
                    n = man.notice()
                    kb_info["notice_show"] = n["show"]              # 本轮「还没弹过」的全屏提示
                    kb_info["fingerprint_mismatch"] = n["changed"]   # 只要不匹配就一直为 True（概览常驻标记）
            except Exception as e:
                kb_info = {"error": str(e)}

        # 工作流
        wf = getattr(plugin, "_workflow", None)
        wf_info = None
        if wf is not None:
            logs = wf.recent_logs(20)
            done = [l for l in logs if l.get("status") == "done"]
            failed = [l for l in logs if l.get("status") in ("failed", "error")]
            running = [l for l in logs if l.get("status") == "running"]
            wf_info = {
                "enabled": bool(self._cfg("agent_workflow_enabled", True)),
                "tool_enabled": bool(self._cfg("enable_mc_workflow", True)),
                "main_provider": self._cfg("llm_provider_id", ""),
                "agents": {
                    "classifier": self._cfg("agent_classifier_provider_id", ""),
                    "judge": self._cfg("agent_judge_provider_id", ""),
                    "engineer": self._cfg("agent_engineer_provider_id", ""),
                    "implementer": self._cfg("agent_implementer_provider_id", ""),
                    "corrector": self._cfg("agent_corrector_provider_id", ""),
                },
                "rounds": {
                    "implement": self._cfg("agent_max_implement_rounds", 3),
                    "correct": self._cfg("agent_max_correct_rounds", 2),
                },
                "recent_total": len(logs),
                "recent_done": len(done),
                "recent_failed": len(failed),
                "recent_running": len(running),
            }

        # 服务器事件转发
        listener = {
            "enabled": bool(self._cfg("enable_event_listener", False)),
            "running": getattr(plugin, "_watcher", None) is not None,
            "server_dir": str(self._cfg("server_dir", "") or ""),
            "targets": self._cfg("notify_targets", []) or [],
            # v0.17.0：会话级播报内容 + 全局类型闸门状态（供 WebUI 渲染子菜单）
            "target_events": self._cfg("notify_target_events", {}) or {},
            "type_switches": {
                "join_leave": bool(self._cfg("notify_join_leave", True)),
                "chat": bool(self._cfg("notify_chat", False)),
                "death": bool(self._cfg("notify_death", True)),
                "advancement": bool(self._cfg("notify_advancement", True)),
                "command": bool(self._cfg("notify_command", True)),
            },
            "event_groups": [
                {"key": k, "label": lbl} for k, lbl in plugin.NOTIFY_EVENT_GROUPS
            ],
        }

        # 最近事件
        events = []
        try:
            for e in list(getattr(plugin, "_recent_events", []))[:15]:
                events.append(dict(e))
        except Exception:
            events = []

        return json_response({
            "ok": True,
            "plugin": PLUGIN_INFO,
            "config": {
                "rcon_host": self._cfg("rcon_host", "127.0.0.1"),
                "rcon_port": self._cfg("rcon_port", 25575),
                "rcon_configured": bool(self._cfg("rcon_password", "")),
                "rcon_timeout": self._cfg("rcon_timeout", 5.0),
                "rcon_end_mode": self._cfg("rcon_end_mode", "sentinel"),
                "rcon_runtime": self._rcon_runtime(),
                "server_dir": str(self._cfg("server_dir", "") or ""),
                # v0.21.15：概览页据此把本地文件类模块标成「异地模式禁用」
                "remote_rcon_mode": bool(self._cfg("remote_rcon_mode", False)),
                "admin_ids": self._cfg("admin_ids", []) or [],
                "feedback_name": self._cfg("feedback_name", "RCON"),
                "danger_command_policy": self._cfg("danger_command_policy", "whitelist"),
                "feedback_tellraw": bool(self._cfg("feedback_tellraw", True)),
            },
            "dictionary": dict_info,
            "knowledge": kb_info,
            "workflow": wf_info,
            "listener": listener,
            "events": events,
        })

    def _rcon_runtime(self) -> dict:
        """RCON 运行态（v0.22.4）：当前**实际生效**的结束边界方式可能已被自动降级。

        · end_mode  ：实例当下用的方式（sentinel / idle）
        · configured：配置里写的方式（用户意图）
        · degraded  ：两者不一致 = 已自动降级（长响应完整性保证变弱，需人工关注）
        """
        configured = str(self._cfg("rcon_end_mode", "sentinel") or "sentinel").strip().lower()
        configured = "idle" if configured == "idle" else "sentinel"
        rcon = getattr(self.plugin, "_rcon", None)
        if rcon is None:
            return {"end_mode": configured, "configured": configured,
                    "degraded": False, "probe_misses": 0, "connected": False,
                    "boundary_confirmed": None, "idle_unconfirmed": 0,
                    "idle_probe": IDLE_PROBE}
        mode = "idle" if str(getattr(rcon, "end_mode", configured)) == "idle" else "sentinel"
        return {
            "end_mode": mode,
            "configured": configured,
            "degraded": mode != configured,
            "probe_misses": int(getattr(rcon, "_probe_misses", 0) or 0),
            "connected": bool(getattr(rcon, "connected", False)),
            # v0.22.5（复审 P2 整改）：三态透传，**不能**把「暂无结果」默认成 True，
            # 否则页面在一条命令都没跑过时也会显示「边界可靠」。
            "boundary_confirmed": getattr(rcon, "last_boundary_confirmed", None),
            "idle_unconfirmed": int(getattr(rcon, "_idle_unconfirmed_count", 0) or 0),
            # 页面要如实写出「静默窗口多少秒」才谈得上「响应完整性未保证」有多严重
            "idle_probe": float(getattr(rcon, "idle_probe", IDLE_PROBE) or IDLE_PROBE),
        }

    async def reset_rcon(self):
        """重建 RCON 连接：丢掉已自动降级的实例，按配置重新开始（回到 sentinel）。"""
        try:
            # v0.22.5（核验 P2）：交给插件的带锁重建 —— 旧实例显式退役并关闭，
            # 不会在有在途命令 / 连点按钮 / 自动降级叠加时泄漏旧连接。
            await self.plugin.reset_rcon()
            runtime = self._rcon_runtime()
            return json_response({
                "ok": True,
                "message": "RCON 连接已重建（按当前配置：%s）" % runtime["configured"],
                "runtime": runtime,
            })
        except Exception as e:  # noqa: BLE001
            return json_response({"ok": False, "error": f"重建 RCON 连接失败：{e}"})

    async def test_rcon(self):
        """测试 RCON 连接，返回在线玩家信息 + 当前结束边界运行态。"""
        plugin = self.plugin
        try:
            rcon = await plugin._get_rcon()
            out = await rcon.command("list")
            parsed = self._parse_list_output(out)
            return json_response({
                "ok": True,
                "output": out,
                "online": parsed["online"],
                "max": parsed["max"],
                "players": parsed["players"],
                "runtime": self._rcon_runtime(),
            })
        except Exception as e:  # noqa: BLE001
            return json_response({"ok": False, "error": str(e), "runtime": self._rcon_runtime()})

    async def get_events(self):
        """返回最近服务器事件列表。"""
        events = []
        try:
            for e in list(getattr(self.plugin, "_recent_events", []))[:50]:
                events.append(dict(e))
        except Exception:
            events = []
        return json_response({"ok": True, "events": events})

    async def get_server_status(self):
        """服务器实时状态：在线玩家、游戏时间、版本。"""
        plugin = self.plugin
        # v0.22.7：**版本能力与 RCON 无关**（读的是配置 + 服务端本地文件），
        # 所以先算好、连不上服务器也照样返回 —— 否则主人会在 RCON 掉线时
        # 连「Agent 到底按哪个版本构造命令」都看不见。
        info: dict = {}
        try:
            caps = plugin._version_capabilities()
            info["version_caps"] = caps
            if caps.get("source") == "override" and caps.get("raw"):
                # 手动声明优先于文件探测 —— UI 要跟 Agent 看到的一致
                info["version"] = f"{caps['raw']}（手动声明）"
            elif not caps.get("known"):
                info["version_hint"] = (
                    "版本未知：附魔 / NBT / 物品组件类请求会被拒绝。"
                    "异地 RCON 模式下探测必然失败，请在「连接 → 手动声明服务端版本」里填写。"
                )
        except Exception as e:  # noqa: BLE001
            info["version_caps_error"] = str(e)

        try:
            rcon = await plugin._get_rcon()
        except Exception as e:
            return json_response({"ok": False, "error": str(e), **info})

        try:
            try:
                out = await rcon.command("list")
                info["list"] = self._parse_list_output(out)
            except Exception as e:
                info["list_error"] = str(e)
            try:
                info["daytime"] = (await rcon.command("time query daytime")).strip()
            except Exception:
                pass
            # 版本原文：原版/Forge 无 version 命令，优先从服务端文件探测。
            # 已由 version_caps 给出结论时不覆盖（那是 Agent 真正使用的事实）。
            if "version" not in info:
                try:
                    ver = plugin.detect_server_version()
                    if not ver:
                        # 兜底：Paper/Spigot 等才支持 version 命令；过滤掉原版报错文本
                        try:
                            raw = (await rcon.command("version")).strip()
                            if raw and "Unknown" not in raw and "incomplete" not in raw:
                                ver = raw
                        except Exception:
                            pass
                    info["version"] = ver or "未检测到（请检查 server_dir 配置）"
                except Exception as e:
                    info["version"] = f"检测失败：{e}"
            return json_response({"ok": True, **info})
        except Exception as e:
            return json_response({"ok": False, "error": str(e), **info})

    async def broadcast(self):
        """向服务器广播消息（chat/title），或模拟任务输出（task）。"""
        data = await request.json() or {}
        message = str(data.get("message", "")).strip()
        mode = str(data.get("mode", "chat")).strip() or "chat"
        if not message:
            return json_response({"ok": False, "error": "消息不能为空"})
        plugin = self.plugin
        try:
            rcon = await plugin._get_rcon()
            name = str(self._cfg("feedback_name", "RCON") or "RCON")
            if mode == "title":
                payload = plugin._colored_payload(message, "color_title", "gradient_colors_title", "gold")
                await rcon.command(f"title @a title {payload}")
            elif mode == "task":
                # 模拟任务输出：与命令执行反馈同款署名 + 任务输出色
                payload = plugin._colored_payload(f"[{name}] {message}", "color_feedback", "gradient_colors_feedback", "gold")
                await rcon.command(f"tellraw @a {payload}")
            else:
                payload = plugin._colored_payload(f"[{name}] {message}", "color_say", "gradient_colors_say", "white")
                await rcon.command(f"tellraw @a {payload}")
            return json_response({"ok": True, "mode": mode, "message": message})
        except Exception as e:
            return json_response({"ok": False, "error": str(e)})

    # ================= 文本颜色 =================

    COLOR_KEYS = ("color_title", "color_say", "color_feedback")
    GRADIENT_COLOR_KEYS = (
        "gradient_colors_title",
        "gradient_colors_say",
        "gradient_colors_feedback",
    )
    COLOR_DEFAULTS = {"color_title": "gold", "color_say": "white", "color_feedback": "gold"}
    GRADIENT_COLOR_DEFAULTS = {
        "gradient_colors_title": "#FFAA00,#FF00FF",
        "gradient_colors_say": "#FFFFFF,#55FFFF",
        "gradient_colors_feedback": "#FFAA00,#55FF55",
    }
    DEFAULT_FORMAT = "json"

    @staticmethod
    def _split_stops(raw):
        """把逗号/空格/分号分隔的颜色串或列表拆成锚点列表。"""
        if isinstance(raw, (list, tuple)):
            return [str(x) for x in raw if str(x).strip()]
        return [p for p in re.split(r"[,，;；\s]+", str(raw or "")) if p.strip()]

    async def get_colors(self):
        """读取三项文本颜色 + 渐变色设置（多锚点列表 + 输出格式）。"""
        return json_response({
            "ok": True,
            "colors": {
                k: self._cfg(k, self.COLOR_DEFAULTS[k]) for k in self.COLOR_KEYS
            },
            "gradient": {
                "enabled": bool(self._cfg("gradient_enabled", False)),
                "format": self._cfg("gradient_format", self.DEFAULT_FORMAT),
                "stops": {
                    k: self._cfg(k, self.GRADIENT_COLOR_DEFAULTS[k])
                    for k in self.GRADIENT_COLOR_KEYS
                },
                "formats": self.plugin.GRADIENT_FORMATS,
            },
        })

    async def save_colors(self):
        """保存文本颜色 + 渐变色（多锚点列表 / 输出格式）：即时生效无需重启。"""
        data = await request.json() or {}
        parsed = {}
        for k in self.COLOR_KEYS:
            if k in data:
                pv = self.plugin._parse_color(data.get(k), "")
                if not pv:
                    return json_response({
                        "ok": False,
                        "error": f"{k} 颜色无法识别：{data.get(k)!r}，支持 #RRGGBB / RGB(255,0,0) / 十进制 / 色名",
                    })
                parsed[k] = pv
        for k in self.GRADIENT_COLOR_KEYS:
            if k in data:
                clean = []
                for it in self._split_stops(data.get(k)):
                    c = self.plugin._parse_color(it, "")
                    if not c:
                        return json_response({
                            "ok": False,
                            "error": f"{k} 中存在无法识别的颜色：{it!r}",
                        })
                    clean.append(c)
                if len(clean) < 2:
                    return json_response({
                        "ok": False,
                        "error": f"{k} 至少需要 2 个渐变颜色（当前 {len(clean)} 个）",
                    })
                parsed[k] = ",".join(clean)
        if "gradient_format" in data:
            fmt = str(data.get("gradient_format") or "").strip().lower()
            if fmt not in self.plugin.GRADIENT_FORMATS:
                return json_response({
                    "ok": False,
                    "error": f"未知的输出格式：{fmt!r}，可选：{', '.join(self.plugin.GRADIENT_FORMATS)}",
                })
            parsed["gradient_format"] = fmt
        if "gradient_enabled" in data:
            parsed["gradient_enabled"] = bool(data["gradient_enabled"])
        if not parsed:
            return json_response({
                "ok": False,
                "error": "参数错误：需要颜色或渐变设置之一",
            })
        # v0.14.0：按分组写回内存并落盘
        self.plugin._set_cfg_batch(parsed)
        grad = bool(self._cfg("gradient_enabled", False))
        fmt_now = self._cfg("gradient_format", self.DEFAULT_FORMAT)
        return json_response({
            "ok": True,
            "colors": {
                k: self._cfg(k, self.COLOR_DEFAULTS[k])
                for k in self.COLOR_KEYS
            },
            "gradient": {
                "enabled": grad,
                "format": fmt_now,
                "stops": {
                    k: self._cfg(k, self.GRADIENT_COLOR_DEFAULTS[k])
                    for k in self.GRADIENT_COLOR_KEYS
                },
            },
            "notice": (
                f"设置已保存并即时生效，渐变色已{'开启' if grad else '关闭'}"
                f"（格式：{self.plugin.GRADIENT_FORMATS.get(fmt_now, fmt_now)}）"
            ),
        })

    # ================= 知识库管理 =================

    async def save_entry(self):
        kb = self._kb()
        if kb is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        topic = (data.get("topic") or "").strip()
        content = (data.get("content") or "").strip()
        old_topic = (data.get("old_topic") or "").strip() or topic
        status = data.get("status") or "untested"
        if not topic or not content:
            return json_response({"ok": False, "error": "topic 与 content 必填"})
        # v0.21.11：详情弹窗支持「改名 + 改内容」；改名撞到已有主题就拒绝，避免静默覆盖别人的条目
        renaming = old_topic != topic
        if renaming:
            if kb.rename_exists(old_topic, topic):
                return json_response({"ok": False,
                                      "error": f"主题「{topic}」已存在，换个名字或先删除它"})
            if not kb.get_entry(old_topic):
                return json_response({"ok": False, "error": "原条目已不存在（可能刚被改名或删除）"})
        entry = kb.save_entry(topic, content, source="webui", status=status,
                              rename_from=old_topic if renaming else "", manual=True)
        return json_response({"ok": True, "entry": entry, "renamed_from": old_topic if renaming else ""})

    async def toggle_entry(self):
        kb = self._kb()
        if kb is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        topic, enabled = data.get("topic", ""), data.get("enabled")
        if not topic or enabled is None:
            return json_response({"ok": False, "error": "topic 与 enabled 必填"})
        entry = kb.set_enabled(topic, bool(enabled))
        if entry is None:
            return json_response({"ok": False, "error": "条目不存在"})
        return json_response({"ok": True, "entry": entry})

    async def delete_entry(self):
        kb = self._kb()
        if kb is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        topic = data.get("topic", "")
        if not topic:
            return json_response({"ok": False, "error": "topic 必填"})
        ok = kb.delete_entry(topic)
        return json_response({"ok": ok, "deleted": ok})

    async def approve_entry(self):
        kb = self._kb()
        if kb is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        topic = data.get("topic", "")
        approve = bool(data.get("approve", True))
        if not topic:
            return json_response({"ok": False, "error": "topic 必填"})
        entry = kb.approve_entry(topic, approve)
        if entry is None:
            return json_response({"ok": False, "error": "条目不存在"})
        return json_response({"ok": True, "entry": entry})

    async def clear_kb(self):
        kb = self._kb()
        if kb is None:
            return json_response(self._kb_uninit())
        stats = kb.clear()
        return json_response({"ok": True, "stats": stats})

    # ================= 知识库预设（v0.18.0） =================

    def _kbman(self):
        return getattr(self.plugin, "_kbman", None)

    def _kb_uninit(self) -> dict:
        """知识库不可用时的统一错误体（v0.21.15）。

        以前一律答「知识库未初始化」，主人看到只会一脸问号；现在把**真正的原因**
        一并说清：异地 RCON 模式（按设计禁用本地文件类能力）／服务端目录校验未通过。
        """
        reason = ""
        try:
            reason = self.plugin.local_files_degraded_reason() or ""
        except Exception:
            reason = ""
        if reason:
            return {
                "ok": False, "kb_unavailable": True,
                "error": f"知识库不可用 —— {reason}。"
                         "请到设置页修正「服务器目录」，或关闭「异地 RCON 模式」。",
            }
        return {"ok": False, "kb_unavailable": True, "error": "知识库未初始化"}

    def _presets_payload(self, man) -> dict:
        """预设状态 + 服务端内容来源（v0.21.20：说明指纹是按 mods/ 还是 plugins/ 算的）。"""
        payload = dict(man.status())
        try:
            payload["server_content"] = self.plugin.server_content_text()
            payload["server_kind"] = self.plugin.server_identity().get("kind") or ""
        except Exception:
            payload["server_content"] = ""
            payload["server_kind"] = ""
        return payload

    async def get_presets(self):
        man = self._kbman()
        if man is None:
            return json_response(self._kb_uninit())
        return json_response({"ok": True, **self._presets_payload(man)})

    async def switch_preset(self):
        man = self._kbman()
        if man is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        pid = data.get("id", "")
        if not pid:
            return json_response({"ok": False, "error": "id 必填"})
        r = man.switch(pid)
        if not r.get("ok"):
            return json_response(r)
        self.plugin._apply_active_knowledge()
        p = man.active_preset() or {}
        return json_response({
            "ok": True, **self._presets_payload(man),
            "notice_text": f"已切换到预设「{p.get('name')}」（指纹 {p.get('fingerprint') or '未绑定'}）",
        })

    async def create_preset(self):
        man = self._kbman()
        if man is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        preset = man.create(
            name=data.get("name", ""),
            copy_from=(data.get("copy_from") or None),
        )
        return json_response({
            "ok": True, **self._presets_payload(man),
            "created": preset,
            "notice_text": f"已新建预设「{preset['name']}」（未绑定指纹，首次写入知识时自动绑定）",
        })

    async def rename_preset(self):
        man = self._kbman()
        if man is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        p = man.rename(data.get("id", ""), data.get("name", ""))
        if p is None:
            return json_response({"ok": False, "error": "预设不存在或名称为空"})
        return json_response({"ok": True, **self._presets_payload(man), "notice_text": "预设已重命名"})

    async def delete_preset(self):
        man = self._kbman()
        if man is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        was_active = data.get("id") == man.reg.get("active")
        r = man.remove(data.get("id", ""))
        if not r.get("ok"):
            return json_response(r)
        if was_active:
            self.plugin._apply_active_knowledge()
        return json_response({"ok": True, **self._presets_payload(man), "notice_text": "预设已删除"})

    async def bind_preset(self):
        """绑定/解绑预设指纹。target=server 表示绑定到当前服务端指纹。"""
        man = self._kbman()
        if man is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        target = data.get("target")
        if target == "server":
            fp: str | None = man.server_id
        elif target == "none":
            fp = None
        else:
            fp = data.get("fingerprint")
        p = man.bind(data.get("id", ""), fp)
        if p is None:
            return json_response({"ok": False, "error": "预设不存在"})
        return json_response({
            "ok": True, **self._presets_payload(man),
            "notice_text": (f"预设「{p['name']}」已绑定指纹 {p['fingerprint']}"
                            if p["fingerprint"] else f"预设「{p['name']}」已解绑指纹"),
        })

    async def transfer_preset(self):
        """把知识整体复制/移动到另一个预设。"""
        man = self._kbman()
        if man is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        src, dst = data.get("from", ""), data.get("to", "")
        mode = "move" if data.get("mode") == "move" else "copy"
        if not src or not dst:
            return json_response({"ok": False, "error": "from / to 必填"})
        r = man.transfer(src, dst, mode)
        if not r.get("ok"):
            return json_response(r)
        self.plugin._apply_active_knowledge()
        verb = "移动" if mode == "move" else "复制"
        extra = f"，{r['conflicts']} 条同名已保留目标版本" if r.get("conflicts") else ""
        return json_response({
            "ok": True, **self._presets_payload(man),
            "notice_text": f"已把「{r['from']}」的 {r['count']} 条知识{verb}到「{r['to']}」{extra}",
        })

    async def preset_notice(self):
        """指纹不匹配提醒：action = seen（弹窗已显示）/ ack（知道了）/ suppress（本次不再提醒）/ enable（重新开启提醒）。"""
        man = self._kbman()
        if man is None:
            return json_response(self._kb_uninit())
        data = await request.json() or {}
        action = data.get("action", "ack")
        if action == "seen":
            # v0.21.8：弹窗只要真的显示过就记账 —— 同一轮（预设 × 服务端指纹）不再重复弹，
            # 指纹下次变动后重新拥有一次弹窗机会。
            man.mark_notice_shown()
            return json_response({"ok": True, **self._presets_payload(man), "notice_text": ""})
        if action == "suppress":
            man.ack_notice(suppress=True)
            text = "已关闭本轮指纹不匹配提醒（服务端指纹下次变动时会重新提示，也可在知识库页重新开启）"
        elif action == "enable":
            man.set_suppress(False)
            text = "已重新开启指纹不匹配提醒"
        else:
            man.ack_notice(suppress=False)
            text = "已了解指纹不匹配提醒"
        return json_response({"ok": True, **self._presets_payload(man), "notice_text": text})

    async def rescan(self):
        """重建物品词典 + 重算服务端指纹（换服 / 排障用）。"""
        plugin = self.plugin
        try:
            note = await plugin._sync_server_context(rebuild_dictionary=True)
        except Exception as e:
            return json_response({"ok": False, "error": f"重建失败：{e}"})
        d = getattr(plugin, "_dictionary", None)
        stats = (
            {"mods": len(d.mods), "items": len(d.items), "recipes": len(d.recipes)}
            if d is not None else {}
        )
        return json_response({"ok": True, "dict_stats": stats, "notice_text": note})
