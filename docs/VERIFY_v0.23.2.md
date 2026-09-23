# v0.23.2 核验单（工作区版 · GPT 续单裁决 Q1~Q6 的落地）

> 上一单（`docs/CONSULT_gpt_cross_version_2.md`）的裁决结论是：
> **不要直接实现完整 1.12 支持，先走 A + D**（收窄承诺 + 显式警告），
> 即「<1.13 明确标记为未支持 / 复杂物品命令拒绝自动生成」，`legacy_preflatten` 留到 P1。
>
> 本单是**按该裁决落地后**的交底：P0 四项全做，P1（`legacy_preflatten` 能力档案）**按裁决不做**。
>
> **主人指令**：先出核验单给 GPT，**不提交、不推送、不打 tag、不发版**。
>
> **本单已更新至第二版**：第一版经 GPT 核验后指出两处安全边界（见文末「第二版」章节），
> P0-1 / P0-2 / P0-3 与 Q6 已全部落地。

| 项 | 值 |
| --- | --- |
| 插件 | `astrbot_plugin_Scintilla_MC_Server_Control` |
| 仓库 | `HuangGuaKnn/astrbot_plugin_Scintilla_MC_Server_Control` |
| 基线提交 | `104bda4`（v0.23.1，工作区干净） |
| 本版改动 | **11 改 + 1 新**，`+1006 / -16`（`git diff --numstat` 合计） |
| 版本号 | **未抬**（`metadata.yaml` 仍为 v0.23.1）—— 等核验通过后由主人拍板；CHANGELOG 条目已按格式预写好 |
| 完整 diff | `docs/v0.23.2.diff`（**81,355 字节**；**不含本文件**） |
| 运行环境 | AstrBot 自带 Python **3.12.12** · Windows |
| MC 服务端 | 本机 **1.20.1 · Forge 47.4.23**（唯一真机样本，**服务器当前未开**） |

---

## 零、边界：本版三类改动

| 类别 | 内容 | 行为影响 |
| --- | --- | --- |
| A **能力层** | `version_caps.py`：新增 `unsupported_preflatten` 态 + `preflatten_block_reason()` 判据 | **有**：<1.13 不再生成命令 |
| B **执行层守门** | `workflow.py` 两条路径 + `main.py` `mc_give_item` 发送前硬拦 | **有**：1.12.2 下带数据命令**不再调用 `rcon.command()`** |
| C 分水岭清单 | `CUTOVERS` 结构化 + 1.13 条目补全 + 新增 1.14 / 1.20.2 | 无（数据；但 `describe_capabilities` 输出字段变多） |
| D 文档 / UI | README 支持范围表、`_conf_schema.json` 两处 hint、`metadata.yaml`、WebUI 能力行、CHANGELOG | 无（纯文本） |

**未动的判定逻辑**：`_status_of` / `_newly_confirmed` / `_halt_on_uncertain` / `_split_confirmed` /
`STATUS_LABEL` / `classify_command_output` / 六态口径 —— 一行未改。
**未动的既有契约**：`ITEM_SYNTAX_LEGACY` / `ITEM_SYNTAX_COMPONENTS` / `ITEM_SYNTAX_UNKNOWN` /
`ITEM_COMPONENTS_CUTOVER` / 附魔 ID 别名 / 1.20.5 分水岭 / 补丁 3 逃生出口（均有回归断言）。

---

## 一、Q1 落地：不再承诺 1.12~1.21+，`<1.13` 独立成态

### 改动

`core/version_caps.py` L45 / L56 / L159（新增常量与分支）：

```python
#: 1.13 以下（1.8~1.12.2）：**预扁平化**世代 —— 数字物品 ID + data 值、
#: 旧 ``execute <实体> <x y z>`` 语法、``ench`` 数字附魔 ID。
#: v0.23.2：本世代**不自动生成任何命令**，仅作能力标注。
ITEM_SYNTAX_PREFLATTEN = "legacy_preflatten"

#: 已知版本 < 1.13 时**生效**的世代：明确不支持自动生成（fail-closed）。
ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN = "unsupported_preflatten"

ITEM_PREFLATTEN_CUTOVER = (1, 13)


def item_syntax_for(mc: tuple[int, ...] | None) -> str:
    if not mc:
        return ITEM_SYNTAX_UNKNOWN
    if tuple(mc) < ITEM_PREFLATTEN_CUTOVER:
        return ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN      # ← 新增：不再落进 legacy_nbt
    return (ITEM_SYNTAX_COMPONENTS
            if tuple(mc) >= ITEM_COMPONENTS_CUTOVER
            else ITEM_SYNTAX_LEGACY)
```

`resolve_item_syntax()` L178 新增**优先级**：**已知** <1.13 时，手动指定的语法世代**也不放行**：

```python
if info.mc is not None and tuple(info.mc) < ITEM_PREFLATTEN_CUTOVER:
    return ITEM_SYNTAX_UNSUPPORTED_PREFLATTEN, "unsupported"
ov = str(override or "auto").strip().lower()
if ov in (ITEM_SYNTAX_LEGACY, ITEM_SYNTAX_COMPONENTS):
    return ov, "override"
```

> 理由：`legacy_nbt` 是 **1.13~1.20.4** 的写法，拿它去「放行」1.12 只会生成错命令。
> **注意与「版本未知」的区别**：未知时手填世代仍是合法逃生出口（异地 RCON 模式），
> 这条**没有被误伤** —— 见第七节回归断言。

### 世代判定矩阵（实测输出）

| 服务端版本 | 世代 | 自动生成 |
| --- | --- | --- |
| 1.8 / 1.12.2 | `unsupported_preflatten` | ⛔ 拒绝 |
| **1.13 / 1.13.2** | `legacy_nbt` | ✅ |
| 1.20.1 / 1.20.4 | `legacy_nbt` | ✅ |
| 1.20.5 / 1.21 / 1.21.2 | `components` | ✅ |
| 未知 / 解析不出 | `unknown` | ⛔ 拒绝带数据命令 |

### 注入 Agent 的片段（1.12.2 实测，L452）

```text
- 服务端：1.12.2（来源：主人手动声明）
- ⚠️ **该版本低于 1.13，本插件暂不支持自动生成命令。**（分水岭：1.13）
  原因：1.13 重写了命令图（`give` 取消数据值参数并把 NBT 移到物品 ID 之后、
  旧 `execute` 废弃、`effect` / `difficulty` 变化、`entitydata`→`data`），
  并做了资源 ID 扁平化。1.8~1.12.2 需要另一套预扁平化写法…
  本插件尚未实现，**也没有真机验证条件**。
  因此：**本次任务不要构造任何命令**，直接输出 `success=false`，reasoning 写明：…
  例外：以下不受 1.13 命令图改动影响的简单命令**可以**照常生成 —— `ban`、`gamemode`、…
```

**关键点**：该片段**不含**任何 NBT / 物品组件模板 —— 给了模板等于诱导 LLM 生成错命令。

---

## 二、Q2 落地：`ench` + 数字 ID（P1 不实现，但档案标注）

裁决要求「1.12 附魔 = `ench` + 数字 ID，且必须是**完整能力档案**，不能只做字段别名替换」。
本版**不实现生成**（属 P1），但已在 `ITEM_SYNTAX_PREFLATTEN` 的注释与 1.13 分水岭详情里
写明旧写法的构成要素（数字 ID + data 值 + `ench` 数字附魔 + 旧 `execute`），
避免将来有人误以为「换个字段名就能支持」。

皮莉卡**同意裁决的判断**：这不是字符串替换能解决的事 —— 数字物品 ID / data 值 / 实体 ID /
命令参数顺序 / `execute` 旧语法各自都要独立的 golden test，故整体留到 P1。

---

## 三、Q3 落地：分水岭扩展（4 条 → 6 条，且分层）

| 版本 | surface | 标题 | affects |
| --- | --- | --- | --- |
| **1.13** | `command_syntax` | 命令图与资源 ID 扁平化（1.13 大断层） | give / execute / effect / difficulty / data / entitydata / blockdata |
| **1.14** | `command_syntax` | execute 条件子命令（if / unless data 等） | execute |
| **1.20.2** | `item_nbt_field` | 部分药水 / 特殊物品 NBT 字段改名 | give / item / replaceitem |
| 1.20.5 | `item_format` | 物品 NBT → 物品组件 | give / item / replaceitem |
| 1.21 | `enchant_id` | 附魔 ID 改名 sweeping → sweeping_edge | give / enchant / item |
| 1.21.2 | `attribute_id` | 属性 ID 去掉分组前缀 | give / attribute / item |

1.13 条目**补全了 `give`**（旧版只写 `execute` —— 这是本次追问的起点）：

```text
1.13 重写命令图：`give` 取消 data 位置参数、NBT 改贴物品 ID 之后、数量后置
（旧写法 `give <玩家> <物品> <数量> <数据值> {NBT}`）；
旧 `execute @p ~ ~ ~ <命令>` 废弃，改为 `execute as/at/positioned/... run`；
`effect` / `difficulty` 语法变化，`entitydata` → `data`；
同时做资源 ID 扁平化（数字 ID 移除、大量方块/物品/实体 ID 改名）。
**本插件对 <1.13 不自动生成命令**。
```

**关于 `/function`**：裁决指出「1.12 有 function 但用旧目录体系，不能简单说 1.12 没有 function」。
皮莉卡**未把它单列成一条分水岭**（因为它不影响「命令字符串生成」这一层，
而是影响函数文件体系 / 数据包结构），但**同意裁决**：这是「不建议当前承诺完整 1.12 支持」的
又一佐证，已并入 P1 范围（见第十节）。

---

## 四、Q4 落地：代码侧硬拦（本版核心）

裁决原话：**「1.12.2 + 附魔请求 → 不调用 `rcon.command()`」**。

### 4.1 判据（`core/version_caps.py` L328）

```python
#: 物品类命令：**只在带数据时**才拦 —— 1.12 的
#: `give <玩家> <物品> [数量] [数据值] [NBT]` 允许省略后两个参数，
#: 所以不带 NBT 的 `give P item N` 在 1.8~1.12.2 与 1.13+ 语义一致。
PREFLATTEN_DATA_ONLY_COMMANDS = frozenset({"give", "clear"})

def preflatten_block_reason(command: str) -> str:
    cmd = str(command or "").strip().lstrip("/")
    name = cmd.split(" ", 1)[0].lower()
    if not name:
        return "命令为空"
    if name in PREFLATTEN_SAFE_COMMANDS:          # 白名单（不受 1.13 命令图影响）
        return ""
    if name in PREFLATTEN_DATA_ONLY_COMMANDS:     # 物品类：看有没有数据
        if "{" in cmd or "[" in cmd:
            return f"`{name}` 带 NBT / 物品组件数据，…本插件对该版本不自动构造带数据的物品命令"
        return ""
    return f"命令 `{name}` 属于 1.13 重写 / 扁平化影响的命令族，…"
```

白名单（`PREFLATTEN_SAFE_COMMANDS`，25 个）：
`time / weather / say / me / tell / msg / w / list / gamemode / kill / seed / save-all / save-off /
save-on / stop / kick / ban / ban-ip / pardon / pardon-ip / op / deop / whitelist / help /
spawnpoint / setworldspawn`

### 4.2 守门位置（三处，全在**发送前**）

| # | 位置 | 行号 | 行为 |
| --- | --- | --- | --- |
| 1 | `core/workflow.py` `_run_simple()` | L238 | 记 `CommandResult(status="skipped")` 后 `continue` |
| 2 | `core/workflow.py` `_exec_commands()` | L695 | 记 `{"status": "skipped"}` 后 `continue` |
| 3 | `main.py` `mc_give_item()` | L1889 | 直接返回拒绝文案（在解析玩家**之前**拦） |

标记来源（`workflow.py` L175，随 `_refresh_version_context()` 每次任务开始重算）：

```python
self._preflatten = False
try:
    _info = self.plugin._resolve_version_info()
    self._preflatten = bool(
        _info.mc is not None and tuple(_info.mc) < ITEM_PREFLATTEN_CUTOVER
    )
except Exception:
    self.logger.warning("预扁平化标记解析失败（按放行处理）: %s", e)
```

### 4.3 用户可见文案

| 出口 | 文案 |
| --- | --- |
| 工作流汇总行 | `…；N 条因服务端版本不支持自动生成而未发送` |
| 工作流明细 | `[未发送] <命令>` + 具体原因 |
| `mc_give_item` | `<原因>。本插件暂不支持该服务端版本的自动命令生成：请手动执行适配该版本的命令，或把服务端升级到 1.13 及以上。` |
| WebUI 能力行 | `⚠ 服务端 1.12.2 低于 1.13，本插件**暂不支持该版本的自动命令生成**。…（不受影响的简单命令如 time / weather / say / list 仍可生成）` |

### 4.4 证据（`tests/test_v0232_preflatten_gate.py` 第七节，实测）

| 断言 | 结果 |
| --- | --- |
| ★★1.12.2 + 附魔 give → **`rcon.command` 一次都没被调用** | PASS |
| ★回执说明「因服务端版本不支持自动生成而未发送」 | PASS |
| ★回执不给成功口径（不出现「已成功 1/1」） | PASS |
| 工作流状态不是 `done` | PASS |
| ★1.12.2 + `time set day` → 正常发送（白名单例外） | PASS |
| ★对照：1.20.1 下同一条命令照常发送（守门不外溢） | PASS |
| ★混合批次只发合法命令（`say 集合` / `weather clear` 发出，附魔命令不发） | PASS |
| ★★复杂路径 `_exec_commands`：非法命令未被发送 | PASS |
| 复杂路径：非法命令记为 `skipped` 且原因含「暂不支持该版本的自动命令生成」 | PASS |

### 4.5 为什么不拦 `mc_execute_command`（**请 GPT 特别看这条**）

裁决说「复杂物品 / NBT / execute / effect 等命令拒绝**自动构造**」。
皮莉卡把守门只挂在**自动构造**路径（工作流 + `mc_give_item`），
`mc_execute_command`（用户显式写下的命令）**不拦** —— 语义上那是用户自己的判断，
全拦会让 1.12 用户连一条命令都发不出（功能性倒退）。
**若 GPT 认为该口径不对，请直接指出。**

---

## 五、Q5 落地：词典边界（本版未动词典，说明分层理由）

裁决「部分接受」：模组物品 ID 可交词典，但**数字 ID / data、实体 ID、命令参数、旧版 NBT**
不能交给词典。皮莉卡**未改动** `core/item_dictionary.py`，理由：

1. 本版走 A + D 路线（<1.13 拒绝自动生成），词典此刻改也无处可用；
2. 词典要支持 1.12 需要新增 `legacy_id` / `legacy_data` / `modern_id` 三元映射（裁决 Q5 第 1 点），
   而这些映射**必须来自真实 1.12 服务端**，凭现代词典推不出来；
3. P7 的表述已在核验单里修正为：**「当前词典可减少模组物品 ID 的跨版本映射工作，
   但不能替代命令语法、实体 ID、数字 ID/data 和旧版 NBT 的版本能力层」** —— 同意裁决原文。

---

## 六、Q6 落地：`CUTOVERS` 结构化（**强烈采纳**）

每条现在带 9 个字段：

```python
{
    "version": (1, 13),                    # 哪条边界
    "surface": "command_syntax",           # 差异层面（与 item_format 等分层，不再混在一层）
    "title": "命令图与资源 ID 扁平化（1.13 大断层）",
    "affects": ("give", "execute", ...),   # 影响哪类命令
    "before": "preflatten",                # 两侧形态
    "after": "flattened",
    "action_before": "unsupported",        # 遇到这一侧要做什么
    "action_after": "supported",
    "tested": False,                       # 该结论是否真机验证过
    "detail": "…",
}
```

`describe_capabilities()` 快照同步新增：

| 字段 | 值示例（1.12.2） | 用途 |
| --- | --- | --- |
| `supported` | `False` | WebUI 直接判断 |
| `support_note` | `服务端低于 1.13：本插件暂不支持该版本的自动命令生成` | 一行说明 |
| `preflatten_cutover` | `1.13` | 与 `cutover`（1.20.5）并列 |
| `verified_on` | `1.20.1 · Forge 47.4.23` | **诚实标注真机矩阵** |
| `cutovers[].surface` / `.tested` / `.affects` | — | 结构化输出 |

> `tested` 字段当前**只有 1.21 条目为 `True`**（横扫之刃改名有实机证据），
> 其余含 1.13 / 1.14 / 1.20.2 / 1.20.5 / 1.21.2 均为 `False` —— 如实标注未实测。

---

## 七、测试与回归

| 项 | 结果 |
| --- | --- |
| `python -m py_compile main.py core/version_caps.py core/workflow.py` | exit=0 ✅ |
| `_conf_schema.json` JSON 合法性 | OK ✅ |
| `run_v0230_all.py` ② 回归 / 单元测试 | **30 / 30 通过** ✅（新增 `test_v0232_preflatten_gate.py` 73 项） |
| `run_v0230_all.py` ③ WebUI 真浏览器链路 | **11 / 11 通过** ✅（`ui_version_override_check.py` 新增 [5][6] 两场景） |
| 全量套件总判定 | **全部通过 ✅** |

### 新增测试覆盖（73 项，八节）

| 节 | 覆盖 |
| --- | --- |
| 一 | 世代三态之外多一态 + 1.13 边界 + **回归**（1.20.4/1.20.5/未知 契约不许破） |
| 二 | 已知 1.12.2 手填 `legacy_nbt`/`components` **都不放行** + **回归**（未知版本逃生出口仍放行） |
| 三 | 注入片段：含拒绝指令 / 点名 1.13 / 要求 `success=false` / **不给任何模板** / 白名单例外 / 对照 1.20.1 不外溢 |
| 四 | 能力快照 `supported` / `support_note` / `preflatten_cutover` / `verified_on` + 对照 |
| 五 | 分水岭结构化字段完整性 + 1.13 补 `give` + 1.20.5 归 `item_format` + 1.14/1.20.2 新增 |
| 六 | 命令族判据：带 NBT 拦 / 不带数据放行 / execute/effect/difficulty/summon/setblock 拦 / 白名单放行 |
| 七 | **代码侧硬拦**（`rcon.sent == []`）+ 白名单例外 + 1.20.1 对照 + 混合批次 + 复杂路径 |
| 八 | 标记算法（1.12.2→True，1.13/1.20.1/未知/解析不出→False） |

### 被同步更新的旧契约（透明列出）

| 文件 | 改动 | 原因 |
| --- | --- | --- |
| `tests/test_tool_guard.py` | `FakeSelf` 补 `_preflatten_block_reason()` 替身（8 行） | 该文件用**动态挂真实方法**到替身的方式测权限闸门；`mc_give_item` 新增了守门调用，替身没跟上会 `AttributeError`。**这是本版唯一一次「改坏又修好」**，如实列出 |
| `tests/ui_version_override_check.py` | 新增 [5] 1.12.2 场景 + [6] 手填不放行场景（27 行） | 裁决要求 UI 显示「不支持」；原文件只覆盖到「未知 → 无法确定」 |

其余 28 个测试文件**零改动**且全绿。

---

## 八、自曝取舍

**取舍 12｜判据用「白名单放行 + 其余拦」，不是「黑名单拦」（本版最值得挑的一条）。**
选白名单的理由：`<1.13` 我们已经声明「不支持自动生成」，那么**保守**才自洽 ——
白名单外的命令被拦，最坏后果是用户觉得「怎么这个也不给发」（可接受）；
黑名单漏掉某个受影响的命令族，最坏后果是**静默错命令**（不可接受）。
代价：1.12 用户能自动生成的命令面变窄（只剩 25 个白名单命令 + 不带数据的 `give`）。
**请 GPT 判：这个宽窄是否合适？25 个白名单要增删吗？**

**取舍 13｜「不带数据的 `give` 放行」是否过宽？**
依据是 1.12 的 `give <玩家> <物品> [数量] [数据值] [NBT]` 允许省略后两参，
所以 `give P item N` 在 1.8~1.12.2 与 1.13+ 参数位置一致。
**但残留风险**：物品 **ID 本身**可能因扁平化而不同（1.12 里 `red_wool` 不存在，只有 `wool` + data 14）。
词典从**当前服务端**扫（若是 1.12.2 服务端，扫出的就是 1.12 的 ID），
所以理论上自洽；但皮莉卡**没有 1.12.2 实例可验**，这条属**未验证的推理**。
**请 GPT 判：是否该把不带数据的 `give` 也一并拦掉（更保守），还是保留？**

**取舍 14｜`mc_execute_command` 不拦（见 4.5）。** 请裁决口径。

**取舍 15｜`verified_on` 硬编码在 `describe_capabilities()` 里。**
`"1.20.1 · Forge 47.4.23"` 是写死的字符串 —— 好处是「真机矩阵」这件事在代码里可见、
不会随版本号漂移；坏处是将来加了新样本要改代码。
替代方案：做成 `tests/` 里的常量或 `docs/` 引用。**请 GPT 判哪种更好。**

**取舍 16｜`tested` 字段几乎全 `False`，信息量低。**
如实标注的代价是「看起来一片未验证」。皮莉卡认为**诚实优先**（这正是裁决 Q4 的精神），
故保留。若 GPT 认为该字段应改成「枚举」或「附证据链接」，请指出。

**取舍 17｜`_preflatten` 标记的时效性。**
在任务开始时（`_refresh_version_context()`）算一次。
若用户在任务执行中途改了版本声明，本任务内不会重算 —— 皮莉卡判断这在实践中不可能发生
（同一任务的时间尺度内没人会去改设置页），故未做每命令重算（那会带来文件 IO 开销）。

**取舍 18｜P1 未做（`legacy_preflatten` 档案）。**
完全同意裁决：没有 1.12.2 真机就不实现。
**皮莉卡特别记下**：这不是「忘了」，是**主动不做**，且已在 README / CHANGELOG 里公开写明。

---

## 九、请 GPT 裁决

1. **取舍 12**：白名单式判据（25 个命令 + 不带数据的 `give`）宽窄是否合适？要增删哪些？
2. **取舍 13**：不带数据的 `give` 放行是否过宽（物品 ID 改名风险）？该不该一并拦？
3. **取舍 14**：`mc_execute_command` 不拦的口径是否正确？
4. **取舍 15 / 16**：`verified_on` 硬编码、`tested` 字段全 `False` —— 实现方式是否可接受？
5. **Q3 补充**：`/function` 未单列成分水岭（只并入 P1）是否正确？还是应该现在就在清单里
   写一条 `function_体系` 分水岭（即使不生成命令，也让能力层可见）？
6. **已知 1.12.2 手填 `legacy_nbt` 不放行** 这条设计（第一节）是否正确？
   会不会有「用户其实想强制用 1.13 语法」的合法场景被误伤？
7. **本版是否满足 P0 的验收标准**（裁决第「推荐的实际改动顺序 · P0」四项）？
   皮莉卡自评：① 新增 pre-1.13 能力状态 ✅ ② `<1.13` 复杂命令 fail-closed ✅
   ③ 1.13 CUTOVERS 文案补全 ✅ ④ 支持状态文档改 1.13+ ✅。
8. **还有没有皮莉卡没看见的静默错命令路径？**（除 `mc_execute_command` 外，
   是否还有别处会绕过守门把 1.13+ 语法发给 1.12 服务端？）

---

## 十、当前状态与留痕

| 项 | 值 |
| --- | --- |
| 是否提交 | **否**（工作区改动，未 `git add` 内容；仅对新测试文件用了 `git add -N` 以便出 diff） |
| 是否推 main | **否** |
| 是否打 tag | **否** |
| 是否发 Release | **否** |
| `metadata.yaml` 版本 | **仍为 v0.23.1**（未抬；等核验通过后由主人拍板） |
| CHANGELOG | 已预写 `## [v0.23.2] - 2026-09-23` 条目（含「未做的（路线图）」小节） |
| 插件是否重载 | **否**（本机服务器未开） |
| 完整 diff | `docs/v0.23.2.diff` |

### P1 路线图（按裁决，等 1.12.2 真机实例）

```text
① give 旧参数顺序（数量 + 数据值在前、NBT 在最后）
② 数字物品 ID / data 值映射（需从真实 1.12 服务端取）
③ ench + 数字附魔 ID（sharpness=16 / smite=17 / bane_of_arthropods=18 /
   knockback=19 / fire_aspect=20 / sweeping=22 / unbreaking=34 / mending=70）
④ 旧 execute + detect
⑤ 旧 function 文件体系（1.12 有 function，但用旧目录；1.13 起才是数据包）
⑥ 旧实体 ID / effect / difficulty 语法
⑦ 每类命令的 golden test（输入语义 → 期望 1.12 命令字符串）
⑧ 真机验证矩阵：Vanilla 1.12.2 / Forge 1.12.2 / Vanilla 1.13.2 /
   Forge·Fabric 1.20.1 / Vanilla 1.20.4 / Vanilla 1.20.5+
   —— 在这些验证到位前，支持状态只能写 experimental / unverified
```

真机矩阵仍只有本机 **1.20.1 · Forge 47.4.23** 一个样本，
这是自 v0.22.6 起一路带着的已知缺口，也是本版「P0 只做拒绝、不做支持」的根本原因。

---

# 【第二版】GPT 核验（2026-09-23）落地

> GPT 对第一版（工作区版）的结论：**P0 目标基本完成，但不建议直接提交发布**。
> 主要问题不是 `<1.13` 状态机本身，而是两个安全边界：
> ① 不带数据的 `give` / `clear` 仍可能因物品 ID 扁平化产生静默错命令；
> ② `mc_execute_command` 及若干直接 RCON 入口绕过 `<1.13` 守门。
> 本版按裁决补完 **P0-1 / P0-2 / P0-3** 与 **Q6**。

## 十一、P0-1 落地：`mc_execute_command` 与全仓执行入口统一守门

### 皮莉卡认下的地方（第一版判断错了）

第一版把 `mc_execute_command` 定义为「用户显式写下的命令」而放过 —— **这个判断是错的**。
它是 `@filter.llm_tool`，命令字符串由 **LLM 生成**，与「用户手写」不是同一件事：

```text
用户说自然语言 → LLM 自己拼一条 1.13+ 命令 → 调用 mc_execute_command → 绕过版本层
```

GPT 的反驳成立，本版改正。

### 落地：新增统一守门（`main.py` L2756）

```python
def _guard_command_for_version(
    self, command: str, *, source: str = "llm_tool", manual: bool = False
) -> str:
    """**统一**版本能力守门：已知服务端 < 1.13 时，该命令能否执行？放行返回空串。"""
    if manual:
        return ""          # 人工原始命令通道（P1 预留）
    try:
        reason = self._preflatten_block_reason(command)
    except Exception as e:
        self.logger.warning("版本能力守门异常（按放行处理）: %s", e)
        return ""
    if reason:
        self.logger.info("[版本守门] 拦截 source=%s 命令=%s 原因=%s", source, command, reason)
        return (f"{reason}。\n本插件暂不支持该服务端版本的自动命令生成："
                "请手动执行适配该版本的命令，或把服务端升级到 1.13 及以上。")
    return ""
```

### 接入的 10 个入口（**AST 扫描证明**，测试第 ⑯ 条）

| # | 入口 | 接入行 | 拦截时行为 |
| --- | --- | --- | --- |
| 1 | `mc_execute_command` | L1752 | 直接返回拒绝文案（**P0-1 的核心**） |
| 2 | `mc_broadcast` | L1862 | 返回拒绝文案 |
| 3 | `mc_give_item` | 改用统一函数 | 返回拒绝文案 |
| 4 | `mcs_say` | `tellraw @a` 前 | `yield` 拒绝文案 |
| 5 | `mcs_title_cmd` | `title @a title` 前 | `yield` 拒绝文案 |
| 6 | `mcs_kick_cmd` | `kick` 前 | `yield` 拒绝文案 |
| 7 | `mcs_ban_cmd` | `ban` 前 | `yield` 拒绝文案 |
| 8 | `mcs_unban_cmd` | `pardon` 前 | `yield` 拒绝文案 |
| 9 | `mc_kick` | `kick` 前 | 返回拒绝文案 |
| 10 | `mc_ban` | `ban` 前 | 返回拒绝文案 |

`mc_workflow` 不在此列 —— 它内部走 `MCWorkflow._preflatten_block()`，调用**同一判据**，两处口径一致。

> GPT 建议的「更理想方案」（`_exec_checked(rcon, command, source=...)` 增加来源参数）
> 皮莉卡**没有采用**：`_exec_checked` 是「执行 + 六态判定」的收口点，
> 塞进版本策略会让它同时承担两件事。当前用入口侧守门 + `source` 日志字段，
> 已经能满足「统一 + 可追溯」。**若 GPT 认为必须下沉到 `_exec_checked`，请指出。**

## 十二、P0-2 落地：物品类命令一律拦

### 皮莉卡认下的地方

第一版放行「不带数据的 `give`」的理由是「1.12 允许省略数据值/NBT，参数位置一致」。
GPT 指出：**语法推理本身不错，但不足以证明语义正确** —— `item` 本身可能就是现代 ID。

皮莉卡当时在取舍 13 里已经自曝了这条（「没有 1.12.2 实例可验，属未验证推理」），
GPT 裁决「当前建议也拦掉」，**皮莉卡同意并落地**。

### 落地

```python
#: v0.23.2 第二版（GPT 核验 P0-2）：**物品类命令一律拒绝**，不再有「不带数据就放行」的例外。
PREFLATTEN_ITEM_COMMANDS: frozenset[str] = frozenset({"give", "clear", "item", "replaceitem"})
```

- 旧的 `PREFLATTEN_DATA_ONLY_COMMANDS` 常量**已删除**（测试断言 `not hasattr(vc, ...)`，防误用）；
- 拒绝文案点名原因：「1.13 以下的物品 ID 与当前版本不同（扁平化前是「数字 ID + data 值」），
  本插件尚未完成旧版物品 ID/data 映射」；
- `item` / `replaceitem` 也归入物品类（文案更精准，原先落进「其余命令族」的通用文案）。

**行为影响（主人须知）**：1.12 用户现在**连 `give Steve stone 1` 都不再自动生成**。
这是有意的功能性收窄 —— 与「不支持就别假装支持」的裁决精神一致。

## 十三、P0-3 落地：白名单补 `tellraw` / `title` + 语义澄清

### 为什么必须补

插件自身的播报 / 标题入口用的就是这两个命令：

| 入口 | 命令 |
| --- | --- |
| `_send_feedback()`（内部反馈） | `tellraw` |
| `mcs_say` | `tellraw @a {...}` |
| `mcs_title_cmd` | `title @a title {...}` |
| `mc_broadcast`（title / actionbar 模式） | `title @a actionbar {...}` |

若不列入白名单，1.12 用户接上守门后会**连播报都用不了** —— 那是功能性倒退，不是安全。
`tellraw`（1.7+）与 `title`（1.8+）在 1.8~1.12.2 与 1.13+ 语法一致，属安全放行。

### Q1 落地：白名单语义澄清

GPT 指出「危险命令和语法安全混在一起」，建议拆成两个集合。
皮莉卡**没有拆集合**，改为在常量注释里写死语义（理由：拆了没有行为差异，纯文档性；
单集合更容易保持一致）。注释原文：

```text
⚠️ **本集合只表示「语法可生成」，不表示「允许执行」。**
权限与副作用由 `_safe_command()`（黑名单/白名单/游客闸门）与
`_admin_gate()`（stop / op / ban 等管理类）**独立**把关 ——
进入本集合**不会**绕过它们。维护时不要把「加进白名单」当成「放开权限」。
```

**若 GPT 认为仍应拆成 `PREFLATTEN_SYNTAX_SAFE_COMMANDS` + `PREFLATTEN_ADMIN_COMMANDS`，请指出。**

## 十四、Q6 落地：`function_system` 分水岭登记

按裁决「建议现在加入能力清单、但暂不实现生成」：

```python
{
    "version": (1, 13),
    "surface": "function_system",
    "title": "function 从旧函数体系迁移到数据包体系",
    "affects": ("function",),
    "before": "legacy_function",
    "after": "datapack_function",
    "action_before": "unsupported",
    "action_after": "reject_generation",
    "tested": False,
    "detail": (
        "1.12 也有 `/function`，但它读的是存档内 `functions/` 目录下的 `.mcfunction`；"
        "1.13 起改为数据包 `data/<命名空间>/functions/`，且函数体内命令语法同步跟随命令图变化。"
        "本插件当前**不生成** `/function`，此处仅作能力登记，为 P1 的旧版函数体系留出落点。"
    ),
}
```

**不扩大当前行为范围**：`/function` 本来就在「其余命令族」里被拦，登记只让能力表完整。

## 十四·五、**未接入守门的内部命令**（透明列出，供 GPT 复查 Q8）

以下 `rcon.command()` 直调点**故意不走守门**，理由都是「不接受用户/LLM 输入、命令字符串固定」：

| 位置 | 命令 | 理由 |
| --- | --- | --- |
| `_send_feedback()` | `tellraw @a {...}` | 插件内部反馈，命令固定；`tellraw` 已在白名单 |
| `_send_bridge_receipt()` | `tellraw @a[name=...] {...}` | 同上 |
| `_online_players()` / `mc_list_players()` / `mcs_status()` / `mc_connection_status()` | `list` | 只读查询；`list` 已在白名单 |
| `mc_server_status()` | `list` + `time query daytime` | 只读；命令名 `time` 已在白名单 |
| `mcs_banlist_cmd()` | `banlist` | 只读；**本版已把 `banlist` 补进白名单**（预防性 —— 将来若给内部只读命令也加守门，不会误伤） |
| `_query_tps_mspt()` | `tps` / `spark tps` / `forge tps` | 模组命令，`try/except + continue` 逐个试探；不接受外部输入 |

**判断依据**：守门要防的是「把 1.13+ 语法发给 1.12 服务端」，而这些点的命令字符串
是插件自己写死的常量，不存在被 LLM 或用户改写的路径。
**若 GPT 认为内部命令也应统一过守门（以便未来新增反馈命令时不自知），请指出** ——
那需要给它们各自加白名单条目。

## 十五、P1 项（GPT 裁决后仍未做，等条件成熟）

| 项 | 裁决 | 皮莉卡处理 |
| --- | --- | --- |
| `verified_on` 结构化（`verification_samples`） | 短期可接受，长期改进 | **本版未改**，记入 P1 |
| `tested` 布尔 → 状态枚举（`verified` / `spec_only` / `blocked_unverified`） | 建议未来细分 | **本版未改**，记入 P1 |
| 人工原始命令通道（`mcs 原始命令` / `allow_legacy_manual_command`） | 「如果确实要允许」 | **本版未做**；但 `_guard_command_for_version(manual=True)` 已预留 |
| `LegacyItemRef(numeric_id, data, legacy_name, modern_name)` | 方案 B | **本版未做**，需 1.12.2 真机词典 |
| `legacy_preflatten` 能力档案 | P1 | 未做（同第一版） |

## 十六、第二版测试与回归

| 项 | 结果 |
| --- | --- |
| `python -m py_compile main.py core/version_caps.py core/workflow.py` | exit=0 ✅ |
| `tests/test_v0232_preflatten_gate.py` | **117 / 117 通过** ✅（第一版 73 项 → 第二版 +44 项） |
| `tests/test_tool_guard.py`（替身补挂 `_guard_command_for_version`） | 全过 ✅ |
| `run_v0230_all.py` ② 单元/回归 | **30 / 30 通过** ✅ |
| `run_v0230_all.py` ③ WebUI 真浏览器 | **11 / 11 通过** ✅ |
| 全量总判定 | **全部通过 ✅** |

### 第二版新增的关键断言（节选）

| 断言 | 结果 |
| --- | --- |
| ★★不带数据的 `give` 也拦（P0-2） | PASS |
| ★物品类拒绝文案点名「旧版物品 ID/data 映射」 | PASS |
| ★旧常量 `PREFLATTEN_DATA_ONLY_COMMANDS` 已移除 | PASS |
| ★物品类集合含 `give` / `clear` / `item` / `replaceitem` | PASS |
| ★`tellraw` / `title` 在 1.12 白名单内 | PASS |
| ★★1.12.2 下 `give` / `execute` / `effect` / `summon` / `setblock` 全部拦 | PASS |
| ★1.12.2 下 `time` / `weather` / `say` / `list` / `kick` / `ban` / `pardon` / `tellraw` / `title` 全放行 | PASS |
| ★拒绝文案含「暂不支持该服务端版本的自动命令生成」+ 两条出路 | PASS |
| ★对照：1.13 / 1.20.1 / 1.21 / 未知 / 解析不出 → 全部放行（守门不外溢） | PASS |
| ★`manual=True` → 放行（P1 通道预留） | PASS |
| ★★**AST 扫描：10 个执行入口全部调用 `_guard_command_for_version()`** | PASS |
| ★`_preflatten_block_reason` 文档不再声称「只拦自动构造路径」 | PASS |
| ★新增 `function_system` 分水岭（归 1.13 / `affects` 含 function / `tested=False`） | PASS |

## 十七、第二版自曝取舍

**取舍 19｜`mc_execute_command` 被拦后，1.12 用户失去「兜底手动通道」。**
第一版留它是出于「别让 1.12 用户一条命令都发不出」的善意，但 GPT 的架构论证更硬：
LLM 工具路径不等于人工路径。**代价**：现在 1.12 用户只能去游戏控制台手打。
若主人觉得需要，P1 的人工通道（仅管理员 + WebUI 警告）可以补上 —— 参数已预留。

**取舍 20｜白名单不拆集合（Q1）。** 见第十三节，用注释达成语义澄清。
**请 GPT 判：够不够？**

**取舍 21｜守门放在入口侧、没下沉进 `_exec_checked`（P0-1 建议的「更理想方案」）。**
理由见第十一节。**请 GPT 判。**

**取舍 22｜`function_system` 只登记不实现。** 与 Q6 裁决一致；`/function` 仍在「其余命令族」被拦，
行为零变化。

## 十八、请 GPT 复核（✅ 已答复，见文末「裁决结果」）

1. **P0-1**：10 个入口统一守门 + AST 扫描证明，是否已闭环？
   `mc_workflow` 走内部同判据、不列入入口表，是否可接受？
2. **P0-2**：`give` / `clear` / `item` / `replaceitem` 一律拦，是否已满足裁决？
   拒绝文案（「尚未完成旧版物品 ID/data 映射」）是否准确？
3. **P0-3**：`tellraw` / `title` 进白名单是否合适？还有别的「插件自身依赖但未列入」的命令吗？
4. **取舍 20**：白名单不拆集合、改用注释澄清语义，是否可接受？
5. **取舍 21**：守门放入口侧（不下沉 `_exec_checked`），是否可接受？
6. **Q8 复查**：GPT 列的 8 个直接入口已全部接入。还有**别的**绕过路径吗？
   （例如 `_send_feedback` 的 `tellraw`、`mcs_status` 的 `list`、`mcs_banlist_cmd` 的 `banlist`、
   `mcs_bridge` 相关路径 —— 这些是**插件内部固定命令**，不是用户/LLM 输入，
   皮莉卡判断不需要守门，但它们用的 `banlist` **不在白名单里**。请裁决。）
7. **本版是否已满足「可以提交」的条件**？主人尚未指示提交/发版。

---

# 【裁决结果】GPT 第二版核验（2026-09-23 15:57）

## 结论：**P0 已闭环，可以进入提交和发布准备**

> GPT 原话：「v0.23.2 第二版 P0 已经闭环，可以进入提交和发布准备。」
> 建议：「可以提交 v0.23.2 / 可以创建 pre-release / **不建议直接标记为正式版**」。

### 八项逐条裁决

| # | 项 | 裁决 |
| --- | --- | --- |
| 1 | P0-1 `mc_execute_command` + 10 入口统一守门 | **通过**（「现在该路径已经堵住」） |
| 2 | P0-2 `give` / `clear` / `item` / `replaceitem` 全拦 | **通过，而且比第一版更安全**（「这一点我认可第二版的修正」） |
| 3 | `tellraw` / `title` 白名单 | 当前可以接受 |
| 4 | 白名单不拆集合（取舍 20） | **接受，不阻断**；后续扩展老版本能力时再拆 |
| 5 | 守门放入口侧、没下沉 `_exec_checked`（取舍 21） | **本轮接受**，列为**下一版防御性重构项** |
| 6 | 其他静默错命令路径扫查 | **未发现新的用户/LLM 输入绕过** |
| 7 | `function_system` 分水岭 | 处理正确（先让能力层可见、不虚假声称支持） |
| 8 | 已知 1.12.2 手填 `legacy_nbt` 仍拒绝 | **设计正确，必须保持** |

### 测试复核（GPT 独立运行）

```text
compileall：通过
单元 / 回归测试：30/30 通过
WebUI 真浏览器测试：11/11 通过
test_v0232_preflatten_gate.py：118 项通过
全部通过 ✅
```

> **118 项 = 皮莉卡本地 117 项 + 补 `banlist` 后新增的 1 项断言**，两处口径一致。

### 三处待判 → GPT 的答复

1. **取舍 20（白名单不拆集合）** → 接受，不阻断。
2. **取舍 21（守门不下沉）** → 本轮接受；建议下一版把 `_guard_command_for_version()`
   作为 `_exec_checked()` 的可选统一层，避免未来新增入口漏接。
3. **内部命令不走守门** → 判定合理（命令字符串是插件写死的常量，不是用户/LLM 生成的版本敏感语法）；
   但新增 **P2**：可加 `PREFLATTEN_INTERNAL_SAFE_COMMANDS` 记录
   `tellraw` / `title` / `list` / `banlist`，避免未来新增内部 RCON 命令时漏考虑版本兼容。

### 为什么不建议直接标正式版（GPT 明确）

仍缺真机矩阵，当前唯一真实样本只有 `1.20.1 · Forge 47.4.23`：

```text
Vanilla 1.12.2 / Forge 1.12.2 / Vanilla 1.13.2 / Vanilla 1.20.4 / Vanilla 1.20.5+
```

## 十九、裁决后新增的路线图项（钉进铁匠笔记）

| 优先级 | 项 | 内容 |
| --- | --- | --- |
| P1 | **守门下沉** | `_exec_checked(rcon, command, source=..., manual=...)` 内部统一调用版本守门；真正人工通道显式传 `manual=True` |
| P1 | **人工原始命令通道** | `mcs 原始命令` 或 WebUI `allow_legacy_manual_command`；要求：仅管理员 + 明确提示不保证兼容 + 不允许 LLM 自动调用 |
| P1 | **验证样本结构化** | `verification_samples` 列表（minecraft / loader / loader_version / scope / tested）；`tested` 布尔 → `verified` / `spec_only` / `blocked_unverified` |
| P1 | **旧版能力实现** | `LegacyItemRef(numeric_id, data, legacy_name, modern_name)`、`legacy_preflatten` 档案、`ench` 数字附魔、旧 `execute` —— 均需 1.12.2 真机词典 |
| P2 | **内部命令能力标签** | `PREFLATTEN_INTERNAL_SAFE_COMMANDS`（`tellraw` / `title` / `list` / `banlist`） |

### 长期红线（GPT 点名，必须记住）

若未来出现以下**任一**路径，**必须**强制走统一守门，**不得**复制当前入口侧模式：

```text
用户可配置命令模板
插件配置里的自定义 RCON 命令
新增 LLM 工具直接拼接命令
```

## 二十、当前发布状态（截至 2026-09-23 15:57 · 已被下一节取代）

| 项 | 状态 |
| --- | --- |
| `origin/main` | `104bda4`（未变） |
| `metadata.yaml` | `v0.23.1`（**未抬**） |
| 提交 / 推送 / tag / Release | **均未执行**（等主人拍板） |
| 插件重载 | 未执行 |
| 真机样本 | 仅 `1.20.1 · Forge 47.4.23` |

---

# 【发布】v0.23.2 pre-release（2026-09-23 16:00 主人指令）

> **主人指令**：「推送 pre-release，但这版我会上到应用市场，注意引导用户遇到错误进行反馈」

## 二十一、本版为何是 pre-release，却要上应用市场

- **GitHub 侧标 pre-release**（不占「Latest」正式版位）：因为仍缺真机矩阵 ——
  唯一真实样本只有 `1.20.1 · Forge 47.4.23`，`1.12.2` / `1.13.2` / `1.20.4` / `1.20.5+` 全是按分水岭推导。
- **但会经 AstrBot 应用市场分发**：市场用户会**直接撞到**这版的行为变化，
  尤其是 1.12 及以下服务端会发现「`give` 不能用了」—— 必须让他们**当场**知道这是预期行为、不是故障。

## 二十二、为「上市场」补的反馈引导（四处）

| # | 位置 | 内容 |
| --- | --- | --- |
| 1 | **`main.py` 拒绝文案** | 守门拦下时，回执末尾追加：「如认为此判断有误（例如你确认该命令在旧版同样有效），请带上服务端版本与这条命令原文到项目 Issues 反馈。」—— **用户撞墙的那一刻就能看到怎么反馈** |
| 2 | **README 新增「遇到问题？请反馈」章节** | ① 先列表说明**哪两种现象不是故障**（1.12 以下命令被拒 / 版本未知被拒）；② 反馈要带的五样：插件版本、服务端版本+加载器、原话+实际命令、报错原文或截图、整合包名+关键模组；③ 点名欢迎 `1.12.2` / `1.13.2` / `1.20.4` / `1.20.5+` 的反馈 |
| 3 | **CHANGELOG / Release 说明** | 新增「⚠️ 遇到错误请反馈（本版要上应用市场）」小节，内容与 README 对齐（Release 说明由 CHANGELOG 小节自动生成，故两处一致） |
| 4 | **`metadata.yaml` 的 `help`** | 应用市场与插件信息页会展示，末尾补「遇到错误请到项目 GitHub Issues 反馈，并附上插件版本、服务端版本与加载器、你的原话与插件实际执行的命令、报错原文或截图」 |

**设计意图**：把「**这不是 bug**」和「**这确实是 bug，这样报**」两件事分开说清 ——
既不让用户为预期行为白白开 issue，也不让真问题因为「以为是预期行为」而漏报。

## 二十三、提交前追加的两处非逻辑改动（如实登记）

> GPT 第二版核验的对象是「追加这两处之前」的工作区。两处均为**纯文案**，
> 未触及守门逻辑、白名单、判据或任何断言：

1. `main.py::_guard_command_for_version()` 的**返回文案**末尾追加反馈引导（第 4 处）；
2. `metadata.yaml` 的 `version` 由 `v0.23.1` → `v0.23.2`（发版必需），`help` 追加反馈引导。

测试已按追加后的工作区**重跑**（结果见下节）。

## 二十四、发布动作与留痕

| 项 | 值 |
| --- | --- |
| 版本号 | `v0.23.1` → **`v0.23.2`**（`metadata.yaml`） |
| pre-release 触发方式 | CHANGELOG 小节标题写作 `## [v0.23.2] - 2026-09-23（预发布）`，`release.yml` 据此自动标 pre-release |
| 发布包 | 由 `release.yml` 用 `git archive` 生成，`.gitattributes` 排除 `tests/` / `docs/VERIFY_*.md` / `docs/*.diff` / `run_*.py` |

### 追加改动后的全量复跑（同一工作区）

```text
② 回归 / 单元测试：30/30 通过
③ WebUI 真浏览器链路：11/11 通过
test_v0232_preflatten_gate.py：118 项通过
全部通过 ✅
```

### 反馈引导的第五处

`_conf_schema.json` → `connection.items.server_version_override` 的 `hint` 末尾追加：
「**遇到与版本相关的错误（含「明明版本填对了却被拒绝」）请到项目 GitHub Issues 反馈**，
并附上插件版本、服务端版本与加载器、你的原话与插件实际执行的命令、报错原文或截图。」——
用户在设置页手填版本的地方，恰好也是最容易产生困惑的地方。

## 二十五、发布完成留痕（2026-09-23 16:12）

| 项 | 值 |
| --- | --- |
| 提交 | `f2a2f82`（14 files changed, 3216 insertions(+), 17 deletions(-)） |
| tag | `v0.23.2`（annotated） |
| 远端 main | `f2a2f82` |
| Release | **pre-release**（`prerelease=true`, `draft=false`） |
| Release URL | https://github.com/HuangGuaKnn/astrbot_plugin_Scintilla_MC_Server_Control/releases/tag/v0.23.2 |
| 附件 | `astrbot_plugin_Scintilla_MC_Server_Control_v0.23.2.zip` · 2,057,841 字节 · state=uploaded |
| CI | `release` ✓ / `tests` ✓ 双绿 |
| Latest 正式版 | 仍为 **v0.23.1**（pre-release 未占位 ✓） |
| Release 说明 | 4,322 字符，含「⚠️ 遇到错误请反馈」小节 ✓ |

### 发布过程中的一个误判（如实登记）

- 皮莉卡用 `GET /repos/{repo}/releases/tags/{tag}` 核对附件时，该端点返回 `assets: []`，
  于是误判「CI 没上传附件」，并删掉重建了一次 Release。
- **实际 CI 早已上传成功**（重新上传时报 `422 already_exists` 就是证据）。
- **根因**：`/releases/tags/{tag}` 的 `assets` 字段存在缓存，而 `/releases/{id}/assets` 是实时的。
  最终以「直接下载 zip（HTTP 206 + `PK\x03\x04` 魔数）」与「`/releases/{id}/assets`」双重验证确认真实存在。
- **结论**：**以后核对附件一律走 `/releases/{id}/assets` 或直接下载链接，不要信 `/releases/tags/{tag}`**。
  本次重建没有造成任何损失（附件名、大小与 CI 产物一致，tag 未动）。
