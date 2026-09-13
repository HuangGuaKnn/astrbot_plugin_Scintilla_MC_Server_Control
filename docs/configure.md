# 配置详解

[返回 README](../README.md) · [界面效果](gallery.md) · [使用指南](usage.md) · [常见问题](faq.md)

插件全部配置共 **10 个分组、78 项**。多数情况下只需要填「连接与服务端」里的 RCON 信息，其余保持默认即可使用。

> 本页每一项的说明与插件内的提示文字一致。「默认」标注的是开箱值。

## 分组导航

- **连接与服务端**（`connection`）
- **权限与安全**（`permission`）
- **事件播报（游戏 → 会话）**（`notify`）
- **聊天桥接（游戏 → 群聊）**（`bridge`）
- **外部平台指令 · mcs 指令组**（`commands`）
- **AI 工具（LLM 可调用）**（`tools`）
- **词典与知识库**（`knowledge`）
- **外观 · 反馈与渐变颜色**（`appearance`）
- **多 Agent 工作流**（`workflow`）
- **多 Agent 提示词**（`prompts`）

## 1. 连接与服务端

`connection` · 共 6 项

**`remote_rcon_mode`** — 异地 RCON 模式（AstrBot 与服务端不同机、只映射 RCON 端口）  
<sub>开关 · 默认：关</sub>

> 开启后只走 RCON，可以不填「服务端目录」；但依赖本地文件的整块能力会按设计禁用：物品词典、知识库、服务器事件播报、版本探测、进程指标。终端与 WebUI 都会明确告知。

**`server_dir`** — 服务端目录  
<sub>文本 · 默认：空</sub>

> 服务端根目录（整合包 / 插件服务端解压后的那一层），例如 D:\Minecraft\Servers\MyPack；读取 logs/latest.log 、mods/ 、plugins/ 均以它为根。保存时会做结构校验（logs/ 、libraries/ 、server.properties 、eula.txt 等特征），不像服务端根目录会拒绝保存。

**`rcon_host`** — RCON 主机地址  
<sub>文本 · 默认：127.0.0.1</sub>

> 服务器与本机同机时填 127.0.0.1

**`rcon_port`** — RCON 端口  
<sub>整数 · 默认：25575</sub>

> 与 server.properties 中的 rcon.port 一致（默认 25575）

**`rcon_password`** — RCON 密码  
<sub>文本 · 默认：空</sub>

> 与 server.properties 中的 rcon.password 一致；以明文保存在插件配置中，请勿泄露

**`rcon_timeout`** — RCON 超时（秒）  
<sub>小数 · 默认：5</sub>

> 单条命令的最长等待时间，0.1 ~ 300

## 2. 权限与安全

`permission` · 共 6 项

**`admin_ids`** — 管理员 ID 列表  
<sub>列表 · 默认：空</sub>

> 拥有全部权限（含危险命令）的用户 ID；群聊里填群号或 QQ 号，WebChat 里填昵称。例如 ["Steve", "123456789"]

**`danger_command_policy`** — 命令工具白名单 / 黑名单  
<sub>文本 · 默认：whitelist</sub>

可选值：`whitelist` / `blacklist`

> 只管「命令工具」：mc_execute_command（执行指令）、mc_give_item（发物品）、mc_broadcast（广播）；喊话、状态、查询、绑定这类插件自带功能不受本策略影响（想限制喊话请改 say_command_public）。whitelist=白名单（默认）：非管理员完全不能使用命令工具；blacklist=黑名单：所有玩家都能使用命令工具，但危险命令（stop/op/ban/kick/whitelist…）与权限等级 3/4 的服务器管理命令仍仅管理员可用

**`permission_hint_injection`** — 权限前置提醒（注入提示词）  
<sub>开关 · 默认：开</sub>

> 开启后，非管理员发起对话时会在系统提示词里追加一段说明：哪些命令工具会被直接拒绝、有哪些替代方式。比事后拦截更早一步，从源头减少「明知不可为而硬试」。关闭则只在调用被拦时才告知。

**`permission_latch`** — 拦截后会话闩锁  
<sub>开关 · 默认：开</sub>

> 开启后，同一会话内一旦有命令被权限闸门拦下，有效期内后续命令类工具调用会被直接短路（不再触达闸门与服务器），并提示 AI 立刻停手。可有效防止 AI「换个参数继续硬试」。

**`permission_latch_ttl`** — 闩锁有效期（秒）  
<sub>整数 · 默认：300</sub>

> 被拦后闩锁保持多久，默认 300 秒（5 分钟），到期自动解除。

**`ban_default_reason`** — 封禁默认理由  
<sub>文本 · 默认：违反服务器规则，由管理员封禁</sub>

> mcs 封禁 未附带理由时使用的预设文案

## 3. 事件播报（游戏 → 会话）

`notify` · 共 8 项

**`enable_event_listener`** — 服务器事件转发  
<sub>开关 · 默认：关</sub>

> 监听服务器日志并记录最近的指定事件，并转发到指定会话（目标见下方 notify_targets）；关闭后下方各类型开关都不生效

**`notify_join_leave`** — 播报：玩家加入 / 离开  
<sub>开关 · 默认：开</sub>

> 玩家进出服务器时推送到播报目标

**`notify_death`** — 播报：玩家死亡  
<sub>开关 · 默认：开</sub>

> 玩家死亡时推送到播报目标

**`notify_advancement`** — 播报：成就 / 进度  
<sub>开关 · 默认：开</sub>

> 玩家解锁成就、完成挑战或达成目标时推送

**`notify_command`** — 播报：玩家指令调用  
<sub>开关 · 默认：开</sub>

> 玩家在游戏内执行指令时推送。原版服务端可拿到指令原文（如 /gamemode）；少数整合包只在日志里写指令反馈文案，此时播报该文案。插件自身经 RCON 下发的指令不计入；完全无反馈的静默指令也不会出现

**`notify_chat`** — 播报：服务器聊天  
<sub>开关 · 默认：关</sub>

> 服务器内聊天内容实时转发到播报目标

**`notify_targets`** — 播报目标会话  
<sub>列表 · 默认：空</sub>

> 格式：平台ID:消息类型:会话ID（如 aiocqhttp:FriendMessage:123456789）；WebUI 设置页提供「选类型 + 填号码」选择器自动生成；也支持简写 私聊:123456789 / 群聊:987654321（缺平台 ID 自动补全）；留空则不播报

**`notify_target_events`** — 各会话的播报内容（WebUI 维护）  
<sub>对象 · 默认：空</sub>

> 由 WebUI「播报目标」列表中点击会话展开的子菜单维护：{会话标识: [事件组]}，事件组取 join_leave / chat / death / advancement / command；未列出的会话默认全开。推送需 总开关 + 类型开关 + 目标 + 该会话勾选的事件 四者齐备。

## 4. 聊天桥接（游戏 → 群聊）

`bridge` · 共 6 项

**`chat_bridge_enabled`** — 桥接总开关  
<sub>开关 · 默认：关</sub>

> 开启后游戏内发送带符号的聊天（如 !123）即转发到指定会话

**`chat_bridge_prefix`** — 触发符号（可多个，用 | 分隔）  
<sub>文本 · 默认：!</sub>

> 默认「!」：游戏内直接打 !123 或全角 ！123（中间无需空格），自动兼容全角/半角，大小写是否敏感见「区分大小写」；留空 = 转发全部聊天

**`chat_bridge_targets`** — 桥接目标会话  
<sub>列表 · 默认：空</sub>

> 格式同上（平台ID:消息类型:会话ID，支持 私聊:123456789 / 群聊:987654321 简写）；留空则沿用「播报目标会话」。推荐在目标会话里发 mcs 桥接 自动绑定

**`chat_bridge_format`** — 群内显示模板  
<sub>文本 · 默认：[MC] {player}：{message}</sub>

> 占位符：{player}=玩家名、{message}=正文；例如 [MC] {player}：{message}

**`chat_bridge_receipt`** — 游戏内转发回执  
<sub>开关 · 默认：关</sub>

> 玩家聊天转发至群聊成功后，将以仅该玩家可见的形式在游戏内发送一条确认信息

**`chat_bridge_case_sensitive`** — 区分大小写  
<sub>开关 · 默认：关</sub>

> 关闭（默认）时符号大小写不敏感，Q 与 q 等价；开启后必须大小写完全一致才算命中。全角/半角始终兼容，这一项只影响英文字母的大小写

## 5. 外部平台指令 · mcs 指令组

`commands` · 共 10 项

**`enable_bind_command`** — 绑定  
<sub>开关 · 默认：开</sub>

> mcs 绑定 <MC玩家ID> / mcs 解绑 / mcs 查询；未指名玩家的任务会自动使用已绑定的游戏名

**`enable_say_command`** — 喊话  
<sub>开关 · 默认：开</sub>

> mcs 喊话 <内容>：把群内消息转发到服务器聊天栏，自动带上发送者昵称

**`say_command_public`** — 喊话对全员开放  
<sub>开关 · 默认：开</sub>

> 打开 = 所有群成员可喊话；关闭 = 仅管理员可喊话

**`enable_status_command`** — 状态  
<sub>开关 · 默认：开</sub>

> mcs 状态：查询 mspt / tps / 在线人数 / 运行时长 / 内存 / CPU（TPS 依赖 Spark 等模组，内存 CPU 需同机部署）

**`enable_kick_command`** — 踢人（管理员）  
<sub>开关 · 默认：开</sub>

> mcs 踢人 <MC玩家ID> [理由]

**`enable_ban_command`** — 封禁（管理员）  
<sub>开关 · 默认：开</sub>

> mcs 封禁 <MC玩家ID> [理由]

**`enable_unban_command`** — 解封（管理员）  
<sub>开关 · 默认：开</sub>

> mcs 解封 <MC玩家ID>

**`enable_banlist_command`** — 封禁列表（管理员）  
<sub>开关 · 默认：开</sub>

> mcs 封禁列表：查看当前被封禁的玩家

**`enable_help_command`** — 帮助  
<sub>开关 · 默认：开</sub>

> mcs 帮助：查看指令组全部用法，管理员专属指令会标注（管理员）

**`enable_title_command`** — 全屏喊话（管理员）  
<sub>开关 · 默认：开</sub>

> mcs 全屏喊话 <内容>：用 title @a title 全屏显示给所有人

## 6. AI 工具（LLM 可调用）

`tools` · 共 12 项

**`enable_mc_execute_command`** — 万能命令执行  
<sub>开关 · 默认：开</sub>

> mc_execute_command：执行任意一条服务器命令

**`enable_mc_list_players`** — 在线玩家查询  
<sub>开关 · 默认：开</sub>

> mc_list_players：查询当前在线玩家与人数

**`enable_mc_server_status`** — 服务器状态查询  
<sub>开关 · 默认：开</sub>

> mc_server_status：版本、在线人数、游戏内时间等

**`enable_mc_connection_status`** — 连接自检  
<sub>开关 · 默认：开</sub>

> mc_connection_status：测试与服务器的 RCON 连接是否正常

**`enable_mc_broadcast`** — 服务器广播  
<sub>开关 · 默认：开</sub>

> mc_broadcast：向服务器内所有玩家广播消息

**`enable_mc_give_item`** — 发放物品  
<sub>开关 · 默认：开</sub>

> mc_give_item：给指定玩家发放物品

**`enable_mc_kick`** — 踢人  
<sub>开关 · 默认：开</sub>

> mc_kick：将玩家踢出服务器

**`enable_mc_ban`** — 封禁  
<sub>开关 · 默认：开</sub>

> mc_ban：封禁指定玩家

**`enable_mc_search_item`** — 物品ID搜索  
<sub>开关 · 默认：开</sub>

> mc_search_item：按中文名 / 英文名 / ID 片段搜索精确物品 ID

**`enable_mc_get_recipes`** — 配方查询  
<sub>开关 · 默认：开</sub>

> mc_get_recipes：正向查合成方法、反向查能造什么

**`enable_mc_list_mods`** — Mod 列表  
<sub>开关 · 默认：开</sub>

> mc_list_mods：列出服务器实际安装的全部 Mod

**`enable_mc_reload_plugin`** — 热重载插件  
<sub>开关 · 默认：开</sub>

> mc_reload_plugin：重载插件让更新立即生效（深度清理模块缓存与 __pycache__，无需重启 AstrBot）。仅作开发/调试用：聊天界面不提供重载指令，日常重载请到 AstrBot「插件管理」页操作

## 7. 词典与知识库

`knowledge` · 共 5 项

**`dictionary_enabled`** — 物品词典  
<sub>开关 · 默认：开</sub>

> 启动时扫描 mods 目录构建中英文物品词典；关闭后物品搜索与配方查询不可用

**`knowledge_enabled`** — 学习型模组知识库  
<sub>开关 · 默认：开</sub>

> 按整合包指纹（mod ID 集合）隔离沉淀 NBT 格式、枪械配件方案等经验

**`enable_mc_search_knowledge`** — 知识检索工具  
<sub>开关 · 默认：开</sub>

> mc_search_knowledge：让 AI 先查库再推理

**`enable_mc_save_knowledge`** — 知识沉淀工具  
<sub>开关 · 默认：开</sub>

> mc_save_knowledge：把推理成功的经验写入知识库

**`enable_mc_correct_knowledge`** — 知识纠错工具  
<sub>开关 · 默认：开</sub>

> mc_correct_knowledge：用正确方案覆盖错误条目（纠错闭环）

## 8. 外观 · 反馈与渐变颜色

`appearance` · 共 10 项

**`feedback_name`** — 反馈署名  
<sub>文本 · 默认：RCON</sub>

> 命令反馈与广播中显示的名字

**`feedback_tellraw`** — 游戏内任务完成后将在公屏反馈  
<sub>开关 · 默认：开</sub>

> 开启后，任务执行完成会在游戏公屏发送署名反馈；反馈文字颜色可在 WebUI「服务器」页的『文本颜色』卡片更改

**`gradient_enabled`** — 启用渐变色（MC 1.16+）  
<sub>开关 · 默认：关</sub>

> 按各处「渐变锚点」逐字符渐变；起始色沿用对应单色配置，空格不参与渐变

**`gradient_format`** — 渐变输出格式  
<sub>文本 · 默认：json</sub>

可选值：`json` / `compat_section` / `compat_amp` / `legacy_amp`

> json=Vanilla（JSON 文本组件，推荐）｜compat_section=Vanilla 兼容（§x§R§R§G§G§B§B）｜compat_amp=Vanilla 兼容（&x&R&R&G&G&B&B）｜legacy_amp=Legacy（&#RRGGBB）

**`color_say`** — 普通喊话颜色  
<sub>文本 · 默认：white</sub>

> mcs 喊话 的聊天栏颜色。支持 hex(#FF0000) / RGB(255,0,0) / 十进制(16711680) 或色名(white/red/gold…)

**`gradient_colors_say`** — 普通喊话 · 渐变锚点  
<sub>文本 · 默认：#FFFFFF,#55FFFF</sub>

> 2 个以上锚点色，逗号分隔，如 #FFFFFF,#55FFFF

**`color_title`** — 全屏喊话颜色  
<sub>文本 · 默认：gold</sub>

> mcs 全屏喊话 的 title 大字颜色，格式同上

**`gradient_colors_title`** — 全屏喊话 · 渐变锚点  
<sub>文本 · 默认：#FFAA00,#FF00FF</sub>

> 2 个以上锚点色，逗号分隔，如 #FFAA00,#FF00FF

**`color_feedback`** — 任务输出颜色  
<sub>文本 · 默认：gold</sub>

> 署名反馈的颜色，格式同上

**`gradient_colors_feedback`** — 任务输出 · 渐变锚点  
<sub>文本 · 默认：#FFAA00,#55FF55</sub>

> 2 个以上锚点色，逗号分隔，如 #FFAA00,#55FF55

## 9. 多 Agent 工作流

`workflow` · 共 10 项

**`agent_workflow_enabled`** — 工作流总开关  
<sub>开关 · 默认：开</sub>

> 开启后复杂模组任务自动交由多 Agent 流水线处理；关闭后 mc_workflow 不再执行，复杂任务会退化为单工具分步模式（AI 自行查库/查 ID/查配方后下发指令）

**`enable_mc_workflow`** — mc_workflow 工具  
<sub>开关 · 默认：开</sub>

> 是否把 mc_workflow 暴露给 LLM（复杂任务总入口）。关闭后模型仍会看到该工具，但调用时会收到「请改用 mc_search_knowledge / mc_search_item / mc_get_recipes / mc_execute_command」的导航提示，不会执行流水线

**`llm_provider_id`** — 主 Provider  
<sub>文本 · 默认：空</sub>

> 流水线的回退 Provider；留空则用会话默认 Provider

**`agent_classifier_provider_id`** — Agent#1 分类器 Provider  
<sub>文本 · 默认：空</sub>

> 判断任务简单 / 复杂；留空回退主 Provider

**`agent_judge_provider_id`** — Agent#2 模板判断器 Provider  
<sub>文本 · 默认：空</sub>

> 判断知识库模板是否够用；留空回退主 Provider

**`agent_engineer_provider_id`** — Agent#3 模板工程师 Provider  
<sub>文本 · 默认：空</sub>

> 前瞻建库、生成操作手册型模板；留空回退主 Provider

**`agent_implementer_provider_id`** — Agent#4 实现器 Provider  
<sub>文本 · 默认：空</sub>

> 套模板构造命令，建议用最强模型保证精度；留空回退主 Provider

**`agent_corrector_provider_id`** — Agent#5 纠错器 Provider  
<sub>文本 · 默认：空</sub>

> 分析失败根因并修正模板；留空回退主 Provider

**`agent_max_implement_rounds`** — 实现器最大尝试轮数  
<sub>整数 · 默认：3</sub>

> 单次任务内实现器最多尝试几轮（含纠错后重试）

**`agent_max_correct_rounds`** — 纠错循环最大轮数  
<sub>整数 · 默认：2</sub>

> 实现失败后最多进入几轮「纠错 → 重试」

## 10. 多 Agent 提示词

`prompts` · 共 5 项

**`agent_prompt_classifier`** — Agent#1 决策分类器 · 系统提示词  
<sub>长文本 · 默认：你是一个 Minecraft 服务器指令任务的分类器。你的职责是判断用户请求属于哪一类，并给出建议。</sub>

> 判断任务属于 simple（原版命令）/ complex（模组任务），并做玩家守门判定（requires_player / target / player_name）。输出严格 JSON · 单轮调用。留空或保持与内置一致＝跟随内置默认（推荐，插件升级时自动获得提示词改进）；改动后即时生效、无需重启。注意：末尾「输出要求（严格 JSON）」的字段约定不要删改，否则流水线无法解析该 Agent 的返回。

**`agent_prompt_judge`** — Agent#2 模板判断器 · 系统提示词  
<sub>长文本 · 默认：你是 Minecraft 知识库模板的「可用性判断器」。给定一个用户请求和知识库检索结果（已按相关度排序，含 t</sub>

> 判断知识库检索结果里的 template 模板是否足以直接完成任务，不足时说明缺口。输出严格 JSON · 单轮调用。留空或保持与内置一致＝跟随内置默认（推荐，插件升级时自动获得提示词改进）；改动后即时生效、无需重启。注意：末尾「输出要求（严格 JSON）」的字段约定不要删改，否则流水线无法解析该 Agent 的返回。

**`agent_prompt_engineer`** — Agent#3 模板工程师 · 系统提示词  
<sub>长文本 · 默认：你是 Minecraft 整合包知识库的「模板工程师」。职责：为当前请求创建或完善【可复用的、前瞻性的】知识库模</sub>

> 前瞻建库：为当前请求沉淀「操作手册型 + 举一反三」的可复用模板。输出严格 JSON · max_tokens 4000 / 180s。留空或保持与内置一致＝跟随内置默认（推荐，插件升级时自动获得提示词改进）；改动后即时生效、无需重启。注意：末尾「输出要求（严格 JSON）」的字段约定不要删改，否则流水线无法解析该 Agent 的返回。

**`agent_prompt_implementer`** — Agent#4 实现器 · 系统提示词  
<sub>长文本 · 默认：你是 Minecraft 服务器指令的「执行专家」。基于提供给你的知识库模板/检索信息，把用户请求转化为精确可执</sub>

> 套用知识库模板把请求转成精确可执行的命令，并判定执行结果、失败重试。输出严格 JSON · max_tokens 2500 / 180s。留空或保持与内置一致＝跟随内置默认（推荐，插件升级时自动获得提示词改进）；改动后即时生效、无需重启。注意：末尾「输出要求（严格 JSON）」的字段约定不要删改，否则流水线无法解析该 Agent 的返回。

**`agent_prompt_corrector`** — Agent#5 纠错器 · 系统提示词  
<sub>长文本 · 默认：你是 Minecraft 知识库模板的「纠错专家」。实现器多次尝试后仍失败，需要你分析根因并修正模板，把这次失败</sub>

> 实现器多轮失败后分析根因、修正模板并给出给实现器的重试指引。输出严格 JSON · max_tokens 4000 / 180s。留空或保持与内置一致＝跟随内置默认（推荐，插件升级时自动获得提示词改进）；改动后即时生效、无需重启。注意：末尾「输出要求（严格 JSON）」的字段约定不要删改，否则流水线无法解析该 Agent 的返回。

---

更多： [返回 README](../README.md) · [界面效果](gallery.md) · [使用指南](usage.md) · [常见问题](faq.md)
