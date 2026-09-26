# v0.23.3 核验单（工作区版 · 权限前置提醒改为按需触发）

> 起因：2026-09-26 主人反馈 —— **日常闲聊也被 MC 插件注入的提示词误伤**。
> 排查结论分成两层：
> ① 主人 QQ 官方平台 openid（`EED91A9D…`）**没进插件独立的 `admin_ids` 表**（只进了 AstrBot 本体的 admins_id），
>    于是插件一直把主人当「非管理员」，每轮都注入那段强指令式提醒；
> ② 就算补好身份，那个钩子**本身也不看话题** —— 它是全局 `on_llm_request`，闲聊照样挂。
>
> 本版同时处理这两层：补身份（配置层，已生效）+ **把提醒从「每轮硬塞」改成「按需触发」**。

---

## 零、边界：本版三类改动

| 类别 | 内容 | 行为影响 |
| --- | --- | --- |
| **A. 触发时机可配** | 新增 `permission_hint_mode`（`always` / `on_demand` / `off`，**默认 `on_demand`**） | 默认档下**日常闲聊不再注入**；三档都不动权限闸门 |
| **B. 话题词表可配** | 新增 `permission_hint_keywords`（列表，默认即内置 **29** 词） | 用户可在设置页增删；清空 = 回退内置 |
| **C. 安全与可观测** | 提醒文案**移除请求者 ID**；注入收口到唯一出口并记审计日志 | openid 不再进模型上下文与第三方日志 |

**一句话**：注入从「无条件的软提醒」变成「有条件的软优化」，而**真正的安全边界始终在工具层**（`tolerant_tool` + `DenyLatch` + `deny_result`）。

---

## 一、A 类：按需触发（`permission_hint_mode`）

### 三档语义

| 档位 | 行为 | 用途 |
| --- | --- | --- |
| `always` | 每次请求都注入（**旧行为**） | 老用户想保原样的逃生出口 |
| **`on_demand`（默认）** | 仅当**两路信号命中**时注入 | 日常闲聊零打扰 |
| `off` | 从不注入，只在调用被拦时告知 | 只要事后拦截 |

### 两路信号（`main.py` `_hint_trigger()`，L1530 附近）

```python
mode = self._hint_mode()
if mode == "off":
    return ""
if mode == "always":
    return "always"
if self._mc_tool_recent_hit(event):   # 信号①：本会话近期试图调用过 MC 工具
    return "recent_tool"
if self._hint_topic_hit(event):       # 信号②：本轮消息命中话题关键词
    return "topic"
return ""
```

**信号①的埋点位置很关键**：不在二十来个工具里逐个改，而是在 `core/tool_guard.py` 的
`tolerant_tool` 装饰器（`wrapper` 第一行）统一回调 `self._mark_mc_tool_used(event)` ——
**所有 MC 工具都过这个装饰器**，且放在参数剥壳**之前**：无论这次调用后续顺不顺利，
「LLM 试图调用过」这个事实都算数。

**信号①的记忆窗口**：默认 600 秒（`HINT_RECALL_TTL`，键为 `UMO|sender`，与会话闩锁同一套键法），
过期自动剪枝。表很小（每个活跃会话一条），顺手全扫即可，不引入定时器。

### 旧键兼容（**请 GPT 特别看这条**）

`permission_hint_injection`（旧开关）**兼容保留**：

```python
mode = str(self._cfg("permission_hint_mode", "") or "").strip().lower()
if mode in HINT_MODES:
    return mode
if self._cfg("permission_hint_injection", None) is False:
    return "off"
return "on_demand"          # ← 旧配置里写死的 true 落到这里
```

**取舍 1｜旧键 `true` 解析为 `on_demand` 而**不是** `always`。**
理由：本版的**目的**就是「别每轮都来」；若旧键 `true` → `always`，则所有老用户升级后行为不变，
等于改了代码没解决问题。反过来，想恢复旧行为只需把 `permission_hint_mode` 显式设为 `always`。
**代价**：这是一次**隐性的行为变更**（对老用户），已在 `_conf_schema.json` / `docs/configure.md` /
插件设置页三处写明。**若 GPT 认为应保持旧键原义，这是一行就能改回的事。**

---

## 二、B 类：话题词表可配（`permission_hint_keywords`）

### 设计：schema 的默认值**就是**内置词表

```json
"permission_hint_keywords": {
  "type": "list",
  "default": [ …29 个词… ]
}
```

好处：用户在设置页**看得到全部词**、随手增删，还能追加自己服务器里的说法。
`tests/test_v0233_hint_config.py` 会把「**schema 默认值 == 内置词表**」钉死 —— 两份清单一旦漂移立刻红
（v0.23.0 就吃过「只加 schema、漏加后端白名单」的亏）。

### 读取语义（`main.py` `_hint_keywords()`）

| 情形 | 行为 |
| --- | --- |
| 配了自定义词 | **替换**内置（默认值本就是全量，删掉即生效） |
| **清空**列表 | **回退内置** —— 免得误删后两路信号只剩一路 |
| 键不存在（老配置 / 新装） | 回退内置；前端会**主动把内置词填上来**给用户看 |
| 后端收到纯文本 | 也认（`逗号 / 中文逗号 / 换行` 分隔） |
| 非法类型 | 静默回退内置，不抛错 |

### 内置词表（29 个，**已按实际语料收窄过一次**）

```
平台专名  mc, minecraft, 我的世界, 麦块, tacz, rcon, nbt
MC 动作   服务器, 指令, 发放, 给我发, 发一把, 发把, 发个
物品体系  物品id, 词典, 模组, 整合包, 配方, 合成
模组玩法  附魔, 满配
运维管理  广播, 喊话, 踢人, 封禁, 白名单, 在线玩家, 任务链
```

**取舍 2｜移除 `枪` / `配件` / `弹药` / `弹匣`（33 → 29）。**
主人原话：「不是所有 MC 整合包都一定有 tacz 枪械的，而这几个词聊游戏几乎都提得到。」
—— 即**命中率虚高**：留着会让本该闲聊的对话又被注入。代价是「给我发一把枪」这类真实请求
不再靠关键词命中，但仍可被信号①（调用过 MC 工具）与 `满配` 等词覆盖；用户也能自行加回。

**取舍 3｜不给「聚合词」做加权 / 打分，纯子串命中。**
两路信号是**并集**语义、且信号①更强，因此关键词这条**宁可漏也不要误**。
若要做权重，就需要阈值标定，而插件没有真实语料统计（当前只有主人一台服务器的体感）——
按「不知道就先别猜」处理，留给有数据时再做。

---

## 三、C 类：安全与可观测

### 3.1 请求者 ID 从文案里移除（**安全项**）

注入文案原本含 `f"当前请求者（ID: {sender}）"` —— 在 QQ 官方平台下那是 **openid 明文**，
**每轮**都会进入 LLM 上下文与第三方 API 日志。现已改为不透明表述：

```
当前请求者**不是管理员**（不在插件 admin_ids 中），命令工具策略 = 白名单：
```

模型本来也不需要这串 ID（注入块拼在同一条用户消息尾部，指代本就明确）。

### 3.2 注入收口到唯一出口 `_emit_hint()` + 审计日志

```python
def _emit_hint(self, req, event, blocks, trigger) -> None:
    if not blocks:
        return
    self._append_user_hint(req, blocks)
    event.set_extra("_mc_perm_hint_done", True)
    self.logger.debug("[提示注入] 触发=%s 时机=%s 块数=%d 请求者=%s", …)
```

**取舍 4｜日志里保留请求者 ID。** 与 3.1 的措施不矛盾：日志是本机、面向管理员排障用的；
**进模型上下文 / 出网**才是要防的。此前注入成功路径**一条日志都不留**，
主人这次排查「为什么每轮都有」只能靠猜 —— 这是补的观测缺口。

### 3.3 「同一次事件只注入一轮」名副其实

旧实现里 `_mc_perm_hint_done` 只守权限提醒，而**能力降级提醒**（异地 RCON / 目录校验未通过）
仍会被每轮补上。现在两者共用同一道闸门。

---

## 四、前端设置页（**这是本版返工补做的部分**）

### 教训：插件有**自己的前端**，改后端 schema 不够

插件页面（AstrBot 里从「插件页面 → MC 智控台」进入）是独立单文件前端：

```
pages/mc_control/index.html     ← 217 KB（HTML + CSS + JS 全在这一个文件里）
```

它的设置项由前端 `CFG_FIELDS` 映射表**硬编码**，**不是**按 `_conf_schema.json` 自动渲染的。
第一版只改了后端 schema + 配置链路，于是主人在插件页面上怎么刷都看不到 ✗

> 根因复盘：最初清点插件目录时，只筛了 `.py / .json / .md` 三类后缀，
> 把 `pages/` 整个目录漏在筛选之外（连 `logo.png`、`requirements.txt` 都没看见）。
> **教训：清点「有哪些文件」时不要按后缀过滤。**

### 本次补齐

| 位置 | 改动 |
| --- | --- |
| `pages/mc_control/index.html`（`card_terminal`） | 「权限前置提醒」开关 → **触发时机三档下拉**；新增**话题关键词标签式列表**（逐个添加 / 单独删除 / 「恢复内置词表」按钮），与管理员列表同一套交互 |
| 同文件（JS） | `CFG_FIELDS` 换新键、`S_DEFAULTS` 补 `on_demand`、卡片状态行改读下拉、新增 `initKeywords/renderKeywords/…` |
| 同文件（`loadSettings`） | 调 `initKeywords(r.hint_keywords_default, "permission_hint_keywords" in cfg)` |
| `core/web_api.py`（`get_settings`） | 返回 `hint_keywords_default`，供前端「恢复内置词表」按钮填充 —— **前端不手抄那 29 个词**，两边不会漂移 |

第二个参数（键是否存在）用来区分两种「列表为空」：
**键不存在**（新装 / 老配置）→ 把内置词填上来给用户看；**键存在但为空**（用户自己清的）→ 如实显示为空。

---

## 五、测试与回归

| 项 | 结果 |
| --- | --- |
| `tests/test_v0233_hint_config.py`（**新增**，23 项） | PASS=23 / FAIL=0 |
| `tests/test_tool_guard.py` | PASS=54 / FAIL=0 |
| `tests/test_remote_rcon_mode.py` | PASS=45 / FAIL=0 |
| `tests/test_review_compliance.py` | PASS=13 / FAIL=0 |
| `tests/test_v0232_preflatten_gate.py` | PASS=118 / FAIL=0 |
| `tests/test_permission_gate.py` | PASS=78 / FAIL=0 |
| `tests/test_complex_routing.py` | PASS=45 / FAIL=0 |
| `tests/test_command_result.py` | PASS=44 / FAIL=0 |
| `tests/test_help_and_reload.py` | PASS=20 / FAIL=0 |
| 设置契约三件套（whitelist / persistence / save_resync） | 全绿 |
| UI 四件套（settings_structure / card_layout / theme / fp_notice） | 全绿（真 Edge 实跑，`pageerror` 为空） |

### 新增测试覆盖（23 项，三节）

| 节 | 覆盖 |
| --- | --- |
| A. schema ↔ 内置词表一致性 | 类型为 list、默认值非空、**默认值 == 内置词表**、mode 默认 `on_demand`、mode 选项 == `HINT_MODES`、后端白名单已收录两键 |
| B. 词表读取语义 | 自定义生效、**替换语义**（原内置词失效）、纯文本解析、大小写不敏感、忽略空白项、非法类型回退、键缺失回退 |
| C. 与触发判定联动 | 命中 → `topic`、闲聊 → 空、调过工具 → `recent_tool`、`always` 不看词表、`off` 一律不注入 |

### 被同步更新的旧契约（透明列出）

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `tests/test_review_compliance.py` | 注入点断言：`_append_user_hint` 出现次数 4 → **1**，并新增「三处分支都经 `_emit_hint`」 | 注入出口收口后，调用点从 4 处变 1 处；断言语义改为「唯一出口」 |
| `tests/test_tool_guard.py` | 桩补 `message_str` / `_mc_tool_recent`，绑定列表补 9 个新方法；用例按三档重写 | 判定链新增了方法 |
| `tests/test_remote_rcon_mode.py` | 降级提醒用例补「与 MC 无关的对话不注入」反向断言 | 降级提醒也纳入按需 |
| `tests/ui_theme_check.py`（夹具） | `/settings` 响应补 `hint_keywords_default` + 配置补两个新键 | 假后端要跟上真实响应格式 |

---

## 六、端到端验证（真实配置 + 真插件类）

```text
permission_hint_mode = on_demand      _hint_mode() = on_demand
内置词表 = 29 | 生效词表（配置优先）= 29

闲聊 →  '今天天气真好呀，陪我聊聊天～'  trigger=''
闲聊 →  '你心情怎么样呀'               trigger=''
闲聊 →  '昨天打枪游戏玩得可爽了'        trigger=''   ← 移词后不再误命中
闲聊 →  '给他配了套配件吧'             trigger=''
闲聊 →  '弹药不太够用了'               trigger=''
MC   →  '帮我看看服务器'               trigger='topic'
MC   →  '满配一把 AK-47'              trigger='topic'
MC   →  '给我发一把钻石剑'             trigger='topic'
会话 →  标记后 trigger='recent_tool'；另一会话 False（隔离正确）

前端（真 Edge 实跑）：
  permission_hint_mode 下拉 = 存在，当前值 on_demand，选项 [always, on_demand, off]
  permission_hint_keywords = 标签渲染正常；「恢复内置词表」按钮可用
  旧开关 #cfg_perm_hint   = 已移除
  卡片状态行               = 当前生效：前置提醒（按需） + 会话闩锁 300s
  页面 JS 异常             = 无
  test_settings_whitelist_contract：真实载荷 78 个键，**无一被后端丢弃**
```

---

## 七、自曝取舍（汇总）

1. **取舍 1**：旧键 `permission_hint_injection: true` → `on_demand`（隐性行为变更，见第一节）。
2. **取舍 2**：移除 `枪 / 配件 / 弹药 / 弹匣`（命中率虚高，但有真实枪械请求不再命中关键词的代价）。
3. **取舍 3**：关键词纯子串、不做加权与打分（没语料就不猜阈值）。
4. **取舍 4**：审计日志仍含请求者 ID（本机排障用，不进模型上下文、不出网）。
5. **取舍 5**：**注入只是优化，不是安全边界**。这句话写进了代码注释、schema 提示与设置页说明 ——
   即使 `off` 档，权限闸门与会话闩锁也照常生效；漏注入最坏结果是模型白试一次后被拦。
6. **取舍 6**：`permission_hint_recall_ttl`（信号①记忆窗口，默认 600 秒）**未在 schema 暴露**，
   走内部常量 `HINT_RECALL_TTL`；但代码已用 `_cfg` 读取，将来加配置项无需改逻辑。

---

## 八、请 GPT 复核

1. **取舍 1**：旧键 `true` → `on_demand` 的隐性变更，是否可接受？还是应保持 `true` → `always`？
2. **取舍 5**：「注入只是优化、安全边界在工具层」这个架构判断，是否成立？有没有我漏掉的场景
   （即：**必须有前置提醒才能拦住**的情况）？
3. **信号①的记忆窗口 600 秒**是否合适？太短会让「第一轮调用工具、第二轮才注入」的空窗变大；
   太长则把无关会话也拉进注入范围。
4. **前端硬编码字段清单**这一设计风险：本次因此返工。是否该改成「按 schema 自动渲染」？
   （代价：现有布局、折叠、标签式交互都要重做，本版按最小改动处理。）

---

## 九、当前状态

| 项 | 状态 |
| --- | --- |
| 代码 / 文档 / 前端 / 测试 | 已完成后，见文末留痕 |
| 插件本地运行 | 已重载生效（`v0.23.3`，日志 `Loading plugin … (v0.23.3)`） |
| 主人这份配置 | `permission_hint_mode=on_demand` + 词表 29 词（已同步，非依赖默认值） |
| 完整 diff | `docs/v0.23.3.diff`（**65,929 字节**；本文件与 diff 均 `export-ignore`，不进发布包） |
