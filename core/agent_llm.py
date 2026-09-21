"""v0.7.0 多 Agent 工作流 · LLM 调用封装。

基于 AstrBot Context 的多 Provider 调用：
- context.llm_generate(chat_provider_id=..., prompt=..., system_prompt=..., **kwargs)
- context.get_current_chat_provider_id(umo=...)
- context.get_all_providers()

Provider 回退链（照搬群分析插件已验证的模式）：
  任务专用配置 → 主 LLM Provider → 当前会话 Provider → 第一个可用 Provider
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context

# 规范要求：插件的日志器必须来自 astrbot.api（不得使用标准库 logging）。
# 构造函数也收一个名为 logger 的参数（插件会传入自己的 plugin_tag logger），
# 同名参数会在 __init__ 里遮蔽这个模块变量，故留一份别名供兜底使用。
_DEFAULT_LOGGER = logger

# 每个 Agent 的配置键 → 回退链中"主 Provider"配置键
_MAIN_PROVIDER_CFG_KEY = "llm_provider_id"


class AgentLLM:
    """多 Agent 的 LLM 调用封装：独立 Provider 选择 + 重试 + 结构化 JSON 提取。"""

    def __init__(self, context: Context, config: dict, get_cfg=None, logger=None):
        self.context = context
        self.config = config if isinstance(config, dict) else {}
        # get_cfg: 插件提供的配置读取函数（用于读插件配置）
        self._get_cfg = get_cfg or self._fallback_cfg
        # 日志器：优先用插件传入的 AstrBot 插件 logger（带 plugin_tag 前缀），
        # 否则用 astrbot.api 的全局 logger —— 两条路都是 astrbot.api 的日志器。
        self._logger = _DEFAULT_LOGGER if logger is None else logger
        # v0.22.6（批次 2）：服务端版本约束片段，由 workflow 在每次任务开始前注入。
        # **代码决定语法世代，LLM 只填 ID 与数值** —— 见 core/version_caps.py。
        # 这里用「每次调用前前置」而不是拼进提示词正文，是为了让主人在
        # WebUI 里自定义的提示词照常生效（注入的是独立一层，不是正文的一部分）。
        self.version_context = ""

    def _fallback_cfg(self, key: str, default=None):
        """兜底配置读取：兼容 v0.14.0 的分组配置与旧版平铺配置。"""
        cfg = self.config
        if not isinstance(cfg, dict):
            return default
        if key in cfg:
            return cfg[key]
        for value in cfg.values():
            if isinstance(value, dict) and key in value:
                return value[key]
        return default

    # =============== Provider 解析 ===============

    def _cfg(self, key: str, default=None):
        return self._get_cfg(key, default)

    async def _provider_exists(self, provider_id: str) -> bool:
        try:
            prov = await self.context.provider_manager.get_provider_by_id(
                provider_id
            )
            return prov is not None
        except Exception:
            return False

    async def resolve_provider_id(self, cfg_key: str, umo: str = "") -> str | None:
        """按回退链解析 Provider ID。
        cfg_key: 插件配置键，如 agent_classifier_provider_id（可为空）
        umo: unified_msg_origin，用于取会话默认 Provider
        """
        # 1. 任务专用配置
        if cfg_key:
            pid = self._cfg(cfg_key, "")
            if pid and isinstance(pid, str) and pid.strip():
                if await self._provider_exists(pid.strip()):
                    return pid.strip()
                self._logger.warning("Agent Provider 配置 %s=%s 不存在，回退", cfg_key, pid)
        # 2. 主 LLM Provider
        main_pid = self._cfg(_MAIN_PROVIDER_CFG_KEY, "")
        if main_pid and isinstance(main_pid, str) and main_pid.strip():
            if await self._provider_exists(main_pid.strip()):
                return main_pid.strip()
        # 3. 当前会话 Provider
        try:
            pid = await asyncio.wait_for(
                self.context.get_current_chat_provider_id(umo=umo or None),
                timeout=30.0,
            )
            if pid and await self._provider_exists(pid):
                return pid
        except asyncio.TimeoutError:
            self._logger.warning("获取会话 Provider 超时(30s)，继续回退")
        except Exception as e:
            self._logger.debug("获取会话 Provider 失败: %s", e)
        # 4. 第一个可用 Provider
        try:
            provs = self.context.get_all_providers()
            for p in provs or []:
                try:
                    meta = p.meta()
                    pid = meta.id
                    if pid:
                        return pid
                except Exception:
                    continue
        except Exception as e:
            self._logger.warning("获取可用 Provider 失败: %s", e)
        return None

    def set_version_context(self, text: str) -> None:
        """注入/清空服务端版本约束片段（空字符串 = 不注入）。"""
        self.version_context = str(text or "").strip()

    def _effective_system(self, system_prompt: str) -> str:
        """把版本约束片段前置到 system prompt。

        前置而非后置：这段是**硬约束**，不能被长提示词正文冲淡。
        空片段时逐字节返回原文（不影响既有行为与测试）。
        """
        if not self.version_context:
            return system_prompt
        return f"{self.version_context}\n\n{system_prompt}"

    # =============== 核心调用 ===============

    async def chat(
        self,
        cfg_key: str,
        system_prompt: str,
        user_prompt: str,
        umo: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        timeout: float = 600.0,
        retries: int = 2,
    ) -> str | None:
        """调用 LLM 生成文本。失败返回 None。"""
        provider_id = await self.resolve_provider_id(cfg_key, umo)
        if not provider_id:
            self._logger.error("无可用 Provider，Agent 调用失败")
            return None

        kwargs: dict[str, Any] = {
            "chat_provider_id": provider_id,
            "prompt": user_prompt,
            "system_prompt": self._effective_system(system_prompt),
            "temperature": temperature,
        }
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await asyncio.wait_for(
                    self.context.llm_generate(**kwargs), timeout=timeout
                )
                text = getattr(resp, "completion_text", "") or ""
                if text and text.strip():
                    return text.strip()
                self._logger.warning("Agent 返回空文本（第 %d 次）", attempt + 1)
            except asyncio.TimeoutError:
                last_err = TimeoutError(f"Agent LLM 调用超时({timeout}s)")
            except Exception as e:
                last_err = e
            if attempt < retries:
                await asyncio.sleep(1.5 * (attempt + 1))
        self._logger.error("Agent LLM 调用失败: %s", last_err)
        return None

    async def chat_json(
        self,
        cfg_key: str,
        system_prompt: str,
        user_prompt: str,
        umo: str = "",
        temperature: float = 0.1,
        max_tokens: int | None = None,
        timeout: float = 600.0,
        retries: int = 2,
    ) -> dict | None:
        """调用 LLM 并解析 JSON 输出。解析失败返回 None。"""
        text = await self.chat(
            cfg_key, system_prompt, user_prompt, umo=umo,
            temperature=temperature, max_tokens=max_tokens,
            timeout=timeout, retries=retries,
        )
        if not text:
            return None
        return extract_json(text)

    # =============== 各 Agent 便捷方法 ===============

    async def classify(self, request: str, umo: str = "") -> dict | None:
        from .agent_prompts import get_agent_prompt
        return await self.chat_json(
            "agent_classifier_provider_id",
            get_agent_prompt("classifier", self._cfg),
            f"用户请求：{request}\n请判断类型并输出 JSON。",
            umo=umo,
        )

    async def judge(self, request: str, knowledge: str, umo: str = "") -> dict | None:
        from .agent_prompts import get_agent_prompt
        return await self.chat_json(
            "agent_judge_provider_id",
            get_agent_prompt("judge", self._cfg),
            f"用户请求：{request}\n\n知识库检索结果：\n{knowledge}\n\n请判断模板是否足够并输出 JSON。",
            umo=umo,
        )

    async def engineer(
        self, request: str, knowledge: str, dictionary: str, umo: str = ""
    ) -> dict | None:
        from .agent_prompts import get_agent_prompt
        return await self.chat_json(
            "agent_engineer_provider_id",
            get_agent_prompt("engineer", self._cfg),
            f"用户请求：{request}\n\n知识库现有条目：\n{knowledge or '(空)'}\n\n词典信息（物品ID参考）：\n{dictionary or '(空)'}\n\n请创建前瞻性模板并输出 JSON。",
            umo=umo, max_tokens=4000, timeout=180.0,
        )

    async def implement(
        self, request: str, player: str, knowledge: str,
        previous_results: str, retry_hint: str, umo: str = "",
        online_players: str = "",
    ) -> dict | None:
        from .agent_prompts import get_agent_prompt
        payload = json.dumps({
            "request": request,
            "player": player or "",
            "online_players": online_players or "",
            "knowledge": knowledge or "",
            "previous_results": previous_results or "",
            "retry_hint": retry_hint or "",
        }, ensure_ascii=False)
        return await self.chat_json(
            "agent_implementer_provider_id",
            get_agent_prompt("implementer", self._cfg),
            f"输入信息：\n{payload}\n\n请构造命令并输出 JSON。",
            umo=umo, max_tokens=2500, timeout=180.0,
        )

    async def correct(
        self, request: str, knowledge: str, failures: str,
        current_templates: str, umo: str = "",
    ) -> dict | None:
        from .agent_prompts import get_agent_prompt
        payload = json.dumps({
            "request": request,
            "knowledge": knowledge or "",
            "failures": failures or "",
            "current_templates": current_templates or "",
        }, ensure_ascii=False)
        return await self.chat_json(
            "agent_corrector_provider_id",
            get_agent_prompt("corrector", self._cfg),
            f"输入信息：\n{payload}\n\n请分析失败根因、修正模板并输出 JSON。",
            umo=umo, max_tokens=4000, timeout=180.0,
        )


def extract_json(text: str) -> dict | None:
    """从 LLM 输出中健壮地提取 JSON 对象。"""
    if not text:
        return None
    # 1. 尝试整体解析
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # 2. 剥离 ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            data = json.loads(m.group(1).strip())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # 3. 找第一个 { 到与之平衡的 }
    start = text.find("{")
    if start >= 0:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        data = json.loads(text[start:i + 1])
                        if isinstance(data, dict):
                            return data
                    except Exception:
                        return None
                    break
    return None
