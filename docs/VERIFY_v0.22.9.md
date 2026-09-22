# v0.22.9 核验单（自曝取舍版）

| 项 | 值 |
| --- | --- |
| 版本 | `v0.22.9`（`metadata.yaml:6` = `v0.22.9`） |
| 基线 | `v0.22.8` / 提交 `0b4c336` |
| 提交 | **`c29f5f8`**（`main` 已推送，2026-09-22 10:21:23 +08:00） |
| 标签 | annotated tag **`v0.22.9`**（tag 对象 `a9f8396` → 指向 `c29f5f8`） |
| 发布 | **GitHub pre-Release 已发布**：`prerelease=true`、`draft=false`、附件 `astrbot_plugin_Scintilla_MC_Server_Control_v0.22.9.zip`（2,042,839 字节）、2026-09-22 10:24（CST） |
| 改动规模 | 7 个文件、+1311 / −8（其中源码 +136 / −8：`workflow.py` +125/−8、`command_result.py` +11） |
| 改动文件 | `core/workflow.py`、`core/command_result.py`、`CHANGELOG.md`、`metadata.yaml` |
| 新增文件 | `tests/test_v0229_failclosed_and_no_duplicate.py`（374 行）、`run_v0229_all.py`（51 行）、`docs/v0.22.9.diff`（683 行快照） |
| 本轮性质 | **纯后端**修复（前端零改动，`ui_*.py` 仅作回归） |
| 核验基准 | 请以提交 `c29f5f8` / tag `v0.22.9` 为准（本单本身是 tag 之后的文档提交，不含被测代码） |

本单目的：把 v0.22.8 复核裁决里的两条 P1 落地，并把皮莉卡自己的取舍全部摊开请 GPT 挑。
下面每节都是「靶心 → 改法 → 证据」结构，可直接对着代码核。

---

## 一、P1-a：`_status_of` 改 fail-closed（缺 `status` 不再默认成功）

### 1. 靶心

旧实现（`core/workflow.py:740` 附近）：

```python
@staticmethod
def _status_of(report: dict) -> str:
    return report.get("status") or ("success" if report.get("ok") else "failed")
```

绕过路径：某条执行报告漏填 `status`、又恰好带 `{"ok": True}`
→ 被判成 `success` → 绕过 `_halt_on_uncertain()` 的熔断 → 工作流继续往下跑。
这与 v0.22.5 假成功事故是**同一种坏默认值**：结构异常时默认成功。

### 2. 改法

```python
status = report.get("status")
return status if status in KNOWN_STATUSES else "unknown"
```

- 新增唯一已知状态集合：`core/command_result.py:693` → `KNOWN_STATUSES = frozenset(STATUS_LABEL)`
  （状态标签表的键 = 状态全集，不在别处再抄一份）。
- 缺字段 / 空串 / `None` / 大小写错 / 将来新增状态忘了登记 → 一律 `unknown` → 熔断。
- **同源收口**：`_fmt_results`（`core/workflow.py:854`）原本躺着一份一模一样的回推逻辑
  （`r.get("status") or ("success" if r.get("ok") else "failed")`），现已改为调用同一个
  `MCWorkflow._status_of(r)` —— 只改一处留一处就是「两套真相」的老病，本次一并收口。
- 展示层标签表（`_fmt_results` 里的 `_tag`）**保留未动**：它只决定措辞，不参与判定
  （v0.22.7 测试锁定了该字面量，动它会破坏既有契约 —— 见取舍 5）。

### 3. 证据

| 断言 | 结果 |
| --- | --- |
| `{ok:True}` 且无 `status` → `unknown` | PASS |
| `{ok:True}` 且无 `status` → **熔断**（旧实现这里放行） | PASS |
| `status=""` / `None` / `"SUCCESS"` / 未知状态串 → `unknown` | PASS |
| 7 个已知状态原样返回、`status=success` 仍不熔断（未扩大化） | PASS |
| 缺 `status` 的回执渲染成 `[未知]`，不再显示 `[OK]` | PASS |
| 静态契约：`_status_of` 只定义 1 次；`r.get("status") or ("success"` 全文件 0 处 | PASS |
| `KNOWN_STATUSES` 只在 `command_result.py` 定义 1 次 | PASS |
| `UNCERTAIN_STATUSES ⊆ KNOWN_STATUSES`（不会自己把自己熔断掉） | PASS |

---

## 二、P1-b：同一任务内不重发「已确认生效」的命令

### 1. 靶心

```python
all_ok = all(r["ok"] for r in exec_reports)
if all_ok and out.get("success"):
    return ...
```

命令**全部确认成功**、但实现器自评 `success=false` → 进下一轮 →
实现器往往重新生成**同一批命令** → 原实现照发 → 重复副作用（例如再发一份物品）。

### 2. 改法（跨轮记账 + 执行前守门）

| 位置 | 作用 |
| --- | --- |
| `core/workflow.py:380` | `confirmed: set[str] = set()` —— 本任务内**已确认成功**命令（规范化文本），跨轮记账 |
| `core/workflow.py:398`、`:461` | 实现器循环 / 纠错循环**执行前**守门：`_split_confirmed(commands, confirmed)` |
| `core/workflow.py:407`、`:470` | 执行后 `confirmed \|= _newly_confirmed(...)` 只记 `status == success` 的 |
| `core/workflow.py:764` | `_split_confirmed`：拆「还没发过的」与「已生效的」 |
| `core/workflow.py:807` | `_confirmed_halt_text`：整批重复 → 停手问人（列「已生效·未重发」+ 裁决指引） |
| `core/workflow.py:796` | `_with_confirmed_note`：下一轮把「既成事实」注入实现器上下文 |
| `core/workflow.py:756` | `_norm_cmd`：折叠空白 / 去前导斜杠，**保留大小写** |

行为表：

| 场景 | 旧行为 | v0.22.9 |
| --- | --- | --- |
| 全成功 + 自评未完成 → 下一轮重发同一批 | **重发（重复副作用）** | **停手**，回执逐条列「已生效·未重发」 |
| 全成功 + 自评未完成 → 下一轮只给新命令 | 照发 | 不变（只发差额，多轮迭代保留） |
| 部分重复 + 部分新命令 | 全发 | 只发差额，重复跳过并写 INFO 日志 |
| 结果不确定（`unknown` / `inferred_success` / `dispatched_unconfirmed`） | 熔断 | 不变（v0.22.8 口径） |
| 确定没生效（`failed` / `syntax_error`） | 纠错重试 | 不变 |

### 3. 证据

| 断言 | 结果 |
| --- | --- |
| 靶心：第二轮重发同一批 → 命令只下发 **1** 次（旧实现会发 2 次） | PASS |
| 实现器确实进了第 2 轮（拦的是「重发」而不是「不迭代」） | PASS |
| 停手回执含「已生效·未重发」+ 被拦命令原文 + 人工裁决指引 | PASS |
| 混批：`sent == [A, B]`（A 只 1 次），成功计数 `1/1` 不把跳过算成生效 | PASS |
| 带斜杠 / 多空格的「同一条命令」同样被拦（规范化生效） | PASS |
| 对照：下一轮给**新**命令 → 各发一次、正常宣告成功（守门没误杀迭代） | PASS |
| 既成事实提示：第 2 轮 `failures` 含「禁止重复生成」并点名已生效命令；第 1 轮不含 | PASS |
| v0.22.8 口径未破：`inferred_success` 仍熔断、语法错误仍可纠错、非幂等空响应仍熔断 | PASS |

---

## 三、测试与回归

| 项 | 结果 |
| --- | --- |
| 新增 `tests/test_v0229_failclosed_and_no_duplicate.py` | **53/53 通过** |
| `tests/test_*.py` 全量 | **25/25 文件通过**（v0.22.3 / v0.22.5 / v0.22.7 / v0.22.8 回归全绿） |
| `tests/ui_*.py` 真浏览器链路 | **10/10 文件通过**（含 `ui_version_override_check.py`） |
| `compileall core main.py` | 通过（exit 0） |
| 解释器 | AstrBot 自带 Python 3.12.12 |
| 一键复现 | `run_v0229_all.py` |

---

## 四、自曝取舍（请重点挑这几条）

**取舍 1｜命令规范化保留大小写 —— 这是「漏拦」方向。**
`_norm_cmd` 只折叠空白、去前导斜杠，**不折叠大小写**。理由：误拦会让人白白停手；
而「漏拦」的后果是重复副作用，更贵。**风险自曝**：若实现器每轮把命令大小写改一下
（例如 `give Steve Diamond` vs `give Steve diamond`），守门就失效、仍会重发。
请裁决：是否应改成折叠大小写（更保守，但可能把语义不同的命令判成同一条）？

**取舍 2｜同批次内的重复命令不拦。**
守门只拦「与**历史轮**已成功命令重复」。同一批里出现两条相同命令仍会各发一次 ——
因为用户本意可能就是「发两份」。**风险自曝**：实现器自己犯傻在同批里写两遍，不会被拦。

**取舍 3｜整批重复时选择「停手问人」，而不是「静默返回成功」。**
命令其实都已生效，静默返回成功看起来更顺，但那等于替实现器谎报「任务完成」
（实现器明明说没做完）。停手回执里给了指引（先看游戏内实际结果 / 换说法重新发起）。
**风险自曝**：主人可能觉得「明明都成功了为什么报失败」——措辞是否够清楚请挑。

**取舍 4｜既成事实提示塞进 `failures` 通道（未改 agent 接口）。**
`_with_confirmed_note` 把提示拼进 `failures` 文本（`agent.implement` 既有参数），
零接口改动。**风险自曝**：失败信息与「既成事实」混在同一字符串里，LLM 可能混淆；
目前靠标题分节隔开，未做结构化字段。

**取舍 5｜状态→标签仍是两份（判据已收口，展示未收口）。**
`core/command_result.py::STATUS_LABEL` 与 `core/workflow.py::_fmt_results` 里的 `_tag`
是两份映射（措辞不同：`成功` vs `OK`）。判据侧已唯一（`KNOWN_STATUSES`），
但展示侧仍是两份 —— 动 `_tag` 会破坏 v0.22.7 的静态契约测试（`'"inferred_success": "已执行·未确认"' in WF_SRC`）。
**请裁决**：是否列为下一版收口项（连同 v0.22.7 那条契约测试一起改）？

**取舍 6｜`zip(commands, exec_reports)` 的等长是隐式契约。**
`_newly_confirmed` 依赖两者一一对应。`_exec_commands` 确实保证等长
（熔断 / 目标不在线时都会补 `skipped` / `failed` 条目），但**没有显式断言**。
若将来有人改动 `_exec_commands` 的返回结构，记账会静默错位。是否要加一条显式长度校验？

**取舍 7｜真机未验证。**
服务器昨夜（2026-09-22 00:21）已按主人指令关服，本轮只有 Stub 级验证 + 全量回归 + UI 链路。
P1-b 的触发场景（实现器自评未完成）在真机上本身也难自然复现。
建议：开服后跑一次普通复杂任务（例如「给某人一把附魔剑」）做冒烟，确认正常路径未受影响。

---

## 五、请 GPT 裁决

1. 取舍 1：大小写是否折叠？（漏拦 vs 误拦）
2. 取舍 2：同批次重复是否也要拦？
3. 取舍 3：整批重复的停手措辞是否够清楚？
4. 取舍 5：展示标签表收口是否列为下一版 P1？
5. 取舍 6：是否补显式等长断言？
6. 本版能否保留 `v0.22.9` pre-release（仍缺 Vanilla / Paper / Fabric 真实矩阵）？

---

## 六、发布与流水线留痕

| 项 | 值 |
| --- | --- |
| 提交 | `c29f5f8`（`main`，2026-09-22 10:21:23 +08:00） |
| 提交信息 | `fix: v0.22.9（预发布）—— 状态判定 fail-closed（缺 status 不再默认成功）+ 同一任务内不重发已确认生效命令` |
| 标签 | annotated tag `v0.22.9`（tag 对象 `a9f8396` → 指向 `c29f5f8`） |
| Release | **pre-Release 已发布**：`prerelease=true`、`draft=false`、标题 `v0.22.9`、附件 `astrbot_plugin_Scintilla_MC_Server_Control_v0.22.9.zip`（**2,042,839 字节**）、发布时间 2026-09-22 02:24:09Z（北京 10:24） |
| Release 说明来源 | 流水线从 `CHANGELOG.md` 的 `## [v0.22.9]` 小节抽取（非自动生成，正文与第四节一致） |
| 流水线（release） | run #11 → `completed/success`（校验 tag 与 `metadata.yaml` 版本一致 → `git archive` 打包 → 抽 CHANGELOG 说明 → 发 Release） |
| 流水线（tests） | run #17（push `main` 触发）→ `completed/success` |
| 预发布判定复查 | 判定读的是 CHANGELOG 小节**标题**里有没有「预发布」，不是 Release 标题 —— 本次标题命中，故 `prerelease=true`（未重演 v0.22.5 那颗判定雷） |
| 本单自身 | 本核验单以 `docs:` 文档提交追加，**晚于**被测提交 `c29f5f8`，不含任何被测代码；插件 zip 由 tag 时的 `git archive` 生成，不受本单影响 |

核验入口：

- 提交本体：`git show c29f5f8`；tag 与指向：`git show v0.22.9`
- 逐行 diff 快照：`docs/v0.22.9.diff`
- 一键复现：`run_v0229_all.py`

发布命令留痕（本地实际执行）：

```
git add CHANGELOG.md core/command_result.py core/workflow.py metadata.yaml \
        tests/test_v0229_failclosed_and_no_duplicate.py run_v0229_all.py docs/v0.22.9.diff
git commit -m "fix: v0.22.9（预发布）—— …"        # → c29f5f8
git push origin main                              # → 0b4c336..c29f5f8
git tag -a v0.22.9 -m "v0.22.9（预发布）：…"       # → a9f8396
git push origin v0.22.9                           # → 触发 release 流水线 → pre-Release
```

（另记一笔环境事实备查：本机 git 配置 `http.proxy=127.0.0.1:7897` 时 `git push` 报 `schannel: failed to receive handshake`，
而 `git ls-remote` 同代理却正常；本次 `git push` 改用 `-c http.proxy= -c https.proxy=` 直连才成功。与插件代码无关。）
