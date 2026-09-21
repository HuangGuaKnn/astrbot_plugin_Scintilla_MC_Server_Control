# GPT 咨询单 · 跨版本命令语法 & 成功判定收口

> 版本：v0.22.5 线上实测暴露 ｜ 日期：2026-09-21 ｜ 提交坐标：`de2bc6a`
> 仓库根：`C:\Users\10316\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control`

---

## 0. 这份单子怎么用（请先读）

- 本文分三类：**【事实】**（已用日志/实测钉死）、**【推断】**（有证据但未穷尽）、**【待裁决】**（请你判断）。
- 所有坐标都是 `仓库相对路径:行号`，可直接定位。
- **请不要给「建议加强校验」这类泛泛结论。**我们要的是：可复现的判据、具体改动点、以及能反驳我们的反例。
- 如果你认为我们的根因判断有误，请给出**能推翻它的具体路径**（哪一行、什么输入、什么结果）。

---

## 1. 触发事件

### 【事实】1.1 现场

用户在群聊请求：给 `HuangGuaKnn` 发一把下界合金剑，附魔锋利5、亡灵杀手5、节肢杀手5、击退2、火焰附加2、横扫之刃3、耐久3、经验修补。

- 服务端：**MC 1.20.1 · Forge 47.4.23**（`/server/status` 实测返回）
- 插件回执：`[MC工作流·完成] 已执行 1/1 条命令` → 聊天侧被渲染成 ✅ 完成
- 实际结果：**游戏内无物品、无提示**
- WebUI 工作流日志：`19:18:04 ✓ 完成 simple`

### 【事实】1.2 分类器实际生成的命令

从 AstrBot 日志 `sources.openai_source:577` 的 completion 原文中提取（该 completion id = `gen_01M31TZTC5J27582JP1HZTQVCC`）：

```
give HuangGuaKnn netherite_sword[enchantments={levels:{"minecraft:sharpness":5,
"minecraft:smite":5,"minecraft:bane_of_arthropods":5,"minecraft:knockback":2,
"minecraft:fire_aspect":2,"minecraft:sweeping_edge":3,"minecraft:unbreaking":3,
"minecraft:mending":1}}] 1
```

两处错误：
1. `[enchantments={levels:{...}}]` 是 **1.21+ 的物品组件语法**；1.20.1 只接受 NBT `{Enchantments:[{id:"...",lvl:N}]}`。
2. `minecraft:sweeping_edge` 在 1.20.1 **不存在**（1.20.1 叫 `minecraft:sweeping`；`sweeping_edge` 是 1.21 的名字）。分类器自己的 reasoning 里写着 `sweeping_edge 3 (sweeping)`，把两版名字混用了。

### 【事实】1.3 服务器真实回应

原样执行该命令，RCON 返回：

```
Expected whitespace to end one argument, but found trailing data
```

即**解析失败，命令从未执行**。

对照组：把同一请求改写成 1.20.1 正确的 NBT 写法后执行，返回

```
Gave 1 [Netherite Sword] to HuangGuaKnn
```

→ 证明**服务器本身完全正常**，问题在命令生成与结果判定。

---

## 2. 根因（三层）

### 【事实】2.1 直接原因：LLM 缺少版本上下文

`core/agent_prompts.py` 全文**没有任何一处**向 Agent 提供 MC 版本 / 服务端形态 / 语法世代信息。
`CLASSIFIER_SYSTEM`（`core/agent_prompts.py:10`）与 `IMPLEMENTER` 提示词（同文件 `:78` 起）都只要求「严格套用知识库模板的 NBT 格式」，但**当前知识库为空（0 条）**，等于让 LLM 凭记忆猜版本。

### 【事实】2.2 结构性原因：分类规则自相矛盾，把这类请求送进无保护路径

`core/agent_prompts.py:13-14`：

- `:13` → **`"simple"`：仅用原版 Minecraft 命令即可完成的任务。例如：发原版物品（diamond_sword…）**
- `:14` → **`"complex"`：…复杂 NBT 结构（附魔、枪械附件 Attachments…）…**

「附魔下界合金剑」**同时命中两条**（既是原版物品，又是复杂 NBT）。实测 LLM 选了 `simple`。

而两条路径的**防护等级完全不同**：

| 路径 | 代码位置 | 输出校验 | 纠错循环 | 游戏内反馈 |
| --- | --- | --- | --- | --- |
| simple | `core/workflow.py:117` `_run_simple()` | **无** | 无 | **无** |
| complex | `core/workflow.py:485` `_exec_commands()` | 有（`_is_success_out`） | 有（最多 2 轮纠错） | 有 |

→ **规则冲突恰好把最需要校验的请求路由到了唯一没有校验的路径上。**

### 【事实】2.3 直接漏洞：`_run_simple` 把「服务器报错文本」当成功

`core/workflow.py:148-151`：

```python
out = await rcon.command(cmd)
results.append(f"{cmd} → {str(out).strip()[:120]}")
ok_count += 1        # ← 只要不抛异常就 +1，完全不检查返回内容
```

服务器「正常地回了一句报错」在 RCON 层面算成功收到响应，于是 `ok_count = 1`。

紧随其后的 `core/workflow.py:157-160`：

```python
summary = f"已执行 {ok_count}/{len(commands)} 条命令"
if ok_count == len(commands):
    return summary          # ← 成功分支丢弃 results，服务器报错文本彻底消失
```

→ 上层（LLM）看不到任何失败信号，于是自信地写了 ✅ 回执。

**关键讽刺**：仓库里**已经有**正确的校验函数 `core/workflow.py:597` `_is_success_out()`，其失败词表**包含 `"expected"`**，能稳稳接住这条报错。复杂路径 `core/workflow.py:532` 用了它。**简单路径是漏用了已有 helper。**

### 【事实】2.4 同类问题的规模

| 统计项 | 数值 |
| --- | --- |
| `rcon.command(` 调用点总数（`main.py` + `core/*.py`） | **30** |
| 其中调用 `_is_success_out()` 做输出校验的 | **2** |

已确认的第二处：`main.py:1670-1673`（`mc_execute_command` 工具）——只要 `out` 非空就写「命令执行成功：…」。实测中它把同一条解析错误标成了「命令执行成功」。

### 【推断】2.5 这是一族反复出现的问题

- v0.22.2 修「超时假成功」
- v0.22.4 修「合法空响应假成功」
- 本次是「**报错文本假成功**」

三次根因相同：**「什么算成功」没有统一收口**，每个调用点各判各的。

---

## 3. 仓库里已有的能力（请勿重复发明）

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| **服务端版本探测** | `main.py:2477` `_detect_server_version()` | 从 `logs/latest.log` 启动行 → `libraries/net/minecraftforge/forge/<mc>-<forge>` → 根目录 jar 名。实测能返回 `MC 1.20.1 · Forge 47.4.23` |
| **服务端形态** | `core/mod_fingerprint.py:280` `describe_shape()` | 产出 `{"sw": "forge", "mc": "1.20.1"}` 与 `shape_text`（如 `forge 1.20.1`） |
| **输出成功判定** | `core/workflow.py:597` `_is_success_out()` | 失败词表含 `unknown/error/failed/invalid/usage:/expected/…` |
| **执行结果三态** | `core/workflow.py:485` `_exec_commands()` | 已有 `success / failed / unknown / skipped` 四态 |
| **调用点清单** | `rcon_call_sites.txt`（仓库根） | 30 处调用点的带行号摘录，可直接用 |

**核心矛盾**：版本探测能力**已经存在**，但**只喂给了 WebUI 的 `/server/status`**，从未进入任何 Agent 提示词。

---

## 4. 约束条件（不可违反）

1. **只走 RCON**，不能要求服务端装任何 mod/插件。
2. 必须同时支持：原版 / Forge / Fabric / NeoForge / Paper / Spigot / Purpur，覆盖 **1.12 ~ 1.21+**。
3. **非幂等命令（give/summon/effect/item/xp/fill/clone/function…）绝不自动重发**——重复即重复副作用。这条是硬约束，前几个版本为它付出了很多设计成本。
4. 不得用「猜」的方式补版本信息：探测不到时**必须显式降级**并让上层知道，不能默认某个版本。
5. 异地 RCON 模式（`remote_rcon_mode=true`）下**读不到服务端文件**，版本探测会失败（`main.py:2485` 已显式返回空）。

---

## 5. 请裁决的问题

### Q1【P0】跨版本语法应该怎么架构？

我们倾向：**不要让 LLM 自由决定语法世代**，而由代码按版本产出「语法约束片段」注入提示词（例如 1.20.5 以下 → 强制 NBT 写法；1.20.5 及以上 → 强制组件写法），并在提示词里给出**该版本的正确模板与附魔 ID 对照表**。

请裁决：
- a) 这个「由代码决定语法世代」的方向对不对？有没有更好的架构？
- b) 附魔 ID 改名（`sweeping` ↔ `sweeping_edge` 等）这类**注册表级差异**，应该用「版本→ID 映射表」硬编码，还是有更可维护的做法？
- c) 1.20.5 是 NBT→组件的分水岭，还有哪些**同类分水岭**（如 1.13 execute 重写、1.19.x 的 `data` 变更）必须在同一张表里表达？请给出**完整分水岭清单**。

### Q2【P0】成功判定如何统一收口？

我们倾向：抽出唯一的执行入口，返回四态 `success / failed / unknown / rejected`，**30 处调用点全部改走它**，禁止再直接调 `rcon.command()`。

请裁决：
- a) 这个收口粒度对不对？`rejected`（权限闸门拒绝）该独立成态还是并入 `failed`？
- b) `_is_success_out()` 目前是**字符串黑名单**（匹配 `error/expected/invalid/…`）。这个思路在**模组环境下**（模组命令返回文本千奇百怪）会不会大量误判？有没有更稳的判据？
- c) 反过来：**误判成失败**比误判成成功更安全吗？在「非幂等命令」语境下，误判失败会不会诱发重复副作用？

### Q3【P1】「解析失败」是否可以安全重试？——这是我们认为最值得讨论的一点

**我们的观点：可以，而且必须重试。**

理由：解析失败意味着**服务器根本没执行这条命令**，不存在重复副作用风险。这与「结果未知」（命令可能已执行）是**两种完全不同**的状态，不应共享同一条「禁止重试」策略。

因此我们想：把「服务器明确报解析/语法错误」单独归为一态（如 `syntax_error`），**允许安全地喂回实现器/纠错 Agent 重新生成**，甚至允许一次自动重写重试。

请裁决：
- a) 这个区分在 RCON 语义下是否成立？有没有「表面像解析错误、实际已产生副作用」的反例？
- b) 若成立，安全重试的次数上限、以及「重试前是否必须重新探测在线玩家」怎么定？
- c) 如何**在代码层面可靠地区分** `syntax_error` 与 `unknown`？（我们目前只能靠输出文本匹配）

### Q4【P1】分类规则冲突怎么修？

`agent_prompts.py:13` 与 `:14` 对「原版物品 + 复杂 NBT」的定义重叠，LLM 选了错的那条。

请裁决：
- a) 应该改**规则**（让「带附魔/NBT 的原版物品」明确归 complex），还是改**架构**（让 simple 路径也具备同等校验能力，使分类失误不再致命）？
- b) 我们的倾向是**两者都做，但架构优先**——因为不能指望提示词 100% 可靠。请评估这个优先级。

### Q5【P2】版本探测不到时怎么办？

异地 RCON 模式、或服务端目录未配置时，版本探测返回空。此时：
- a) 应该**拒绝生成含 NBT 的命令**，还是**退化为最保守的语法**？
- b) 能否用 RCON 侧手段探测版本？（我们的实测：原版/Forge **没有** `version` 命令，`main.py:2478` 已注明；`list` 输出也不含版本。）请给出**不依赖服务端文件**的可行方案，若确实没有，请明确说「没有」。

### Q6【P2】可追溯性缺口

本次能破案，**纯属运气**：日志里恰好留了 LLM 原始 completion。而插件自身的审计日志（`main.py:1666`）**只覆盖 `mc_execute_command` 工具**，工作流执行的命令**一条都没记**，且 `_run_simple` 成功时连结果明细都丢弃。

请裁决：命令级审计日志应该记在哪一层、记哪些字段（命令原文 / 服务器原文 / 判定结果 / 判定依据），才能让下一次同类问题**不需要靠运气**。

---

## 6. 我方候选方案（请挑刺或合并）

| 方案 | 内容 | 我方评估 |
| --- | --- | --- |
| A | 把版本 + 语法世代注入所有 Agent 提示词 | 成本最低，但依赖 LLM 守规矩 |
| B | 代码侧维护「版本 → 语法世代 → 模板/ID 对照表」，按版本产出提示词片段 | 与 A 叠加，把不确定性从 LLM 挪到代码 |
| C | 执行后按输出判定四态；`syntax_error` 允许安全重写重试 | **我方认为收益最大**（见 Q3） |
| D | 含 `{`/`[`/附魔/NBT 的请求**禁止走 simple 路径** | 兜底，防分类失误 |
| E | 提示词同时给「NBT 版」与「组件版」两套模板，按版本二选一 | 简单，但提示词会变长，可能稀释注意力 |

---

## 7. 交付要求

1. 每条结论请标注：**采纳 / 部分采纳 / 驳回**，驳回请给理由。
2. 涉及改动的，请给 `文件:行号` 与**改动要点**（不必给完整代码）。
3. 每条改动请附**可复现的验证方法**，最好包含**对照实验**（例：「删掉 X 这一行，断言 Y 立刻变红」）。
4. 若你认为上述任一【事实】判断有误，请优先推翻它。
