# v0.22.9 核验报告（GPT 裁定 · 收录）

- 核验目录：`C:\Users\10316\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control`
- 被测代码提交：`c29f5f8`
- 文档追加提交：`b2a7b95`
- tag：`v0.22.9`
- GitHub Release：已发布、`prerelease=true`、`draft=false`
- `metadata.yaml`：`v0.22.9`
- 裁定转达时间：2026-09-22 10:45（CST）

## 结论

**v0.22.9 核心修复通过，可以保留为 pre-release。**

当前仓库状态核对：被测代码提交 `c29f5f8`、文档追加提交 `b2a7b95`、tag `v0.22.9`、GitHub Release 已发布、`prerelease=true`、`draft=false`、`metadata.yaml=v0.22.9`、Release 附件已上传且大小与交接单记录一致。

## 1. `_status_of` fail-closed：通过

已确认：

```text
缺少 status + ok=True → unknown
status="" / None / SUCCESS / 未知值 → unknown
合法状态 → 原样返回
```

`KNOWN_STATUSES` 由 `STATUS_LABEL` 唯一派生，`_fmt_results()` 也复用 `_status_of()`，没有留下第二套状态回推逻辑。缺少 `status` 的报告不会再显示成 `[OK]`，而是 `[未知]`。该项修复有效。

## 2. 已确认成功命令不重复执行：通过

新增测试 `test_v0229_failclosed_and_no_duplicate.py` 的 53 项全部通过。已验证：

- 第一轮命令成功；
- Agent 自评 `success=false`；
- 第二轮仍生成相同命令；
- 相同命令只发送一次；
- 实现器确实进入了第二轮，说明没有粗暴关闭迭代；
- 整批重复时会停止并提示人工裁决；
- 部分重复时跳过旧命令，只执行新命令；
- 带斜杠和多余空格的命令也能识别为同一条；
- 新命令仍可正常进入下一轮；
- `unknown`、`inferred_success`、`dispatched_unconfirmed` 仍然熔断；
- `syntax_error` 仍允许安全纠错重试。

## 3. 回执字段安全：通过

`_fmt_results()` 已经使用：

```python
MCWorkflow._status_of(r)
r.get("command", "(空)")
r.get("output", "")
```

因此报告缺少字段时：不会触发 `KeyError`；不会把缺失状态默认成成功；能正常生成熔断回执。

## 4. 测试结果

使用 AstrBot 自带 Python 运行新增和关键测试：

| 项 | 结果 |
| --- | --- |
| `test_v0229_failclosed_and_no_duplicate.py` | **53 项通过** |
| `test_remote_deploy_audit.py` | 通过 |
| `ui_version_override_check.py` | 20 项通过 |
| 版本控件真实 Edge 链路 | 通过 |

版本设置链路已实际验证：

```text
填写 1.20.1
→ 提交 settings/save
→ 后端重新计算能力
→ 页面显示 legacy_nbt
→ 来源显示为手动声明
→ 清空后恢复为版本未知
```

需要说明：交接脚本 `run_v0229_all.py` 使用 `sys.executable`，若用普通系统 Python 启动会因缺少 AstrBot 依赖导致部分测试和 UI 测试失败；必须使用：

```powershell
C:\Users\10316\AppData\Local\AstrBot\backend\python\python.exe run_v0229_all.py
```

因此 GPT 不把「完整 25 + 10 批处理」作为本轮独立复跑结论；新增靶心测试、远程部署测试和真实 Edge 版本链路已单独通过。

> 皮莉卡跟进（v0.22.9 文档提交）：`run_v0229_all.py` 已改为**自动探测 AstrBot 解释器**
> （`ASTRBOT_PYTHON` 环境变量 → AstrBot 自带解释器 → 当前解释器，逐个校验 `import astrbot`），
> 探测失败会打印告警与手工指定示例，避免再用系统 Python 误跑。

## 对交接单取舍的裁决

### 取舍 1：命令规范化是否折叠大小写

**当前保留大小写，接受。** Minecraft 中玩家名、资源标识符、模组命令参数可能有大小写语义，全局转小写可能误把不同命令参数视为同一条，造成不必要的跳过。当前策略：只折叠空白和前导斜杠、保留大小写。代价是 Agent 改变大小写时可能漏拦重复命令，但这是偏向「宁可多执行一次确认过的不同文本，也不误跳过用户明确要求的新命令」的取舍。后续可以只对命令名和已知资源 ID 做局部规范化，不建议全局小写。

### 取舍 2：同一批内重复命令不拦

**当前接受。** 同一批内重复可能是用户确实要求两次执行（例如给两名玩家分别发一份）。跨轮重复和同批重复的语义不同，当前只阻止「跨轮重复已确认命令」，符合现有工作流设计。若以后要防止 Agent 自身重复，应新增显式配置或 Agent 级别的重复检测，不建议直接改变当前语义。

### 取舍 3：整批重复时停手问人

**接受，措辞基本清楚。** 当前回执明确写出「已生效·未重发 / 不会自动重试 / 请先在游戏内确认实际结果 / 确实还差什么，请换一种说法重新发起」。它没有错误地说「命令失败」，也没有静默报告任务完成，能够解释为什么看起来「没有继续执行」。

### 取舍 4：把既成事实提示放入 `failures`

**本版接受，下一版可优化。** 当前做法不需要改 Agent 接口，且有标题分节提示，短期风险可控。长期建议给 `implement()` 增加结构化字段，例如：

```python
confirmed_commands=[...]
previous_failures="..."
```

这样能避免 LLM 把「已成功命令」误读成「失败命令」。

### 取舍 5：展示标签表有两份

**列为下一版 P2/P1 整理项，不阻断 v0.22.9。** 当前判定状态已经只有一份真相（`KNOWN_STATUSES = frozenset(STATUS_LABEL)`），重复的 `_tag` 只影响展示措辞，不影响熔断和执行判定。后续可以让 `_fmt_results()` 直接使用公共 `STATUS_LABEL`，同时更新旧的静态测试契约。

### 取舍 6：`zip(commands, exec_reports)` 缺显式长度校验

**建议补，列为下一版 P2。** 当前 `_exec_commands()` 的确会保持等长，现有测试也覆盖了跳过条目，但这是隐式契约。建议改为：

```python
if len(commands) != len(exec_reports):
    raise RuntimeError(
        f"命令与执行报告数量不一致：{len(commands)} != {len(exec_reports)}"
    )
```

更稳妥的做法是不依赖 `zip()`，而是让每份 report 自带命令，然后按命令字段对应；至少也应在 `_newly_confirmed()` 开头做 fail-closed 校验。

### 取舍 7：没有真实服务端测试

**接受继续保持 pre-release。** 目前新增逻辑主要由 Stub 和回归测试验证，真实服务端矩阵仍未覆盖：Vanilla、Paper、Fabric、Forge/NeoForge 其他版本、不同语言和模组命令反馈。建议服务器重启后做一次真实冒烟：给指定玩家发一个简单物品；执行一个复杂附魔物品任务；让 Agent 自评未完成但命令已成功的路径尽量进行模拟；确认同一任务不会重复发放。

## 最终结论

**v0.22.9 验收通过，建议继续作为 pre-release 保留。** 本轮两条 P1 已有效落地：

1. 缺少 `status` 时 fail-closed，不再默认成功；
2. 同一任务内不重复执行已经确认生效的命令。

## 下一版（v0.22.10 候选）待办

| # | 项 | 来源 | 优先级 |
| --- | --- | --- | --- |
| 1 | `_newly_confirmed()` 显式等长校验（fail-closed，不依赖 `zip()` 隐式契约） | 取舍 6 裁决 | P2 |
| 2 | 统一 `STATUS_LABEL` 与 workflow 展示标签（连带更新 v0.22.7 静态契约测试） | 取舍 5 裁决 | P2/P1 整理项 |
| 3 | `confirmed_commands` 从 `failures` 字符串拆成结构化 Agent 参数 | 取舍 4 裁决 | 下一版 |
| 4 | 开服后完成至少一次真实复杂 NBT 冒烟测试（含「自评未完成但命令已成功」路径） | 取舍 7 裁决 | 待开服 |
