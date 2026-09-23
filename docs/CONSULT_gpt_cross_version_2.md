# GPT 咨询单 · 跨版本命令语法覆盖矩阵（续单）

> **前单**：`docs/CONSULT_gpt_cross_version.md`（v0.22.5 事故，2026-09-21，提交 `de2bc6a`）
> **本单**：v0.23.1 上架后的**补漏追问** ｜ 日期：2026-09-23 ｜ 当前提交 `7dde230`（tag `v0.23.1`）
> **仓库根**：`C:\Users\10316\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control`
> **市场状态**：v0.23.1 已 Published，VirusTotal 与 Claude Code Agent 安全检查均为 Low

---

## 0. 这份单子怎么用（与前单同规范）

- 三类标注：**【事实】**（有代码坐标或日志钉死）、**【推断】**（有证据未穷尽）、**【待裁决】**（请你判断）。
- 坐标一律 `仓库相对路径:行号`。
- **不要给「建议加强校验」这类泛泛结论**：要可复现判据、具体改动点、能推翻我们的反例。
- 前单已裁决并落地的部分（版本上下文注入、六态判定、复杂路由守门）**本单不再重复**，只做**覆盖面**的追问。

---

## 1. 为什么再开一单

### 【事实】1.1 前单问过「完整分水岭清单」，但落地只有 4 条

前单 §5 Q1c 原文：

> 1.20.5 是 NBT→组件的分水岭，还有哪些同类分水岭（如 1.13 execute 重写、1.19.x 的 `data` 变更）必须在同一张表里表达？请给出**完整分水岭清单**。

前单之后的裁决落地成了 `core/version_caps.py:173 CUTOVERS`，目前**只有 4 条**：

| # | 版本 | 标题 |
| --- | --- | --- |
| 1 | 1.13 | execute 重写 |
| 2 | 1.20.5 | 物品 NBT → 物品组件（核心） |
| 3 | 1.21 | 附魔 ID 改名 sweeping → sweeping_edge |
| 4 | 1.21.2 | 属性 ID 去掉分组前缀 |

### 【事实】1.2 前单的约束写着「覆盖 1.12 ~ 1.21+」，实现却没有 <1.13 路径

前单 §4 约束条件第 2 条原文：

> 必须同时支持：原版 / Forge / Fabric / NeoForge / Paper / Spigot / Purpur，覆盖 **1.12 ~ 1.21+**。

而当前实现：

- 语法世代只有两档 —— `core/version_caps.py:38`（`legacy_nbt`）/ `:40`（`components`），分界只有 `ITEM_COMPONENTS_CUTOVER = (1, 20, 5)`（`:45`）；
- 全仓搜索 `1.13` **只命中 `core/version_caps.py:180` 的一句描述文本**，没有任何 <1.13 的代码路径；
- 命令生成点 `main.py:1891` / `:1903` 固定产出 `give {player} {item} {count}`，这是 **1.13+ 的参数顺序**。

### 【推断】1.3 后果：对 1.12 服务端会「静默生成错命令」

1.12 及以下要求 `/give @p item amount data {NBT}`（NBT 前必须有两个位置参数）。我们生成的形式在 1.12 上会解析失败，而版本层**不会介入、不会拒绝、也不会警告** —— 这是 v0.22.5 那类「假成功」的同类隐患，只是换了触发条件。

---

## 2. 现状事实（代码坐标）

| 环节 | 位置 | 现状 |
| --- | --- | --- |
| 版本事实来源 | `core/version_caps.py:124` `resolve_version_info()` | 手动声明 > 文件探测 > 未知 |
| 版本探测 | `main.py:2602` `detect_server_version()` | logs → libraries → jar 名；异地模式直接返回空 |
| 语法世代 | `core/version_caps.py:145` `item_syntax_for()` | **两档**：<1.20.5 = `legacy_nbt`；≥1.20.5 = `components`；未知 = `unknown`（拒绝生成带数据命令） |
| 逃生出口 | `core/version_caps.py:156` + 配置项 `item_syntax_override` | 手填 `legacy_nbt` / `components`，版本未知时也能放行 |
| 分水岭清单 | `core/version_caps.py:173` `CUTOVERS` | 4 条（见 §1.1） |
| 附魔 ID 改名 | `core/version_caps.py:214` `ENCHANT_RENAMES` | 1 条（sweeping） |
| 提示词注入 | `core/version_caps.py:274` `build_version_context()` → `core/agent_llm.py:122` `_effective_system()`（前置到 system prompt） | 在分类器之前注入（`core/workflow.py:104`） |
| 运行期判定 | `core/command_result.py:617` | 语法错 → `syntax_error`（16 个标记，见 `:486`） |
| give 生成 | `main.py:1891` / `:1903` | `give <player> <item> <count>`（1.13+ 顺序） |

---

## 3. 版本区间真实差异（本轮查证）

### 【事实】3.1 `/give` 至少有三个世代

| 世代 | 版本区间 | 形态 |
| --- | --- | --- |
| 1 | **1.8 ~ 1.12.2** | `/give @p item amount data {NBT}` —— 数字 ID 与**旧附魔格式** |
| 2 | **1.13 ~ 1.20.4** | `/give @p minecraft:item{NBT} amount` —— 数据值参数取消、NBT 贴 ID、数量后置 |
| 3 | **1.20.5+** | `/give @p minecraft:item[components] amount` |

查证来源：

- `https://mineschematic.com/tools/give-generator` —— 原文：「Minecraft has changed /give **three times**. On 1.20.5 and newer it uses item components, on **1.13 to 1.20.4** it uses an NBT tag, and on **1.8 to 1.12.2** it uses **numeric ids and the old enchantment format**.」
- `https://dedicatedminecraft.host/tools/give-command` —— 原文：「Java 1.13+ `/give @p minecraft:item[components] amount`；Java 1.12 and below `/give @p item amount data {NBT}`」
- `https://gaming.stackexchange.com/q/330789` —— 1.13 起 `give @p <item> 1 0 {NBT}` 里的 `1 0` 两个位置参数消失。

**我们目前只覆盖世代 2 与世代 3。**

### 【事实】3.2 1.13 是一次命令系统大断层

- **扁平化（The Flattening）**：数字 ID 移除；大量方块 / 物品 / 生物群系 / 粒子 / 实体 / 统计 / 音效 ID 改名。来源：Minecraft Wiki「Java Edition 1.13/Flattening」。
- **`execute` 重写**：`execute <实体> <x y z> <命令>`（含 `detect` 子命令）→ `execute as/at/positioned/... run <命令>`。

### 【待核实】3.3 需要你补齐的部分

1. 1.8~1.12.2 的物品 NBT 里，附魔是 `Enchantments:[{id:"minecraft:sharpness",lvl:5}]`（字符串 ID）还是必须 `ench:[{id:16,lvl:5}]`（数字 ID）？两者在该区间是否都有效？（生成器文档称老版本用「old enchantment format」，我们无法确定指哪一种。）
2. 1.11 的实体 ID 改名（如 `EntityHorse` → `horse`）是否影响 `summon` 类命令生成？
3. 1.12 新增的 `/function`、`/advancement`、`/recipe` 是否需要在清单里表达？
4. 1.14.x 相对 1.13.2 有哪些**会影响我们生成命令**的差异？
5. 1.13 之前 `execute` 的完整语法（含 `detect`）与我们的生成面是否相交？

---

## 4. 问题清单（请逐条回应）

### P1【事实 · 文档】1.13 条目只写了 execute，漏了 give 参数顺序

`core/version_caps.py:174-181` 的 1.13 条目只描述 `execute` 重写，**完全没提 `give` 的参数顺序变化**。而生成点固定用 1.13+ 顺序 → **清单描述与实际生成行为不一致**。纯文档修正，**零风险**。

### P2【事实 · 真缺口】<1.13 没有任何处理路径

见 §1.2 / §1.3。当前对 1.12.2 服务端：不拒绝、不警告、按 1.13+ 生成 → 解析失败。

### P3【事实 · 单向】`execute` 分水岭只写了一个方向

现文只讲「旧写法在 1.13+ 会失败」，没讲「**在 <1.13 上必须用旧写法**」。若服务端是 1.12.2，我们仍会让 LLM 用新写法。

### P4【推断】老版本附魔 NBT 形式未评估

见 §3.3 第 1 条。这直接决定 B 方案（见 §5）是否可行。

### P5【推断】老版本其它差异未盘点

见 §3.3 第 2~5 条。

### P6【待裁决】语法世代是否需要第三档

是否引入 `legacy_preflatten`（<1.13）：`give` 生成 `<count> <data> {NBT}` 形式 + 旧 `execute` 模板？代价是只能靠资料判据、**我们无法实测**（见 P8）。

### P7【已化解 · 请确认】物品 ID 层不需要版本映射

插件物品词典是从**服务端 `mods/` 目录解析各 mod 的 jar** 生成的（`core/item_dictionary.py:3`；Paper/Spigot 无 `mods/` 时退化为从服务端 jar 读原版物品表，`:64`）。也就是说 1.12.2 服务端扫出来的就是 1.12.2 的 ID，**1.13 扁平化改名不会踩到**。

请确认这个理解无误（即：不必在 `CUTOVERS` 里表达 ID 改名）。

### P8【约束】验证条件

真机矩阵目前**只有 1.20.1 · Forge 47.4.23**；我们**没有** 1.8~1.12.2 / 1.13~1.20.4 的服务端可供实测。任何 <1.13 的改动都将是**未实测交付**，需要你判断：这种改动该不该做、该怎么标注风险。

---

## 5. 候选方案（请挑刺 / 合并 / 排序）

| 方案 | 内容 | 我方评估 |
| --- | --- | --- |
| **A** | 补全清单 + 显式警告：<1.13 时 WebUI 黄字 + 提示词注入「本插件命令生成面向 1.13+，带数据命令请手动构造」 | 成本低，**立刻消除「静默生成错命令」**；但不提升老版本可用性 |
| **B** | 增加 `legacy_preflatten` 世代：按 1.12 语法生成 give / execute | 真支持 1.12；但**无法实测**，且要先解决附魔 NBT 形式问题（P4） |
| **C** | 保守拒绝：<1.13 时带数据的命令一律拒绝生成（与「版本未知」同口径） | 与现有 fail-closed 一致；但老版本用户会觉得「什么都不能做」 |
| **D** | 收窄约束：把「覆盖 1.12 ~ 1.21+」改为「覆盖 1.13 ~ 1.21+」，并在文档 / UI 明示 | 最诚实、成本最低；等于放弃 1.12 用户 |

我方倾向：**A 与 D 二选一先落地（消除隐患）**，B 视你的判断；无论选哪个，**P1 都应立刻修**（纯文档修正）。

---

## 6. 请裁决的问题

- **Q1【P0】** 「覆盖 1.12 ~ 1.21+」这条约束现在还算数吗？若算数，B 是唯一出路吗？若不算数，D（收窄到 1.13+）你认可吗？
- **Q2【P0】** 1.8~1.12.2 的附魔 NBT 形式（P4 / §3.3-1）请给准确答案 —— 这决定 B 是否可行。
- **Q3【P1】** `give` 三世代之外，还有哪些**会让我们生成的命令解析失败**的硬分水岭？请直接给清单（含版本号与判据）。
- **Q4【P1】** 未实测的老版本支持值不值得做？若做，如何在代码 / 文档 / UI 上**诚实标注「未实测」**？
- **Q5【P1】** P7 的理解对吗 —— 物品 ID 层交给服务端词典扫描，清单里不必表达 ID 改名？
- **Q6【P2】** P1 的文档修正有没有更好的写法（例如给 `CUTOVERS` 增加「方向」字段：`requires_below` / `breaks_above`）？

---

## 7. 交付要求（同前单）

1. 每条标注 **采纳 / 部分采纳 / 驳回** + 理由；
2. 涉及改动的给 `文件:行号` + 改动要点（不必给完整代码）；
3. 每条改动附**可复现的验证方法**，最好含**对照实验**；
4. 若认为上述任一【事实】判断有误，**请优先推翻它**。
