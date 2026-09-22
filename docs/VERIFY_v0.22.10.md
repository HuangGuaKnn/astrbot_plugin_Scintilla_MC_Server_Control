# v0.22.10 核验单（自曝取舍版）

| 项 | 值 |
| --- | --- |
| 版本 | `v0.22.10`（`metadata.yaml:6` = `v0.22.10`） |
| 基线 | `v0.22.9` / 提交 `c29f5f8`（文档提交 `b2a7b95`、`97e13f0` 之后） |
| 提交 | **`1d7d805`**（2026-09-22 提交，已推送 `main`） |
| 标签 | **`v0.22.10`**（annotated tag，已推送，触发 release 流水线 run **#12**） |
| 发布 | **pre-Release 已发布**（`prerelease=true`、`draft=false`，附件 `..._v0.22.10.zip` = **2,107,619 字节**） |
| 改动规模 | **8 个文件、+474 / −26**（源码 `core/workflow.py` +67/−16；测试 +259/−9；`CHANGELOG.md` +61；`metadata.yaml` ±1；`run_v02210_all.py` +86） |
| 改动文件 | `core/workflow.py`、`metadata.yaml`、`CHANGELOG.md`、`tests/test_v0227_result_hardening.py`、`tests/test_v0229_failclosed_and_no_duplicate.py`、`tests/test_simple_workflow_result.py` |
| 新增文件 | `tests/test_v02210_label_and_contract.py`（249 行）、`run_v02210_all.py`（86 行）、`docs/v0.22.10.diff`（632 行快照） |
| 本轮性质 | **纯后端** + 测试契约同步（前端零改动，`ui_*.py` 仅作回归） |
| 核验基准 | 请以提交 **`1d7d805`** 为准（本单是 tag 之前的文档提交，不含被测代码） |

本单目的：把 GPT 对 v0.22.9 的核验裁决里点名的两条（取舍 5、取舍 6）落地，
再把皮莉卡**自己多揪出来的一条**（记账的「第二套成功口径」）一并摊开请挑。

---

## 一、取舍 5：展示标签只留一份（靶心 → 改法 → 证据）

### 1. 靶心

`core/workflow.py::_fmt_results` 自带一份 `_tag`：

```python
_tag = {
    "success": "OK",
    "inferred_success": "已执行·未确认",
    "dispatched_unconfirmed": "已发送·未确认",
    "failed": "FAIL",
    "syntax_error": "语法错误",
    "unknown": "未知",
    "skipped": "未发送",
}
```

而 `core/command_result.py::STATUS_LABEL` 是另一份（`成功` / `失败` / `结果未知`…）。
**同一份判定，两套措辞**：单命令路径（`format_results`）显示「成功」，
工作流回执显示「OK」—— 主人对不上号，也等于把「已知状态集合」的边界
在展示层又抄了一遍。

### 2. 改法

```python
status = MCWorkflow._status_of(r)
label = STATUS_LABEL.get(status, STATUS_LABEL["unknown"])   # 单一真相
lines.append(f"[{label}] {r.get('command', '(空)')} → {r.get('output', '')}")
```

- `workflow.py` 导入 `STATUS_LABEL`，删除自带 `_tag`（全仓只留一份标签表）。
- 兜底写成 `STATUS_LABEL["unknown"]`（不是字面量）：将来改标签表，兜底一起变；
  且兜底措辞是「结果未知」，**绝不允许退回能被读成成功的措辞**。
- 用户可见措辞变化（本版唯一的界面级改动，回执文本）：

| 状态 | 旧回执 | 新回执 |
| --- | --- | --- |
| `success` | `[OK]` | `[成功]` |
| `failed` | `[FAIL]` | `[失败]` |
| `unknown` / 缺 `status` | `[未知]` | `[结果未知]` |
| `dispatched_unconfirmed` | `[已发送·未确认]` | `[已发送·边界未确认]` |
| `inferred_success` / `syntax_error` / `skipped` | 不变 | 不变 |

### 3. 证据

| 断言 | 结果 |
| --- | --- |
| 七态渲染出的标签集合 == `STATUS_LABEL.values()` | PASS |
| `success → [成功]`、`failed → [失败]`、`unknown → [结果未知]` | PASS |
| 缺 `status` 且 `ok=True` → `[结果未知]`，不出现任何成功字样 | PASS |
| `dispatched_unconfirmed → [已发送·边界未确认]`、`skipped → [未发送]` | PASS |
| 静态：`workflow.py` 里 `_tag = {` **0 处**；`STATUS_LABEL.get(status` 命中 | PASS |
| 静态：`STATUS_LABEL = {` 只在 `command_result.py` 定义 1 次 | PASS |

---

## 二、取舍 6：记账改 fail-closed（显式等长校验）

### 1. 靶心

```python
for c, r in zip(commands, exec_reports):   # 长度不一致 → 静默截断
    if r.get("ok"):
```

`zip()` 是隐式契约：`_exec_commands` 今天确实保证等长（熔断 / 目标不在线 /
空命令都会补 `skipped` / `failed` 条目），但**没有任何断言**。哪天有人改了它的
返回结构，记账会静默错位 —— 而错位的后果是「该拦的没拦（重复副作用）」
或「该记的没记（下一轮重发）」，两种都贵。

### 2. 改法

```python
if len(commands) != len(exec_reports):
    return set(), (
        f"命令与执行报告数量不一致：{len(commands)} 条命令 / "
        f"{len(exec_reports)} 份报告"
    )
```

返回签名由 `set[str]` 改为 `tuple[set[str], str | None]`：
第二个元素非空 = **这笔账不可信**，调用方必须停手，不许拿半截账继续跑。

新增 `_contract_halt_text()`：

> 工作流已暂停：内部执行报告与命令数量对不上，无法判断哪几条真正发出去过。
> （命令与执行报告数量不一致：N 条命令 / M 份报告）
> 为避免重复副作用或漏发，本轮不会自动重试、也不交给纠错 Agent 重发。
> 请先在游戏内确认实际结果，再决定是否重新发起；这属于插件缺陷，麻烦连同这条回执一起反馈给作者。

两处调用点（实现器循环 / 纠错循环）都改成：

```python
newly, contract_err = self._newly_confirmed(fresh, exec_reports)
if contract_err:
    return self._contract_halt_text(contract_err), False
confirmed |= newly
```

### 3. 证据

| 断言 | 结果 |
| --- | --- |
| 报告少于命令 → 结构异常且**一条都不记**（旧实现 `zip` 静默截断） | PASS |
| 命令少于报告 → 结构异常且一条都不记 | PASS |
| 异常说明写清两侧数量（`2 条命令` / `1 份报告`） | PASS |
| 对照：等长正常 → 只记 `success` 那条、无结构异常 | PASS |
| 对照：空对空不算结构异常 | PASS |
| 停手回执含「不会自动重试」+「重复副作用」+「漏发」+ 原因原文 | PASS |
| 停手回执不谎称任务已完成 | PASS |
| 静态：两处调用点都 `newly, contract_err = ...` 并处理 | PASS |

---

## 三、皮莉卡自曝：记账判据是「第二套成功口径」（GPT 未点名）

### 1. 靶心

同一行里还藏着另一个坏形状：

```python
if r.get("ok"):        # 记账看 ok
```

而熔断判据（v0.22.9）看的是 `status`：

```python
status = report.get("status")
return status if status in KNOWN_STATUSES else "unknown"
```

**两个判据、两双眼睛**。后果：报告只要漏填 `status` 又带 `ok=True`
（正是 v0.22.9 刚在熔断判据里堵掉的那种坏形状），它会被记成「已生效」→
下一轮同一条命令被当成重复而**跳过** → 主人要的东西少执行一次。
（本次实测该场景不会真的发生，因为 `_halt_on_uncertain` 会先把这类报告熔断掉；
但只要将来有人在记账与熔断之间插一行代码，这个洞就活了 —— 属于「靠顺序巧合活着」。）

### 2. 改法

```python
if MCWorkflow._status_of(r) == "success":     # 与熔断判据同一双眼睛
```

对 `_exec_commands()` 的**正常输出零行为变化**（它保证 `ok=True` ⟺ `status=success`：
`ok = res.status == "success"`，`unknown` 非幂等熔断、`unknown` 幂等降级为 `failed`）。
变化只发生在**结构异常报告**上：从「记账」挪到「不记账」。

### 3. 证据

| 断言 | 结果 |
| --- | --- |
| `{ok:True}` 缺 `status` → **不记账**（旧实现记成已生效） | PASS |
| 对照：`ok=True, status=success` → 正常记账 | PASS |
| `unknown` / `inferred_success` / `dispatched_unconfirmed` / `skipped` / `syntax_error` 一律不记账 | PASS |
| 静态：`workflow.py` 里 `if r.get("ok")` **0 处** | PASS |
| 闭环：记账结果喂给 `_split_confirmed` → 上一轮成功的命令下一轮被拦下 | PASS |
| 闭环：没做过的新命令正常放行 | PASS |

---

## 四、被本版同步更新的三处旧契约（透明列出）

本版正题就是改「措辞」与「记账」，三处测试原本**锁定了被改动的字面量**，
必须同步；我保留了每条的**断言意图**，没有削弱强度：

| 文件 | 原断言 | 新断言 | 意图是否保留 |
| --- | --- | --- | --- |
| `test_v0227_result_hardening.py` | `'"inferred_success": "已执行·未确认"' in WF_SRC` | `"STATUS_LABEL.get(status" in WF_SRC and "_tag = {" not in WF_SRC` | 保留（改为「措辞必须来自标签表」） |
| `test_v0229_failclosed_and_no_duplicate.py` | `"[未知]" in fmt and "[OK]" not in fmt` | `"[结果未知]" in fmt and "[成功]" not in fmt` | 保留（仍是「不得出现成功字样」） |
| `test_v0229_failclosed_and_no_duplicate.py` | `'"success": "OK"' in src` | `"_tag = {" not in src and "STATUS_LABEL.get(status" in src` | 保留（仍是「标签表只一份」） |
| `test_simple_workflow_result.py` | `"已发送·未确认" in fmt` | `"已发送·边界未确认" in fmt` | 保留（仍是「新状态有中文标签」） |

**自曝**：这类「改测试」有「迁就代码」的嫌疑，请 GPT 重点看这四条是否仍咬得住
原意图；若认为哪条被削弱了，请指名。

---

## 五、测试与回归

| 项 | 结果 |
| --- | --- |
| 新增 `tests/test_v02210_label_and_contract.py` | **43/43 断言通过**（6 组：标签单一真相 / 等长校验 / 判据同源 / 记账→拦截闭环 / 静态契约 / 旧口径回归） |
| `tests/test_*.py` 全量 | **26/26 文件通过**（v0.22.3 / v0.22.5 / v0.22.7 / v0.22.8 / v0.22.9 回归全绿） |
| `tests/ui_*.py` 真浏览器链路 | **10/10 文件通过** |
| `compileall core main.py` | 通过（exit 0） |
| 解释器 | AstrBot 自带 Python 3.12.12（`run_v02210_all.py` 自动探测，无告警） |
| 一键复现 | `run_v02210_all.py` |

---

## 六、自曝取舍（请重点挑这几条）

**取舍 1｜等长校验选「停手」，没做「按 command 字段配对」。**
GPT 给的另一条路是「让每份 report 自带命令、按字段对应」。我没做，理由：
`_exec_commands` 的 report **已经**带 `command` 字段，理论上能按文本配对；
但**同一批里出现两条相同命令是合法的**（用户本意可能就是发两份，见 v0.22.9 取舍 2），
按文本配对会把它们合并 → 反而制造错位。所以选择「显式等长 + 对不上就停手」。
**请裁决**：是否够？还是必须做字段配对（若做，同批重复怎么解）？

**取舍 2｜记账跟随 `status` 而不是 `ok`。**
这是本版的行为方向选择：正常路径零变化，异常路径从「记账」改为「不记账」。
另一条路是跟随 `ok`（保持旧行为）—— 但那等于保留第二套成功口径。
**请裁决**：跟随 `status` 是否正确？（我判断正确，因为 `KNOWN_STATUSES` 已是唯一状态全集。）

**取舍 3｜停手回执里写了「这属于插件缺陷，麻烦反馈给作者」。**
好处：结构异常是插件自身 bug，不该让主人以为是服务器问题。
**风险自曝**：措辞偏重（把内部缺陷摊到用户面前），可能显得插件不稳。
请裁决是否要软化（例如只说「内部状态异常，已停止本次操作」）。

**取舍 4｜标签统一的用户可见变化（`[OK]` → `[成功]` 等）。**
本版唯一界面级变化。**风险自曝**：老用户（包括主人的习惯用语）可能一时对不上；
README / 文档里若有回执截图需一并核对（本次已确认 README 无回执截图）。
请裁决是否可接受、是否需要在小节里再显眼提示。

**取舍 5｜兜底标签用 `STATUS_LABEL["unknown"]`（二次查表）而不是写死字符串。**
好处：兜底措辞跟着标签表走。风险：`STATUS_LABEL` 若哪天删掉 `unknown` 键会 `KeyError`
（但 `KNOWN_STATUSES` 就是它的键集，删掉等于全局崩，属可接受）。
请裁决这种「二次查表」是否够清楚。

**取舍 6｜真机未验证。**
服务器昨夜（2026-09-22 00:21）已按主人指令关服，本轮只有 Stub 级验证 + 全量回归 + UI 链路。
本版改动集中在**回执措辞**与**记账记账边界**，正常路径零行为变化，
但仍建议开服后跑一次普通复杂任务（例如「给某人一把附魔剑」）做冒烟。

---

## 七、请 GPT 裁决

1. 取舍 1：等长校验 + 停手是否足够，还是必须做 report 字段配对？
2. 取舍 2：记账跟随 `status`（而非 `ok`）是否正确？
3. 取舍 3：停手回执措辞是否要软化？
4. 取舍 4：标签措辞变化（`[OK]` → `[成功]`）是否可接受？
5. 第四节：四条旧契约的更新是否属于合理同步、有无被削弱？
6. 本版能否作为 `v0.22.10` pre-release 发布（仍缺 Vanilla / Paper / Fabric 真实矩阵）？

> 注：主人已拍板「先推 pre-Release」，故 tag `v0.22.10` 与 pre-Release 已按第八节留痕发布；
> 第 6 问仍请 GPT 裁决「此形态是否可接受为稳定版候选」，以及是否需在开服后补真机冒烟。

---

## 八、发布与流水线留痕

| 项 | 值 |
| --- | --- |
| 提交 | `1d7d805`（已推送 `main`；`main` 已推进到本单文档提交） |
| 提交信息 | `fix: v0.22.10（预发布）—— 回执标签统一走 STATUS_LABEL + 记账 fail-closed（显式等长校验 / 判据同源）` |
| 标签 | **`v0.22.10`**（annotated tag，已推送） |
| Release | **已发布为 pre-Release**：run **#12**（`release`，conclusion = `success`）<br>tag = `v0.22.10`、`prerelease = true`、`draft = false`、发布时间 `2026-09-22T03:08:56Z`<br>附件：`astrbot_plugin_Scintilla_MC_Server_Control_v0.22.10.zip`（**2,107,619 字节**） |
| 流水线 | `release` run **#12**（tag `v0.22.10`，head `4b82301`）→ `success`<br>`tests` run **#19**（`97e13f0`）→ `success`；run **#20**（`4b82301`）→ `success`；run **#21**（`6e3d735`）→ `success`<br>（每次 `main` 推送都会自动触发 `tests`，本单后续文档推送亦同，最新结论以 Actions 页面为准） |
| 插件重载 | **未重载**（等主人开服 / 拍板；本版无前端改动，重载只为生效后端） |
| 本单自身 | 本核验单以 `docs:` 文档提交追加，**晚于**被测提交，不含被测代码（本单最终 hash 见 `git log -1 -- docs/VERIFY_v0.22.10.md`） |
| 推送备注 | 本机经代理（`127.0.0.1:7897`）推送时 schannel 在 HTTP/2 下握手不稳，改用 `git -c http.version=HTTP/1.1 push` 后 `main` 推送成功（tag 那次首次即通） |

核验入口：

- 提交本体：`git show 1d7d805`
- 逐行 diff 快照：`docs/v0.22.10.diff`
- 一键复现：`run_v02210_all.py`
