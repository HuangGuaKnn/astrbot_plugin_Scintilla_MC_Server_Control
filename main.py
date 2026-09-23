"""AstrBot Minecraft 服务器控制插件。

通过 RCON 协议向 Minecraft 服务器下发命令，并将能力注册为 LLM 工具，
支持自然语言操控（如「给张三发一把钻石剑」→ 自动调用 give 命令）。

服务器事件转发：同机部署时可读取服务器日志，转发玩家进出/聊天/死亡/成就/指令事件到指定会话。

所有功能均可在 AstrBot 插件配置页中独立开关（enable_* / notify_* 配置项）。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

try:
    import psutil
except ImportError:  # 可选依赖：仅「mcs 状态」的进程指标（内存/CPU/时长）需要
    psutil = None

from .core.command_result import (
    STATUS_LABEL,
    CommandResult,
    classify_command_output,
)
from .core.item_dictionary import ItemDictionary
from .core.knowledge_base import KnowledgePresetManager, ModKnowledgeBase
from .core.mod_fingerprint import compute_server_identity
from .core.player_bindings import PlayerBindings
from .core.workflow import MCWorkflow
from .core.web_api import McControlWebApi
from .core.log_watcher import (
    EVENT_ADVANCEMENT,
    EVENT_CHAT,
    EVENT_COMMAND,
    EVENT_DEATH,
    EVENT_JOIN,
    EVENT_LEAVE,
    EVENT_ME,
    EVENT_WHISPER,
    LogWatcher,
)
from .core.rcon import AsyncRcon, RconError, RconTimeoutError
from .core.server_dir_check import format_check, inspect_server_dir
from .core.version_caps import (
    ITEM_PREFLATTEN_CUTOVER,
    build_version_context,
    describe_capabilities,
    preflatten_block_reason,
    resolve_version_info,
)
from .core.java_commands import check_command as check_command_policy
from .core.tool_guard import (
    ALTERNATIVES,
    COMMAND_TOOLS,
    DenyLatch,
    deny_result,
    latch_result,
    tolerant_tool,
)
from .core.hot_reload import (
    install_reload_patch,
    is_patch_installed,
    perform_hot_reload,
    plugin_dirs_for,
)

# 插件配置文件（AstrBot 侧）与 schema 文件名
PLUGIN_CONF_FNAME = "astrbot_plugin_Scintilla_MC_Server_Control_config.json"
CFG_SCHEMA_FNAME = "_conf_schema.json"


def load_cfg_group_index(schema_path: Path) -> dict:
    """从 _conf_schema.json 读取分组结构 → {扁平键: 分组名}。

    v0.14.0 起插件配置在 schema 中按「object 分组」整理（原生设置界面
    会渲染成带标题的卡片）；本索引用于让插件代码继续以扁平键读写配置。
    """
    index: dict = {}
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return index
    for gname, gmeta in schema.items():
        if isinstance(gmeta, dict) and gmeta.get("type") == "object":
            items = gmeta.get("items")
            if isinstance(items, dict):
                for key in items:
                    index[key] = gname
    return index


# 危险命令与权限等级判定已移到 core/java_commands.py（数据来源：中文 Minecraft Wiki
# 《命令》条目 Java 版命令表）。此处仅保留说明（v0.20.0 简化版）：
#   * 闸门只管「命令工具」：mc_execute_command / mc_give_item / mc_broadcast；
#     喊话、状态、查询、绑定等插件自带功能各有自己的开关，不受策略影响。
#   * whitelist（默认）：非管理员完全不能用「口头命令工具」（执行指令 / 发物品 / 广播）；
#   * blacklist：所有人都能用，但危险命令 + 权限等级 ≥ MIN_ADMIN_LEVEL(3) 的管理命令仅管理员。
#
# v0.21.0「拦截即终局」：光拦住不够，还得让决策 AI 知道这拦得没有商量余地。
#   1) core/tool_guard.tolerant_tool —— 剥掉 LLM 偶发的 {"arguments": {...}} 参数外壳，
#      避免工具因 TypeError 根本没跑起来（LLM 会误判成「用法不对」而反复重试）；
#   2) deny_result / latch_result —— 拒绝文案「终局化」（明说不是参数问题、重试与换工具都无效）；
#   3) DenyLatch —— 会话级闩锁：一轮对话内被拦过，后续命令类调用直接短路；
#   4) on_llm_request —— 请求阶段就把「哪些工具用不了」挂到用户消息的额外内容块里，从源头劝退。
#
# v0.21.1 前端设置页：管理员 admin_ids 改为「标签式」输入（与触发符号 / 会话转发一致），
#   逐个添加（输入框 + 添加按钮，回车也行）、单个 × 删除、重复项自动拦截，
#   保存格式仍是「, 」分隔的纯文本，沿用既有配置链路，后端无需改动。
#
# v0.21.2 前端设置页：顶部再加一个「保存全部设置」（.savebar 常驻吸顶条，粘在页签下方，
#   z-index 低于 .tabs 所以偏移有差也不会盖住内容）。设置页很长，不必滚到底才能存；
#   保存状态提示（setSaveNotice）同步写到上下两处 notice。
#
# v0.21.3 前端设置页：顶部吸顶条成为唯一保存入口——撤掉页尾那张「保存全部设置」卡片
#   （连带旧 id btn_save_cfg / cfg_notice 一并移除，setSaveNotice 只写顶部那条），
#   吸顶条提示文案改为「校验通过后写盘并热应用：大部分设置即时生效，服务器事件转发自动重启」。
#
# v0.21.4 唤醒词展示归一（防误导）：_wake_prefix() 只服务于文案展示，纯反斜杠的唤醒词
#   （如配置里的「\\」）一律按「无前缀」返回空串 —— mcs 帮助、各用法提示、绑定提示、
#   桥接提示、WebUI「指令」页 tip 全部跟着去掉那个 \；正常唤醒词（如 /）仍原样跟随。
#   读不到配置时也不再硬塞 "/"，宁可什么都不展示。终端提示文案分空/非空两种说法。
#
# v0.21.5 帮助文案精简：删掉「群聊里需先按 AstrBot 的唤醒词唤醒，私聊可直接发送」那行
#   （会用 AstrBot 的人自然懂唤醒词，无前缀展示时不再补说明）；结尾的吩咐提示改为
#   「也可以用自然语言直接吩咐我干活，例如「给Steve发一把钻石剑」」。非空前缀下的
#   「前缀跟随唤醒词设置」提示保留。
#
# v0.21.6 下线「mcs 热重载」指令（重载回 AstrBot 管）：聊天界面不再提供重载入口 ——
#   删掉 mcs 热重载 handler、schema 里的 enable_reload_command 开关、帮助里的那行，
#   并把「插件未重载」的提示统一指向 AstrBot「插件管理」页（WebUI + settings 接口）。
#   热重载补丁保留（AstrBot 自己的重载按钮照样深度清理），mc_reload_plugin 工具保留。
#
# v0.21.7 换服务端就地刷新（不再靠重载插件）：WebUI「保存全部设置」里改了
#   server_dir / dictionary_enabled / knowledge_enabled 时，立刻重算整合包指纹、
#   重建物品词典、刷新知识库的「当前服务端」基准（仍**不自动切换**预设，v0.18.0 策略不变）。
#   修复：此前指纹/词典只在 initialize() 时算一次，换服保存后 WebUI 仍显示旧指纹，
#   必须重载插件才更新。
#
# v0.21.8 指纹不匹配弹窗改为「检测到就弹 · 同一轮只弹一次」：WebUI 的全屏大弹窗
#   （#fp_modal）只要检测到「激活预设指纹 ≠ 当前服务端指纹」就弹出来，且以
#   (预设 × 服务端指纹) 为一个轮次——同一轮只弹一次（弹窗**显示过**即记账，
#   不要求主人点按钮），服务端指纹下次再变动（或换到别的预设）轮次翻新、重新弹一次。
#   实现：KnowledgePresetManager 的轮次记账（popup_keys / suppress_keys：按
#   「激活预设 × 服务端指纹」各记一条，旧的永久 suppressed / 单个 ack 自动迁移为
#   「本轮不再提醒」）；新增 kb/presets/notice 的 seen 动作供前端在弹窗显示后记账；
#   概览页「指纹不匹配」常驻标记改用 mismatch（不再随弹窗消失）。
#
# v0.21.9 指纹弹窗「不弹」两处根因修复：① WebUI 设置页「保存全部设置」改 server_dir
#   后没有补检指纹不匹配（只在切页签/刷新时检测）→ 主人停在设置页反复换服务端会一直
#   等不到弹窗；现在保存完立刻补检一次。② 两个服务端都没装 mod（或 server_dir 指到
#   没有 mods 的空壳目录）时指纹撞成同一个 d41d8cd98f00 → 「换服务端」被判成同一轮、
#   弹窗永远不再出现；现在轮次判据改为「指纹 + 服务器目录」（notice.last_dir），
#   并在弹窗里用 server_fp_weak 额外警告「mods 目录是空的、指纹认不出服务端」。
#
# v0.21.10 弹窗按钮语义合一：撤掉「本轮不再提醒」勾选框（它和「知道了」两条路都只是
#   记账，纯属自相矛盾），改成一行说明文字；「知道了」= 唯一出口（ack）。
#   后端 suppress 接口保留：旧记账数据、知识库页「重新开启」按钮仍要走它。
#
# v0.21.11 知识库条目「查看详情 / 编辑」：卡片预览被 72px 硬截断（看不全长内容），
#   新增与指纹弹窗同款的全屏子窗口 —— 「查看详情」按钮或直接点内容都能开，
#   里面显示全文并可编辑 topic/content；保存走 kb/entry（带 old_topic 支持改名，
#   撞已有主题拒绝），manual=True 保住验证状态、不因 auto_apply 关闭而转 pending。
#   配套：KnowledgeBase.get_entry/_public_entry、save_entry(rename_from, manual)。
#
# v0.21.13 插件页浅色模式：原先配色只有深色一套，且顶栏/遮罩/白色叠层/滚动条写死了
#   深色 rgba；现全部抽成 --bar-bg/--bar-bg2/--mask/--sub/--sub-bd/--hover/--shadow/--sb
#   等「结构色」变量，并新增 :root[data-theme="light"] 整套浅色变量（强调色在浅底上
#   压暗，实心按钮文字改走 --on-accent 翻白）。切换按钮在 WebUI 顶栏右上角，
#   选择存 localStorage（mcctrl_theme），head 里的引导脚本在首次绘制前套用，
#   无存档时跟随系统 prefers-color-scheme，二者都没有则深色。
#
# v0.21.15 「异地 RCON 模式」与「服务端目录硬校验」（异地部署可用性收口）：
#   背景：AstrBot 与服务端不同机、只映射 RCON 端口时，server_dir 若填了一个
#   「存在但不相干」的目录（空壳目录 / 父级目录 / 客户端 .minecraft），插件会照样
#   初始化 —— 指纹退化成空集合、词典 0 mod、日志永远读不到，看着像配好了其实全是空数据。
#   1) core/server_dir_check.inspect_server_dir —— 只认「服务端天然会有」的特征
#      （logs / libraries / server.properties / eula.txt / 启动脚本 / 服务端 jar），
#      **特意不**把 mods/（原版与客户端都不带）与 world/（首次开服前不存在）当必需项，
#      它们只作为提示；客户端目录单独识别并拒绝；只填了外壳目录时给出可复制的正确路径。
#   2) 保存设置时硬校验：结构不对 → **拒绝写盘**并回报原因（WebUI 设置页会显示细节）；
#      插件初始化时同样把关，结构不对就不建词典/知识库、不启事件监听。
#   3) 新增开关 remote_rcon_mode「异地 RCON 模式」：开启后 server_dir 可留空，所有
#      依赖服务端本地文件的能力（事件播报 / 物品词典 / 知识库 / 版本探测 / 进程指标）
#      一律按设计禁用，工具与 mcs 状态都会明确告知「为什么没有、怎么恢复」。
#      （可用 server_dir 为空且开关未开，那是「还没配」的起点，照旧放行；开关一开，
#       目录填不填都不再校验——因为按设计根本不用本地文件。）
#   4) 告知口径统一：工具层 _local_gate 给「【异地 RCON 模式·已禁用】/【服务端目录
#      校验未通过】+ 怎么恢复」；mcs 状态与帮助各加一行降级说明；决策 AI 侧在
#      on_llm_request 注入能力降级提醒（省得它反复调用注定失败的查询工具）；
#      WebUI 知识库页不再一律答「知识库未初始化」，改为说清原因。
#
# v0.21.16 异地 RCON 模式的「看得见的禁用」（WebUI 可用性收口）：
#   背景：v0.21.15 把「异地 RCON 模式」下的本地文件类能力在后端禁用了，但设置页只有
#   服务器目录置灰 + 一段说明 —— 事件播报 / 聊天桥接 / 物品词典 / 知识库 / 依赖词典的
#   LLM 工具（物品ID搜索 / 配方查询 / Mod 列表）的开关还亮着，主人看着像是「能开」，
#   开完保存、回来还是废的，只能靠猜。
#   做法（纯前端，后端判据一字未改）：新增 .rmt-off / .rmt-off-row 一套「亚克力磨砂」——
#   内容整体模糊 + backdrop-filter 磨砂 + 停用交互，正中盖一枚不参与模糊的亚克力提示牌，
#   写明「哪块不可用 + 为什么不可用」；行级只封那一行、配一枚 .rmt-tag 小标签。
#   深/浅两套主题各有 --rmt-veil / --rmt-acr / --rmt-acr-bd，浅色下提亮而不是糊成黑块。
#   配套：概览页把异地模式记进全局（RMT.on），服务器页「实时状态」在异地模式下说明
#   「版本会一直显示 -」，免得主人以为是自己没配好。
#   开关一勾即时生效、取消即还原（syncRemoteModeUI 负责挂牌与摘牌，重复调用不叠牌子）。

#
# v0.21.42 重排序精排（可选 · 默认关闭）：
#   * 配置项 knowledge_rerank：检索升级为「召回 → 精排」两段式 —— 先用 BM25
#     （+可选语义通道 RRF）召回 RERANK_POOL=20 条候选池，再交给 Rerank 模型
#     （Cross-Encoder）逐条打分重排，最后取前 limit 条。
#   * 与语义通道**相互独立**：没有嵌入模型也能单独开（只对 BM25 召回池精排）；
#     两者同时开启就是「BM25 + 向量 RRF 召回 → rerank 精排」的完整三段式。
#   * 取 AstrBot 已加载的第一个 Rerank Provider（如 siliconflow_rerank /
#     Qwen3-Reranker-8B）；没配置时开关开了也不报错，安静退化为召回顺序。
#   * 精排是纯附加延迟（多一次 API 往返），小库不值得开——条目多、问法杂时才划算。
#   * 实现要点：召回与补召逻辑分别抽成 _rank_candidates / _template_fill，
#     普通检索与精排检索共用同一套召回口径，避免两条路径行为漂移。
#
# v0.21.41 语义增强检索（可选 · 默认关闭，只建议大型服务器启用）：
#   * 配置项 knowledge_semantic_search：知识库在 BM25 之外再走一条「嵌入向量」语义
#     通道，两条通道各排各的名次、再用 RRF（倒数排名融合）合并。开启后提问可以
#     「换种说法」——问「后台连不上了」也能捞到库里那条「RCON 连接失败排查」。
#   * 只建议大型服务器启用：每次检索多一次嵌入调用（约 300~500ms），建库时每条约
#     一次嵌入调用（后台分批 + 落盘缓存 + 只算新增/改动条目）。小库用 BM25 已经够准。
#   * 需要先在 AstrBot「服务提供商」添加一个 Embedding 模型（推荐文本嵌入模型）；
#     没配置时开关开了也不报错，安静退化为纯 BM25。
#   * 无关查询误召回：RRF 只看名次、不看分数，所以还要一道**余弦相似度门槛**
#     （SEMANTIC_MIN_SIM = 0.50）把「整库都不相关」的查询挡在融合之外。
#   * 本机实测（438 条语料 / 24 组带标准答案查询 / 槽位 K=6）：
#       纯 BM25              Hit@1 75.0%  Hit@6  79.2%  MRR 0.771  无关误召回 0
#       + 语义（无门槛）      Hit@1 83.3%  Hit@6  91.7%  MRR 0.868  无关误召回 18
#       + 语义（门槛 0.50）   Hit@1 87.5%  Hit@6 100.0%  MRR 0.931  无关误召回 0  ← 当前
#     （拆分看：字面型 Hit@1 91.7%→100%；口语改写型 Hit@1 58.3%→75.0%、Hit@6 58.3%→100%）
#   * 另修一处建库性能：向量入库改为整批拼接（原本逐条 vstack 是 O(n²)，5000 条要 21s）。
#   * 新增 tests/test_kb_semantic_search.py（14 组断言）：融合生效、门槛边界、换模型
#     （维度不符）安全退化、开关关闭、批量入库性能。
#
# v0.21.40 知识库检索引擎换代（BM25 成为默认，旧版降为可选项）：
#   * 新增 BM25 检索引擎：中文二元切分（bigram，保留字序）+ BM25（idf 自动压低
#     「通用/方案/规则」等高频套话）+ 倒排索引 + 最低分门槛。
#   * 中文不再按单字匹配 —— 「黄铜」≠「铜黄」、查 `tac` 不再捞出 `taconite`。
#   * 本机实测（26 条语料 / 17 组查询）：结果精确率 20.5% → 58.6%（2.9 倍），
#     无关条目 66 → 12（减少 82%）；5000 条规模单次检索 6.3ms → 2.0ms。
#     代价：建索引约 0.03ms/条（5000 条约 144ms），仅写操作后重建一次。
#   * 配置项 knowledge_search_engine：bm25（默认）/ legacy（旧版，字面与历史一致），
#     保存设置即热切换，不必重载插件；非法值一律回落默认，检索永不因此报错。
#   * 索引生命周期挂在 load()/save() 上：沉淀 / 纠错 / 启停 / 删除后立即生效。
#
# v0.21.39 工程卫生 + 权限策略口径统一：
#   * 口径：白名单下「非管理员完全不能使用口头命令工具（执行指令 / 发物品 / 广播），
#     喊话 / 状态 / 查询 / 绑定等插件自带功能照旧」——README、WebUI 策略卡片、mcs 帮助、
#     配置说明与市场文案（metadata.yaml）全链路对齐。
#   * 修复：core/agent_llm.py 与 core/workflow.py 文件头的 UTF-8 BOM（静态工具会报 U+FEFF）。
#   * 工程：新增 .github/workflows（tests.yml 跑回归、release.yml 打 tag 自动发版）、
#     .editorconfig、CHANGELOG.md、Issue 模板与 README 徽章；Release 包不再带 tests/。
#

class McControlPlugin(Star):
    """AstrBot Minecraft 服务器控制插件（RCON + 服务器事件转发）。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if isinstance(config, dict) else {}
        # v0.14.0：配置分组索引（扁平键 → 分组名），供 _cfg 读写嵌套配置
        self._cfg_index: dict = load_cfg_group_index(
            Path(__file__).with_name(CFG_SCHEMA_FNAME)
        )
        self._migrate_config_layout()
        self._rcon: AsyncRcon | None = None
        #: v0.22.5：RCON 实例的建连/重建互斥锁。reset 与 _get_rcon 共用一把锁，
        #: 避免「刚把旧实例摘掉、新连接还没建好」的空窗，也避免并发重建重复建连。
        #: （锁对象在无事件循环时创建是安全的：Python 3.10+ 起 asyncio.Lock 不再绑定
        #:   创建时的循环，会在首次 await 时才取当前循环。）
        self._rcon_lock = asyncio.Lock()
        self._watcher: LogWatcher | None = None
        self._dictionary: ItemDictionary | None = None
        self._knowledge: ModKnowledgeBase | None = None
        self._kbman: KnowledgePresetManager | None = None
        self._bindings: PlayerBindings | None = None
        self._admins: set[str] = set(self._cfg("admin_ids", []))
        self._workflow: MCWorkflow | None = None
        # v0.21.0：权限拦截会话闩锁（被拦过 → 后续命令类调用直接短路）
        self._deny_latch: DenyLatch = DenyLatch(
            ttl=float(self._cfg("permission_latch_ttl", 300) or 300)
        )
        # 最近服务器事件缓存（WebUI 实时动态展示）
        self._recent_events: deque = deque(maxlen=60)

    # ================= 生命周期 =================

    async def initialize(self):
        # 双重保障：刷新管理员列表，兼容 __init__ 阶段配置未就绪的情况
        self._admins = set(self._cfg("admin_ids", []))
        self.logger.info(
            "MC控制插件初始化完成，管理员: %s", self._admins or "（未设置）"
        )
        # v0.16.0：给 AstrBot 的 PluginManager.reload 挂「重载即生效」补丁
        # （深度清理 sys.modules + __pycache__，让插件更新重载即可应用、无需重启）
        try:
            if install_reload_patch():
                self.logger.info(
                    "热重载补丁已钉上：插件重载将深度清理模块缓存与 __pycache__。"
                )
            elif is_patch_installed():
                self.logger.info("热重载补丁已在位（本次加载刷新了清理实现）。")
        except Exception as e:
            self.logger.warning("热重载补丁安装失败（不影响其他功能）: %s", e)
        # 构建 Mod 物品词典（整合包特化）
        # v0.21.15：先过「异地 RCON 模式 + 服务端目录结构校验」两道关——结构不对就不建，
        # 免得拿一个空壳目录算出「0 mod / 0 物品」的空数据，界面看着像配好了其实全是空的。
        server_dir = str(self._cfg("server_dir", "") or "").strip()
        self._dir_check = self.server_dir_check(max_age=0)
        remote_mode = self.is_remote_mode()
        valid_dir = bool(server_dir) and bool(self._dir_check.get("ok"))
        if remote_mode:
            self.logger.info(
                "异地 RCON 模式已开启：跳过服务端目录校验，事件播报 / 物品词典 / 知识库 / "
                "版本探测 / 进程指标按设计禁用（RCON 能力不受影响）。"
            )
        elif server_dir and not valid_dir:
            self.logger.warning(
                "服务端目录未通过结构校验：%s —— %s",
                server_dir, "；".join(self._dir_check.get("errors") or ["结构不符"]),
            )
        if self._cfg("dictionary_enabled", True) and not remote_mode and valid_dir:
            cache = StarTools.get_data_dir("astrbot_plugin_Scintilla_MC_Server_Control") / "items.json"
            self._dictionary = ItemDictionary(server_dir, str(cache))
            try:
                stats = await asyncio.to_thread(self._dictionary.build)
                self.logger.info("物品词典构建完成: %s", stats)
            except Exception as e:
                self.logger.warning("物品词典构建失败: %s", e)
        # 初始化学习型模组知识库（预设槽位制，稳定指纹=mod ID 集合）
        # v0.18.0：指纹变化**不再自动切换/继承**知识库，只记录提醒，由用户在 WebUI 手动处理
        if self._cfg("knowledge_enabled", True) and not remote_mode and valid_dir:
            try:
                kdir = StarTools.get_data_dir("astrbot_plugin_Scintilla_MC_Server_Control")
                cache = kdir / "mod_ids_cache.json"
                # v0.21.20：指纹来源分层 —— mods/ → plugins/（Paper 系）→ 服务端形态
                ident = await asyncio.to_thread(
                    compute_server_identity, str(server_dir), str(cache)
                )
                self._server_identity = ident
                kid = ident["fingerprint"]
                self._kbman = KnowledgePresetManager(
                    str(kdir), kid, server_dir, search_engine=self._kb_engine(),
                    semantic_enabled=self._kb_semantic(),
                    rerank_enabled=self._kb_rerank(),
                )
                self._kbman.server_weak = ident.get("weak")
                self._apply_active_knowledge()
                stats = self._knowledge.stats() if self._knowledge else {}
                self.logger.info(
                    "知识库已加载: 预设「%s」指纹 %s / 服务端指纹 %s → %s"
                    "（%s，%d 条知识，共 %d 个预设）",
                    stats.get("preset_name"), stats.get("fingerprint") or "未绑定",
                    kid,
                    {True: "匹配", False: "不匹配⚠", None: "未绑定"}.get(stats.get("match")),
                    ident.get("summary"), stats.get("total", 0),
                    len(self._kbman.list_presets()),
                )
                if ident.get("weak"):
                    self.logger.warning(
                        "服务端指纹偏弱：%s —— 该形态下指纹认不出同形态的另一台服务端。",
                        ident.get("summary"),
                    )
                notice = self._kbman.notice()
                if notice["changed"]:
                    self.logger.warning(
                        "检测到预设指纹与当前服务端不匹配：预设「%s」绑定 %s，当前服务端 %s。"
                        "已按 v0.18.0 策略跳过自动切换，WebUI 会弹一次全屏提示（同一指纹只弹一次），"
                        "请到知识库页选择/绑定预设。",
                        notice["preset_name"], notice["preset_fp"], notice["server_fp"],
                    )
                # v0.21.41：语义通道开着就后台补算向量（不阻塞启动；失败不影响其它功能）
                if self._kb_semantic():
                    self._inject_embed_fn()
                    asyncio.create_task(self._kb_build_semantic())
                # v0.21.42：精排开关开着就注入重排序调用（无需预计算，注完即用）
                if self._kb_rerank():
                    self._inject_rerank_fn()
            except Exception as e:
                self.logger.warning("模组知识库初始化失败: %s", e)
        # 初始化玩家绑定存储（v0.9.0：决策AI玩家守门）
        try:
            kdir = StarTools.get_data_dir("astrbot_plugin_Scintilla_MC_Server_Control")
            self._bindings = PlayerBindings(str(kdir))
            self.logger.info("玩家绑定存储已就绪（%d 条绑定）", len(self._bindings.all()))
        except Exception as e:
            self.logger.warning("玩家绑定存储初始化失败: %s", e)
        # 初始化多 Agent 工作流（v0.7.0）
        try:
            self._workflow = MCWorkflow(self)
            self.logger.info("多Agent工作流已就绪")
        except Exception as e:
            self.logger.warning("多Agent工作流初始化失败: %s", e)
        # 注册 WebUI 页面 API
        try:
            McControlWebApi(self).register()
            self.logger.info("WebUI 页面 API 已注册")
        except Exception as e:
            self.logger.warning("WebUI 页面 API 注册失败: %s", e)
        if self._cfg("enable_event_listener", False):
            if remote_mode:
                self.logger.warning(
                    "服务器事件转发已开启，但当前是「异地 RCON 模式」（只映射了 RCON 端口）："
                    "服务端本地日志读不到，已跳过。需要播报请关闭该开关并填写可读的服务端目录。"
                )
            elif not server_dir:
                self.logger.warning("服务器事件转发已开启但未配置 server_dir，已跳过。")
            elif not valid_dir:
                self.logger.warning(
                    "服务器事件转发已开启，但服务端目录未通过结构校验（%s），已跳过。",
                    server_dir,
                )
            else:
                self._watcher = LogWatcher(server_dir, self._on_server_event)
                await self._watcher.start()
                self.logger.info(
                    "日志监听已启动: %s/logs/latest.log", server_dir
                )

    async def terminate(self):
        if self._watcher:
            await self._watcher.stop()
            self._watcher = None
        if self._rcon:
            try:
                self._rcon.retire()
                await self._rcon.close()
            except Exception:
                pass
            self._rcon = None

    # ================= 内部工具 =================

    def _cfg(self, key: str, default: Any = None) -> Any:
        """读取配置项：优先分组内，回退旧版平铺位置（兼容未迁移的配置）。"""
        cfg = self.config
        if not isinstance(cfg, dict):
            return default
        group = self._cfg_index.get(key)
        if group:
            box = cfg.get(group)
            if isinstance(box, dict) and key in box:
                return box[key]
        if key in cfg:
            return cfg[key]
        return default

    def _cfg_flat(self, prefix: str | None = None) -> dict:
        """把分组配置摊平成旧版扁平字典（WebUI / 调试用）。

        prefix 为空时返回全部设置项；否则只返回以 prefix 开头的键。
        """
        cfg = self.config
        out: dict = {}
        if not isinstance(cfg, dict):
            return out
        # 旧版平铺键（尚未迁移或用户手改）
        for k, v in cfg.items():
            if isinstance(v, dict) and k in set(self._cfg_index.values()):
                continue
            out[k] = v
        for key, group in self._cfg_index.items():
            box = cfg.get(group)
            if isinstance(box, dict):
                out[key] = box.get(key, out.get(key))
        if prefix is None:
            return out
        return {k: v for k, v in out.items() if k.startswith(prefix)}

    def _cfg_path(self) -> Path:
        """插件配置文件路径（data/config/astrbot_plugin_Scintilla_MC_Server_Control_config.json）。"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            return Path(get_astrbot_data_path()) / "config" / PLUGIN_CONF_FNAME
        except Exception:
            data_dir = Path(StarTools.get_data_dir("astrbot_plugin_Scintilla_MC_Server_Control"))
            return data_dir.parent.parent / "config" / PLUGIN_CONF_FNAME

    def _migrate_config_layout(self) -> int:
        """旧版平铺配置 → 分组结构（v0.14.0）。返回迁移的键数。

        正常升级时配置文件已在落盘阶段迁移好；此处是兜底，防止 AstrBot
        在加载 schema 时把平铺旧键当作「多余键」删除后配置丢失。
        """
        cfg = self.config
        if not isinstance(cfg, dict) or not self._cfg_index:
            return 0
        moved: list[str] = []
        for key, group in self._cfg_index.items():
            if key not in cfg:
                continue
            box = cfg.get(group)
            if not isinstance(box, dict):
                box = {}
                cfg[group] = box
            box[key] = cfg.pop(key)
            moved.append(key)
        if not moved:
            return 0
        self._save_config()
        try:
            self.logger.info("配置已迁移为分组结构：%d 项", len(moved))
        except Exception:
            pass
        return len(moved)

    def _save_config(self) -> bool:
        """落盘当前配置（优先走 AstrBotConfig.save_config）。"""
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                saver()
                return True
            except Exception as e:
                try:
                    self.logger.warning("配置保存失败：%s", e)
                except Exception:
                    pass
        try:
            path = self._cfg_path()
            data = json.dumps(self.config, ensure_ascii=False, indent=4)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(path)
            return True
        except Exception:
            return False

    def _set_cfg_batch(self, values: dict) -> list:
        """按分组写入配置项并落盘，返回成功写入的键列表（WebUI 保存用）。"""
        cfg = self.config
        if not isinstance(cfg, dict):
            return []
        written: list = []
        for key, value in values.items():
            group = self._cfg_index.get(key)
            if group:
                box = cfg.get(group)
                if not isinstance(box, dict):
                    box = {}
                    cfg[group] = box
                box[key] = value
            else:
                cfg[key] = value
            written.append(key)
        if written:
            self._save_config()
        return written

    def _tool_enabled(self, name: str) -> bool:
        """读取对应 LLM 工具的开关配置（配置变更即时生效）。"""
        return bool(self._cfg(f"enable_{name}", True))

    # ================= 会话目标解析（UMO 规范化） =================

    # AstrBot 消息类型（UMO 第二段）
    _MESSAGE_TYPES = ("FriendMessage", "GroupMessage", "GuildMessage", "OtherMessage")

    def _platforms(self) -> list:
        """当前已加载平台实例列表：[{"id": 平台标识, "name": 显示名}]。

        用于（1）补全会话标识的平台前缀；（2）WebUI 会话目标选择器的下拉选项。
        """
        out: list = []
        seen: set = set()
        try:
            for inst in self.context.platform_manager.get_insts():
                try:
                    meta = inst.meta()
                except Exception:
                    continue
                pid = str(getattr(meta, "id", "") or "").strip()
                if not pid or pid in seen:
                    continue
                seen.add(pid)
                name = str(
                    getattr(meta, "adapter_display_name", "")
                    or getattr(meta, "name", "")
                    or ""
                ).strip()
                out.append({"id": pid, "name": name or pid})
        except Exception:
            pass
        return out

    def _platform_ids(self) -> list:
        """当前已加载平台实例的 id 列表（用于补全会话标识）。"""
        return [p["id"] for p in self._platforms()]

    # 会话类型别名（大小写不敏感、支持中英文）→ AstrBot 标准消息类型
    _TYPE_ALIASES = {
        "friendmessage": "FriendMessage", "friend": "FriendMessage",
        "private": "FriendMessage", "私聊": "FriendMessage",
        "好友": "FriendMessage", "好友消息": "FriendMessage", "单聊": "FriendMessage",
        "groupmessage": "GroupMessage", "group": "GroupMessage",
        "群聊": "GroupMessage", "群": "GroupMessage", "群消息": "GroupMessage",
        "guildmessage": "GuildMessage", "guild": "GuildMessage", "频道": "GuildMessage",
        "othermessage": "OtherMessage", "other": "OtherMessage", "其他": "OtherMessage",
    }

    @classmethod
    def _normalize_umo(cls, target: str) -> str:
        """把简写会话标识规范化为「[平台ID:]消息类型:会话ID」。

        支持：群聊:987654321 / 私聊:123456789 / friend:123 / aiocqhttp:group:987654321
        （大小写不敏感、中英文混用皆可、全角冒号自动转半角）。
        """
        t = str(target or "").strip().replace("：", ":")
        if not t:
            return ""
        parts = [p.strip() for p in t.split(":")]
        for i in (0, 1):
            if len(parts) <= i or not parts[i]:
                continue
            alias = cls._TYPE_ALIASES.get(parts[i].lower())
            if alias:
                parts[i] = alias
                break
        return ":".join(parts)

    # ================= 会话级播报内容（v0.17.0） =================

    # 事件组：会话级开关的粒度（与全局类型开关一一对应）
    NOTIFY_EVENT_GROUPS = (
        ("join_leave", "玩家加入 / 离开"),
        ("chat", "聊天内容"),
        ("death", "玩家死亡"),
        ("advancement", "成就 / 进度"),
        ("command", "玩家指令调用"),
    )
    # 日志事件类型 → 会话级事件组
    EVENT_TO_NOTIFY_GROUP = {
        EVENT_CHAT: "chat",
        EVENT_WHISPER: "chat",
        EVENT_ME: "chat",
        EVENT_JOIN: "join_leave",
        EVENT_LEAVE: "join_leave",
        EVENT_DEATH: "death",
        EVENT_ADVANCEMENT: "advancement",
        EVENT_COMMAND: "command",
    }

    def _notify_target_prefs(self) -> dict:
        """读取「会话 → 事件组列表」映射（键统一规范化为标准 UMO）。"""
        raw = self._cfg("notify_target_events", {}) or {}
        out: dict = {}
        if not isinstance(raw, dict):
            return out
        for key, val in raw.items():
            umo = self._normalize_umo(str(key)) or str(key)
            if isinstance(val, str):
                items = [x.strip() for x in val.replace("，", ",").split(",")]
            elif isinstance(val, (list, tuple, set)):
                items = [str(x).strip() for x in val]
            else:
                items = []
            known = {g for g, _ in self.NOTIFY_EVENT_GROUPS}
            out[umo] = [x for x in items if x in known]
        return out

    @staticmethod
    def _umo_tail(umo: str) -> str:
        """取会话标识的「消息类型:会话ID」部分（忽略平台前缀差异）。"""
        parts = [p for p in str(umo or "").split(":") if p]
        if len(parts) >= 2:
            return ":".join(parts[-2:])
        return parts[0] if parts else ""

    def _notify_target_allows(self, target: str, group: str) -> bool:
        """该目标会话是否订阅了某个事件组；未配置的会话默认全开。

        匹配策略：完整标识优先，其次按「消息类型:会话ID」兜底，
        这样手改配置时写 群聊:123456 也能对应上 aiocqhttp:GroupMessage:123456。
        """
        if not group:
            return True
        prefs = self._notify_target_prefs()
        if not prefs:
            return True
        umo = self._normalize_umo(target) or str(target)
        if umo in prefs:
            return group in prefs[umo]
        tail = self._umo_tail(umo)
        for key, groups in prefs.items():
            if self._umo_tail(key) == tail:
                return group in groups
        return True

    def _target_candidates(self, target) -> list:
        """把配置中的一个推送目标展开为候选会话标识（UMO）列表。

        支持写法：
        - 完整：aiocqhttp:FriendMessage:123456789 → 原样使用；
        - 缺平台前缀：FriendMessage:123456789 → 自动补全所有已加载平台 id；
        - 中文简写：私聊:123456789 / 群聊:987654321 → 同上（v0.16.1）。
        """
        t = self._normalize_umo(target)
        if not t:
            return []
        parts = t.split(":")
        if len(parts) >= 3:
            return [t]
        if len(parts) == 2 and parts[0] in self._MESSAGE_TYPES:
            ids = self._platform_ids()
            if ids:
                return [f"{pid}:{t}" for pid in ids]
        return [t]

    async def _send_to_targets(self, targets, chain) -> bool:
        """向一组目标会话推送消息；自动补全平台前缀，返回是否至少成功一次。"""
        delivered = False
        for target in targets or []:
            for umo in self._target_candidates(target):
                try:
                    ok = await StarTools.send_message(umo, chain)
                except Exception as e:
                    self.logger.warning("消息推送失败 → %s: %s", umo, e)
                    continue
                # StarTools.send_message 返回「是否找到匹配平台」；
                # 部分版本可能返回 None，此时不视为失败（避免误报并重复尝试）
                if ok is False:
                    self.logger.warning(
                        "消息推送失败（无匹配平台）→ %s（请检查会话格式，"
                        "正确形如 aiocqhttp:FriendMessage:123456789）", umo
                    )
                    continue
                delivered = True
                break
        return delivered

    # ================= 配置热更新 =================

    def _config_file(self) -> Path:
        """插件配置文件路径（兼容旧调用，等价于 _cfg_path）。"""
        return self._cfg_path()

    def _apply_config(self, updates: dict) -> None:
        """热更新插件配置：按分组改内存（立即生效）并落盘（重启保留）。"""
        try:
            self._set_cfg_batch(dict(updates or {}))
        except Exception as e:
            self.logger.warning("配置持久化失败: %s", e)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return bool(self._admins) and str(self._sender_id(event)) in self._admins

    def _is_whitelist_policy(self) -> bool:
        """当前是否为白名单策略（默认：非管理员不能用口头命令工具）。"""
        return str(self._cfg("danger_command_policy", "whitelist") or "whitelist") != "blacklist"

    def _wake_prefix(self) -> str:
        r"""读取 AstrBot 全局唤醒词前缀 —— 仅用于文案展示，不影响指令本身的匹配。

        纯反斜杠的唤醒词（例如配置里的 ``\\`` 或 ``\`` ）在聊天里展示只会误导人，
        统一按「无前缀」展示（文案直接写 mcs 帮助）；正常唤醒词（如 / ）原样跟随；
        读不到配置时也不硬塞 "/"，宁可什么都不展示。
        """

        try:
            cfg = self.context.get_config()
            prefixes = cfg.get("wake_prefix") if cfg is not None else None
        except Exception:
            prefixes = None
        if isinstance(prefixes, str):
            candidates = [prefixes]
        elif isinstance(prefixes, (list, tuple)):
            candidates = [str(x) for x in prefixes]
        else:
            candidates = []
        for item in candidates:
            item = item.strip()
            if item:
                if not item.strip("\\"):
                    return ""  # 「\\」「\」这类：展示时去掉，避免误导
                return item
        return ""

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except Exception:
            return "未知"

    def _kb_engine(self) -> str:
        """读取「检索引擎」配置（bm25=默认 / legacy=旧版；非法值回落默认）。"""
        from .core.knowledge_base import DEFAULT_SEARCH_ENGINE, SEARCH_ENGINES
        value = str(self._cfg("knowledge_search_engine", DEFAULT_SEARCH_ENGINE) or "").strip()
        return value if value in SEARCH_ENGINES else DEFAULT_SEARCH_ENGINE

    def _kb_semantic(self) -> bool:
        """读取「语义增强检索」开关（v0.21.41，默认关闭）。"""
        return bool(self._cfg("knowledge_semantic_search", False))

    def _kb_rerank(self) -> bool:
        """读取「重排序精排」开关（v0.21.42，默认关闭）。"""
        return bool(self._cfg("knowledge_rerank", False))

    # ---- 模型挑选（v0.21.43：可手动指定嵌入 / 重排序模型）----
    # 在此之前固定取「已加载的第一个」，多模型用户没得选。现在两条通道各自支持
    # 指定 provider id（配置项 knowledge_embed_provider_id / knowledge_rerank_provider_id），
    # 留空仍按老规矩取第一个 —— 不指定就跟以前一模一样，保持向后兼容。

    @staticmethod
    def _inst_provider_id(inst) -> str:
        """从 Provider 实例上取它的 provider id（取不到给空串，绝不抛异常）。"""
        cfg = getattr(inst, "provider_config", None)
        if isinstance(cfg, dict):
            return str(cfg.get("id") or "").strip()
        return str(getattr(inst, "provider_id", "") or "").strip()

    def _provider_insts(self, kind: str) -> list:
        """取 AstrBot 已加载的嵌入 / 重排序 Provider **实例**列表（挑模型用）。

        kind：embedding | rerank。AstrBot 侧接口异常时给空列表 —— 对调用方而言只是
        「没得选」，检索照常回落，不报错。
        """
        try:
            if kind == "rerank":
                return list(self.context.provider_manager.rerank_provider_insts or [])
            return list(self.context.get_all_embedding_providers() or [])
        except Exception:
            return []

    def _provider_choices(self, kind: str, insts: list | None = None) -> list:
        """把实例列表转成前端下拉能用的 [{id, model, type}]（设置页用）。

        注意与 _pick_provider 的分工：**挑模型要用实例**，而这里产出的是纯 dict
        —— dict 上没有 provider_config，喂给 _pick_provider 会永远匹配不上
        （v0.21.43 的测试就靠这条逮住过一次：active 会一直空着）。
        """
        insts = self._provider_insts(kind) if insts is None else insts
        out = []
        for inst in insts:
            cfg = getattr(inst, "provider_config", None)
            cfg = cfg if isinstance(cfg, dict) else {}
            out.append({
                "id": self._inst_provider_id(inst),
                "model": str(cfg.get("model") or ""),
                "type": str(cfg.get("type") or ""),
            })
        return out

    def _pick_provider(self, insts: list, want_id: str, kind_cn: str):
        """按配置指定的 id 挑 Provider 实例；没指定或指定失效则回落第一个。

        回落只打一条 warning 不报错：模型被删掉 / 停用 / 改名时，检索应当照常可用
        ——「锦上添花」的通道宁可降级，也不能让主人整条检索用不了。
        """
        if not insts:
            return None
        want_id = str(want_id or "").strip()
        if want_id:
            for inst in insts:
                if self._inst_provider_id(inst) == want_id:
                    return inst
            self.logger.warning(
                "知识库指定的%s模型 %s 不存在（未配置 / 未启用 / 已删除），回落到第一个可用模型",
                kind_cn, want_id,
            )
        return insts[0]

    def kb_model_status(self) -> dict:
        """设置页展示用：两条通道各自的候选列表、配置值、实际生效的 provider id。

        fallback=True 表示「指定了但没找到，已回落」——设置页要如实提示主人，
        不能让主人以为指定生效了。
        """
        status = {}
        # 注意：配置键是 knowledge_embed_provider_id / knowledge_rerank_provider_id
        # —— 不是 f"knowledge_{kind}_provider_id"（embed 后面没有 ding），这里写显式映射，
        # 免得下一个人手滑拼错（v0.21.43 的测试就是靠这条断言逮住过一次）。
        for kind, cn, cfg_key in (
            ("embedding", "嵌入", "knowledge_embed_provider_id"),
            ("rerank", "重排序", "knowledge_rerank_provider_id"),
        ):
            insts = self._provider_insts(kind)          # 挑模型必须用实例，不能用 dict 列表
            want = str(self._cfg(cfg_key, "") or "").strip()
            picked = self._pick_provider(insts, want, cn)
            active = self._inst_provider_id(picked) if picked is not None else ""
            status[kind] = {
                "choices": self._provider_choices(kind, insts),
                "configured": want,
                "active": active,
                "fallback": bool(want) and bool(active) and active != want,
            }
        return status

    def kb_effective_label(self, kind: str) -> str:
        """当前**实际生效**的模型展示名（提示文案用）：如 `qwen3-embed · Qwen3-Embedding-4B`。

        指定了却没找到时会带上「已回落到第一个」的提醒 —— 提示文案必须说实话，
        不能让主人以为指定的模型生效了。
        """
        st = (self.kb_model_status() or {}).get(kind) or {}
        cid = st.get("active") or ""
        if not cid:
            return ""
        model = ""
        for c in st.get("choices") or []:
            if c.get("id") == cid:
                model = c.get("model") or ""
                break
        label = f"{cid} · {model}" if model else cid
        if st.get("fallback"):
            label += "（指定的模型没找到，已回落到第一个）"
        return label

    def _resolve_embed_fn(self):
        """构造知识库用的异步嵌入调用（没配好嵌入模型则返回 None）。

        优先用配置指定的那个嵌入 Provider（knowledge_embed_provider_id，v0.21.43）；
        留空或指定失效则取 AstrBot 已加载的第一个（与记忆库 / 知识库页同一个）。
        调它的 get_embeddings(批量)。任何异常都吞掉并返回 None —— 语义通道是
        「锦上添花」的可选能力，缺模型时应当安静退化为纯 BM25，而不是让检索报错。
        """
        providers = self._provider_insts("embedding")
        provider = self._pick_provider(
            providers, self._cfg("knowledge_embed_provider_id", ""), "嵌入"
        )
        if provider is None:
            return None

        async def _embed(texts: list[str]) -> list[list[float]]:
            out = await provider.get_embeddings(list(texts))
            return [list(v) for v in out]

        return _embed

    def _resolve_rerank_fn(self):
        """构造知识库用的异步重排序调用（没配好 Rerank 模型则返回 None）。

        优先用配置指定的那个 Rerank Provider（knowledge_rerank_provider_id，v0.21.43）；
        留空或指定失效则取 AstrBot 已加载的第一个 Rerank Provider（取实例，不按 id 硬编码）。
        调它的 rerank(query, documents)。返回 [(index, score)]，index 是**候选
        列表中的下标**（与 AstrBot RerankResult 的语义一致，由调用方按池子下标
        还原成 topic）。任何异常都吞掉并返回 None —— 精排是可选的锦上添花能力，
        缺模型时应当安静退化为召回顺序，而不是让检索报错。
        """
        provs = self._provider_insts("rerank")
        provider = self._pick_provider(
            provs, self._cfg("knowledge_rerank_provider_id", ""), "重排序"
        )
        if provider is None:
            return None

        async def _rerank(query: str, docs: list[str]):
            res = await provider.rerank(query, list(docs))
            out = []
            for r in res or []:
                try:
                    out.append((int(r.index), float(r.relevance_score)))
                except Exception:
                    continue
            return out

        return _rerank

    def _inject_embed_fn(self) -> bool:
        """把嵌入调用注入知识库（预设管理器 + 当前 KB 实例）。返回是否可用。"""
        fn = self._resolve_embed_fn()
        man = getattr(self, "_kbman", None)
        if man is not None:
            man.embed_fn = fn
            if man.kb is not None:
                man.kb.embed_fn = fn
        kb = getattr(self, "_knowledge", None)
        if kb is not None:
            kb.embed_fn = fn
        return fn is not None

    def _inject_rerank_fn(self) -> bool:
        """把重排序调用注入知识库（预设管理器 + 当前 KB 实例）。返回是否可用。"""
        fn = self._resolve_rerank_fn()
        man = getattr(self, "_kbman", None)
        if man is not None:
            man.rerank_fn = fn
            if man.kb is not None:
                man.kb.rerank_fn = fn
        kb = getattr(self, "_knowledge", None)
        if kb is not None:
            kb.rerank_fn = fn
        return fn is not None

    def _apply_active_knowledge(self) -> None:
        """把内存中的知识库实例切到「当前激活预设」（WebUI 切换预设后调用）。"""
        man = getattr(self, "_kbman", None)
        if man is None:
            return
        kb = man.kb if man.kb is not None else man.reload_active()
        if kb is not None:
            kb.on_first_write = man.bind_active_if_needed
            kb.embed_fn = getattr(man, "embed_fn", None)
            kb.rerank_fn = getattr(man, "rerank_fn", None)
        self._knowledge = kb

    async def _kb_build_semantic(self, force: bool = False) -> dict:
        """后台补算知识库向量（语义通道专用）。失败只记日志，不影响其它功能。"""
        kb = getattr(self, "_knowledge", None)
        if kb is None:
            return {"ok": False, "reason": "知识库未初始化"}
        if not self._inject_embed_fn():
            return {"ok": False, "reason": "未找到可用的嵌入模型 Provider"}
        try:
            return await kb.build_vectors(force=force)
        except Exception as e:
            self.logger.warning("知识库向量构建失败: %s", e)
            return {"ok": False, "reason": str(e)}

    def _kb_touch_vectors(self) -> None:
        """知识写入后，后台把新条目补进向量索引（语义通道关闭时什么都不做）。

        「攒一下再算」的理由：批量导入 / 连续纠错时会连着触发很多次，
        每次立刻烧一次嵌入调用既费额度又没必要 —— 加个小延迟合并。
        """
        if not self._kb_semantic():
            return
        try:
            task = getattr(self, "_kb_vec_task", None)
            if task is not None and not task.done():
                return            # 已有一轮在排队/进行中，等它把这批一起带走
            self._kb_vec_task = asyncio.create_task(self._kb_debounced_vectors())
        except Exception:
            pass

    async def _kb_debounced_vectors(self) -> None:
        """延后一小会儿再算，让连续的写入合并成一轮。"""
        try:
            await asyncio.sleep(2.0)
            await self._kb_build_semantic()
        except Exception as e:
            self.logger.debug("知识库向量补算调度异常（已忽略）: %s", e)

    async def _restart_event_listener(self) -> str:
        """按当前配置重启日志监听器（WebUI 保存设置后热应用）。

        返回人类可读的结果说明。
        """
        # 先停掉旧的监听任务
        old = getattr(self, "_watcher", None)
        if old is not None:
            try:
                await old.stop()
            except Exception:
                pass
            self._watcher = None
        if not self._cfg("enable_event_listener", False):
            return "服务器事件转发已关闭"
        if self.is_remote_mode():
            return (
                "服务器事件转发未启动：当前是「异地 RCON 模式」，服务端本地日志读不到"
                "（需要播报请关闭该开关，并把「服务器目录」指向可读的服务端目录）"
            )
        server_dir = str(self._cfg("server_dir", "") or "").strip()
        if not server_dir:
            return "服务器事件转发未启动（未配置 server_dir）"
        chk = self.server_dir_check(max_age=0)
        if not chk.get("ok"):
            return (
                "服务器事件转发未启动：服务端目录未通过结构校验（"
                f"{(chk.get('errors') or ['结构不符'])[0]}）"
            )
        try:
            self._watcher = LogWatcher(server_dir, self._on_server_event)
            await self._watcher.start()
            return "服务器事件转发已重启并生效"
        except Exception as e:
            self.logger.warning("服务器事件转发重启失败: %s", e)
            return f"服务器事件转发启动失败：{e}"

    async def _sync_server_context(self, *, rebuild_dictionary: bool = True) -> str:
        """按当前配置重新识别「当前服务端」：指纹 / 物品词典 / 知识库基准。

        v0.21.7：换服务端后不再必须重载插件——WebUI「保存全部设置」改了 server_dir
        就地重算，避免指纹、物品词典与知识库停留在旧服务端。

        返回人类可读的结果说明（供 WebUI 保存提示展示）。
        """
        server_dir = str(self._cfg("server_dir", "") or "").strip()
        kdir = StarTools.get_data_dir("astrbot_plugin_Scintilla_MC_Server_Control")
        parts: list[str] = []
        remote_mode = self.is_remote_mode()
        check = self.server_dir_check(max_age=0)
        dir_ok = bool(check.get("ok"))
        valid_dir = bool(server_dir) and dir_ok
        if remote_mode:
            parts.append(
                "异地 RCON 模式：所有依赖服务端本地文件的能力已按设计禁用"
                "（事件播报 / 物品词典 / 知识库 / 版本探测 / 进程指标），"
                "RCON 能力不受影响"
            )
        elif server_dir and not dir_ok:
            parts.append(
                "服务端目录未通过结构校验："
                + (check.get("errors") or ["结构不符"])[0]
                + (f"（建议改填：{check['suggest_path']}）" if check.get("suggest_path") else "")
            )

        # ---------- 1) 整合包指纹（mod ID 集合哈希，带 jar 解析缓存） ----------
        if not self._cfg("knowledge_enabled", True):
            self._kbman = None
            self._knowledge = None
            parts.append("知识库已在配置层停用")
        elif remote_mode:
            self._kbman = None
            self._knowledge = None
            parts.append("知识库已禁用（异地 RCON 模式下算不出整合包指纹）")
        elif not valid_dir:
            self._kbman = None
            self._knowledge = None
            parts.append("服务端指纹未刷新（server_dir 未配置或不是有效目录）")
        else:
            try:
                ident = await asyncio.to_thread(
                    compute_server_identity, str(server_dir), str(kdir / "mod_ids_cache.json")
                )
            except Exception as e:
                self.logger.warning("服务端指纹重算失败: %s", e)
                parts.append(f"服务端指纹重算失败：{e}")
            else:
                self._server_identity = ident
                kid = ident["fingerprint"]
                man = getattr(self, "_kbman", None)
                if man is None:
                    try:
                        self._kbman = KnowledgePresetManager(
                            str(kdir), kid, server_dir, search_engine=self._kb_engine(),
                            semantic_enabled=self._kb_semantic(),
                            rerank_enabled=self._kb_rerank(),
                        )
                        self._kbman.server_weak = ident.get("weak")
                        self._apply_active_knowledge()
                        parts.append(
                            f"知识库已按当前服务端初始化（指纹 {kid}；{ident.get('summary')}）"
                        )
                    except Exception as e:
                        self.logger.warning("知识库初始化失败: %s", e)
                        parts.append(f"知识库初始化失败：{e}")
                else:
                    old = man.server_id
                    try:
                        man.set_server_id(kid, server_dir=server_dir, weak=ident.get("weak"))
                        self._apply_active_knowledge()
                    except Exception as e:
                        self.logger.warning("知识库基准刷新失败: %s", e)
                        parts.append(f"知识库基准刷新失败：{e}")
                    if old == kid:
                        parts.append(f"服务端指纹未变化（{kid}；{ident.get('summary')}）")
                    else:
                        parts.append(
                            f"服务端指纹已刷新 {old} → {kid}（{ident.get('summary')}）"
                        )
                        self.logger.warning(
                            "服务端指纹已变化：%s → %s（%s）。按 v0.18.0 策略"
                            "不自动切换知识库，请到 WebUI 知识库页确认/绑定预设。",
                            old, kid, ident.get("summary"),
                        )
                if ident.get("weak"):
                    # 两边都没内容（空壳目录 / 原版没装东西）→ 指纹分不出同形态的服务端
                    shape = ident.get("shape_text")
                    basis = (f"服务端形态「{shape}」" if shape
                             else "服务端形态（版本线索也读不到，指纹只是占位值）")
                    parts.append(
                        f"⚠ 当前服务端既没有 mod 也没有插件（mods/ 与 plugins/ 里都没有 jar），"
                        f"指纹 {kid} 只能按{basis}来算，认不出同形态的另一台服务端 —— "
                        "请确认 server_dir 是否指对了"
                    )
                    self.logger.warning(
                        "服务端内容为空：%s —— 指纹 %s 无法区分同形态的服务端。",
                        server_dir, kid,
                    )

        # ---------- 2) 物品词典（整包特化，换服务端必须重建） ----------
        if not self._cfg("dictionary_enabled", True):
            self._dictionary = None
            parts.append("物品词典已在配置层停用")
        elif remote_mode:
            self._dictionary = None
            parts.append("物品词典已禁用（异地 RCON 模式下读不到服务端目录）")
        elif not valid_dir:
            self._dictionary = None
            parts.append("物品词典未重建（server_dir 未配置或不是有效目录）")
        elif rebuild_dictionary or self._dictionary is None:
            try:
                self._dictionary = ItemDictionary(server_dir, str(kdir / "items.json"))
                stats = await asyncio.to_thread(self._dictionary.build)
                parts.append(
                    f"物品词典已重建（{stats['mods']} 个 Mod / "
                    f"{stats['items']} 个物品 / {stats['recipes']} 条配方）"
                )
            except Exception as e:
                self.logger.warning("物品词典重建失败: %s", e)
                parts.append(f"物品词典重建失败：{e}")
        return "；".join(parts) or "服务端识别已完成（无变化）"

    # ================= 异地 RCON 模式 · 本地文件能力闸门（v0.21.15） =================
    # 判定与文案集中在这里，避免「词典说未初始化、知识库说未初始化、状态说无法获取」
    # 各说各话，让主人猜不透到底哪儿出了问题。
    REMOTE_MODE_HINT = (
        "若插件确实能读到服务端目录（同机部署、共享盘 / 挂载副本、同步过来的副本），"
        "请关闭「异地 RCON 模式」并把「服务器目录」指向它。"
        "只映射 RCON 端口时这部分能力按设计不可用，但指令下发、发物品、广播、"
        "查询玩家、踢人封禁等 RCON 能力完全不受影响。"
    )

    def is_remote_mode(self) -> bool:
        """是否处于「异地 RCON 模式」：AstrBot 与服务端不同机、只映射了 RCON 端口。"""
        return bool(self._cfg("remote_rcon_mode", False))

    def server_dir_check(self, *, max_age: float = 30.0) -> dict:
        """当前 server_dir 的结构校验结果（默认带 30 秒缓存，避免每次工具调用都扫盘）。"""
        sd = str(self._cfg("server_dir", "") or "").strip()
        now = time.time()
        cache = getattr(self, "_dir_check_cache", None)
        if cache and cache[0] == sd and (now - cache[1]) < max_age:
            return cache[2]
        try:
            chk = inspect_server_dir(sd)
        except Exception as e:                                   # noqa: BLE001
            chk = {
                "path": sd, "ok": False, "level": "error",
                "errors": [f"目录校验失败：{e}"], "warnings": [],
                "markers": {}, "suggest_path": "", "summary": "目录校验失败",
            }
        self._dir_check_cache = (sd, now, chk)
        return chk

    # ---------- 服务端身份（v0.21.20：mods/ + plugins/ + 服务端形态） ----------

    def server_identity(self) -> dict:
        """最近一次算出的服务端身份（指纹 / 内容来源 / 形态）。未算过时为空 dict。"""
        return getattr(self, "_server_identity", None) or {}

    def server_content_text(self) -> str:
        """一行说明「当前服务端的内容装在哪」，供 WebUI 与提示文案复用。"""
        if self.is_remote_mode():
            return "异地 RCON 模式：未读取本地服务端目录"
        return str(self.server_identity().get("summary") or "")

    def _content_dir_label(self, text: str) -> str:
        """把文案里的 mods/ 换成实际内容目录（插件服务端是 plugins/）。

        闸门/提示里写的都是「服务端 mods/ 目录」，在 Paper 系服务端上会误导用户
        去找一个根本不存在的目录 —— 按身份引擎认出的类型就地改写。
        """
        kind = self.server_identity().get("kind")
        if not kind or "mods" not in text:
            return text
        if kind == "plugins":
            return text.replace("mods/ 目录", "plugins/ 目录").replace("mods/*.jar", "plugins/*.jar") \
                       .replace("mods/", "plugins/").replace("mods", "plugins")
        if kind == "hybrid":
            return text.replace("服务端 mods/", "服务端 mods/ + plugins/")
        return text

    def _local_gate(self, feature: str, need: str) -> str | None:
        """本地文件类能力的统一闸门。

        返回 None = 放行（交给各能力原有的「未初始化」判据）；
        返回字符串 = 直接作为结果返回给用户，说清「为什么不可用、怎么恢复」。
        """
        need = self._content_dir_label(need)
        if self.is_remote_mode():
            return (
                f"【异地 RCON 模式·已禁用】{feature}需要读服务端本地文件（{need}），"
                "当前配置声明了「AstrBot 与服务端不同机、只映射 RCON 端口」，因此按设计禁用。" + "\n"
                + self.REMOTE_MODE_HINT
            )
        sd = str(self._cfg("server_dir", "") or "").strip()
        if not sd:
            return None                      # 保持既有「未初始化：请先填写 server_dir」文案
        chk = self.server_dir_check()
        if chk.get("ok"):
            return None
        tip = ("\n建议改填：" + str(chk["suggest_path"])) if chk.get("suggest_path") else ""
        return (
            f"【服务端目录校验未通过】{feature}需要读服务端本地文件（{need}），"
            f"但当前「服务器目录」{chk.get('path') or '（未填写）'}不像是服务端根目录："
            f"{(chk.get('errors') or ['结构不符'])[0]}{tip}" + "\n"
            "请到插件设置页修正「服务器目录」后重试（保存时会做同样的校验）。"
        )

    def local_files_degraded_reason(self) -> str:
        """一行说明「本地文件能力当前是否可用」（空串 = 一切正常），供状态 / 概览用。"""
        if self.is_remote_mode():
            return ("异地 RCON 模式：事件播报 / 物品词典 / 知识库 / 版本探测 / 进程指标 "
                    "已按设计禁用（只保留 RCON 能力）")
        sd = str(self._cfg("server_dir", "") or "").strip()
        if not sd:
            return "未配置服务器目录：事件播报 / 物品词典 / 知识库 / 版本探测均不可用"
        chk = self.server_dir_check()
        if not chk.get("ok"):
            return f"服务器目录结构校验未通过：{(chk.get('errors') or [''])[0]}"
        return ""

    def _build_rcon(self) -> AsyncRcon:
        """按当前配置构造一个 RCON 实例（同步、不持锁、不建连）。

        v0.22.5：抽出这个纯构造器，供 ``_get_rcon()`` 与 ``reset_rcon()`` 共用。
        此前 ``reset_rcon()`` 在持 ``_rcon_lock`` 的情况下又 ``await self._get_rcon()``，
        而后者要抢同一把**非重入**锁 —— 直接死锁：设置页一保存，整个插件就再也不回话。
        """
        return AsyncRcon(
            host=str(self._cfg("rcon_host", "127.0.0.1")),
            port=int(self._cfg("rcon_port", 25575)),
            password=str(self._cfg("rcon_password", "") or ""),
            timeout=float(self._cfg("rcon_timeout", 5.0)),
            # v0.22.3：默认用「结束哨兵」判定响应收完（可靠边界），
            # 只有显式配置 idle 才退回静默窗口降级模式。
            end_mode=str(self._cfg("rcon_end_mode", "sentinel") or "sentinel"),
            probe_command=str(self._cfg("rcon_probe_command", "") or ""),
            idle_probe=float(self._cfg("rcon_idle_probe", 0.5) or 0.5),
            logger=self.logger,
        )

    async def _get_rcon(self) -> AsyncRcon:
        async with self._rcon_lock:
            if self._rcon is None:
                self._rcon = self._build_rcon()
            return self._rcon

    async def reset_rcon(self) -> AsyncRcon:
        """丢弃旧 RCON 实例、按当前配置重建一个（v0.22.5 核验 P2）。

        与 ``_get_rcon()`` 共用 ``_rcon_lock``，因此不存在「旧实例已摘、新实例未建」
        的空窗，也不会与并发调用重复建连。旧实例**显式** ``retire()``：
          · 若它正握在途命令，就让它跑完那条命令再自关（不粗暴掐断成「结果未知」）；
          · 空闲实例则会丢弃底层 socket，而不是被无声遗弃、连着 StreamWriter 一起泄漏。

        注意：这里**不能**再 ``await self._get_rcon()`` —— 锁不可重入，会自锁死。
        """
        async with self._rcon_lock:
            old = self._rcon
            self._rcon = None
            if old is not None:
                old.retire()
                try:
                    # 只有「没有在途命令」时才由 reset 自己收尾（关掉底层连接）；
                    # 若它正握着命令，就让那条命令跑完、由 command() 的 finally 回收。
                    if not old.in_flight:
                        await old.close()
                except Exception as e:  # noqa: BLE001
                    self.logger.warning(f"重建 RCON 时关闭旧连接失败（已忽略）: {e}")
            self._rcon = self._build_rcon()
            return self._rcon

    # ================= 权限护栏（v0.21.0「拦截即终局」） =================

    def _latch(self) -> DenyLatch:
        """取会话闩锁实例（TTL 跟随配置实时更新）。"""
        latch = getattr(self, "_deny_latch", None)
        if latch is None:
            latch = self._deny_latch = DenyLatch()
        try:
            latch.ttl = max(0.0, float(self._cfg("permission_latch_ttl", 300) or 0))
        except (TypeError, ValueError):
            latch.ttl = 300.0
        return latch

    def _latch_enabled(self) -> bool:
        return bool(self._cfg("permission_latch", True))

    def _latch_key(self, event: AstrMessageEvent) -> str:
        """闩锁键：会话 + 请求者（同一个人在同一会话里被拦过才算数）。"""
        try:
            umo = str(event.unified_msg_origin or "")
        except Exception:
            umo = ""
        return f"{umo}|{self._sender_id(event)}"

    def _latch_hit(self, event: AstrMessageEvent, tool: str) -> str | None:
        """闩锁命中 → 返回短路文案（调用方直接 return，不再触碰闸门与服务器）。"""
        if not self._latch_enabled():
            return None
        hit = self._latch().peek(self._latch_key(event))
        if not hit:
            return None
        reason, hits = hit
        self.logger.warning(
            "[审计] 权限闩锁短路 请求者=%s 工具=%s 本会话累计=%d 原因=%s",
            self._sender_id(event), tool, hits, reason,
        )
        return latch_result(reason, tool=tool, hits=hits)

    def _deny(self, event: AstrMessageEvent, reason: str, *, tool: str, command: str = "") -> str:
        """统一产出「终局化」拒绝文案，并登记闩锁/审计日志。"""
        key = self._latch_key(event)
        repeat = self._latch().record(key, reason) if self._latch_enabled() else 1
        self.logger.warning(
            "[审计] 权限拒绝 请求者=%s 工具=%s 策略=%s 命令=%r 原因=%s",
            self._sender_id(event), tool,
            str(self._cfg("danger_command_policy", "whitelist") or "whitelist"),
            command, reason,
        )
        return deny_result(
            reason, tool=tool, command=command,
            sender=self._sender_id(event), repeat=repeat,
        )

    def _admin_gate(self, event: AstrMessageEvent, *, tool: str, action: str) -> str | None:
        """管理操作工具闸门：非管理员 → 终局化拒绝（或闩锁短路）。None = 放行。"""
        if self._is_admin(event):
            return None
        latched = self._latch_hit(event, tool)
        if latched:
            return latched
        return self._deny(event, f"「{action}」属于管理操作，仅管理员可用。", tool=tool)

    async def _safe_command(
        self, event: AstrMessageEvent, command: str, tool: str = "mc_execute_command"
    ) -> str | None:
        """命令工具权限闸门：返回 None 表示放行，否则返回**完整**的终局化拒绝文案。

        白名单（默认）：非管理员不能用口头命令工具（执行指令 / 发物品 / 广播）；
        黑名单：所有人都能用，但危险命令与权限等级 ≥ 3 的管理命令仅管理员。
        详细等级表与判定逻辑见 core/java_commands.py。

        v0.21.0：拒绝理由不再「裸奔」返回——LLM 会把一句平铺直叙的说明理解成
        「换个参数/换个工具就能过」，于是反复硬试。这里改为终局化文案 + 会话闩锁。
        """
        if not self._is_admin(event):  # 管理员不受闩锁影响
            latched = self._latch_hit(event, tool)
            if latched:
                return latched
        policy = str(self._cfg("danger_command_policy", "whitelist") or "whitelist")
        is_admin = self._is_admin(event)
        denied = check_command_policy(command, policy=policy, is_admin=is_admin)
        if denied:
            return self._deny(event, denied, tool=tool, command=command)
        return None

    # ================= 权限前置提醒（v0.21.0） =================

    def _append_user_hint(self, req: ProviderRequest, blocks: list[str]) -> None:
        """把提示块挂到「用户消息的额外内容块」上（v0.22.1：不再改写 system_prompt）。

        为什么不写 system_prompt：这段内容含**请求者 ID**，每人 / 每会话都不同。
        系统提示词一旦被动态内容拼改，前缀缓存（prompt cache）当场失效，
        命中率掉下去 = 又慢又贵。AstrBot 给插件准备的正规注入口是
        `ProviderRequest.extra_user_content_parts`（AstrBot 自身的系统提醒也走它）：
        它被拼在同一条用户消息的末尾，模型照样读得到，但系统提示词一个字不动。

        老版本 AstrBot 没有该字段时**宁可不提醒**（权限闸门本身仍然拦得住），
        也绝不回退去改写 system_prompt。
        """
        if not blocks:
            return
        parts = getattr(req, "extra_user_content_parts", None)
        if parts is None:
            self.logger.debug(
                "当前 AstrBot 无 extra_user_content_parts 字段，跳过权限前置提醒"
            )
            return
        for block in blocks:
            parts.append({"type": "text", "text": block})

    @filter.on_llm_request()
    async def _inject_permission_hint(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """在 LLM 请求组装阶段把「哪些工具用不了」挂到用户消息的额外内容块上。

        比事后拦截更早一步：让模型从一开始就知道命令工具不可用，
        从源头减少「明知不可为而硬试」。同一次事件的多轮工具循环只注入一次。

        注意：**不改写 system_prompt**（含请求者 ID 的动态内容会废掉前缀缓存），
        所有注入统一走 _append_user_hint。
        """
        try:
            if not self._cfg("permission_hint_injection", True):
                return
            blocks: list[str] = []
            # v0.21.15：本地文件类能力被禁用（异地 RCON 模式 / 目录校验未通过）时，
            # 同样在请求阶段先告知，省得模型反复调用注定失败的查询工具。与权限无关，
            # 管理员也会注入；复用同一个「提示词注入」总开关。
            reason = ""
            _reason_fn = getattr(self, "local_files_degraded_reason", None)
            if callable(_reason_fn):
                try:
                    reason = _reason_fn() or ""
                except Exception:
                    reason = ""
            if reason:
                blocks.append(
                    "【能力降级提醒 · 由 MC 控制插件注入】\n"
                    f"{reason}。\n"
                    "因此 mc_search_item / mc_get_recipes / mc_list_mods / "
                    "mc_rescan_dictionary / mc_search_knowledge / mc_save_knowledge / "
                    "mc_correct_knowledge 当前**不可用**（调用会直接返回禁用说明，重试与"
                    "换工具都无效）；mc_server_status 里的版本 / 内存 / CPU / 运行时长也会缺失。\n"
                    "指令下发、发物品、广播、查询在线玩家、踢人封禁等 RCON 能力不受影响，"
                    "该用就用。若用户问起，请如实说明原因（异地 RCON 模式或服务端目录未配置/"
                    "结构不符），并提示「关闭异地 RCON 模式或修正服务器目录即可恢复」。"
                )
            if event.get_extra("_mc_perm_hint_done"):
                if blocks:
                    self._append_user_hint(req, blocks)
                return
            if self._is_admin(event):
                if blocks:
                    self._append_user_hint(req, blocks)
                    event.set_extra("_mc_perm_hint_done", True)
                return
            enabled = [t for t in COMMAND_TOOLS if self._tool_enabled(t)]
            if not enabled:
                if blocks:
                    self._append_user_hint(req, blocks)
                    event.set_extra("_mc_perm_hint_done", True)
                return
            sender = self._sender_id(event)
            if self._is_whitelist_policy():
                hint = (
                    "【权限前置提醒 · 由 MC 控制插件注入】\n"
                    f"当前请求者（ID: {sender}）**不是管理员**，命令工具策略 = 白名单："
                    "mc_execute_command / mc_give_item / mc_broadcast / mc_workflow 会被权限闸门"
                    "**直接拒绝**（与参数、措辞、重试次数无关，重试与换工具都无效）。\n"
                    "遇到「发物品 / 执行指令 / 广播 / 满配枪械 / 改服务器设置」这类请求时，"
                    "请**不要尝试调用**上述工具，直接如实告知用户「已被权限限制」，"
                    f"并给出替代方式：{ALTERNATIVES}，或请管理员把该账号加入 admin_ids、"
                    "把策略切换为 blacklist。\n"
                    "查询类工具（mc_list_players / mc_server_status / mc_connection_status / "
                    "mc_search_item / mc_get_recipes / mc_list_mods / mc_search_knowledge）不受影响，"
                    "该用就用。"
                )
            else:
                hint = (
                    "【权限前置提醒 · 由 MC 控制插件注入】\n"
                    f"当前请求者（ID: {sender}）**不是管理员**，命令工具策略 = 黑名单："
                    "命令工具本身可用，但 stop / op / ban / kick / whitelist 等危险命令与"
                    "权限等级 ≥ 3 的服务器管理命令仍会被拒绝；"
                    "mc_kick / mc_ban / mc_reload_plugin / mc_rescan_dictionary / "
                    "mc_save_knowledge / mc_correct_knowledge / mc_workflow 也不可用。\n"
                    "被拒绝时请如实告知用户，不要改写参数重试，也不要换成别的工具绕过。"
                )
            blocks.append(hint)
            self._append_user_hint(req, blocks)
            event.set_extra("_mc_perm_hint_done", True)
        except Exception as e:  # 提示注入失败绝不能影响正常对话
            self.logger.warning("权限前置提醒注入失败: %s", e)


    async def _send_feedback(self, rcon: AsyncRcon, text: str) -> None:
        """用 tellraw 向服务器内玩家发送署名（feedback_name）的反馈消息。"""
        if not self._cfg("feedback_tellraw", True):
            self.logger.warning("反馈被跳过：feedback_tellraw 配置为关闭")
            return
        name = str(self._cfg("feedback_name", "RCON") or "RCON")
        try:
            payload = self._colored_payload(
                f"[{name}] {text}", "color_feedback", "gradient_colors_feedback", "gold"
            )
            cmd = f"tellraw @a {payload}"
            self.logger.info("发送反馈 → %s", cmd)
            out = await rcon.command(cmd)
            self.logger.info("反馈发送完成，返回: %r", out)
        except Exception as e:
            self.logger.error("反馈发送失败: %s", e, exc_info=True)

    # ================= 玩家解析（v0.9.0 决策AI玩家守门） =================

    # ================= 统一命令结果判定（v0.22.6） =================
    #
    # 背景：v0.22.5 线上实测暴露「复杂 NBT 假成功」——服务器回
    #     Expected whitespace to end one argument, but found trailing data
    # 而执行入口只要 rcon.command() 不抛异常就报「命令执行成功」。
    # 根因是「什么算成功」没有收口：全仓 30 处 rcon.command() 各判各的。
    #
    # 从现在起，**所有用户可见的执行入口**都必须走 _exec_checked()，
    # 不要再直接 rcon.command() 之后用 `if out:` 判成功 ——
    # 服务器「正常地回一句报错」在 RCON 层面同样算收到了响应。

    async def _exec_checked(self, rcon, command: str) -> CommandResult:
        """执行一条命令并按统一规则判定结果（唯一入口）。"""
        try:
            out = await rcon.command(command)
        except RconTimeoutError as e:
            # 结果未知：命令可能已执行、也可能没有 —— 不计成功，也绝不自动重发
            return CommandResult(
                command=command, status="unknown",
                reason=f"未收到响应（结果未知，不会自动重发）：{e}",
            )
        except RconError as e:
            # v0.22.7（核验 P1-6）：**不能**一律判 failed。
            # RconError 混着两种语义：connect 阶段 = 命令根本没发出去（判 failed 安全），
            # send/read/protocol 阶段 = 命令可能已经发出去了 —— 判 failed 会诱导重试，
            # 而非幂等命令重试就是重复副作用（give 多发一把剑）。
            # 阶段由 core/rcon.py 在构造异常时标注，不靠异常文本猜。
            if getattr(e, "phase", "unknown") == RconError.PHASE_CONNECT:
                return CommandResult(
                    command=command, status="failed",
                    reason=f"未建立连接 / 未发送，命令未执行：{e}",
                )
            return CommandResult(
                command=command, status="unknown",
                reason=(
                    f"RCON 通信异常（阶段：{getattr(e, 'phase', 'unknown')}），"
                    f"命令可能已执行，结果未知（不会自动重发）：{e}"
                ),
            )
        return classify_command_output(
            command,
            str(out),
            # 两个维度分别取事实，缺一不可（见 core/command_result.py docstring）
            boundary_confirmed=getattr(rcon, "last_boundary_confirmed", None),
            response_received=bool(getattr(rcon, "last_response_received", False)),
        )

    def _render_result(self, r: CommandResult, ok_text: str, fail_prefix: str) -> str:
        """把判定结果渲染成用户可见回执（统一措辞）。

        · success                → ok_text
        · dispatched_unconfirmed → ok_text + 边界未确认说明（**不报失败**，
                                   也不报「结果未知」：消息确实发出去了）
        · 其它                   → fail_prefix + 服务器原文
        """
        if r.status == "success":
            return ok_text
        if r.status == "dispatched_unconfirmed":
            return (
                f"{ok_text}（注意：响应边界未确认 —— 服务器已收到本命令，"
                "但无法保证响应完整；如需绝对可靠请把 rcon_end_mode 设回 sentinel）"
            )
        if r.status == "inferred_success":
            # 命令发出去了、服务器也没报错，但**没有成功证据** ——
            # 既不写「成功」（那是谎报，v0.22.6 的假成功就是这么来的），
            # 也不写「失败」（那是误导）。如实说「未确认」。
            detail = r.output or r.reason or "（服务器未给出可读反馈）"
            return (
                f"⚠ 未确认：{ok_text} —— 但服务器未返回可识别的成功反馈，"
                f"请以游戏内实际结果为准。服务器原文：{detail}"
            )
        label = STATUS_LABEL.get(r.status, r.status)
        detail = r.output or r.reason or "（服务器未给出可读反馈）"
        return f"{fail_prefix}（{label}）：{detail}"

    async def _online_players(self) -> list[str]:
        """RCON list 获取当前真实在线玩家名列表。失败返回空列表。"""
        try:
            rcon = await self._get_rcon()
            out = str(await rcon.command("list")).strip()
            m = re.search(r"players online:\s*(.*)$", out)
            if not m:
                return []
            names = m.group(1).strip()
            if not names or names.lower() in ("none", "无", "0"):
                return []
            return [n.strip() for n in names.split(",") if n.strip()]
        except Exception:
            return []

    async def _resolve_target_player(self, event: AstrMessageEvent, player: str) -> str:
        """目标玩家解析（决策 AI 守门规则）。

        优先级：指令声明的玩家（含聊天昵称→在线真实名映射） > 绑定 id。
        返回玩家名；无法确定时返回空串（调用方应终止并提示）。
        """
        p = (player or "").strip()
        if p:
            # 指令声明优先：尝试把昵称/别名映射成服务器真实在线玩家名
            if not p.startswith("@"):
                online = await self._online_players()
                if online:
                    low = {n.lower(): n for n in online}
                    if p.lower() in low:
                        return low[p.lower()]
                    cands = [
                        n for n in online
                        if p.lower() in n.lower() or n.lower() in p.lower()
                    ]
                    if len(cands) == 1:
                        return cands[0]
            return p  # 保留原值，交给服务器校验
        # 未声明 → 回落到绑定 id
        if self._bindings is not None:
            return self._bindings.get(str(self._sender_id(event)))
        return ""

    # ================= LLM 工具（自然语言操控） =================

    @filter.llm_tool(name="mc_workflow")
    @tolerant_tool
    async def mc_workflow(
        self, event: AstrMessageEvent, request: str, player: str = ""
    ):
        """【MC任务总入口·多Agent工作流】处理 Minecraft 服务器任务，自动路由到最合适的执行路径。

        复杂场景优先用本工具：数值计算（配比/产量/耗材等）、模组枪械满配（如「满配M4A1」）、模组物品/方块发放、复杂 NBT 构造、模组任务链、查配方或模组机制、批量或多条混合指令。
        简单单条指令用 mc_execute_command；查玩家 mc_list_players；广播 mc_broadcast；查物品ID mc_search_item；查配方 mc_get_recipes。

        Args:
            request(string): 完整的任务请求描述，例如「给 Steve 满配一把 M4A1 步枪并附上弹药」「把服务器天气设为雷雨」
            player(string): 可选。任务目标玩家名，留空则按请求内容推断
        """
        if not self._tool_enabled("mc_workflow"):
            return (
                "【mc_workflow 已停用】本插件配置里已关闭 mc_workflow 工具，请**不要**再调用本工具。"
                "请改用以下具体工具分步完成复杂任务："
                "① mc_search_knowledge 查已沉淀的模组经验（NBT 格式/配件方案）；"
                "② mc_search_item 查准确物品 ID；③ mc_get_recipes 查合成配方；"
                "④ mc_execute_command 下发指令（可带复杂 NBT）；⑤ mc_give_item 发放物品。"
                "若任务确实需要多 Agent 流水线，例如数值计算（配比/产量/耗材等）、满配枪械、复杂 NBT 推理，请提示用户到插件配置或 WebUI「自动化」页开启工作流总开关。"
            )
        denied = self._admin_gate(event, tool="mc_workflow", action="多 Agent 工作流")
        if denied:
            return denied
        if self._workflow is None:
            return "多Agent工作流未初始化，请检查插件配置。"
        if not self._workflow.enabled:
            return (
                "【mc_workflow 总开关未开启】多 Agent 流水线已被禁用，请**不要**再调用本工具。"
                "请改用具体工具自行分步完成：mc_search_knowledge（查经验）→ mc_search_item（查 ID）"
                "→ mc_get_recipes（查配方）→ mc_execute_command / mc_give_item（执行）。"
                "若需恢复流水线，请提示用户到插件配置或 WebUI「自动化」页打开工作流总开关。"
            )
        return await self._workflow.dispatch(event, request, player)

    @filter.llm_tool(name="mc_execute_command")
    @tolerant_tool
    async def mc_execute_command(
        self, event: AstrMessageEvent, command: str, feedback: str = ""
    ):
        """向 Minecraft 服务器执行任意一条命令，结果会以自然语言提示展示给游戏内玩家。

        仅用于简单单条指令；数值计算（配比/产量/耗材等）、模组枪械满配、复杂 NBT、批量指令请用 mc_workflow。

        Args:
            command(string): 要执行的 Minecraft 命令（不含开头的斜杠），例如 "time set noon"、"give Steve netherite_sword 1"
            feedback(string): 可选。展示给游戏内玩家的提示文案（插件自动加「[署名]」，署名取自「反馈署名」配置）。需贴合用户原话，例如用户说「给我发一把锋利5的下界合金剑」时填「已给 Steve 发了一把锋利5的下界合金剑」；无法生成时留空
        """
        if not self._tool_enabled("mc_execute_command"):
            return "该功能已在插件配置中停用。"
        denied = await self._safe_command(event, command, tool="mc_execute_command")
        if denied:
            return denied
        # v0.23.2 第二版（GPT 核验 P0-1）：本工具是 @filter.llm_tool，
        # 命令由 LLM 生成、不等于「用户手写」，必须过版本守门。
        blocked = self._guard_command_for_version(command, source="mc_execute_command")
        if blocked:
            return blocked
        try:
            rcon = await self._get_rcon()
            r = await self._exec_checked(rcon, command)
            self.logger.info(
                "[审计] 请求者=%s 命令=%s 状态=%s 结果=%r",
                self._sender_id(event), command, r.status, r.output,
            )
            if r.status == "success":
                tip = feedback.strip() if feedback and feedback.strip() else f"命令执行成功：{command}"
                await self._send_feedback(rcon, tip)
                return f"命令执行成功：{command}\n服务器返回：{r.output or '（无输出）'}"
            if r.status == "dispatched_unconfirmed":
                # 命令确实发出去了，只是响应边界未确认 —— 不报失败、也不报「结果未知」
                return (
                    f"命令已发送：{command}\n"
                    "（服务器已收到本命令，但响应边界未确认；"
                    "如需绝对可靠请把 rcon_end_mode 设回 sentinel）"
                )
            if r.status == "inferred_success":
                # v0.22.7：命令下发成功、服务器也没报错，但**没有成功证据** ——
                # 既不写「执行成功」（那是谎报，v0.22.6 的假成功就是这么来的），
                # 也不写「执行失败」（那是误导）。如实说「未确认」。
                return (
                    f"命令已下发但未确认：{command}\n"
                    "服务器未返回可识别的成功反馈，无法确认是否生效，请以游戏内实际结果为准。"
                    "不要盲目重发（非幂等命令会重复生效）。\n"
                    f"服务器返回：{r.output or r.reason}"
                )
            if r.status == "unknown":
                return (
                    f"命令结果未知：{command}\n"
                    f"{r.reason}\n服务器可能已执行、也可能没有执行；为避免重复副作用，"
                    "本次不会自动重发，请先用查询类命令（状态 / 在线列表）确认结果。"
                )
            # syntax_error / failed：把服务器原文如实回传，绝不包装成「成功」
            label = STATUS_LABEL.get(r.status, r.status)
            return (
                f"命令执行失败（{label}）：{command}\n"
                f"服务器返回：{r.output or r.reason}"
            )
        except RconError as e:
            return f"命令执行失败：{e}"

    @filter.llm_tool(name="mc_list_players")
    @tolerant_tool
    async def mc_list_players(self, event: AstrMessageEvent):
        """查询 Minecraft 服务器当前在线的玩家列表与人数。
        """
        if not self._tool_enabled("mc_list_players"):
            return "该功能已在插件配置中停用。"
        try:
            rcon = await self._get_rcon()
            out = await rcon.command("list")
            return out or "服务器未返回玩家信息。"
        except RconError as e:
            return f"查询失败：{e}"

    @filter.llm_tool(name="mc_server_status")
    @tolerant_tool
    async def mc_server_status(self, event: AstrMessageEvent):
        """查询 Minecraft 服务器的运行状态：版本、在线人数、游戏内时间等。
        """
        if not self._tool_enabled("mc_server_status"):
            return "该功能已在插件配置中停用。"
        try:
            rcon = await self._get_rcon()
            players = await rcon.command("list")
            day = await rcon.command("time query daytime")
            return f"{players}\n{day}"
        except RconError as e:
            return f"查询失败：{e}"

    @filter.llm_tool(name="mc_broadcast")
    @tolerant_tool
    async def mc_broadcast(
        self, event: AstrMessageEvent, message: str, mode: str = "chat"
    ):
        """向 Minecraft 服务器内的所有玩家广播一条消息。

        Args:
            message(string): 要广播的消息内容
            mode(string): 广播方式，chat=聊天栏(say)，title=全屏标题，actionbar=动作栏，默认 chat
        """
        if not self._tool_enabled("mc_broadcast"):
            return "该功能已在插件配置中停用。"
        if not str(message or "").strip():
            return "请提供要广播的内容。"
        if not self._is_admin(event):
            latched = self._latch_hit(event, "mc_broadcast")
            if latched:
                return latched
        try:
            rcon = await self._get_rcon()
            name = str(self._cfg("feedback_name", "RCON") or "RCON")
            if mode == "title":
                payload = self._colored_payload(message, "color_title", "gradient_colors_title", "gold")
                cmd = f"title @a title {payload}"
            elif mode == "actionbar":
                payload = self._colored_payload(message, "color_title", "gradient_colors_title", "gold")
                cmd = f"title @a actionbar {payload}"
            else:
                payload = self._colored_payload(f"[{name}] {message}", "color_say", "gradient_colors_say", "white")
                cmd = f"tellraw @a {payload}"
            # 命令工具（广播）：白名单策略下非管理员一律被挡下
            denied = await self._safe_command(event, cmd, tool="mc_broadcast")
            if denied:
                return denied
            blocked = self._guard_command_for_version(cmd, source="mc_broadcast")
            if blocked:
                return blocked
            r = await self._exec_checked(rcon, cmd)
            return self._render_result(r, f"已在服务器内广播：{message}", "广播失败")
        except RconError as e:
            return f"广播失败：{e}"

    @filter.llm_tool(name="mc_give_item")
    @tolerant_tool
    async def mc_give_item(
        self,
        event: AstrMessageEvent,
        player: str = "",
        item: str = "",
        count: int = 1,
        feedback: str = "",
    ):
        """给指定玩家发放物品，结果会以自然语言提示展示给游戏内玩家。

        仅用于简单原版物品；数值计算（配比/产量/耗材等）、带复杂 NBT（附魔/枪械 Attachments）或批量发放请用 mc_workflow。

        Args:
            player(string): 可选。目标玩家名（真实游戏名或聊天昵称，会自动映射为在线真实名）。留空则用当前账号绑定的MC玩家ID（未绑定会提示）
            item(string): 物品ID，例如 diamond_sword、oak_log、netherite_sword。
                注意：服务端低于 1.13 时，本工具只接受不带 NBT/物品组件的简单物品ID
            count(number): 数量，默认 1
            feedback(string): 可选。展示给游戏内玩家的提示文案（插件自动加「[署名]」）。需贴合用户原话，例如「已给 Steve 发了一把锋利5的下界合金剑」；无法生成时留空
        """
        if not self._tool_enabled("mc_give_item"):
            return "该功能已在插件配置中停用。"
        if not item or not str(item).strip():
            return "请指定要发放的物品ID。"
        # v0.23.2（GPT 续单裁决 Q4）：已知服务端 < 1.13 时，带数据的物品命令不自动构造。
        # 放在最前面 —— 与 v0.21.0「拦截即终局」同一口径，免得 AI 先折腾绑定再撞墙。
        _pf = self._guard_command_for_version(f"give {item}", source="mc_give_item")
        if _pf:
            return _pf
        try:
            count = max(1, int(count))
        except (TypeError, ValueError):
            count = 1
        # 命令工具（发物品）：先过闸门，再做玩家解析 —— v0.21.0「拦截即终局」，
        # 免得 AI 先折腾绑定/换参数、绕一圈才撞墙。
        denied = await self._safe_command(
            event, f"give {player or 'player'} {item} {count}", tool="mc_give_item"
        )
        if denied:
            return denied
        # 决策AI玩家守门：指令声明 > 在线真实名映射 > 绑定 id
        player = await self._resolve_target_player(event, player)
        if not player:
            return (
                "未指定目标玩家，且当前账号未绑定MC玩家ID。"
                f"请先在聊天平台中使用 {self._wake_prefix()}mcs 绑定 <你的MC游戏名> 绑定后再发，"
                "或在请求中直接指名目标玩家。"
            )
        cmd = f"give {player} {item} {count}"
        try:
            rcon = await self._get_rcon()
            r = await self._exec_checked(rcon, cmd)
            ok_text = f"已给 {player} 发放 {item}×{count}"
            if r.status == "success":
                tip = feedback.strip() if feedback and feedback.strip() else ok_text
                await self._send_feedback(rcon, tip)
                return f"{ok_text}\n服务器返回：{r.output or '（无输出）'}"
            if r.status == "dispatched_unconfirmed":
                return (
                    f"{ok_text}（注意：响应边界未确认 —— 服务器已收到本命令，"
                    "但无法保证响应完整；物品是否落定请用 /clear 计数法确认）"
                )
            if r.status == "unknown":
                return (
                    f"发放结果未知：{cmd}\n"
                    f"{r.reason}\n物品可能已经发出，也可能没有发出；为避免重复发放，"
                    "本次不会自动重发，请先用查询命令确认背包后再决定。"
                )
            # 非幂等命令：syntax_error / failed 一律如实上报，**不自动重发**
            label = STATUS_LABEL.get(r.status, r.status)
            return (
                f"发放失败（{label}）：{cmd}\n"
                f"服务器返回：{r.output or r.reason}"
            )
        except RconError as e:
            return f"发放失败：{e}"

    # ================= 玩家绑定指令组（v0.10.0：改用 AstrBot 插件指令，不再交给 LLM 判断） =================
    # 玩家在聊天平台用唤醒词前缀触发，例如「mcs 绑定 Steve」（前缀跟随 AstrBot 唤醒词设置）。
    # 绑定数据仍作为决策AI玩家守门的只读兜底（_resolve_target_player / workflow._decide_player）。

    @filter.command_group("mcs")
    def mcs(self):
        """Minecraft Server 智控台 指令组（绑定/查询/喊话/状态/桥接/踢人/封禁/解封）"""
        return None

    @mcs.command("绑定", alias={"bind"})
    async def mcs_bind(self, event: AstrMessageEvent, player: str):
        """绑定当前账号到你的MC游戏名，例如 mcs 绑定 Steve"""
        if not self._cfg("enable_bind_command", True):
            yield event.plain_result("绑定指令已在插件配置中停用。")
            return
        if not player or not str(player).strip():
            yield event.plain_result(f"用法：{self._wake_prefix()}mcs 绑定 <你的MC游戏名>，例如 {self._wake_prefix()}mcs 绑定 Steve")
            return
        if self._bindings is None:
            yield event.plain_result("玩家绑定功能不可用。")
            return
        ok, msg = self._bindings.bind(self._sender_id(event), str(player).strip())
        yield event.plain_result(msg)
        return

    @mcs.command("解绑", alias={"unbind"})
    async def mcs_unbind(self, event: AstrMessageEvent):
        """解除当前账号与MC游戏名的绑定，例如 mcs 解绑"""
        if not self._cfg("enable_bind_command", True):
            yield event.plain_result("绑定指令已在插件配置中停用。")
            return
        if self._bindings is None:
            yield event.plain_result("玩家绑定功能不可用。")
            return
        _, msg = self._bindings.unbind(self._sender_id(event))
        yield event.plain_result(msg)
        return

    @mcs.command("查询", alias={"show", "info"})
    async def mcs_show(self, event: AstrMessageEvent):
        """查询当前账号已绑定的MC游戏名，例如 mcs 查询"""
        if not self._cfg("enable_bind_command", True):
            yield event.plain_result("绑定指令已在插件配置中停用。")
            return
        if self._bindings is None:
            yield event.plain_result("玩家绑定功能不可用。")
            return
        info = self._bindings.info(self._sender_id(event))
        if not info["bound"]:
            yield event.plain_result(f"当前未绑定MC游戏ID。可用 {self._wake_prefix()}mcs 绑定 <你的MC游戏名> 进行绑定。")
            return
        yield event.plain_result(f"当前已绑定：{info['player']}")
        return

    # ================= 聊天桥接绑定（v0.12.1：会话自绑定） =================

    @mcs.command("桥接", alias={"bridge"})
    async def mcs_bridge(self, event: AstrMessageEvent):
        """把当前会话绑定为聊天桥接目标（游戏内聊天 → 此会话），例如 mcs 桥接"""
        if not self._is_admin(event):
            yield event.plain_result("桥接绑定仅限管理员使用。")
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo.count(":") < 2:
            yield event.plain_result(f"无法获取有效的会话标识：{umo!r}")
            return
        prefix = str(self._cfg("chat_bridge_prefix", "!群") or "!群")
        targets = [str(t) for t in (self._cfg("chat_bridge_targets", []) or [])]
        if umo in targets:
            turned_on = False
            if not self._cfg("chat_bridge_enabled", False):
                self._apply_config({"chat_bridge_enabled": True})
                turned_on = True
            yield event.plain_result(
                f"当前会话已是桥接目标：\n{umo}\n"
                + ("桥接总开关此前未开启，已自动开启。" if turned_on else "桥接总开关：已开启。")
            )
            return
        targets.append(umo)
        self._apply_config({"chat_bridge_targets": targets, "chat_bridge_enabled": True})
        yield event.plain_result(
            "✓ 已将当前会话设为聊天桥接目标（并开启桥接总开关）：\n"
            f"{umo}\n"
            f"玩家在游戏内直接发送「{prefix}内容」（如 {prefix}123）即可转发到本会话，"
            "全角/半角、有无空格都认得；符号大小写"
            + ("区分（须完全一致）。\n" if self._bridge_case_sensitive() else "不区分（Q 与 q 等价）。\n")
            + f"如需解除，发送：{self._wake_prefix()}mcs 取消桥接"
        )
        return

    @mcs.command("取消桥接", alias={"unbridge"})
    async def mcs_unbridge(self, event: AstrMessageEvent):
        """解除当前会话的聊天桥接绑定，例如 mcs 取消桥接"""
        if not self._is_admin(event):
            yield event.plain_result("桥接绑定仅限管理员使用。")
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        targets = [str(t) for t in (self._cfg("chat_bridge_targets", []) or [])]
        if umo not in targets:
            yield event.plain_result("当前会话不在桥接目标中。")
            return
        targets = [t for t in targets if t != umo]
        self._apply_config({"chat_bridge_targets": targets})
        yield event.plain_result(f"已解除当前会话的桥接绑定（剩余 {len(targets)} 个目标）。")
        return

    # ================= mcs 指令组扩展（v0.11.0：喊话/状态/踢人/封禁/解封） =================

    @mcs.command("喊话", alias={"say"})
    async def mcs_say(self, event: AstrMessageEvent, content: GreedyStr):
        """将群内消息转发到服务器聊天栏（自动带上QQ发送者昵称，而非插件署名），例如 mcs 喊话 大家好呀"""
        if not self._cfg("enable_say_command", True):
            yield event.plain_result("喊话指令已在插件配置中停用。")
            return
        if not content or not str(content).strip():
            yield event.plain_result(f"用法：{self._wake_prefix()}mcs 喊话 <内容>，例如 {self._wake_prefix()}mcs 喊话 大家好呀")
            return
        if not self._cfg("say_command_public", True) and not self._is_admin(event):
            yield event.plain_result("喊话指令已设为仅管理员可用。")
            return
        # 喊话属于「插件自带功能」：不受白/黑名单策略影响（想限制就关掉上面的开关）。
        text = str(content).strip()
        try:
            rcon = await self._get_rcon()
            nickname = event.get_sender_name() or f"玩家{self._sender_id(event)}"
            payload = self._colored_payload(
                f"[群聊→{nickname}] {text}", "color_say", "gradient_colors_say", "white"
            )
            _cmd = f"tellraw @a {payload}"
            blocked = self._guard_command_for_version(_cmd, source="mcs_say")
            if blocked:
                yield event.plain_result(blocked)
                return
            r = await self._exec_checked(rcon, _cmd)
            self.logger.info(
                "[审计] 请求者=%s 喊话=%s 状态=%s",
                self._sender_id(event), text, r.status,
            )
            yield event.plain_result(
                self._render_result(r, f"已在服务器内喊话：{text}", "喊话失败")
            )
        except RconError as e:
            yield event.plain_result(f"喊话失败：{e}")
        return

    @mcs.command("全屏喊话", alias={"title", "titleraw"})
    async def mcs_title_cmd(self, event: AstrMessageEvent, content: GreedyStr):
        """用全屏 title 向服务器所有人喊话（仅管理员可调用），例如 mcs 全屏喊话 全体集合！"""
        if not self._cfg("enable_title_command", True):
            yield event.plain_result("全屏喊话指令已在插件配置中停用。")
            return
        if not self._is_admin(event):
            yield event.plain_result("全屏喊话属于管理操作，仅管理员可执行。")
            return
        text = str(content).strip() if content else ""
        if not text:
            yield event.plain_result(f"用法：{self._wake_prefix()}mcs 全屏喊话 <内容>，例如 {self._wake_prefix()}mcs 全屏喊话 全体集合！")
            return
        try:
            rcon = await self._get_rcon()
            payload = self._colored_payload(
                text, "color_title", "gradient_colors_title", "gold"
            )
            _cmd = f"title @a title {payload}"
            blocked = self._guard_command_for_version(_cmd, source="mcs_title_cmd")
            if blocked:
                yield event.plain_result(blocked)
                return
            r = await self._exec_checked(rcon, _cmd)
            self.logger.info(
                "[审计] 请求者=%s 全屏喊话=%s 状态=%s",
                self._sender_id(event), text, r.status,
            )
            yield event.plain_result(
                self._render_result(r, f"已向全服发送全屏喊话：{text}", "操作失败")
            )
        except RconError as e:
            yield event.plain_result(f"操作失败：{e}")
        return

    @mcs.command("状态", alias={"status"})
    async def mcs_status(self, event: AstrMessageEvent):
        """查询服务器 mspt/tps/在线人数/运行时长/内存/CPU 状态，例如 mcs 状态"""
        if not self._cfg("enable_status_command", True):
            yield event.plain_result("状态指令已在插件配置中停用。")
            return
        lines = ["📊 服务器状态"]
        remote_mode = self.is_remote_mode()
        if remote_mode:
            lines.append(
                "🔌 异地 RCON 模式：只走 RCON 通信 —— 版本 / 内存 / CPU / 运行时长"
                "（需读服务端本地文件或进程）按设计不可用"
            )
        # 0) 服务端版本（从服务端文件探测，原版/Forge 无 version 命令）
        try:
            _ver = self.detect_server_version()
            if _ver:
                lines.append(f"🧩 版本：{_ver}")
        except Exception:
            pass
        rcon = None
        # 1) 在线玩家
        try:
            rcon = await self._get_rcon()
            out = str(await rcon.command("list"))
            lines.append(self._parse_list_output(out))
        except RconError as e:
            lines.append(f"⚠ RCON 连接失败：{e}")
        # 2) TPS / MSPT（依赖 Spark 等 TPS 类模组，纯原版不可用）
        if rcon is not None:
            try:
                tps_info = await self._query_tps_mspt(rcon)
                if tps_info and tps_info.get("tps") is not None:
                    lines.append(f"🐢 TPS：{tps_info['tps']}（{tps_info['source']}）")
                    if tps_info.get("mspt") is not None:
                        lines.append(f"⚡ MSPT：{tps_info['mspt']}ms")
                else:
                    lines.append("🐢 TPS/MSPT：不可用（需 Spark 等模组提供 /tps，纯原版无此命令）")
            except RconError as e:
                lines.append(f"🐢 TPS 查询失败：{e}")
        # 3) 本机进程指标（同机部署：内存/CPU/运行时长）
        metrics = await self._server_process_metrics()
        if metrics:
            lines.append(f"⏱ 运行时长：{metrics['uptime']}")
            lines.append(f"💾 内存：已用 {metrics['rss_mb']}MB / JVM 最大堆 {metrics['xmx']}")
            lines.append(f"⚙ CPU：{metrics['cpu_percent']}%")
        else:
            if remote_mode:
                lines.append("⏱ 内存/CPU/运行时长：不可用（服务端在另一台机器上，本机看不到它的进程）")
            else:
                lines.append("⏱ 内存/CPU/运行时长：无法获取（未定位到本机服务端进程）")
        yield event.plain_result("\n".join(lines))
        return

    @mcs.command("踢人", alias={"kick"})
    async def mcs_kick_cmd(self, event: AstrMessageEvent, player: str, reason: str = ""):
        """将指定玩家踢出服务器（仅管理员可调用），例如 mcs 踢人 Steve 违规刷物品"""
        if not self._cfg("enable_kick_command", True):
            yield event.plain_result("踢人指令已在插件配置中停用。")
            return
        if not self._is_admin(event):
            yield event.plain_result("踢人属于管理操作，仅管理员可执行。")
            return
        player = await self._resolve_target_player(event, player)
        if not player:
            yield event.plain_result(f"未指定玩家。用法：{self._wake_prefix()}mcs 踢人 <MC玩家ID> [理由]")
            return
        try:
            rcon = await self._get_rcon()
            cmd = f"kick {player} {reason}".strip() if reason else f"kick {player}"
            blocked = self._guard_command_for_version(cmd, source="mcs_kick_cmd")
            if blocked:
                yield event.plain_result(blocked)
                return
            r = await self._exec_checked(rcon, cmd)
            self.logger.info(
                "[审计] 请求者=%s 踢人=%s 理由=%s 状态=%s",
                self._sender_id(event), player, reason, r.status,
            )
            if r.status == "success":
                await self._send_feedback(rcon, f"已将 {player} 踢出服务器")
            # 旧实现「if out: 报成功 / else: 也报成功」——服务器回 Player not found
            # 照样显示「已踢出」，这条路径现在如实判负。
            yield event.plain_result(
                self._render_result(r, f"已踢出 {player}", "踢出失败")
            )
        except RconError as e:
            yield event.plain_result(f"操作失败：{e}")
        return

    @mcs.command("封禁", alias={"ban"})
    async def mcs_ban_cmd(self, event: AstrMessageEvent, player: str, reason: str = ""):
        """封禁指定玩家（仅管理员可调用），理由可省略（使用配置的默认理由），例如 mcs 封禁 Steve 恶意破坏"""
        if not self._cfg("enable_ban_command", True):
            yield event.plain_result("封禁指令已在插件配置中停用。")
            return
        if not self._is_admin(event):
            yield event.plain_result("封禁属于管理操作，仅管理员可执行。")
            return
        player = await self._resolve_target_player(event, player)
        if not player:
            yield event.plain_result(f"未指定玩家。用法：{self._wake_prefix()}mcs 封禁 <MC玩家ID> [理由]")
            return
        if not reason or not str(reason).strip():
            reason = str(
                self._cfg("ban_default_reason", "违反服务器规则，由管理员封禁")
                or "违反服务器规则，由管理员封禁"
            )
        try:
            rcon = await self._get_rcon()
            cmd = f"ban {player} {reason}".strip()
            blocked = self._guard_command_for_version(cmd, source="mcs_ban_cmd")
            if blocked:
                yield event.plain_result(blocked)
                return
            r = await self._exec_checked(rcon, cmd)
            self.logger.info(
                "[审计] 请求者=%s 封禁=%s 理由=%s 状态=%s",
                self._sender_id(event), player, reason, r.status,
            )
            if r.status == "success":
                await self._send_feedback(rcon, f"已将 {player} 封禁")
            yield event.plain_result(
                self._render_result(r, f"已封禁 {player}", "封禁失败")
            )
        except RconError as e:
            yield event.plain_result(f"操作失败：{e}")
        return

    @mcs.command("解封", alias={"unban", "pardon"})
    async def mcs_unban_cmd(self, event: AstrMessageEvent, player: str):
        """解封指定玩家（仅管理员可调用），例如 mcs 解封 Steve"""
        if not self._cfg("enable_unban_command", True):
            yield event.plain_result("解封指令已在插件配置中停用。")
            return
        if not self._is_admin(event):
            yield event.plain_result("解封属于管理操作，仅管理员可执行。")
            return
        player = (player or "").strip()
        if not player:
            yield event.plain_result(f"未指定玩家。用法：{self._wake_prefix()}mcs 解封 <MC玩家ID>")
            return
        try:
            rcon = await self._get_rcon()
            _cmd = f"pardon {player}"
            blocked = self._guard_command_for_version(_cmd, source="mcs_unban_cmd")
            if blocked:
                yield event.plain_result(blocked)
                return
            r = await self._exec_checked(rcon, _cmd)
            self.logger.info(
                "[审计] 请求者=%s 解封=%s 状态=%s",
                self._sender_id(event), player, r.status,
            )
            if r.status == "success":
                await self._send_feedback(rcon, f"已解封 {player}")
            yield event.plain_result(
                self._render_result(r, f"已解封 {player}", "解封失败")
            )
        except RconError as e:
            yield event.plain_result(f"操作失败：{e}")
        return

    # ================= mcs 指令组扩展（v0.11.1：封禁列表/帮助） =================

    @mcs.command("封禁列表", alias={"banlist"})
    async def mcs_banlist_cmd(self, event: AstrMessageEvent):
        """查看当前被封禁的玩家名单（仅管理员可调用），例如 mcs 封禁列表"""
        if not self._cfg("enable_banlist_command", True):
            yield event.plain_result("封禁列表指令已在插件配置中停用。")
            return
        if not self._is_admin(event):
            yield event.plain_result("封禁列表属于管理操作，仅管理员可执行。")
            return
        try:
            rcon = await self._get_rcon()
            out = str(await rcon.command("banlist"))
            self.logger.info("[审计] 请求者=%s 查询封禁列表", self._sender_id(event))
            yield event.plain_result(self._format_banlist(out))
        except RconError as e:
            yield event.plain_result(f"操作失败：{e}")
        return

    @mcs.command("帮助", alias={"help"})
    async def mcs_help_cmd(self, event: AstrMessageEvent):
        """查看 mcs 指令组的全部用法（AstrBot 聊天平台指令），例如 mcs 帮助"""
        if not self._cfg("enable_help_command", True):
            yield event.plain_result("帮助指令已在插件配置中停用。")
            return
        p = self._wake_prefix()
        lines = [f"🛠 {p}mcs 指令帮助（Minecraft Server 智控台 · 聊天平台指令）", ""]
        # ---- 全员可用指令 ----
        public = []
        if self._cfg("enable_bind_command", True):
            public += [
                f"{p}mcs 绑定 <MC玩家ID>：把当前账号绑定到你的MC游戏名",
                f"{p}mcs 解绑：解除账号绑定",
                f"{p}mcs 查询：查看当前绑定状态",
            ]
        if self._cfg("enable_say_command", True):
            say_flag = ""
            if not self._cfg("say_command_public", True):
                say_flag = "（仅管理员）"
            public.append(f"{p}mcs 喊话 <内容>：把群内消息转发到服务器聊天栏{say_flag}")
        if self._cfg("enable_status_command", True):
            public.append(f"{p}mcs 状态：查询服务器 TPS/在线/内存/CPU")
        public.append(f"{p}mcs 帮助：查看本帮助")
        # ---- 仅管理员指令 ----
        admin = [
            f"{p}mcs 桥接：把当前会话设为聊天桥接目标（游戏内「!群 xxx」转发到这里）",
            f"{p}mcs 取消桥接：解除当前会话的桥接绑定",
        ]
        if self._cfg("enable_banlist_command", True):
            admin.append(f"{p}mcs 封禁列表：查看当前被封禁的玩家（仅管理员）")
        if self._cfg("enable_kick_command", True):
            admin.append(f"{p}mcs 踢人 <MC玩家ID> [理由]：把玩家踢出服务器（仅管理员）")
        if self._cfg("enable_ban_command", True):
            admin.append(f"{p}mcs 封禁 <MC玩家ID> [理由]：封禁玩家（仅管理员）")
        if self._cfg("enable_unban_command", True):
            admin.append(f"{p}mcs 解封 <MC玩家ID>：解除玩家封禁（仅管理员）")
        if self._cfg("enable_title_command", True):
            admin.append(f"{p}mcs 全屏喊话 <内容>：向全服发送全屏 title 喊话（仅管理员）")
        if public:
            lines.append("【全员可用】")
            lines.extend(public)
        if admin:
            if public:
                lines.append("")
            lines.append("【仅管理员】")
            lines.extend(admin)
        lines.append("")
        lines.append(
            "当前命令工具策略："
            + (
                "白名单（默认）——非管理员不能使用口头命令工具（执行指令 / 发物品 / 广播）；"
                "喊话、状态、查询、绑定这类自带功能不受影响。"
                if self._is_whitelist_policy()
                else "黑名单——所有人都能使用命令工具；但 stop / op / ban / kick / whitelist "
                "等危险命令与权限等级 3、4 的管理命令仍仅管理员可用。"
            )
        )
        if p:
            lines.append(
                f"提示：以上都是 AstrBot 聊天平台指令（不是在游戏里输入）；前缀「{p}」跟随 AstrBot 的唤醒词设置，改了唤醒词这里会自动跟着变。"
            )
        # 无前缀展示时不补说明：会用 AstrBot 的人自然懂唤醒词，多一行解释反而啰嗦（v0.21.5）
        # v0.21.15：本地文件类能力被禁用时点一句，省得主人以为是插件坏了
        _reason_fn = getattr(self, "local_files_degraded_reason", None)
        if callable(_reason_fn):
            try:
                _reason = _reason_fn()
            except Exception:
                _reason = ""
            if _reason:
                lines.append(f"注意：{_reason}")
        lines.append("提示：也可以用自然语言直接吩咐我干活，例如「给Steve发一把钻石剑」")
        yield event.plain_result("\n".join(lines))
        return

    # ================= 热重载（v0.16.0：插件更新免重启） =================

    def _plugin_manager(self):
        """取运行中的 PluginManager 实例（AstrBot 在 Context 上挂了引用）。"""
        return getattr(self.context, "_star_manager", None)

    def _schedule_hot_reload(self, target: str | None, delay: float = 0.6) -> str:
        """安排一次热重载：延迟一点点，先让本条回复发出去，再动刀。"""
        pm = self._plugin_manager()
        if pm is None:
            return "未取到插件管理器，无法热重载（可能是 AstrBot 版本差异）。"
        if target and not plugin_dirs_for(pm, target):
            return f"未找到插件「{target}」。可把插件名留空做全量重载。"
        label = f"插件「{target}」" if target else "全部插件"
        mode = (
            "深度清理（模块缓存 + __pycache__）"
            if is_patch_installed()
            else "标准清理（补丁未在位）"
        )

        async def _runner():
            await asyncio.sleep(delay)
            try:
                ok, msg = await perform_hot_reload(pm, target)
                self.logger.info("[热重载] %s", msg)
            except Exception as e:  # 兜底：绝不让重载把插件搞崩
                self.logger.warning("[热重载] 执行失败: %s", e)

        try:
            asyncio.get_running_loop().create_task(_runner())
        except RuntimeError:
            return "当前没有可用的 asyncio 事件循环，热重载已放弃。"
        return (
            f"已安排重载 {label}：{mode} → 重新导入。约 1 秒后完成，"
            "之后新代码立即生效（无需重启 AstrBot）♡"
        )

    # v0.21.6：mcs 热重载 指令已下线 —— 重载插件请回 AstrBot 插件管理页操作。
    # 热重载补丁仍在位（AstrBot 自己的重载按钮同样享受深度清理），mc_reload_plugin 工具保留。

    # ================= 状态查询辅助（v0.11.0） =================

    @staticmethod
    def _parse_list_output(out: str) -> str:
        """解析 list 命令输出为「在线玩家：n/max（名单）」文本。"""
        out = (out or "").strip()
        if not out:
            return "在线玩家：未知"
        m = re.search(r"(\d+)\s+of\s+a\s+max\s+of\s+(\d+)", out)
        names = out.split(":", 1)[1].strip() if ":" in out else ""
        if m:
            cur, mx = m.group(1), m.group(2)
            if names and names.lower() not in ("none", ""):
                return f"👥 在线玩家：{cur}/{mx}（{names}）"
            return f"👥 在线玩家：{cur}/{mx}"
        return f"👥 在线玩家：{out}"

    @staticmethod
    def _format_banlist(out: str) -> str:
        """解析 banlist 命令输出为易读的封禁名单文本。"""
        out = (out or "").strip()
        if not out:
            return "📋 封禁列表：无法获取（RCON 无返回）"
        if "no banned players" in out.lower() or "there are 0 banned" in out.lower():
            return "📋 封禁列表：当前没有被封禁的玩家"
        m = re.search(r"(\d+)\s+banned players?", out, re.IGNORECASE)
        count = f"（{m.group(1)} 人）" if m else ""
        body = out.split(":", 1)[1].strip() if ":" in out else out
        items = []
        for raw in body.splitlines():
            raw = raw.strip().rstrip(",")
            if not raw or raw.lower() in ("none", ""):
                continue
            if ":" in raw:
                name, _, reason = raw.partition(":")
                items.append(f"• {name.strip()}：{reason.strip()}")
            else:
                items.append(f"• {raw}")
        if not items:
            return "📋 封禁列表：当前没有被封禁的玩家"
        return f"📋 封禁列表{count}\n" + "\n".join(items)

    @staticmethod
    def _parse_color(value, fallback: str = "white") -> str:
        """把用户配置的颜色值统一为 MC 文本组件可用的 color。

        支持：hex（#FF0000 / FF0000）、RGB（255,0,0 / rgb(255,0,0)）、
        十进制 RGB（16711680）、MC 内置色名（red/gold/white...）。
        解析失败时回退到 fallback。
        """
        if value is None:
            return fallback
        v = str(value).strip().lower()
        if not v:
            return fallback
        # MC 内置命名颜色
        named = {
            "black", "dark_blue", "dark_green", "dark_aqua", "dark_red",
            "dark_purple", "gold", "gray", "dark_gray", "blue", "green",
            "aqua", "red", "light_purple", "yellow", "white",
        }
        if v in named:
            return v
        # #RRGGBB 或 RRGGBB
        m = re.fullmatch(r"#?([0-9a-f]{6})", v)
        if m:
            return "#" + m.group(1).upper()
        # rgb(255,0,0) 或 255,0,0
        m = re.fullmatch(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)", v) or \
            re.fullmatch(r"(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})", v)
        if m:
            r, g, b = (int(x) for x in m.groups())
            if all(0 <= x <= 255 for x in (r, g, b)):
                return f"#{r:02X}{g:02X}{b:02X}"
            return fallback
        # 十进制 RGB（0-16777215）
        if v.isdigit():
            n = int(v)
            if 0 <= n <= 0xFFFFFF:
                return f"#{n:06X}"
            return fallback
        return fallback

    # ================= 渐变色（MC 1.16+ 逐字符渐变 · 多锚点 + 多格式） =================

    # 渐变输出格式（key -> 说明）
    GRADIENT_FORMATS = {
        "json": "Vanilla（JSON 文本组件）",
        "compat_section": "Vanilla 兼容（§x§R§R§G§G§B§B）",
        "compat_amp": "Vanilla 兼容（&x&R&R&G&G&B&B）",
        "legacy_amp": "Legacy（&#RRGGBB，EssentialsX / CMI 等插件）",
    }

    @staticmethod
    def _gradient_colors(stops: list, steps: int) -> list:
        """等价 color-stepper::generateSteps：多锚点 RGB 分量线性插值。

        stops: 锚点色列表（>=1 个 #RRGGBB）；steps: 需要生成的颜色数量。
        算法：stepWidth=(n-1)/(steps-1)，逐点定位相邻锚色做 RGB 线性插值
        （r=start.r+(end.r-start.r)*t，g/b 同理），支持任意多锚点。
        """
        def _h2rgb(h):
            h = h.lstrip("#")
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

        def _rgb2h(rgb):
            return "#{:02X}{:02X}{:02X}".format(*rgb)

        if not stops or steps <= 0:
            return []
        rgb = [_h2rgb(c) for c in stops]
        if len(rgb) <= 1 or steps == 1:
            return [_rgb2h(rgb[len(rgb) // 2])]
        step_width = (len(rgb) - 1) / (steps - 1)
        out = []
        for i in range(steps):
            arg = i * step_width
            idx = int(arg)
            if idx >= len(rgb) - 1:
                out.append(_rgb2h(rgb[-1]))
                continue
            s, e = rgb[idx], rgb[idx + 1]
            t = arg - idx
            out.append(_rgb2h((
                round(s[0] + (e[0] - s[0]) * t),
                round(s[1] + (e[1] - s[1]) * t),
                round(s[2] + (e[2] - s[2]) * t),
            )))
        return out

    def _gradient_stops(self, raw, fallback: str) -> list:
        """解析渐变锚点列表（逗号/空格分隔的多颜色），返回至少 2 个合法颜色。"""
        parts = [p for p in re.split(r"[,，;；\s]+", str(raw or "")) if p.strip()]
        stops = []
        for p in parts:
            c = self._parse_color(p, "")
            if c:
                stops.append(c)
        if not stops:
            fb = self._parse_color(fallback, "white")
            return [fb, fb]
        if len(stops) == 1:
            return [stops[0], stops[0]]
        return stops

    @staticmethod
    def _hex_to_char_code(hex_color: str, char_code: str) -> str:
        """#FF0000 → §x§F§F§0§0§0§0（char_code 为 '§' 或 '&'）。"""
        body = "".join(f"{char_code}{c}" for c in hex_color.lstrip("#").upper())
        return f"{char_code}x{body}"

    def _gradient_sequence(self, text: str, stops: list) -> list:
        """逐字符分配渐变色：空白字符不染色、也不消耗渐变步数。

        返回 [(char, color|None), ...]，与输入字符一一对应。
        """
        chars = list(text)
        n_solid = sum(1 for c in chars if c.strip())
        if n_solid == 0:
            return [(c, None) for c in chars]
        colors = self._gradient_colors(stops, n_solid)
        seq = []
        i = 0
        for c in chars:
            if not c.strip():
                seq.append((c, None))
            else:
                seq.append((c, colors[i] if i < len(colors) else colors[-1]))
                i += 1
        return seq

    def _render_gradient(self, text: str, stops: list, fmt: str) -> str:
        """按指定格式渲染渐变文本。

        json: 返回完整文本组件；其余格式返回带字符码的纯文本
        （客户端渲染时会解析 § / & 字符码）。
        """
        seq = self._gradient_sequence(text, stops)
        if fmt == "compat_section":
            return "".join(
                c if col is None else self._hex_to_char_code(col, "§") + c
                for c, col in seq
            )
        if fmt == "compat_amp":
            return "".join(
                c if col is None else self._hex_to_char_code(col, "&") + c
                for c, col in seq
            )
        if fmt == "legacy_amp":
            return "".join(c if col is None else f"&{col}" + c for c, col in seq)
        extra = [
            {"text": c} if col is None else {"text": c, "color": col}
            for c, col in seq
        ]
        return json.dumps({"text": "", "extra": extra}, ensure_ascii=False)

    def _colored_payload(self, text: str, base_key: str, stops_key: str, default: str) -> str:
        """构建 JSON 文本组件字符串（供 tellraw / title 使用）。

        渐变色开关开启且锚点含多种颜色时逐字符渐变，否则退回单色。
        """
        base = self._parse_color(self._cfg(base_key, default), default)
        if self._cfg("gradient_enabled", False):
            stops = self._gradient_stops(self._cfg(stops_key, ""), base)
            fmt = str(self._cfg("gradient_format", "json") or "json").strip().lower()
            if fmt not in self.GRADIENT_FORMATS:
                fmt = "json"
            if len(set(stops)) > 1:
                rendered = self._render_gradient(text, stops, fmt)
                if fmt == "json":
                    return rendered
                return json.dumps({"text": rendered}, ensure_ascii=False)
        return json.dumps({"text": text, "color": base}, ensure_ascii=False)

    def detect_server_version(self) -> str:
        """探测服务端版本。

        原版/Forge 服务端没有 version 命令（那是 Bukkit/Paper 的），
        因此改为从服务端文件推断，依次尝试：
        1) logs/latest.log 的启动行（最准，含 MC + Forge 版本）
        2) libraries/net/minecraftforge/forge/<mc>-<forge> 目录名
        3) 根目录 forge-*.jar / server*.jar 文件名
        全部失败返回空字符串。
        """
        if self.is_remote_mode():
            # v0.21.15：异地 RCON 模式下读不到服务端文件，别假装能探测
            return ""
        sd = str(self._cfg("server_dir", "") or "").strip()
        if not sd:
            return ""
        root = Path(sd)
        if not root.is_dir():
            return ""
        # 1) 日志启动行
        try:
            log = root / "logs" / "latest.log"
            if log.is_file():
                with log.open("r", encoding="utf-8", errors="replace") as f:
                    head = f.read(200000)
                m = re.search(r"Forge mod loading, version ([\w.\-]+), for MC ([\d.]+)", head)
                if m:
                    return f"MC {m.group(2)} · Forge {m.group(1)}"
                m = re.search(r"Loading Minecraft ([\d.]+) with Forge ([\w.\-]+)", head)
                if m:
                    return f"MC {m.group(1)} · Forge {m.group(2)}"
                m = re.search(r"Starting minecraft server version ([\d.]+)", head)
                if m:
                    return f"MC {m.group(1)}"
        except Exception:
            pass
        # 2) libraries/net/minecraftforge/forge/<mc>-<forge>
        try:
            lib = root / "libraries" / "net" / "minecraftforge" / "forge"
            if lib.is_dir():
                names = sorted(d.name for d in lib.iterdir() if d.is_dir())
                if names:
                    latest = names[-1]
                    m = re.fullmatch(r"([\d.]+)-([\w.\-]+)", latest)
                    if m:
                        return f"MC {m.group(1)} · Forge {m.group(2)}"
                    return latest
        except Exception:
            pass
        # 3) jar 文件名
        try:
            for p in root.glob("forge-*.jar"):
                m = re.match(r"forge-([\d.]+)-([\w.\-]+?)(?:-installer)?\.jar$", p.name)
                if m:
                    return f"MC {m.group(1)} · Forge {m.group(2)}"
            for p in root.glob("server*.jar"):
                m = re.match(r"server[-.]?([\d.]+)\.jar$", p.name)
                if m:
                    return f"MC {m.group(1)}（原版）"
        except Exception:
            pass
        return ""

    # =============== v0.22.6（批次 2）版本能力上下文 ===============

    def _resolve_version_info(self):
        """解析服务端版本事实。优先级：**手动声明 > 文件探测 > 未知**（补丁 3）。

        为什么必须有手动声明这一层：异地 RCON 模式（remote_rcon_mode=true）
        读不到服务端文件，探测**必然**失败。若没有出口，所有附魔 / NBT 请求
        会被一律拒绝且**无法解除** —— 那是功能性倒退。
        手动声明是**用户显式提供的事实**，不违反「不得猜测」约束。
        """
        override = str(self._cfg("server_version_override", "") or "").strip()
        detected = ""
        if not override:
            # 声明了就不再探测：省一次文件 IO，也避免两处结论打架
            try:
                detected = self.detect_server_version()
            except Exception as e:  # noqa: BLE001
                self.logger.warning("服务端版本探测失败: %s", e)
        return resolve_version_info(override, detected)

    def _version_context_text(self) -> str:
        """产出注入 Agent 提示词的「版本约束片段」。

        探测失败时返回的是**显式降级指令**（禁止构造带数据命令 + 告诉用户去哪填），
        而不是空串 —— 空串等于让 LLM 凭记忆猜版本，正是本次事故的根因。
        """
        info = self._resolve_version_info()
        return build_version_context(info, self._cfg("item_syntax_override", "auto"))

    def _version_capabilities(self) -> dict:
        """给 WebUI / 概览用的版本能力快照（不含提示词正文）。"""
        info = self._resolve_version_info()
        return describe_capabilities(info, self._cfg("item_syntax_override", "auto"))

    def _preflatten_block_reason(self, command: str) -> str:
        """已知服务端 < 1.13 时，该命令能否**执行**？返回拒绝原因或空串（v0.23.2）。

        为什么要在代码侧拦、而不是只靠提示词：GPT 续单裁决 Q4 明确要求
        「1.12.2 + 附魔请求 → 不调用 rcon.command()」—— 提示词是软约束，
        静默生成错命令的代价太高（用户会以为发了、实际解析失败）。

        本函数是**纯版本判据**（版本解析 + 委托 ``preflatten_block_reason()``），
        所有执行入口请改调 :meth:`_guard_command_for_version`（带来源与日志）。
        """
        try:
            info = self._resolve_version_info()
        except Exception:  # noqa: BLE001
            return ""      # 版本解析异常不得阻断主流程
        if info.mc is None or tuple(info.mc) >= ITEM_PREFLATTEN_CUTOVER:
            return ""
        return preflatten_block_reason(command)

    def _guard_command_for_version(
        self, command: str, *, source: str = "llm_tool", manual: bool = False
    ) -> str:
        """**统一**版本能力守门：已知服务端 < 1.13 时，该命令能否执行？放行返回空串。

        v0.23.2 第二版（GPT 核验 P0-1 / P0-3）：**所有执行型入口**都必须过这里 ——
        LLM 工具（``mc_execute_command`` / ``mc_broadcast`` / ``mc_give_item``）、
        指令入口（``mcs_say`` / ``mcs_title_cmd`` / ``mcs_kick_cmd`` / ``mcs_ban_cmd`` /
        ``mcs_unban_cmd``）与工具入口（``mc_kick`` / ``mc_ban``）。
        工作流内部另有 ``MCWorkflow._preflatten_block()``，调用同一判据，两处口径一致。

        为什么 ``mc_execute_command`` 也要拦：它是 ``@filter.llm_tool``，
        命令字符串由 **LLM 生成**、不等于「用户手写」，同样可能把 1.13+ 语法
        发给 1.12 服务端 —— 这正是 GPT 核验点名的绕过路径。

        Args:
            command: 待执行命令（不含前导斜杠）。
            source: 调用来源，仅用于日志追溯（哪个入口拦下的）。
            manual: 是否「人工原始命令」通道（P1 预留）。当前无调用方传 True；
                将来若开放，应仅限管理员 + WebUI 明确警告 + 回执写明「不保证跨版本兼容」。
        """
        if manual:
            return ""
        try:
            reason = self._preflatten_block_reason(command)
        except Exception as e:  # noqa: BLE001
            self.logger.warning("版本能力守门异常（按放行处理）: %s", e)
            return ""
        if reason:
            self.logger.info(
                "[版本守门] 拦截 source=%s 命令=%s 原因=%s", source, command, reason
            )
            return (
                f"{reason}。\n"
                "本插件暂不支持该服务端版本的自动命令生成："
                "请手动执行适配该版本的命令，或把服务端升级到 1.13 及以上。\n"
                "如认为此判断有误（例如你确认该命令在旧版同样有效），"
                "请带上服务端版本与这条命令原文到项目 Issues 反馈。"
            )
        return ""

    @staticmethod
    def _format_uptime(seconds: float) -> str:
        """把秒数格式化为「X天X小时X分」。"""
        seconds = max(0, int(seconds))
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        if days:
            return f"{days}天{hours}小时{mins}分"
        if hours:
            return f"{hours}小时{mins}分{secs}秒"
        return f"{mins}分{secs}秒"

    @staticmethod
    def _parse_tps_output(text: str) -> tuple[float | None, float | None]:
        """从 spark/tps 类模组输出解析 (TPS, MSPT)。返回 (None, None) 表示解析失败。"""
        text = re.sub(r"§.", "", text)
        star_lines = [
            ln.strip() for ln in text.splitlines() if ln.strip().startswith("*:")
        ]
        if star_lines:
            # spark 格式：*: TPS, ...  / *: avg/min/max mspt, ...
            first = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", star_lines[0])]
            tps = first[0] if first else None
            mspt = None
            if len(star_lines) > 1:
                second = [
                    float(x) for x in re.findall(r"\d+(?:\.\d+)?", star_lines[1])
                ]
                if second:
                    mspt = second[0]
            return tps, mspt
        # forge tps 格式：Mean tick time: X ms. Mean TPS: Y（多维度 + Overall）
        tps_vals = [float(x) for x in re.findall(r"Mean TPS:\s*([\d.]+)", text)]
        tick_vals = [
            float(x) for x in re.findall(r"Mean tick time:\s*([\d.]+)", text)
        ]
        if tps_vals or tick_vals:
            # 取最后一个（Overall 汇总行）
            return (
                tps_vals[-1] if tps_vals else None,
                tick_vals[-1] if tick_vals else None,
            )
        # 非 spark 格式：只取最后一个冒号之后的数值（过滤 0~20 的 TPS 候选）
        tail = text.rsplit(":", 1)[-1] if ":" in text else text
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", tail)]
        cands = [n for n in nums if 0 <= n <= 20]
        return (cands[0] if cands else None), None

    async def _query_tps_mspt(self, rcon: AsyncRcon) -> dict | None:
        """尝试用服务器已装的 TPS 类模组命令获取 TPS/MSPT，全部不可用返回 None。"""
        for cmd in ("tps", "spark tps", "forge tps"):
            try:
                out = str(await rcon.command(cmd, timeout=3.0)).strip()
            except Exception:
                continue
            if not out:
                continue
            low = out.lower()
            if any(
                k in low
                for k in (
                    "unknown command",
                    "unknown or incomplete",
                    "unknown/incorrect",
                    "未知的命令",
                    "not found",
                )
            ):
                continue
            tps, mspt = self._parse_tps_output(out)
            if tps is not None:
                return {"tps": tps, "mspt": mspt, "source": cmd}
        return None

    def _find_server_process(self):
        """通过命令行/工作目录定位本机 Minecraft 服务端进程（同机部署场景）。"""
        if psutil is None:
            return None
        server_dir = str(self._cfg("server_dir", "") or "").lower()
        candidates = []
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                    name = (proc.info.get("name") or "").lower()
                    cwd = ""
                    try:
                        cwd = (proc.cwd() or "").lower()
                    except Exception:
                        pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if "java" not in name and "java" not in cmdline:
                    continue
                if server_dir and (
                    server_dir in cmdline or cwd.startswith(server_dir)
                ):
                    return proc
                candidates.append((proc, cmdline))
        except Exception:
            return None
        # 回退：优先取带 -jar 启动的 java 进程
        for proc, cmdline in candidates:
            if "-jar" in cmdline:
                return proc
        return None

    async def _server_process_metrics(self) -> dict | None:
        """采集本机服务端进程的 CPU/内存/运行时长（psutil，非阻塞采样）。

        v0.21.15：异地 RCON 模式下服务端进程在另一台机器上，本机 psutil 看不到，
        直接返回 None（调用方会显示明确的「不可用」原因，而不是甩一串假数字）。
        """
        if self.is_remote_mode():
            return None
        if psutil is None:
            return None
        proc = await asyncio.to_thread(self._find_server_process)
        if proc is None:
            return None
        try:
            with proc.oneshot():
                proc.cpu_percent(None)  # 首次调用仅建立基线
                rss = proc.memory_info().rss
                create_time = proc.create_time()
                cmdline = " ".join(proc.cmdline() or [])
            await asyncio.sleep(1.0)  # 等待 1s 采样窗口
            cpu = proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            return None
        xmx = self._resolve_xmx(cmdline)
        return {
            "pid": proc.pid,
            "rss_mb": round(rss / 1024 / 1024, 1),
            "cpu_percent": round(cpu, 1),
            "uptime": self._format_uptime(time.time() - create_time),
            "xmx": xmx,
        }

    def _resolve_xmx(self, cmdline: str) -> str:
        """解析 JVM 最大堆内存（-Xmx，可能位于 user_jvm_args.txt/启动脚本）。"""
        m = re.search(r"-Xmx(\d+)([mMgG])", cmdline)
        if m:
            val = int(m.group(1))
            return f"{val}MB" if m.group(2).lower() == "m" else f"{val}GB"
        server_dir = str(self._cfg("server_dir", "") or "")
        for fname in ("user_jvm_args.txt", "run.bat", "start.bat", "启动.bat"):
            try:
                text = (Path(server_dir) / fname).read_text(
                    encoding="utf-8", errors="ignore"
                )
            except Exception:
                continue
            m = re.search(r"-Xmx(\d+)([mMgG])", text)
            if m:
                val = int(m.group(1))
                return f"{val}MB" if m.group(2).lower() == "m" else f"{val}GB"
        return "未知"

    @filter.llm_tool(name="mc_kick")
    @tolerant_tool
    async def mc_kick(
        self, event: AstrMessageEvent, player: str, reason: str = ""
    ):
        """将指定玩家踢出服务器（仅管理员可调用）。

        Args:
            player(string): 玩家名
            reason(string): 踢出原因，可留空
        """
        if not self._tool_enabled("mc_kick"):
            return "该功能已在插件配置中停用。"
        denied = self._admin_gate(event, tool="mc_kick", action="踢人")
        if denied:
            return denied
        try:
            rcon = await self._get_rcon()
            cmd = f"kick {player} {reason}".strip() if reason else f"kick {player}"
            blocked = self._guard_command_for_version(cmd, source="mc_kick")
            if blocked:
                return blocked
            r = await self._exec_checked(rcon, cmd)
            if r.status == "success":
                await self._send_feedback(rcon, f"已将 {player} 踢出服务器")
            if r.status == "unknown":
                return f"踢出结果未知：{player}｜{r.reason}（请用 状态/在线列表 确认后决定是否重发）"
            return self._render_result(r, f"已踢出 {player}", "踢出失败")
        except RconError as e:
            return f"操作失败：{e}"

    @filter.llm_tool(name="mc_ban")
    @tolerant_tool
    async def mc_ban(
        self, event: AstrMessageEvent, player: str, reason: str = ""
    ):
        """封禁指定玩家（仅管理员可调用）。

        Args:
            player(string): 玩家名
            reason(string): 封禁原因，可留空
        """
        if not self._tool_enabled("mc_ban"):
            return "该功能已在插件配置中停用。"
        denied = self._admin_gate(event, tool="mc_ban", action="封禁")
        if denied:
            return denied
        try:
            rcon = await self._get_rcon()
            cmd = f"ban {player} {reason}".strip() if reason else f"ban {player}"
            blocked = self._guard_command_for_version(cmd, source="mc_ban")
            if blocked:
                return blocked
            r = await self._exec_checked(rcon, cmd)
            if r.status == "success":
                await self._send_feedback(rcon, f"已将 {player} 封禁")
            if r.status == "unknown":
                return f"封禁结果未知：{player}｜{r.reason}（请用 封禁列表 确认后再决定是否重发）"
            return self._render_result(r, f"已封禁 {player}", "封禁失败")
        except RconError as e:
            return f"操作失败：{e}"

    @filter.llm_tool(name="mc_connection_status")
    @tolerant_tool
    async def mc_connection_status(self, event: AstrMessageEvent):
        """测试与 Minecraft 服务器的 RCON 连接是否正常。
        """
        try:
            rcon = await self._get_rcon()
            out = await rcon.command("list")
            return f"RCON 连接正常。服务器返回：{out}"
        except RconError as e:
            return f"RCON 连接异常：{e}"

    # ================= 热重载工具（v0.16.0：更新免重启） =================
    # v0.21.6：聊天界面不再有 mcs 热重载 指令；本工具只作开发/调试用途（管理员限定）

    @filter.llm_tool(name="mc_reload_plugin")
    @tolerant_tool
    async def mc_reload_plugin(self, event: AstrMessageEvent, plugin_name: str = ""):
        """重载一个 AstrBot 插件以应用最新代码，无需重启 AstrBot。仅管理员可调用。

        仅供开发/调试：正规做法是在 AstrBot 插件管理页点「重载」。重载前会深度清理该插件的模块缓存与 __pycache__（含 sys.path 引入的模块）。

        Args:
            plugin_name(string): 可选。插件名或目录名，例如 astrbot_plugin_Scintilla_MC_Server_Control；留空 = 全量重载所有插件
        """
        if not self._tool_enabled("mc_reload_plugin"):
            return "该功能已在插件配置中停用。"
        denied = self._admin_gate(event, tool="mc_reload_plugin", action="热重载")
        if denied:
            return denied
        return self._schedule_hot_reload((plugin_name or "").strip() or None)

    # ================= 整合包特化：词典工具 =================

    @filter.llm_tool(name="mc_list_mods")
    @tolerant_tool
    async def mc_list_mods(self, event: AstrMessageEvent):
        """列出 Minecraft 服务器当前安装的全部 Mod（ID 与显示名）。
        """
        if not self._tool_enabled("mc_list_mods"):
            return "该功能已在插件配置中停用。"
        _gate = self._local_gate("Mod 列表查询（mc_list_mods）", "服务端 mods/ 目录")
        if _gate:
            return _gate
        if not self._dictionary:
            return "物品词典未初始化：请先在插件配置中填写 server_dir（保存设置后即刻生效，无需重载插件）。"
        mods = self._dictionary.mods
        if not mods:
            return "服务器当前未安装任何 Mod（纯原版）。"
        lines = [f"- {m['id']} ({m['name']})" for m in mods]
        return f"已安装 Mod 共 {len(mods)} 个：\n" + "\n".join(lines)

    @filter.llm_tool(name="mc_search_item")
    @tolerant_tool
    async def mc_search_item(self, event: AstrMessageEvent, keyword: str):
        """搜索 Minecraft 服务器物品的精确 ID，支持中文名、英文名或 ID 片段。

        构造 give/summon 等命令前若不确定物品 ID，先用本工具查询；复杂任务优先用 mc_workflow，例如数值计算（配比/产量/耗材等）、满配枪械、复杂 NBT。

        Args:
            keyword(string): 搜索关键词，例如 "黄铜锭"、"brass"、"create:brass_ingot"
        """
        if not self._tool_enabled("mc_search_item"):
            return "该功能已在插件配置中停用。"
        _gate = self._local_gate("物品 ID 搜索（mc_search_item）", "服务端 mods/*.jar")
        if _gate:
            return _gate
        if not self._dictionary:
            return "物品词典未初始化：请先在插件配置中填写 server_dir（保存设置后即刻生效，无需重载插件）。"
        results = self._dictionary.search_items(keyword)
        if not results:
            return (
                f"词典中未找到与「{keyword}」相关的物品。建议："
                f"1) 尝试英文名或更短的关键词；"
                f"2) 用网络搜索「<Mod名> <物品名> item id」获取准确 ID。"
                f"提示：若目标是数值计算（配比/产量/耗材等）、模组枪械满配、复杂 NBT 构造等复杂任务，建议改用 mc_workflow 工具走完整流水线。"
            )
        lines = []
        for r in results:
            names = " / ".join(x for x in (r.get("zh"), r.get("en")) if x)
            lines.append(f"- {r['id']}  （{names}）")
        return f"找到 {len(results)} 个匹配物品：\n" + "\n".join(lines)

    @filter.llm_tool(name="mc_get_recipes")
    @tolerant_tool
    async def mc_get_recipes(
        self, event: AstrMessageEvent, item: str, direction: str = "forward"
    ):
        """查询 Minecraft 物品的合成配方。正向=该物品怎么造；反向=它能用来造什么。

        Args:
            item(string): 物品 ID，例如 "create:brass_ingot"
            direction(string): forward=查询该物品的合成方法（默认）；reverse=查询该物品能作为原料合成什么
        """
        if not self._tool_enabled("mc_get_recipes"):
            return "该功能已在插件配置中停用。"
        _gate = self._local_gate("配方查询（mc_get_recipes）", "服务端 mods/*.jar")
        if _gate:
            return _gate
        if not self._dictionary:
            return "物品词典未初始化：请先在插件配置中填写 server_dir（保存设置后即刻生效，无需重载插件）。"
        recipes = self._dictionary.get_recipes(item, direction)
        if not recipes:
            hint = "可能不是配方产物，或未在词典中收录。" if direction == "forward" else "没有找到以它为原料的配方。"
            return f"未找到「{item}」的相关配方（{hint}）"
        lines = []
        for r in recipes[:10]:
            inputs = " + ".join(r["inputs"]) if r["inputs"] else "（无原料/未收录）"
            lines.append(f"{r['output']}×{r['count']} ← {inputs}  [{r['type']}]")
        head = "合成方法：" if direction == "forward" else "可用来合成："
        return f"「{item}」{head}\n" + "\n".join(lines)

    @filter.llm_tool(name="mc_rescan_dictionary")
    @tolerant_tool
    async def mc_rescan_dictionary(self, event: AstrMessageEvent):
        """重新扫描服务端目录（mods/ 或 plugins/）并重建物品词典。仅管理员可调用。

        更换整合包、新增 Mod 或插件后使用。
        """
        denied = self._admin_gate(event, tool="mc_rescan_dictionary", action="重建词典")
        if denied:
            return denied
        _gate = self._local_gate("词典重建（mc_rescan_dictionary）", "服务端 mods/*.jar")
        if _gate:
            return _gate
        if not self._dictionary:
            return "物品词典未初始化：请先在插件配置中填写 server_dir（保存设置后即刻生效，无需重载插件）。"
        stats = await asyncio.to_thread(self._dictionary.build)
        return (
            f"词典重建完成：{stats['mods']} 个 Mod，{stats['items']} 个物品，"
            f"{stats['recipes']} 条配方。"
        )

    # ================= 学习型知识库工具（LLM 智能路由） =================

    @filter.llm_tool(name="mc_search_knowledge")
    @tolerant_tool
    async def mc_search_knowledge(self, event: AstrMessageEvent, topic: str):
        """检索本整合包的模组知识库（已沉淀的经验：物品ID、NBT格式、枪械配件方案、任务信息等）。

        不确定模组细节时先查库；查不到再自行推理，推理成功后用 mc_save_knowledge 沉淀。

        Args:
            topic(string): 检索主题，例如 "tac 枪械 附件 nbt"、"黄铜锭"、"FTB Quests 村民任务"
        """
        if not self._tool_enabled("mc_search_knowledge"):
            return "该功能已在插件配置中停用。"
        _gate = self._local_gate("知识库检索（mc_search_knowledge）", "服务端 mods/ 目录（用于算整合包指纹）")
        if _gate:
            return _gate
        if not self._knowledge:
            return "知识库未初始化：请先在插件配置中填写 server_dir（保存设置后即刻生效，无需重载插件）。"
        if not self._knowledge.state.enabled:
            return "知识库能力已停用（总开关关闭），本任务直接走硬推理。"
        warn = ""
        try:
            _st = self._knowledge.stats()
            if _st.get("match") is False:
                warn = (
                    f"⚠ 提醒：当前知识库预设「{_st.get('preset_name')}」绑定的指纹 "
                    f"{_st.get('fingerprint')} 与当前服务端指纹 {_st.get('server_id')} 不一致，"
                    f"以下沉淀经验可能不适用于当前整合包，请谨慎采用（建议改用 mc_workflow 重新推导，"
                    f"或让管理员在 WebUI 知识库页切换/绑定预设）。\n\n"
                )
        except Exception:
            warn = ""
        results = await self._knowledge.asearch(topic)
        if not results:
            return (
                f"知识库中未找到与「{topic}」相关的沉淀知识。"
                f"可自行推理，成功后调用 mc_save_knowledge 沉淀经验。"
                f"提示：若该任务属于数值计算（配比/产量/耗材等）、模组枪械满配、复杂 NBT 构造、批量/混合指令等复杂场景，"
                f"建议改用 mc_workflow 工具走完整流水线，效果更佳。"
            )
        lines = []
        for r in results:
            status_mark = {"verified": "✓已验证", "untested": "○未验证", "corrected": "✎已修正"}.get(r["status"], r["status"])
            kind_mark = "【模板·可泛化到同类物品】" if r.get("kind") == "template" else "【实例】"
            lines.append(
                f"【{r['topic']}】({status_mark}) mod={r.get('mod','general')} {kind_mark}\n{r['content']}"
            )
        return warn + "\n\n".join(lines)

    @filter.llm_tool(name="mc_save_knowledge")
    @tolerant_tool
    async def mc_save_knowledge(
        self, event: AstrMessageEvent, topic: str, content: str
    ):
        """把一次成功的推理经验沉淀进知识库（学习闭环）。仅管理员可调用。

        Args:
            topic(string): 知识主题，例如 "tac 满改 sig_mcx_spear nbt 方案"
            content(string): 知识内容（含物品ID、NBT格式、命令示例、注意事项等关键细节）
        """
        if not self._tool_enabled("mc_save_knowledge"):
            return "该功能已在插件配置中停用。"
        denied = self._admin_gate(event, tool="mc_save_knowledge", action="沉淀知识")
        if denied:
            return denied
        _gate = self._local_gate("知识沉淀（mc_save_knowledge）", "服务端 mods/ 目录（用于算整合包指纹）")
        if _gate:
            return _gate
        if not self._knowledge:
            return "知识库未初始化：请先在插件配置中填写 server_dir（保存设置后即刻生效，无需重载插件）。"
        if not self._knowledge.state.enabled:
            return "知识库能力已停用（总开关关闭），无法写入。"
        if not self._knowledge.state.learning:
            return "知识库学习已关闭，不会写入任何知识。"
        entry = self._knowledge.save_entry(topic, content, source="llm_learn")
        self._kb_touch_vectors()
        if entry["status"] == "pending":
            return (
                f"⏳ 知识已提交【{entry['topic']}】待管理员审批（自动应用已关闭）。\n"
                f"审批通过后才会生效。\n{content}"
            )
        return (
            f"✓ 知识已沉淀【{entry['topic']}】（状态: {entry['status']}）\n"
            f"{content}"
        )

    @filter.llm_tool(name="mc_correct_knowledge")
    @tolerant_tool
    async def mc_correct_knowledge(
        self, event: AstrMessageEvent, topic: str, correction: str
    ):
        """用正确的方案覆盖知识库中的错误条目（纠错闭环）。仅管理员可调用。

        Args:
            topic(string): 要纠正的知识主题，例如 "tac 满改 sig_mcx_spear nbt 方案"
            correction(string): 修正后的正确知识内容
        """
        if not self._tool_enabled("mc_correct_knowledge"):
            return "该功能已在插件配置中停用。"
        denied = self._admin_gate(event, tool="mc_correct_knowledge", action="纠正知识")
        if denied:
            return denied
        _gate = self._local_gate("知识纠错（mc_correct_knowledge）", "服务端 mods/ 目录（用于算整合包指纹）")
        if _gate:
            return _gate
        if not self._knowledge:
            return "知识库未初始化：请先在插件配置中填写 server_dir（保存设置后即刻生效，无需重载插件）。"
        if not self._knowledge.state.enabled:
            return "知识库能力已停用（总开关关闭），无法纠正。"
        if not self._knowledge.state.learning:
            return "知识库学习已关闭，无法纠正写入。"
        entry = self._knowledge.correct_entry(topic, correction)
        if entry is None:
            return f"知识库中不存在「{topic}」，请用 mc_save_knowledge 新增。"
        self._kb_touch_vectors()
        return (
            f"✓ 知识已纠正【{entry['topic']}】（状态: {entry['status']}）\n"
            f"{correction}"
        )

    # ================= 聊天桥接（游戏 → 群聊） =================

    # 可作为桥接来源的事件类型：普通聊天 / 私聊 / /me 动作
    _BRIDGE_SOURCES = (EVENT_CHAT, EVENT_WHISPER, EVENT_ME)

    @staticmethod
    def _norm_symbol(s: str, ignore_case: bool = True) -> str:
        """规范化触发符号：全角→半角（NFKC）；大小写是否折叠由 ignore_case 决定。

        全角/半角属于输入法差异，始终归一（中文状态下打出的全角「！」与半角「!」等价）；
        大小写是否敏感由配置 chat_bridge_case_sensitive 控制（v0.17.8）：
        默认忽略大小写，「Q」与「q」等价；开启后必须大小写完全一致。
        """
        try:
            norm = unicodedata.normalize("NFKC", s)
        except Exception:
            norm = s
        return norm.casefold() if ignore_case else norm

    def _bridge_case_sensitive(self) -> bool:
        """是否区分大小写（默认 False：不区分）。"""
        return bool(self._cfg("chat_bridge_case_sensitive", False))

    def _prefix_len_in_text(self, text: str, norm_prefix: str, ignore_case: bool) -> int:
        """返回原文中「实际占用」的字符数，使切片不会因 NFKC/casefold 变形而切歪。

        正常情况下归一化不改变长度，直接返回 len(norm_prefix) 对应的原文长度；
        仅当原文中命中前缀的归一化长度与配置前缀不一致时（如「㍿」「ß」），
        逐字符累积定位真实边界。
        """
        if len(self._norm_symbol(text[:len(norm_prefix)], ignore_case)) == len(norm_prefix):
            return len(norm_prefix)
        acc = ""
        for i, ch in enumerate(text):
            acc += ch
            if len(self._norm_symbol(acc, ignore_case)) >= len(norm_prefix):
                return i + 1
        return len(text)

    def _bridge_prefixes(self) -> list:
        """解析配置中的触发前缀（支持逗号/竖线分隔多个别名，如「!」或「!|！」）。"""
        raw = str(self._cfg("chat_bridge_prefix", "!") or "")
        return [p for p in re.split(r"[,，|｜]", raw) if p.strip()]

    def _match_bridge_prefix(self, message: str) -> tuple[bool, str]:
        """判断消息是否命中桥接前缀，返回 (是否命中, 剥离后正文)。

        - 前缀留空 = 转发全部聊天；
        - 始终兼容全角/半角（NFKC），无需切换输入法；
        - 大小写是否敏感由 chat_bridge_case_sensitive 控制（默认不区分，Q 与 q 等价）；
        - 前缀后的分隔符（空格/冒号/逗号/破折号等）可省略：
              !123   ！123   ! 123   !：123   !-123   →   正文均为「123」
        """
        text = (message or "").strip()
        prefixes = self._bridge_prefixes()
        if not prefixes:
            return True, text
        ignore_case = not self._bridge_case_sensitive()
        norm_text = self._norm_symbol(text, ignore_case)
        for p in prefixes:
            norm_prefix = self._norm_symbol(p, ignore_case)
            if not norm_prefix:
                continue
            if norm_text.startswith(norm_prefix):
                # 按命中前缀在原文中的真实长度切片，正文保持玩家原本输入的内容
                cut = self._prefix_len_in_text(text, norm_prefix, ignore_case)
                rest = text[cut:].lstrip(" \t:：,，;；-—~～·>")
                return True, rest.strip()
        return False, ""

    async def _maybe_bridge_to_group(self, etype: str, player: str, detail: str) -> bool:
        """聊天桥接主流程：命中前缀则转发到指定会话，返回是否已处理。

        载体（由玩家在游戏内自由选择，日志层不区分）：
            Steve: !今晚八点开团              ← 普通聊天（主推：全服可见，一目了然）
            Steve: ！今晚八点开团             ← 全角符号同样识别（中文输入法免切换）
            * Steve !今晚八点开团             ← /me 动作（全服可见，带 * 前缀）
            /msg @s !今晚八点开团             ← 私聊自己（附带能力：仅自己可见）
        全服可见是有意设计——让在线玩家都看到这条消息是发给群聊的。
        """
        if etype not in self._BRIDGE_SOURCES:
            return False
        if not self._cfg("chat_bridge_enabled", False):
            return False
        hit, message = self._match_bridge_prefix(detail)
        if not hit:
            return False
        if not message:
            return False
        targets = self._cfg("chat_bridge_targets", []) or self._cfg("notify_targets", [])
        if not targets:
            return False
        tpl = str(self._cfg("chat_bridge_format", "[MC] {player}：{message}") or "")
        text = tpl.replace("{player}", str(player)).replace("{message}", message)
        chain = MessageChain().message(text)
        delivered = await self._send_to_targets(targets, chain)
        if delivered and self._cfg("chat_bridge_receipt", False):
            await self._send_bridge_receipt(player)
        return delivered

    async def _send_bridge_receipt(self, player: str) -> None:
        """给转发者本人一条游戏内回执（仅其本人可见，不影响他人）。

        用灰色（#AAAAAA）而非高亮绿：这只是「已完成转发」的安静确认，
        不需要抢眼，避免刷屏干扰玩家视线。
        """
        if not re.fullmatch(r"[A-Za-z0-9_]{1,16}", player or ""):
            return
        try:
            rcon = await self._get_rcon()
            payload = json.dumps(
                {"text": "✓ 已转发到群聊", "color": "#AAAAAA"}, ensure_ascii=False
            )
            await rcon.command(f"tellraw @a[name={player}] {payload}")
        except Exception:
            pass

    # ================= 服务器事件推送 ================

    async def _on_server_event(self, etype: str, player: str, detail: str):
        """日志监听回调：记录最近事件 + 聊天桥接 + 按配置开关决定是否推送。"""
        # 无论是否配置播报，都缓存最近事件供 WebUI 展示
        try:
            self._recent_events.appendleft({
                "ts": time.strftime("%m-%d %H:%M:%S"),
                "type": etype,
                "player": player,
                "detail": detail,
            })
        except Exception:
            pass
        # 聊天桥接（游戏 → 群）：命中的消息由桥接推送，跳过常规播报避免重复
        try:
            if await self._maybe_bridge_to_group(etype, player, detail):
                return
        except Exception as e:
            self.logger.warning("聊天桥接处理异常: %s", e)
        targets = self._cfg("notify_targets", [])
        if not targets or not self._cfg("enable_event_listener", False):
            return
        text = ""
        group = self.EVENT_TO_NOTIFY_GROUP.get(etype, "")
        if etype == EVENT_CHAT and self._cfg("notify_chat", False):
            text = f"💬 [{player}] {detail}"
        elif etype == EVENT_JOIN and self._cfg("notify_join_leave", True):
            text = f"🟢 {player} 加入了游戏"
        elif etype == EVENT_LEAVE and self._cfg("notify_join_leave", True):
            text = f"🔴 {player} 离开了游戏"
        elif etype == EVENT_DEATH and self._cfg("notify_death", True):
            text = f"💀 {detail}"
        elif etype == EVENT_ADVANCEMENT and self._cfg("notify_advancement", True):
            text = f"🏆 {detail}"
        elif etype == EVENT_COMMAND and self._cfg("notify_command", True):
            # 原版格式 detail 带斜杠（/gamemode creative）；整合包格式为指令反馈文案
            text = f"⌨ [{player}] {detail}"
        if not text:
            return
        # v0.17.0：会话级过滤 —— 总开关 + 类型开关之后，再看每个会话勾选了哪些内容
        targets = [t for t in targets if self._notify_target_allows(t, group)]
        if not targets:
            return
        chain = MessageChain().message(text)
        await self._send_to_targets(targets, chain)
