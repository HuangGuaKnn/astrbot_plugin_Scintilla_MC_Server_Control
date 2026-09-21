# v0.22.5 核验交接单（精准路径版，交给 GPT 复核用）

## 0. 坐标约定

- 仓库根：`C:\Users\10316\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control`
- 下列路径**均相对仓库根**，行号为 **2026-09-21 15:40 工作区实测**（未提交状态；提交后行号可能漂移，请以 `git diff` 内容为准）
- 基线：`3292d8b`（v0.22.4）。对照旧行为用 `git show 3292d8b:main.py` / `git show 3292d8b:core/rcon.py`
- 版本号：`metadata.yaml:6` → `v0.22.5`
- 本轮状态：**仅工作区，未提交、未打 tag、未建 Release、未重载线上插件**

---

## 1. 核验目录（逐条带路径）

### A 核心修复

**A1 · P0｜`reset_rcon()` 自锁死是否真的消失**

| 角色 | 精准路径 |
| --- | --- |
| 修复后的纯构造器 | `main.py:1299` `def _build_rcon()`（同步、不持锁、不建连） |
| 取或建（锁内） | `main.py:1319` `async def _get_rcon()`；`main.py:1320` `async with self._rcon_lock:` |
| 摘旧→退役→建新（同一把锁） | `main.py:1325` `async def reset_rcon()`；`main.py:1335` `async with self._rcon_lock:`；`main.py:1336-1345` |
| 锁的创建 | `main.py:274` `self._rcon_lock = asyncio.Lock()` |
| 缺陷旧形态（对照） | `git show 3292d8b:main.py` 中 v0.22.4 的 `reset_rcon()` —— 持锁后 `await self._get_rcon()` |

请核验：
1. 从 `main.py:1325` 到 `main.py:1346`（`return`）之间，`main.py:1344` `await old.close()` 是唯一 `await`；确认**没有**任何路径再次进入 `_get_rcon()` 或重取 `_rcon_lock`；
2. `main.py:1337` `self._rcon = None` 到 `main.py:1345` `self._rcon = self._build_rcon()` 之间**持有锁**，确认并发调用 `_get_rcon()` 不会观察到 `None` 空窗；
3. `main.py:274` 的 `asyncio.Lock()` 可能在无事件循环的 `__init__` 阶段创建，确认 Python 3.10+ 语义下在后续运行循环中使用安全（本机实测解释器：`C:\Users\10316\AppData\Local\AstrBot\backend\python\python.exe` → Python 3.12.12）。

**A2 · P0｜真实触发路径是否全覆盖**

| 触发点 | 精准路径 |
| --- | --- |
| 设置页保存 | `core/web_api.py:388` `save_settings()` → `core/web_api.py:455`（RCON 项集合判定）→ `core/web_api.py:458` `await self.plugin.reset_rcon()` |
| 「重建 RCON 连接」按钮 | `core/web_api.py:854` `async def reset_rcon()` → `core/web_api.py:859` `await self.plugin.reset_rcon()` |
| 连接测试（只读，不重建） | `core/web_api.py:869` `test_rcon()` → `plugin._get_rcon()` |
| 插件卸载 | `main.py:423` `async def terminate()` → `main.py:429` `self._rcon.retire()` + `main.py:430` `await self._rcon.close()` |
| 工作流取连接 | `core/workflow.py:135`、`core/workflow.py:354`、`core/workflow.py:489`（均 `_get_rcon()`） |

请核验：全仓是否还有**其他**直接写 `plugin._rcon` / 绕过 `reset_rcon()` 的路径（建议 `git grep -n "_rcon" -- main.py core pages`）。

### B idle 降级模式

**B1 · P1｜静默窗口到期即关连接**

- 全部改动集中在 `core/rcon.py:352-374`（`if probe_id is None and chunks:` 分支）：
  - `core/rcon.py:362` `self._close_socket()`
  - `core/rcon.py:363` `self.last_boundary_confirmed = False`
  - `core/rcon.py:364` `self._idle_unconfirmed_count += 1`
  - `core/rcon.py:365-373` warning 日志
  - `core/rcon.py:374` `return "".join(chunks)...`（仍返回已收到文本）
- 标志位声明：`core/rcon.py:134-135`

请核验：
1. `last_boundary_confirmed` **只在** `core/rcon.py:350`（哨兵确认成功）被置 True、`core/rcon.py:363` 被置 False，`_close_socket()`（`core/rcon.py:420`）不重置它 —— 因此在 `end_mode=idle` 下该标志一旦 False 就**再无机会回到 True**（除非把配置切回 sentinel 并重建连接）。请判定这是否算「标志位语义缺陷」；
2. 「服务端先回半截、后补后半截」与「完全不回包」两种情形下，迟到包是否确实打在被废弃的连接上（测试覆盖：`tests/test_v0225_idle_reset.py:108` 的 `split` 模式 + `tests/test_v0225_idle_reset.py:96` `_late`）；
3. 关连接瞬间，已到内核缓冲但未被读走的包会被丢弃 → 用户拿到不完整文本却没有返回值层面的区分（只有标志位 + warning）。请判定可接受性。

**B2 · P1｜idle 零响应仍是「结果未知」**

- 零响应路径：`core/rcon.py:382-392`（`if chunks:` 之外，走到 `core/rcon.py:389-392` 抛 `RconTimeoutError`）
- 哨兵「有输出但没等到边界」：`core/rcon.py:376-381` 抛 `RconPartialResponseError`
- 请核验：idle 与 sentinel 在「零响应」上的行为是否严格一致（都不返回空串冒充合法空响应）。

⚠ **B3 · 分歧点｜idle 的正常单包响应也被标「边界未确认」**

- 现状：哨兵成功 → `core/rcon.py:349-350` 置 True；idle 正常单包 → 同样走 `core/rcon.py:362-374` 置 **False**
- 测试同时把这条写成了断言：`tests/test_v0225_idle_reset.py:192`（「对照组：idle 单包也标记为边界未确认」）
- 提交人观点：idle 本质无法证明边界，一律 False 更诚实，但代价是标志位在 idle 模式下**无区分度**
- 请裁断：保持「一律 False」，还是区分「单包且窗口内无新包」为高置信？若后者，标志位语义需重新定义（可能需三态）。

⚠ **B4 · 分歧点｜两个新观测位只到后端，未上 UI**

- 后端已并入 runtime：`core/web_api.py:827` `_rcon_runtime()`；字段落点 `core/web_api.py:840`（无实例时的默认值）、`core/web_api.py:850`（`boundary_confirmed`）、`core/web_api.py:851`（`idle_unconfirmed`）
- 前端只渲染旧字段：`pages/mc_control/index.html:835`（`#rcon_runtime_line` 容器）、`pages/mc_control/index.html:1307-1321`（`renderRconRuntime`，只用 `degraded/end_mode/configured/probe_misses/connected`）、`pages/mc_control/index.html:1323-1331`（`resetRcon` 按钮）
- 提交人决定：**本轮不改前端**，等主人裁决。请评估不展示是否构成可观测性缺口。

### C 退役与连接所有权

**C1 · P2｜退役实例的收尾语义**

| 语义 | 精准路径 |
| --- | --- |
| 退役标志声明 | `core/rcon.py:138` `self._retired = False` |
| 退役后拒绝重连 | `core/rcon.py:153` `connect()`；`core/rcon.py:159-161` 抛 `RconError` |
| `command()` 的 finally 收尾 | `core/rcon.py:227` `command()`；`core/rcon.py:246-247` `if self._retired: self._close_socket()` |
| `retire()` | `core/rcon.py:249-255`（只置标志，不关连接） |
| `in_flight` 判据 | `core/rcon.py:258-264`（`self._lock.locked()`） |
| reset 侧调用 | `main.py:1339` `old.retire()`；`main.py:1343` `if not old.in_flight:`；`main.py:1344` `await old.close()` |

请核验（**重点**）：
1. **TOCTOU 竞态**：`main.py:1343` 判定「无在途」之后、`main.py:1344` `close()` 之前，若恰有一条命令开始执行并被锁接纳，是否会把该在途命令掐断成「结果未知」？注意判定与关闭之间存在一个 `await`；
2. 旧实例正握命令时，其 `finally`（`core/rcon.py:243-247`）在**异常路径**与**任务被取消路径**下是否都必然关连接；
3. `close()`（`core/rcon.py:430-433`，不持 `_lock`）与 `command()` 内部自动建连（`core/rcon.py:266-...` `_command_locked`）是否可能互相打架 —— 两者的锁粒度不同。

⚠ **C2 · 分歧点｜连接数上界是经验值**

- 断言位置：`tests/test_v0225_idle_reset.py:292-301`（连点 5 次并发重建块），其中连接数上界断言为 `socks_before + 2`（`tests/test_v0225_idle_reset.py:301`），基准 `socks_before` 取于 `tests/test_v0225_idle_reset.py:285`；`CONNS` 记录表定义在 `tests/test_v0225_idle_reset.py:105`、服务端接受连接时写入 `tests/test_v0225_idle_reset.py:110`
- 请判定：该上界是否过松、会不会掩盖真实泄漏；是否存在可写成不变量的更强断言。

### D 测试自身可信度（请重点审）

⚠ **D1 · 分歧点｜本轮改了 3 个既有测试，是否属「改测试掩盖问题」**

事实：4 处替身用 `object.__new__(McControlPlugin)` 绕过 `__init__`，v0.22.5 新增 `self._rcon_lock`（`main.py:274`）后替身缺该属性。

| 文件:行 | 现状 | 改前实测后果 |
| --- | --- | --- |
| `tests/test_remote_deploy_audit.py:53`（替身构造）/`:60`（补锁） | 本轮补 `_rcon_lock` | **直接 AttributeError 崩掉**（`main.py:1320` 抛 `'McControlPlugin' object has no attribute '_rcon_lock'`，exit=1） |
| `tests/test_settings_save_resync.py:65`（替身）/`:78`（补锁） | 本轮补 `_rcon_lock` | 第 [2] 组**静默吞错**：notice 里带「RCON 连接重置失败」，测试仍报「✓ 全部通过」 |
| `tests/test_remote_rcon_mode.py:77`/`:84` | 上一轮已补（v0.22.5 前序改动） | — |
| `tests/test_v0225_idle_reset.py:278` | 新用例自带 | — |

本轮同时给静默路径补了硬断言：`tests/test_settings_save_resync.py:123`（notice 含「连接重置失败」即判失败）、`tests/test_settings_save_resync.py:125`（必须含「RCON 连接已重置」）。

请核验：
1. 这属于**夹具缺陷**还是**实现缺陷**？即真实运行中 `_rcon_lock` 是否一定存在（`main.py:274` 之前是否有早退/异常路径会跳过它）；
2. 是否还有其他替身/桩未与实现对齐（建议全仓扫 `object.__new__(m.McControlPlugin)`：`tests/test_remote_deploy_audit.py:53`、`tests/test_remote_rcon_mode.py:77`、`tests/test_server_switch_resync.py:55`、`tests/test_settings_save_resync.py:65`）；
3. 反向风险：替身补了锁，是否掩盖了「测试里会挂、真实环境也会挂」的路径。

**D2｜`require_app()` 新护栏**

- 位置：`tests/_paths.py:60` `require_app()`；`tests/_paths.py:75` `asyncio.get_running_loop()`；`tests/_paths.py:78` `raise RuntimeError(...)`
- 请核验：`get_running_loop()` 抛 `RuntimeError` 即判定「安全」的逻辑在同步上下文是否可能**误拦**；是否有测试在模块加载阶段之外调用它。

**D3｜新用例的假服务端够不够毒**

- 夹具：`tests/test_v0225_idle_reset.py:108` `make_server(mode)`（模式 `split` / `reorder` / `silent` / `slow_reply`）、`tests/test_v0225_idle_reset.py:96` `_late()`（迟到包）、`tests/test_v0225_idle_reset.py:153` `serve()`
- 用例体：`tests/test_v0225_idle_reset.py:160` `idle_cases()`（含 `:184` 对照组、`:198` 零响应回归、`:214` 哨兵乱序回归）、`tests/test_v0225_idle_reset.py:232` `reset_cases()`（含 `:267` 插件层、`:292` 连点并发）
- 请核验：是否缺「哨兵与命令响应交错」「多包 + 迟到包混合」「连接中途被服务端 RST」场景；时长参数是否有 flaky 风险（实测取值：`tests/test_v0225_idle_reset.py:164` 与 `:186` 与 `:200` 的 `idle_probe=0.1`、`tests/test_v0225_idle_reset.py:172` 的 `sleep(0.45)` 等待迟到包、`tests/test_v0225_idle_reset.py:200-203` 的 `timeout=0.4`）。

### E 回归与范围

**E1 · 全仓测试（本轮实测，27 支全绿）**

- 带逐条计数的：`test_permission_gate.py` 78、`test_v0223_hardening.py` 71、`test_remote_rcon_mode.py` 44、`test_tool_guard.py` 43、`test_rcon_protocol.py` 39、`test_remote_deploy_audit.py` 28、`test_v0225_idle_reset.py` 24、`test_help_and_reload.py` 20、`test_review_compliance.py` 12
- 以 `✓ 全部通过` 收尾（不逐条计数）：`test_kb_entry_edit.py`、`test_kb_model_pick.py`、`test_kb_rerank.py`、`test_kb_search_engine.py`、`test_kb_semantic_search.py`、`test_server_identity.py`、`test_server_switch_resync.py`、`test_settings_save_resync.py`、`test_fingerprint_notice.py`
- 未跑：`tests/bench_kb_engines.py`（评测脚本，非断言型）
- 请核验：是否有应跑而未跑到的用例。

**E2 · 范围确认**

- `tests/` UI 断言 9 支：`ui_broadcast_order_check.py` / `ui_card_layout_check.py` / `ui_fp_notice_check.py` / `ui_kb_detail_check.py` / `ui_kb_fold_check.py` / `ui_remote_blur_check.py` / `ui_settings_structure_check.py` / `ui_sw_wrap_check.py` / `ui_theme_check.py`
- `git diff --stat`（基线 `3292d8b`）：`CHANGELOG.md`、`core/rcon.py`、`core/web_api.py`、`main.py`、`metadata.yaml`、`tests/_paths.py`、`tests/test_remote_deploy_audit.py`、`tests/test_remote_rcon_mode.py`、`tests/test_settings_save_resync.py`、`tests/ui_fp_notice_check.py` + 新增 `tests/test_v0225_idle_reset.py`、`docs/VERIFY_v0.22.5.md`
- 本版**未改**权限判定、工作流熔断、知识库检索、WebUI 样式
- v0.22.1 上架审核两条硬要求请顺带抽查：日志器统一 `from astrbot.api import logger`；权限前置提醒走用户消息内容块而非改写 `system_prompt`

---

## 2. 复现命令

```powershell
cd "$env:USERPROFILE\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control"
$env:PYTHONIOENCODING="utf-8"; [Console]::OutputEncoding=[System.Text.Encoding]::UTF8
$py="C:\Users\10316\AppData\Local\AstrBot\backend\python\python.exe"

& $py tests\test_v0225_idle_reset.py     # 本轮新用例（24 断言）
& $py tests\test_remote_deploy_audit.py  # 改前崩、改后全绿
& $py tests\test_settings_save_resync.py # 改前静默吞错、改后硬断言
& $py tests\test_remote_rcon_mode.py

Get-ChildItem tests -File -Filter "test_*.py" | ForEach-Object { & $py "tests\$($_.Name)" }
```

护栏注意：`import astrbot_plugin_...` 必须写在**模块加载阶段**；放进 `asyncio.run()` 内会被 `tests/_paths.py:78` 直接抛错（有意设计）。

## 3. 明确不在本版范围

1. 未提交、未打 tag、未建 Release、未重载线上插件
2. 未改前端（`boundary_confirmed` 是否上 UI 见 B4）
3. 工作区三件历史待办未动：中文物品名映射、旧知识库 `e38d99c65853` 清理、`[RCON]` 署名改回 `[皮莉卡]`

---

## 4. v0.22.5 复审整改记录（2026-09-21 补充，请复核本节的 diff）

复审结论：A1 / A2 / B1 / C1 / D1 / D2 通过；**B4 与 P2-2 未通过**，本轮已补齐，坐标仍在 `3292d8b` 的未提交工作区。

### 4.1 P2-1｜前端运行态三状态互不替代（B4 结案）

- 位置：`pages/mc_control/index.html:renderRconRuntime`（原 `1307` 行附近）
- 改前逻辑：`if (rt.degraded) {...} else { "✓ 边界可靠" }` → **主人亲手把 `rcon_end_mode` 配成 idle 时 `degraded=false`，页面照样显示「✓ 边界可靠」**
- 改后：三条状态**各自独立**判定，互不替代
  1. `end_mode === "idle"` → 「⚠ 静默窗口模式」+ 明写「**响应完整性未保证**」+ 标出静默窗口秒数（`idle_probe`）
  2. `degraded === true` → 额外「⚠ 已自动降级」，并同时列出「当前运行 / 配置」两种方式 + 给出「重建 RCON 连接」这条恢复路径
  3. `boundary_confirmed === false` → 「⚠ 最近一次响应可能不完整」；`null` → 「尚未执行过命令，暂无边界确认结果」
- 附带修掉一个同类问题：点「重建 RCON 连接」后原先只追加一行结果文字、**不重渲染运行态**，会把旧的「边界未确认」留在页面上；现在重置后同样走 `renderRconRuntime(r.runtime)`，重建即回到「尚未执行过命令」。

### 4.2 P2-2｜`last_boundary_confirmed` 必须是「最近一次」（结案）

- 位置：`core/rcon.py:_command_locked` 入口
- **每条命令事务开始时先把 `last_boundary_confirmed` 置为 `False`**；只有「命令响应包 + 哨兵」按序到齐才置 `True`；字段初始值 `None`（`__init__`）＝ 还没执行过命令。`_close_socket()` 依旧不写这个字段。
- 于是以下路径全部落到 `False`，不再沿用上一次的 `True`：命令零响应 / 命令或哨兵超时 / 哨兵乱序 / 协议异常 / idle 静默窗口到期。
- 后端默认值同步为 `boundary_confirmed: null`（无实例时），页面据此显示「尚未执行过命令」。

### 4.3 两个对照实验（证明不是空跑断言）

| 对象 | 操作 | 结果 |
| --- | --- | --- |
| `core/rcon.py` | 删掉「事务开始置 `False`」那一行 | `test_v0225_idle_reset.py` 用例⑤ 立刻红（字段停在 `True`） |
| `pages/.../index.html` | 用 `git show 3292d8b:` 的旧页面跑同一组断言 | `ui_fp_notice_check.py` 第 [8] 组 6 项红（旧页面显示「✓ 边界可靠」） |

两个对照脚本均为临时文件，跑完已删，不进仓库。

### 4.4 分歧点裁断回执

- **B1-1**（idle 下标志位再无机会回 `True`）：按复审意见**保留**现有语义 —— 该字段表达「是否确认了可靠结束边界」，而非「有没有收到内容」。idle 即使只收到一个完整包也无法凭静默证明完整。
- **B3**（idle 正常单包是否算高置信）：**保留一律 `False`**，不引入「单包高置信」这个第四态，语义保持单一。
- **B4**：已按裁决补 UI（见 4.1）。
- **仍存的分歧（请主人裁决）**：sentinel 模式下服务端「响应包与哨兵包一起丢」时走 `RconTimeoutError`（不计降级）。理由是整体失联与「服务端不支持哨兵」在网络层不可区分；代价是同一个不可靠服务端会被反复当成「失联」，`probe_misses` 永不累加 → **自动降级可能迟迟不触发**。只影响可用性，不影响数据正确性（该路径永远是「结果未知」，绝不静默假成功）。

### 4.5 交接单 E1 口径订正

原文「21 支」与仓库实际不符，按实际文件重列：`tests/` 顶层**带断言的 `test_*.py` 为 18 支**，另有 **9 支**不以 `test_` 命名的 UI 检查脚本（`ui_*.py`）同样是断言型、也参与回归 —— 顶层断言脚本合计 **27 支**。`bench_kb_engines.py` / `make_docs_images.py` 为工具脚本，不计入。测试用例数订正：新用例 `test_v0225_idle_reset.py` 为 **35 项**（原 24 项 + 复审补 11 项），`ui_fp_notice_check.py` 新增第 [8] 组 **12 项**。

### 4.6 本轮实测（AstrBot Python 3.12.12）

- 全部顶层断言脚本 27 支（18 个 `test_*.py` + 9 个 `ui_*.py`）退出码 **0**；
- `test_v0225_idle_reset.py` **35/35**；`ui_fp_notice_check.py` **48 项全过**（含新增第 [8] 组 12 项）；
- `python -m compileall core main.py tests` 退出码 0；
- 未提交、未打 tag、未建 Release、**未重载线上插件**（复审要求的浏览器端 UI 验收本轮已由 Playwright 断言覆盖，但仍是本地文件，未上线上环境）。

