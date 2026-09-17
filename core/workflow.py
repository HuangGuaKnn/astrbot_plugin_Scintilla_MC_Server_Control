"""v0.7.0 多 Agent 工作流 · 流水线编排。

流水线（全部 Agent 为独立提示词的 LLM 调用，无人格，与主会话隔离）：

  入口 mc_workflow 工具
    ├─ Agent#1 分类器：simple（原版指令）→ 直接执行，同步秒回
    │                 complex（模组任务）→ 后台流水线 + 完成通知
    ├─ Agent#2 模板判断器：现有知识库模板够用？
    │    够 → 直接进入实现
    │    不够 → Agent#3 模板工程师（前瞻建库，写回知识库）
    ├─ Agent#4 实现器：套模板构造命令 → RCON 执行 → 验证（多轮修正）
    │    成功 → 结束，回传简短结果
    │    失败 → Agent#5 纠错器（根因分析 → 修正模板写库 → 重试）
    └─ 最终结果：主动推送「完成通知」（后台任务路径）
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain

from .agent_llm import AgentLLM
from .agent_prompts import AGENT_DEFINITIONS

MAX_IMPLEMENT_ROUNDS = 3      # 实现器单次最多尝试轮数
MAX_CORRECT_ROUNDS = 2        # 纠错循环最多轮数
KB_RESULT_LIMIT = 6           # 知识库检索条数


class MCWorkflow:
    """多 Agent 工作流编排器。"""

    def __init__(self, plugin):
        self.plugin = plugin
        # 使用 AstrBot 管理的插件 logger（带 plugin_tag，避免日志格式化崩溃）
        self.logger = plugin.logger
        self.agent = AgentLLM(plugin.context, plugin.config, plugin._cfg, logger=plugin.logger)
        self._tasks: set[asyncio.Task] = set()
        self.logs: deque[dict] = deque(maxlen=30)  # 最近任务日志（WebUI 展示）

    def _log(self, **kw) -> None:
        """记录一条任务日志。"""
        self.logs.appendleft({
            "ts": time.strftime("%H:%M:%S"),
            **kw,
        })

    def recent_logs(self, limit: int = 12) -> list[dict]:
        return list(self.logs)[:limit]

    # =============== 配置 ===============

    def _cfg(self, key: str, default: Any = None) -> Any:
        return self.plugin._cfg(key, default)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg("agent_workflow_enabled", True))

    # =============== 工具入口（主 LLM 调用） ===============

    async def dispatch(
        self, event: AstrMessageEvent, request: str, player: str = ""
    ) -> str:
        """mc_workflow 工具入口。返回给主 LLM 的简短文本。"""
        if not self.enabled:
            return "[MC工作流] 多Agent工作流未启用。"
        if not request or not request.strip():
            return "[MC工作流] 请求内容为空，请描述要执行的任务。"

        umo = event.unified_msg_origin
        # ---- Agent#1 分类（同步，快速）----
        cls = await self.agent.classify(request, umo=umo)
        if not cls:
            self._log(request=request, player=player, status="error", detail="分类器无响应")
            return "[MC工作流] 分类器无响应，请稍后重试或直接使用具体工具。"
        ctype = str(cls.get("type", "complex")).strip().lower()

        # ---- 玩家决策守门（v0.9.0）：指令声明 > 绑定id > 终止任务并通知 ----
        decided = await self._decide_player(event, request, player, cls)
        if decided["abort"]:
            self._log(request=request, player=player, status="aborted",
                      detail=decided["reason"], kind="player_missing")
            return f"[MC工作流·终止] {decided['reason']}"
        player = decided["player"]

        if ctype == "simple":
            # 简单路径：同步执行，秒回
            result = await self._run_simple(cls, request, player, event)
            self._log(request=request, player=player, status="done",
                      detail=f"simple：{result}", kind="simple")
            return f"[MC工作流·完成] {result}"

        # 复杂路径：后台流水线 + 完成通知
        self._log(request=request, player=player, status="running",
                  detail="复杂任务进入后台流水线", kind="complex")
        task = asyncio.create_task(
            self._run_complex_async(request, player, event, umo)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return f"[MC工作流·已开始] 复杂任务已进入后台流水线：{request.strip()[:60]}，完成后将自动通知。"

    # =============== 简单路径 ===============

    async def _run_simple(
        self, cls: dict, request: str, player: str, event: AstrMessageEvent
    ) -> str:
        """simple：执行分类器给出的建议命令。"""
        commands = cls.get("commands") or []
        if not commands:
            return f"任务「{request.strip()[:40]}」无需执行命令（已由分类器判定为简单查询类）。"
        # v0.9.0：把命令中的玩家目标替换为决策后的玩家名（绑定兜底/@选择器除外）
        commands = self._apply_player_to_commands(commands, player)

        rcon = await self.plugin._get_rcon()
        results = []
        ok_count = 0
        for cmd in commands:
            cmd = str(cmd).strip()
            if cmd.startswith("/"):
                cmd = cmd[1:]  # 防呆：去掉多余的斜杠
            if not cmd:
                continue
            denied = await self.plugin._safe_command(event, cmd)
            if denied:
                results.append(f"命令「{cmd}」被拒绝：{denied}")
                continue
            try:
                out = await rcon.command(cmd)
                results.append(f"{cmd} → {str(out).strip()[:120]}")
                ok_count += 1
            except Exception as e:
                results.append(f"{cmd} → 执行异常: {e}")
        summary = f"已执行 {ok_count}/{len(commands)} 条命令"
        if ok_count == len(commands):
            return summary
        return f"{summary}；明细：{'；'.join(results[:5])}"

    # =============== 复杂路径（后台） ===============

    async def _run_complex_async(
        self, request: str, player: str, event: AstrMessageEvent, umo: str
    ) -> None:
        """后台执行完整复杂流水线，完成后主动推送通知。"""
        t0 = time.time()
        # 总超时兜底（防止 LLM 挂起导致任务永久卡死）
        total_timeout = max(60, int(self._cfg("workflow_total_timeout", 1800)))
        try:
            result_text, ok = await asyncio.wait_for(
                self._run_complex(request, player, umo), timeout=total_timeout
            )
        except asyncio.TimeoutError:
            result_text, ok = (
                f"工作流执行超时（超过 {total_timeout}s），请稍后重试或拆分请求。",
                False,
            )
        except Exception as e:
            self.logger.exception("复杂工作流执行异常")
            result_text, ok = f"工作流内部异常: {e}", False
        self._log(request=request, player=player,
                  status="done" if ok else "failed",
                  detail=result_text[:120], kind="complex",
                  cost=f"{time.time()-t0:.1f}s")

        # 推送完成通知（平台消息）
        try:
            head = "完成" if ok else "失败"
            await event.send(
                MessageChain([Plain(f"[MC工作流·{head}] {result_text}")])
            )
        except Exception as e:
            self.logger.warning("推送完成通知失败: %s", e)

    async def _run_complex(self, request: str, player: str, umo: str):
        """复杂路径核心逻辑。返回 (结果文本, 是否成功)。"""
        # 玩家名规范化：把 explicit 的聊天昵称/别名映射成服务器真实在线玩家名（bound 已是真实名）
        player, online_players = await self._resolve_player(player, request)
        online_text = ", ".join(online_players) if online_players else ""
        kb = self.plugin._knowledge
        dictionary = self.plugin._dictionary

        # ---- 检索知识库 ----
        kb_entries = []
        if kb is not None:
            try:
                kb_entries = await kb.asearch(request, limit=KB_RESULT_LIMIT)
            except Exception as e:
                self.logger.warning("知识库检索失败: %s", e)
        kb_text = self._fmt_entries(kb_entries, "知识库检索结果")

        # ---- Agent#2 模板判断 ----
        judge = await self.agent.judge(request, kb_text, umo=umo)
        sufficient = bool(judge and judge.get("sufficient"))

        if not sufficient:
            # ---- Agent#3 模板工程师（前瞻建库）----
            dict_text = ""
            if dictionary is not None:
                try:
                    hits = dictionary.search_items(request, limit=5)
                    dict_text = "；".join(
                        f"{h.get('zh','') or h.get('en','')}={h.get('id','')}" for h in hits
                    )
                except Exception:
                    pass
            eng = await self.agent.engineer(
                request, kb_text, dict_text, umo=umo
            )
            created = []
            if eng and eng.get("templates"):
                for tpl in eng["templates"]:
                    topic = str(tpl.get("topic", "")).strip()
                    content = str(tpl.get("content", "")).strip()
                    if topic and content and kb is not None:
                        try:
                            kb.save_entry(topic, content, source="agent_engineer")
                            created.append(topic)
                        except Exception as e:
                            self.logger.warning("模板写库失败 %s: %s", topic, e)
                if created:
                    kb_text = (
                        f"{kb_text}\n\n【模板工程师新建/更新】\n"
                        + "\n---\n".join(
                            f"topic: {t}\ncontent: {c}"
                            for t, c in [
                                (str(x.get("topic", "")), str(x.get("content", "")))
                                for x in eng["templates"]
                            ]
                        )
                    )
            self.logger.info("模板工程师产出: %s", created or eng)

        # ---- Agent#4 实现器循环 ----
        max_impl = max(1, int(self._cfg("agent_max_implement_rounds", MAX_IMPLEMENT_ROUNDS)))
        max_corr = max(0, int(self._cfg("agent_max_correct_rounds", MAX_CORRECT_ROUNDS)))
        failures = ""
        retry_hint = ""
        last_out = None
        for rnd in range(1, max_impl + 1):
            out = await self.agent.implement(
                request, player, kb_text, failures, retry_hint, umo=umo,
                online_players=online_text,
            )
            if not out:
                failures = "实现器无响应。"
                break
            last_out = out
            commands = out.get("commands") or []
            if not commands:
                failures = f"实现器未给出命令（第{rnd}轮）。reasoning: {out.get('reasoning','')}"
                continue

            exec_reports = await self._exec_commands(commands, player, online_players)
            all_ok = all(r["ok"] for r in exec_reports)
            failures = self._fmt_results(exec_reports)
            if all_ok and out.get("success"):
                return self._success_text(out, commands, exec_reports), True

        # ---- Agent#5 纠错循环 ----
        for cnd in range(1, max_corr + 1):
            corr = await self.agent.correct(
                request, kb_text, failures, kb_text, umo=umo
            )
            if not corr:
                break
            # 修正模板写库
            fixed = []
            for c in corr.get("corrections") or []:
                topic = str(c.get("topic", "")).strip()
                content = str(c.get("content", "")).strip()
                if topic and content and kb is not None:
                    try:
                        kb.save_entry(topic, content, source="agent_corrector")
                        fixed.append(topic)
                    except Exception as e:
                        self.logger.warning("纠错写库失败 %s: %s", topic, e)
            if fixed:
                kb_text = (
                    f"{kb_text}\n\n【纠错专家已修正模板】{', '.join(fixed)}\n"
                    f"教训：{corr.get('lessons','')}"
                )
            # 用修正后的模板重试实现
            retry_hint = str(corr.get("retry_hint", ""))
            for rnd in range(1, MAX_IMPLEMENT_ROUNDS + 1):
                out = await self.agent.implement(
                    request, player, kb_text, failures, retry_hint, umo=umo,
                    online_players=online_text,
                )
                if not out:
                    break
                last_out = out
                commands = out.get("commands") or []
                if not commands:
                    failures = f"实现器未给出命令（纠错后第{rnd}轮）"
                    continue
                exec_reports = await self._exec_commands(commands, player, online_players)
                failures = self._fmt_results(exec_reports)
                if all(r["ok"] for r in exec_reports) and out.get("success"):
                    return self._success_text(out, commands, exec_reports), True

        # ---- 兜底 ----
        detail = ""
        if last_out:
            detail = f" 实现器最后说明：{str(last_out.get('reasoning',''))[:100]}"
        return f"复杂任务未能完成（已尝试实现{max_impl}轮+纠错{max_corr}轮）。{detail}", False

    # =============== 工具方法 ===============

    async def _online_players(self) -> list[str]:
        """RCON list 获取当前真实在线玩家名列表。失败返回空列表。"""
        try:
            rcon = await self.plugin._get_rcon()
            out = str(await rcon.command("list")).strip()
            # 形如 "There are 2 of a max of 20 players online: a, b" 或 "There are 0 ..."
            m = re.search(r"players online:\s*(.*)$", out)
            if not m:
                return []
            names = m.group(1).strip()
            if not names or names.lower() in ("none", "无", "0"):
                return []
            return [n.strip() for n in names.split(",") if n.strip()]
        except Exception:
            return []

    async def _decide_player(
        self, event: AstrMessageEvent, request: str, explicit_player: str, cls: dict
    ) -> dict:
        """玩家决策守门（v0.9.0）。

        优先级：指令明确声明 > 绑定 id > 终止任务并通知。
        - 用户明确指名（target=named/all 或 LLM 提取的 player 参数）→ 用指令声明（优先级最高）；
        - 未指名但任务需要玩家（给我/发我/送我…）→ 回落到绑定 id；
        - 绑定也没有 → 终止（abort=True），绝不猜测玩家名。
        返回 {"player", "source", "abort", "reason"}。
        """
        requires = bool(cls.get("requires_player"))
        target = str(cls.get("target", "") or "").strip().lower()
        named = str(cls.get("player_name", "") or "").strip()

        # 启发式兜底：请求明显指向「我」但分类器漏判 → 视为 self
        if not (requires and target):
            t = (request or "").lower()
            if any(p in t for p in ("给我", "发我", "送我", "给我发", "发给我", "我要", "我想要")):
                requires, target = True, "self"

        # 不需要玩家 → 不干预（保留 LLM 传入的 player 参数）
        if not requires or target == "none":
            return {"player": (explicit_player or "").strip(), "source": "none",
                    "abort": False, "reason": ""}

        # 1) 指令明确声明（最高优先级：用户指令 > 绑定）
        if target in ("named", "all") and named:
            return {"player": named, "source": "explicit", "abort": False, "reason": ""}
        if explicit_player and explicit_player.strip():
            return {"player": explicit_player.strip(), "source": "explicit",
                    "abort": False, "reason": ""}

        # 2) 绑定 id
        bound = ""
        try:
            if self.plugin._bindings is not None:
                bound = self.plugin._bindings.get(str(self.plugin._sender_id(event)))
        except Exception:
            bound = ""
        if bound:
            return {"player": bound, "source": "bound", "abort": False, "reason": ""}

        # 3) 未指名且未绑定 → 终止任务并通知（不猜测、不执行）
        return {
            "player": "", "source": "missing", "abort": True,
            "reason": (
                "该任务需要指定玩家，但请求中未指名目标玩家，且当前账号未绑定MC玩家ID。"
                f"请先在聊天平台使用 {self.plugin._wake_prefix()}mcs 绑定 <你的MC游戏名> 绑定后再试，"
                "或在请求中直接指明目标玩家。"
            ),
        }

    @staticmethod
    def _apply_player_to_commands(commands: list, player: str) -> list[str]:
        """把玩家目标命令中的目标替换为决策后的玩家名（@选择器除外）。

        适用 give/kick/ban/pardon/op/deop 及 item give|replace entity。
        """
        player = (player or "").strip()
        out = []
        for cmd in commands:
            c = str(cmd).strip()
            if player:
                m = re.match(
                    r"^(give|kick|ban|pardon|op|deop)\s+(\S+)(.*)$", c, re.IGNORECASE
                )
                if m and not m.group(2).startswith("@"):
                    c = f"{m.group(1)} {player}{m.group(3)}"
                else:
                    m2 = re.match(
                        r"^item (give|replace) entity\s+(\S+)(.*)$", c, re.IGNORECASE
                    )
                    if m2 and not m2.group(2).startswith("@"):
                        c = f"item {m2.group(1)} entity {player}{m2.group(3)}"
            out.append(c)
        return out

    async def _resolve_player(
        self, player: str, request: str
    ) -> tuple[str, list[str]]:
        """把聊天昵称/别名解析成服务器真实在线玩家名。

        匹配优先级：精确（忽略大小写）→ 唯一包含匹配（昵称是某在线名的子串）。
        返回 (解析后的玩家名, 在线玩家名列表)。解析不到时返回原值（实现器会结合在线名单修正）。
        """
        online = await self._online_players()
        if not online:
            return (player or "").strip(), online
        low = {n.lower(): n for n in online}

        def sub_match(text: str) -> str | None:
            """文本与在线名互为子串（短昵称⊂在线名，或在线名⊂长文本），唯一匹配才返回。"""
            t = text.lower().strip()
            if not t:
                return None
            cands = [n for n in online if (t in n.lower() or n.lower() in t)]
            if len(cands) == 1:
                return cands[0]
            return None

        # 1) 精确匹配（忽略大小写）
        if player and player.strip():
            p = player.strip().lower()
            if p in low:
                return low[p], online
        # 2) 昵称是某在线名的子串 → 唯一则映射（如 Xiaoming → Xiaoming_233）
        if player and player.strip():
            r = sub_match(player)
            if r:
                return r, online
        # 3) 从 request 里提取：request 文本包含某在线名
        if request:
            r = sub_match(request)
            if r:
                return r, online
        return (player or "").strip(), online

    async def _exec_commands(
        self, commands: list[dict], player: str, online_players: list[str] | None = None
    ) -> list[dict]:
        """执行命令列表（复杂流水线专用；入口 mc_workflow 已限管理员，故此处不重复判定策略）。"""
        rcon = await self.plugin._get_rcon()
        if online_players is None:
            online_players = await self._online_players()
        online_low = {n.lower(): n for n in online_players}
        reports = []
        for c in commands:
            cmd = str(c.get("command", "")).strip()
            if cmd.startswith("/"):
                cmd = cmd[1:]  # 防呆：去掉多余的斜杠
            fb = str(c.get("feedback", "")).strip()
            if not cmd:
                reports.append({"command": "(空)", "ok": False, "output": "命令为空"})
                continue
            # 目标玩家存在性校验：give/item replace 等针对实体的命令
            target = self._command_target(cmd)
            if target and not target.startswith("@") and online_low:
                if target.lower() not in online_low:
                    reports.append({
                        "command": cmd, "ok": False,
                        "output": f"目标玩家「{target}」不在线或不存在（当前在线：{', '.join(online_players) or '无'}）。请使用真实游戏名。",
                    })
                    continue
            try:
                out = await rcon.command(cmd)
                out_s = str(out).strip()
                ok = self._is_success_out(out_s)
                reports.append({"command": cmd, "ok": ok, "output": out_s[:300]})
                if ok and fb and player:
                    # 命令成功且给了反馈文案 → 游戏内署名提示
                    await self.plugin._send_feedback(rcon, fb)
            except Exception as e:
                reports.append({"command": cmd, "ok": False, "output": str(e)[:300]})
        return reports

    @staticmethod
    def _command_target(cmd: str) -> str:
        """从命令里提取目标玩家名（无则返回空）。支持 give / item replace entity / kill / kick 等。"""
        c = cmd.strip()
        m = re.match(r"^(\S+)\s+(\S+)", c)
        if not m:
            return ""
        first, second = m.group(1), m.group(2)
        if first == "give":
            return second
        if first == "item" and second == "replace":
            m2 = re.match(r"^item replace entity\s+(\S+)", c)
            return m2.group(1) if m2 else ""
        if first == "item":
            m2 = re.match(r"^item give entity\s+(\S+)", c)
            return m2.group(1) if m2 else ""
        return ""

    @staticmethod
    def _is_success_out(out_s: str) -> bool:
        """判断 RCON 返回是否代表命令成功（识别常见失败文本，防误报成功）。"""
        if not out_s:
            return False
        _fail = (
            "unknown", "unrecogn", "error", "failed", "no entity", "not found",
            "cannot", "unable", "invalid", "exception", "usage:", "expected",
            "不存在", "找不到", "无效", "失败", "错误", "未知", "无法", "没有找到",
            "未找到", "no such", "bad ", "denied", "unable to parse",
        )
        low = out_s.lower()
        return not any(h in low for h in _fail)

    @staticmethod
    def _fmt_results(reports: list[dict]) -> str:
        return "\n".join(
            f"[{'OK' if r['ok'] else 'FAIL'}] {r['command']} → {r['output']}"
            for r in reports
        )

    @staticmethod
    def _fmt_entries(entries: list[dict], title: str) -> str:
        if not entries:
            return f"{title}：（无）"
        lines = [f"{title}（{len(entries)}条）："]
        for e in entries:
            kind = e.get("kind", "?")
            mod = e.get("mod", "?")
            topic = e.get("topic", "")
            content = str(e.get("content", ""))[:1200]
            lines.append(f"--- [{kind}/{mod}] {topic}\n{content}")
        return "\n".join(lines)

    @staticmethod
    def _success_text(
        out: dict, commands: list[dict], exec_reports: list[dict]
    ) -> str:
        n_ok = sum(1 for r in exec_reports if r["ok"])
        summary = str(out.get("reasoning", "")).strip()[:80]
        return f"任务执行成功（{n_ok}/{len(commands)} 条命令生效）。{summary}"
