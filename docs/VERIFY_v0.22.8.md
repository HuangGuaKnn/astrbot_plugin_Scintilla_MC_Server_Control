# v0.22.8 核验交接单（精准路径版，交给 GPT 复核用）

## 0. 坐标约定

- 仓库根：`C:\Users\10316\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control`
- 下列路径**均相对仓库根**，行号为 **提交 `0b4c336` 实测**（2026-09-22 01:55 CST）
- 基线：`32a310d`（v0.22.7，已推送 `origin/main`）。对照旧行为用 `git show 32a310d:core/workflow.py`
- 版本号：`metadata.yaml:6` → `v0.22.8`
- 本轮状态：**已提交 `0b4c336`、tag `v0.22.8` 已推 origin、CI 已自动建 pre-release**
  （zip `astrbot_plugin_Scintilla_MC_Server_Control_v0.22.8.zip`，2,026,005 字节；
  `prerelease=true` / `draft=false`，创建于 2026-09-22 01:50 CST；说明正文取自 `CHANGELOG.md:10` 小节）
- 线上插件是否已重载：**不在本单范围**，需主人确认（本地无法探测进程内版本）

### 随附证据

| 证据 | 路径 |
| --- | --- |
| 全量 diff | `docs/v0.22.8.diff`（59,404 字节；导出方式 `git diff 32a310d 0b4c336`）——**未入库**，属工作区导出物 |
| 全量测试结果 JSON | `C:\Users\10316\.astrbot\data\plugins\v0228_test_results.json`（24 个 `tests/test_*.py`，未入库） |
| 真浏览器结果 JSON | `C:\Users\10316\.astrbot\data\plugins\v0228_ui_results.json`（10 个 `tests/ui_*.py`，未入库） |
| 跑批脚本 | `run_v0228_all.py`（逐个跑 `tests/test_*.py` / `tests/ui_*.py`，超时 180s，结果落盘 JSON）——**未入库**（写死本机解释器路径，与 `.gitignore` 里的 `run_full.py` 同类） |

改动面（`git diff --stat 32a310d 0b4c336`，6 文件 / +609 −24）：

```
 CHANGELOG.md                              |  67 +++++++
 core/command_result.py                    |  12 +-
 core/workflow.py                          |  80 ++++++---
 metadata.yaml                             |   2 +-
 tests/test_v0228_no_retry_on_uncertain.py | 285 ++++++++++++++++++++++++++++++
 tests/ui_version_override_check.py        | 187 ++++++++++++++++++++
```

---

## 1. 核验对象：本轮 3 条

### ① P1｜`inferred_success` 不再触发自动重试（重复副作用保护）

**事故链条**（v0.22.7 引入 `inferred_success` 时留下的口子）：模组命令返回「非空、无错误标记、
也无成功证据」→ `inferred_success`（`ok=False` / `accepted=True`）→ 复杂工作流的重试判据是
`all_ok = all(r["ok"] for r in exec_reports)` → 本轮被判「没做完」→ 实现器**重新生成命令再发一次**
→ 对未被 `NON_IDEMPOTENT_COMMANDS` 覆盖的模组命令 = 重复副作用。

| 角色 | 精准路径 |
| --- | --- |
| 状态集合**唯一定义**（含为何不拦 failed / syntax_error 的说明） | `core/workflow.py:51` `UNCERTAIN_STATUSES = frozenset({"unknown", "inferred_success", "dispatched_unconfirmed"})`（注释块 `:44-50`） |
| **唯一判据** | `core/workflow.py:712` `def _halt_on_uncertain(self, exec_reports)`（docstring 明确 `accepted` 与本判据无关） |
| 实现器循环调用点 | `core/workflow.py:395` `halt = self._halt_on_uncertain(exec_reports)` |
| 纠错循环调用点 | `core/workflow.py:442` 同上一行（两处共用同一实现） |
| 状态回推（与 `_fmt_results` 同口径） | `core/workflow.py:708` `def _status_of(report)` |
| 旧写法（已删除） | diff 里 `-` 掉的两段 `any(r.get("unknown") for r in exec_reports)` |
| `accepted` 语义文档（防后人再当重试开关） | `core/command_result.py:306` `def accepted`（docstring 起点 `:316` 写明「**不构成自动重试的依据**」）；模块文档 `core/command_result.py:51`「不得把它当作「可以重试」的依据」 |

**请重点核验**：

1. `_halt_on_uncertain` 是否为全仓**唯一**熔断入口 —— 除它之外，还有没有任何一处用 `ok` 直接决定「重跑/重发」；
2. 熔断返回后，控制流是否确实**不可达**后续的 `all_ok` 分支与纠错 Agent（即 `:395` / `:442` 之后不存在绕过路径）；
3. 是否存在别的路径能让 `inferred_success` / `dispatched_unconfirmed` 落到「重试」语义上（含 `main.py`、`core/agent_llm.py` 侧）。

测试：`tests/test_v0228_no_retry_on_uncertain.py`
—— `:136`（零、前提：该模组命令确实落 `inferred_success`）、`:149`（一、判据单元 + 混批）、
`:179`（二、静态契约：判据只定义 1 次、调用点 ≥2、旧写法清零）、`:200`（三、靶心：实现器 1 次 / 命令 1 条 / 纠错 0 次）、
`:218`（四、`dispatched_unconfirmed` 同样不重发）、`:231`（五、对照：语法错误仍可纠错重试）、`:244`（六、非幂等空响应仍熔断）。

### ② 熔断回执不再因缺字段而崩

| 角色 | 精准路径 |
| --- | --- |
| 改动点 | `core/workflow.py:754-762` `_fmt_results`：`r['command']` / `r['output']` → `r.get('command', '(空)')` / `r.get('output', '')` |

理由：回执是熔断时**唯一的出口**，`KeyError` 等于主人什么都收不到。
**请核验**：回执路径（含 `_success_text`、`main.py` 侧渲染）是否还有别的硬取键。

### ③ WebUI 版本控件真机链路（v0.22.7 的已销账项）

`tests/ui_version_override_check.py` —— **真 Edge**（Playwright `channel="msedge"`，本机无自带 chromium）
+ 假后端夹具，验「填写 `1.20.1` → 保存 → `item_syntax` 切到 `legacy_nbt`」。
能力结论由 `core/version_caps.py` **现算**（不是测试里手写的期望值）。

| 角色 | 精准路径 |
| --- | --- |
| 假后端状态与路由（`settings` / `settings/save` / `server/status`） | `tests/ui_version_override_check.py:50-86` |
| [1] 控件存在、三档齐全、初值来自后端 | `:109` |
| [2] 只改草稿不保存 → 能力行不动 | `:128` |
| [3] 保存 → 载荷带 `server_version_override=1.20.1` → 自动重刷 → 能力行 `legacy_nbt`（来源：主人手动声明） | `:137` |
| [4] 清空再保存 → 回到「无法确定服务端版本」且不残留旧结论 | `:160` |
| 全程无 JS 异常（`pageerror`） | `:173` |

**请核验**：这条链路是否真走「前端 → `settings/save` 载荷 → 后端能力计算 → 页面显示」，
而不是把期望值塞进测试；`version_caps.resolve_version_info` 的来源标注（手动声明 / 文件探测）是否会被前端写死覆盖。

---

## 2. 测试证据（2026-09-22 01:56-02:0x 本机实测）

| 范围 | 文件数 | 结果 |
| --- | --- | --- |
| `tests/test_*.py` | 24 | **全部 exit 0**（含新 `test_v0228_no_retry_on_uncertain.py`：**33 项 PASS / 0 FAIL**） |
| `tests/ui_*.py` | 10 | **全部 exit 0**（含新 `ui_version_override_check.py`：**20 项断言全通过**） |

- 解释器：`C:\Users\10316\AppData\Local\AstrBot\backend\python\python.exe`
- 已知噪声：stderr 里的 `loguru` 日志轮转 `PermissionError`（`astrbot.log` 被常驻进程占用），
  与用例无关，不影响 exit code —— 复核时看到请忽略。
- **如实纠偏（记账错误）**：`CHANGELOG.md:52-60` 写 `ui_version_override_check.py` 为「22 项」，
  实测 `check()` 调用 **20 处**、输出 20 项全通过 —— 数字写大了 2 项。
  代码与行为无误，属提交人记账疏漏，**已在本单更正**，请 GPT 按 20 项复核。

---

## 3. 提交人自曝的取舍（请重点挑刺）

1. **`_status_of` 缺 `status` 时按 `ok` 回推**（`core/workflow.py:708`）——
   若有报告漏填 `status` 且 `ok=True`，会被当 `success` 放行：即「漏 `status` 的 `inferred_success` 不会被熔断」。
   这是为兼容旧报告格式留的口子。**是否该改成 fail-closed（缺 `status` → 熔断）？**
2. **三类不确定共用一条回执**，只换首句：含 `unknown` → 「结果未知」；否则 → 「未确认」。
   混批时首句只按「是否含 `unknown`」选，逐条状态在明细里。措辞是否够清楚、会不会误导？
3. **未扩大熔断范围**：`failed` / `syntax_error` 明确不拦（确定没生效 → 重发安全），
   以及 §4 那条同源但会触及「多轮迭代」设计意图的问题，都没擅自动手。
4. **熔断回执带全量明细**（含 `skipped` 条目），主人看到的是整批命令列表，可能偏长。
5. **测试用 `StubRcon` / `StubAgent`**，非真服务端；真机只在本机 **1.20.1 · Forge 47.4.23** 手测过（v0.22.5 那轮 13 项）。
6. **本轮未改实现层前端**：③ 是**补测试**，`core/web_api.py` 与 `pages/` 本轮零改动 ——
   若 GPT 认为该链路需要改代码才能算收口，请直接说。

---

## 4. 待主人裁决的悬置项（本轮未改，非缺陷申报）

- **实现器自评未完成、但本轮命令全部成功**：此时 `all_ok=True` 而 `out["success"]=False`，
  工作流仍进下一轮 → 实现器可能重新生成同一批命令再发一次。
  与 ① **同源**，但改动会触及「多轮迭代」这一设计意图，故未擅自扩大范围（见 `CHANGELOG.md:67` 已知问题）。
- **未做真实服务端矩阵测试**：仅本机 1.20.1 · Forge 47.4.23，Vanilla / Paper / Fabric 未覆盖 —— 这也是本版仍标 pre-release 的原因。
- v0.22.7 遗留的其它 P2 若仍有未收口项，请一并列出清单，下轮一并处理。

---

## 5. 复核请回话的格式（省事用）

1. ① 唯一入口 / 不可达性：**通过 / 有洞**（附路径:行号）
2. ② 回执硬取键：**通过 / 还有 N 处**（附路径:行号）
3. ③ 真机链路：**真链路 / 假链路**（附理由）
4. §3 六条取舍：逐条「接受 / 要求改」
5. §4 悬置项：**本轮改 / 下轮改 / 不改**

---

## 6. GPT 复核回执（2026-09-22 02:12 CST，已闭环）

| 核验项 | 结论 |
| --- | --- |
| ① 唯一熔断入口 / 后续不可达 | **通过**（`unknown` / `inferred_success` / `dispatched_unconfirmed` 均进 `core/workflow.py:712`；全仓无 `ok`/`accepted` 绕过路径） |
| ② 熔断回执硬取键 | **通过**（另扫 `core/workflow.py` 与 `main.py`，无其他 `r["command"] / r["output"] / r["status"] / r["ok"]` 硬取） |
| ③ WebUI 版本链路 | **真链路通过**（能力结论确由 `version_caps.resolve_version_info()` 现算） |
| 取舍 1（`_status_of` 缺 `status` 回推） | **本版可接受 → 列为下一版 P1**（要求改 fail-closed） |
| 取舍 2 / 3 / 4 / 5 / 6 | **接受**（其中 5 要求继续维持 pre-release） |
| §4 悬置项（`all_ok=True` 但 `out["success"]=False` → 仍进下一轮） | **确认存在，建议下一版处理** |
| 总判定 | **v0.22.8 验收通过，保留 pre-release**；暂不转正式版（缺 Vanilla / Paper / Fabric 矩阵测试） |

### 下一版（v0.22.9）待办清单

**P1-a｜`_status_of` 改为 fail-closed**（`core/workflow.py:708`）
缺 `status` 时不再按 `ok` 回推，改为「未知 → 熔断」。
提交人补充两条：

1. `KNOWN_STATUSES` 必须取自 `core/command_result.py` 的状态定义，**不得在 `core/workflow.py` 再抄一份**
   （v0.22.6 / v0.22.8 的教训：两份真相迟早分叉）；
2. `core/workflow.py:758` 的 `_fmt_results` 里存在**同一份回推逻辑**
   （`r.get("status") or ("success" if r.get("ok") else "failed")`）——**两处必须一起收口**，
   否则本次修的正是同一种病。

**P1-b｜命令全成功但实现器自评未完成 → 不得重发已成功命令**（`CHANGELOG.md:67`）
提交人候选方案（待主人 / GPT 拍板）：下一轮把「已确认成功的命令」作为**既成事实**注入实现器上下文，
并在执行前加一道重复命令守门（与已成功命令完全重复 → 拦下停手问人）——
既保留「多轮迭代」设计意图，又不产生重复副作用。

**非本轮（环境面）**：Vanilla / Paper / Fabric / NeoForge 矩阵、非英文服务端反馈、模组自定义命令输出。
