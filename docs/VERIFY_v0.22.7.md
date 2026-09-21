# v0.22.7 核验交接单（精准路径版，交给 GPT 复核用）

## 0. 坐标约定

- 仓库根：`C:\Users\10316\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control`
- 下列路径**均相对仓库根**，行号为 **2026-09-22 00:40 工作区实测**（未提交状态；提交后行号可能漂移，请以 `docs/v0.22.7.diff` 为准）
- 基线：`7b5daee`（v0.22.6，已推送 `origin/main`）。对照旧行为用 `git show 7b5daee:core/command_result.py`
- 版本号：`metadata.yaml:6` → `v0.22.7`
- 本轮状态：**仅工作区** —— 未提交、未打 tag、未建 Release、未重载线上插件

### 随附证据

| 证据 | 路径 |
| --- | --- |
| 全量 diff（含未跟踪新文件） | `docs/v0.22.7.diff`（141,044 字节；导出方式 `git add -N` 未跟踪文件后 `git diff`，索引已还原）——**未入库**，属工作区导出物 |
| 全量测试结果 JSON | `C:\Users\10316\.astrbot\data\plugins\v0226_test_results.json`（插件目录上一级，未入库） |
| 跑批脚本 | `run_v0226_tests.py` / `run_v0226_ui.py`（逐个跑 `tests/test_*.py` / `tests/ui_*.py`，超时 120s，结果落盘 JSON）——**未入库**（写死本机解释器路径，与 `.gitignore` 里的 `run_full.py` 同类） |

改动面（`git diff --stat`，12 文件 / +1129 −121）：

```
 CHANGELOG.md                         |  77 ++++
 core/command_result.py               | 332 ++++++++++++++-----
 core/rcon.py                         |  87 +++++--
 core/web_api.py                      |  71 +++---
 core/workflow.py                     |  18 +-
 main.py                              |  37 ++-
 metadata.yaml                        |   2 +-
 pages/mc_control/index.html          |  71 ++++++
 run_v0226_tests.py                   |   8 +
 run_v0226_ui.py                      |   8 +
 tests/test_command_result.py         |  79 +++++-
 tests/test_v0227_result_hardening.py | 460 +++++++++++++++++++++++++++++++++++
```

---

## 1. 核验对象：上一轮裁决的 8 条

### ① 非幂等命令的**空响应** → `unknown`（不得被更宽的分支截走）

| 角色 | 精准路径 |
| --- | --- |
| 判定入口与顺序说明（1～9 步） | `core/command_result.py:516` `def classify_command_output(`（docstring 第 1 步即本条） |
| 非幂等集合 | `core/command_result.py:107` `NON_IDEMPOTENT_COMMANDS = frozenset({`（give / summon / effect / item / fill / function …） |
| 空响应分支·非幂等先判 | `core/command_result.py:559` `if name in NON_IDEMPOTENT_COMMANDS:` → `unknown` + `retryable=False`，**在 `boundary_confirmed` 判断之前** |
| 幂等/静默分支（对照） | `core/command_result.py:566` 起（`boundary_confirmed is True` → `success`；`response_received` → `dispatched_unconfirmed`） |
| 判定辅助 | `core/command_result.py:436-438` `is_non_idempotent_command()` |

请核验：`dispatched_unconfirmed` 的 `accepted` 语义（`core/command_result.py:309-314`，`accepted = status in ("success","inferred_success","dispatched_unconfirmed")`）——全仓是否还存在**别的**能让非幂等命令拿到 `accepted=True` 的路径（那等于拆掉 v0.22.4 的重复副作用保护）。

测试：`tests/test_v0227_result_hardening.py:90`（组一，含「非幂等 + 空响应 + 边界已确认」与「边界未确认」两路）

### ② 非幂等命令在「边界未确认 + 非空响应」下也必须是 `unknown`

- 实现：`core/command_result.py:611` `if name in NON_IDEMPOTENT_COMMANDS:`（位于第 6 步「边界未确认」分支内，**早于**第 7 步「明确成功」）
- 消息类（`tellraw` / `say` / `title`）与幂等命令仍报 `dispatched_unconfirmed`（同分支下半段）
- 请核验：该步位置是否确实**早于**任何 `success` 返回点（对照 `core/command_result.py:602` 的第 5 步与随后的第 7 步）

测试：`tests/test_v0227_result_hardening.py:135`（组二）

### ③ `Operation aborted` 必须判失败

| 角色 | 精准路径 |
| --- | --- |
| 短语失败标记（子串匹配，含空格短语） | `core/command_result.py:156` `_PHRASE_FAILURE_MARKERS = (` |
| 本轮新增项 | `core/command_result.py:169` `"operation aborted",`（同组还有 refused / cancelled / rejected / denied） |
| 判定函数 | `core/command_result.py:469` `def is_explicit_failure_output(`；短语判定 `:478` |
| 中文标记 | `core/command_result.py:189` `_CJK_FAILURE_MARKERS = (`；判定 `:480` |

测试：`tests/test_v0227_result_hardening.py:167`（组三）

### ④ 失败词**词边界**匹配 + 锚定成功模式优先于失败词扫描

| 角色 | 精准路径 |
| --- | --- |
| 英文失败词（词边界） | `core/command_result.py:180` `_WORD_FAILURE_MARKERS = (`；正则 `:193-194` `_FAILURE_WORD_RE`（`\b(?:...)\b`） |
| 锚定成功模式表 | `core/command_result.py:204` `ANCHORED_SUCCESS_PATTERNS: dict[...]`；`give` 项 `:205` `(r"^gave\b",)`；编译 `:249` |
| 锚定判定 | `core/command_result.py:485` `def is_anchored_success_output(` |
| 判定顺序落点 | `core/command_result.py:602` `if not anchor_ok and is_explicit_failure_output(out_s):` —— 已拿到锚定成功证据时**跳过**全局失败词扫描 |

请核验两点：
1. `Gave 1 [Diamond] to Error`（玩家名叫 `Error`）是否确实判 `success`（锚定 `^gave\b` 先命中）；
2. `Operation error:` 这类**非锚定**错误输出是否仍判 `failed`（`\berror\b` 词边界命中）。

测试：`tests/test_v0227_result_hardening.py:167`（组三，含正反两向用例）

### ⑤ 未知非空输出不得默认 `success`

| 角色 | 精准路径 |
| --- | --- |
| 第 8 步（无成功证据） | `core/command_result.py:650` `if name in NON_IDEMPOTENT_COMMANDS:` → `unknown`；否则 `inferred_success` |
| 开关 | `core/command_result.py:71` `INFERRED_SUCCESS_ENABLED = True`（置 False 即全落 `unknown`） |
| 状态名与 `ok` 语义 | `core/command_result.py:75`（状态名元组）、`:48`（`inferred_success` 的 `ok` 为 `False`）、`:309-314`（`accepted`） |
| 工作流单列（不并入成功） | `core/workflow.py:225-237`（`inferred` 单独计数）、`core/workflow.py:711`（标签「已执行·未确认」）、`core/workflow.py:748` |
| 游戏内回执措辞 | `main.py:1621` `_render_result(`、`main.py:1636` `if r.status == "inferred_success":`（明确写「未确认」）、`main.py:1766` |

请核验：`inferred_success`（`accepted=True` / `ok=False`）是否还有任何消费方把它当成功（例如工作流 `done` 判定、UI 计数、`success` 字符串比较）。

测试：`tests/test_v0227_result_hardening.py:247`（组四）、`:381`（组七）

### ⑥ RCON 异常必须**分阶段**（只有 connect 才允许判 failed）

| 角色 | 精准路径 |
| --- | --- |
| 阶段常量 | `core/rcon.py:98-104` `PHASE_CONNECT / PHASE_SEND / PHASE_READ / PHASE_PROTOCOL / PHASE_UNKNOWN` |
| 「命令可能已执行」判据 | `core/rcon.py:119-122` `command_may_have_run`（`phase != PHASE_CONNECT`） |
| 异常类型 | `core/rcon.py:128` `RconTimeoutError.__init__(..., phase="read")`；`core/rcon.py:133` `RconPartialResponseError`（继承 Timeout，语义一致） |
| connect 阶段标注点 | `core/rcon.py:210`、`:223`、`:233`、`:238`、`:242`、`:248`（**只有这些判 failed 安全**） |
| protocol 阶段标注点 | `core/rcon.py:275`、`:285`、`:428` |
| send 阶段 | `core/rcon.py:357` `raise RconError(f"RCON 发送失败: {e}", phase=RconError.PHASE_SEND)` |
| read 阶段 | `core/rcon.py:389`、`:406` |
| 部分响应 / 超时 | `core/rcon.py:417`、`:464`（`RconPartialResponseError`）、`:474`（`RconTimeoutError`） |
| 响应到达标志 | `core/rcon.py:181`（声明）、`:342`（置 False）、`:399`（置 True） |

请核验：`except` 侧（`main.py` / `core/workflow.py`）是否**按 `phase` 分流**，而不是一律判 `failed`；以及 `PHASE_UNKNOWN`（未标注）是否走了最保守路径。

测试：`tests/test_v0227_result_hardening.py:281`（组五）

### ⑦ `execute` 解析器全仓只留一份

- 复用来源：`core/command_result.py:64` `unwrap_command,`（来自 `core/java_commands.py`）
- 使用点：`core/command_result.py:356-367`（注释说明 v0.22.3 的「找第一个 `run` 会翻车」教训；`:367` `inner = unwrap_command(command)`）
- 解析失败 fail-closed：解析失败时命令名退化为 `execute`，不给任何豁免（`core/command_result.py:516` docstring 第 1 步与测试组六）

请核验：`git grep -n 'rfind(" run ")' -- main.py core` 是否已无残留手搓实现。

测试：`tests/test_v0227_result_hardening.py:329`（组六）

### ⑧ WebUI 必须能改、也必须能看见版本能力

| 角色 | 精准路径 |
| --- | --- |
| 设置页输入框 | `pages/mc_control/index.html:839`（`server_version_override`）、`:843`（`item_syntax_override` 下拉，auto / legacy_nbt / components） |
| CFG_FIELDS 绑定（否则存不了） | `pages/mc_control/index.html:2133-2134` |
| 能力状态行容器 | `pages/mc_control/index.html:851` `id="ver_caps_line"` |
| 渲染函数 | `pages/mc_control/index.html:2233` `renderVersionCaps(`（含「无法确定服务端版本」与来源显示：手动声明 / 文件探测） |
| 保存后刷新 | `pages/mc_control/index.html:2256` `refreshVersionCaps(`；调用点 `:2912`（loadSettings 内） |
| 服务器页消费 `version_hint` | `pages/mc_control/index.html:1458`（渲染 `version_caps`）、`:1470-1474`（`version_hint`，此前只生成、无人显示） |
| 后端计算 | `core/web_api.py:877-920`（`version_caps` 在 `_get_rcon()` **之前**算；`:910` `info["version_caps"]`；`:915` `info["version_hint"]`；`:920` `version_caps_error`；掉线时 error 分支仍带 `**info`） |
| 版本上下文注入 | `main.py:2675`（`server_version_override` 读取）、`main.py:2692`（`build_version_context`）、`main.py:2697`（`describe_capabilities`） |

测试：`tests/test_v0227_result_hardening.py:398`（组八）

---

## 2. 测试证据

- 新增回归：`tests/test_v0227_result_hardening.py`（460 行 / 八组 / **103 项 PASS**）
- 受影响旧用例扩充：`tests/test_command_result.py`（+79 行，**45 项 PASS**）
- 相关既有：`tests/test_complex_routing.py`（30 项）、`tests/test_version_capabilities.py`（42 项）、`tests/test_v0225_idle_reset.py`、`tests/test_simple_workflow_result.py`
- 全量跑批：`python run_v0226_tests.py` → **23 个测试文件全部 exit=0**（结果 JSON 见第 0 节）

复现命令（本机解释器 `C:\Users\10316\AppData\Local\AstrBot\backend\python\python.exe`，Python 3.12.12）：

```powershell
cd C:\Users\10316\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control
python tests\test_v0227_result_hardening.py
python run_v0226_tests.py
```

---

## 3. 提交人自曝的取舍（请重点挑刺）

1. **`inferred_success` 保留但降级**：`ok=False`、`accepted=True`。理由是「不谎报成功」与「不无故熔断整批」的折中；若判定过宽，开关 `INFERRED_SUCCESS_ENABLED`（`core/command_result.py:71`）可直接关掉。
2. **消息类命令的边界未确认仍报 `dispatched_unconfirmed`**：`say` 的输出是消息回显本身，若玩家把 `Error` 之类失败词喊进频道，是否会被第 8 步以外的路径误判？请裁断是否需要为消息类做单独豁免。
3. **中文失败标记是子串匹配**（`core/command_result.py:189`）：中文没有词边界，若物品/玩家名里含「失败」等字，可能误判 failed —— 是否接受该风险。
4. **`item_syntax_override` 手动覆盖优先于探测**：手动声明错了会全盘按错的世代构造命令，是否需要在 UI 上给出更显眼的警告（现仅在能力状态行显示来源）。
5. **本轮未做**：未提交、未打 tag、未建 Release、未重载线上插件；WebUI 新控件**未在真实浏览器点过**（仅静态断言 + 后端单测）。

---

## 4. 待主人裁决的悬置项（非本轮改动）

- 中文物品名映射（v0.22.6 遗留，主人自改）
- 旧知识库条目 `e38d99c65853` 清理（主人自改）
- `[RCON]` 署名改回 `[皮莉卡]`（主人自改）
- 服务端真实矩阵测试（Vanilla / Paper / Fabric / Forge）尚未做，v0.22.7 仍按 **pre-release** 口径
