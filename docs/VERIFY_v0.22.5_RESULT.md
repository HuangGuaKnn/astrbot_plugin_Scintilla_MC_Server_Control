# v0.22.5 核验报告

核验目录：C:\Users\10316\.astrbot\data\plugins\astrbot_plugin_Scintilla_MC_Server_Control
基线：3292d8b；当前为工作区未提交改动，metadata.yaml=v0.22.5。

## 结论

A1/A2/C1 核心连接重建与退役语义通过；B1 idle 连接污染修复通过；D1 测试替身修复没有发现通过删断言掩盖失败的证据；全仓 18 个 test_*.py 使用 AstrBot Python 3.12.12 全部退出码 0（交接单写 21 支，但当前仓库实际 glob 到 18 个）。

仍有两个 P2 观测/界面问题，建议修复后再发布：

1. `last_boundary_confirmed` 在零响应/哨兵超时等未知结果路径可能保留上一次 True。WebUI 运行态可能显示边界可靠，但最近一次命令没有边界确认。
2. 前端未消费 `boundary_confirmed`：配置本身为 idle 时 `degraded=False`，页面走「边界可靠」分支，掩盖 idle 模式本来就不可靠的事实。

## 已验证

- `tests/test_v0225_idle_reset.py`：AstrBot Python 执行，24 项通过，插件层 reset 与并发重建均实际执行。
- `tests/test_settings_save_resync.py`：AstrBot Python 执行，4 组接口断言通过，RCON 设置保存确实走带锁重建。
- 全部 `tests/test_*.py`：18/18 退出码 0。
- `compileall` 通过。
- `_rcon_lock` 在插件构造时创建；`_get_rcon` 与 `reset_rcon` 共用同一把锁。
- reset 在锁内摘旧、标记退役、空闲旧实例关闭、构造新实例；在途命令不被 reset 直接掐断，命令结束后旧实例 finally 关闭。
- idle 静默窗口会关闭连接、设置 `last_boundary_confirmed=False`，下一条命令重新建连，残留包不会污染下一条调用。
- `require_app()` 的运行事件循环护栏与当前模块级导入约定一致；全套依赖 AstrBot 的测试均已实际通过。
- v0.22.1 合规检查 `test_review_compliance.py` 通过。

## P2-1：边界观测位没有覆盖所有不确定路径

`core/rcon.py` 在 sentinel 正常结束时设置 True；idle 静默窗口设置 False。但命令零响应超时、sentinel 部分响应超时、协议异常等路径在进入末尾异常前没有统一设置 False。若上一次命令是 sentinel 成功，下一次命令超时，`last_boundary_confirmed` 仍可能是 True。

建议：在 `_command_locked` 开始就设为 False，只有收到完整 sentinel 边界后设 True；所有未知/异常路径保持 False。

## P2-2：前端 B4 分歧未解决

`core/web_api.py` 已返回 `boundary_confirmed` 与 `idle_unconfirmed`，但 `pages/mc_control/index.html:renderRconRuntime` 只判断 `rt.degraded`。当用户显式配置 `rcon_end_mode=idle` 时，`degraded` 为 False，页面会显示「✓ 边界可靠」，这与 idle 的实际弱保证相矛盾。即使配置为 sentinel 但尚未建立实例，返回默认 `boundary_confirmed=True` 也可能过于乐观。

建议前端优先判断：

    if (rt.end_mode === "idle" || rt.boundary_confirmed === false) {
        显示「⚠ 边界未确认 / idle 弱保证」
    }

后端无实例时建议返回 `boundary_confirmed: null`，前端显示「尚未建立连接」，不要使用 True 默认值。

## D1/D2/D3 判断

- 修改替身补 `_rcon_lock` 是夹具与实现新增字段同步，不是绕过产品逻辑；settings_save 新增硬断言能捕获重建失败。
- D2 `get_running_loop()` 只在运行事件循环内拒绝，模块加载阶段正常返回；当前 18 个测试均通过。
- 新靶场覆盖 idle split/迟到包、零响应、乱序、并发 reset，但未覆盖「命令响应与哨兵交错多包」和真实 RST。建议后续加入，但不影响本轮已验证结论。

源码未修改、未提交、未推送。
