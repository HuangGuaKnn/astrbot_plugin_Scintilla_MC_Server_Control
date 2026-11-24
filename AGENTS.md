# AGENTS.md · 给 AI 助手的项目说明

> 这份文件是**给下一个接手的人（或 AI）看的**。改代码前先读完它，能省掉至少两次踩坑。
> 出单人约定：**动手前先读 §3 的红线，验收前先读 §4 的三件套，发版前先读 §6。**

---

## 1. 这是什么

AstrBot 插件：把 Minecraft 服务器的状态、玩家管理、RCON 终端、审计搬进 WebUI。
支持本地服务端与异地 RCON 两种模式，兼容 1.7.10 至 1.21 多个世代。

- 仓库根：本目录（脚本一律用 `__file__` 定位，克隆到哪都能跑）
- 部署目录：`<AstrBot 数据目录>/plugins/astrbot_plugin_Scintilla_MC_Server_Control`
- Python：`<AstrBot 安装目录>/backend/python/python.exe`
- 本机真实路径写在 `docs/_internal/STATUS.md`（那份不进仓库，守卫 `test_v0240_no_local_paths.py`
  也不扫它）—— **仓库内任何文件都不许出现本机路径**，这是个会响的警报，别写死。

---

## 2. 目录地图

| 路径 | 作用 |
| --- | --- |
| `core/server_card.py` | 服务器卡片：四字段 × 五态判定（**判定语义不许改**） |
| `core/command_result.py` | 命令回执判定内核：锚定成功/失败模式、认不出时带原文（**红线**） |
| `core/version_caps.py` | 世代能力表：哪个版本支持哪条命令（**红线**） |
| `core/rcon.py` | RCON 客户端：哨兵包、超时、进程树回收（**红线**） |
| `core/player_admin.py` | 名单 / 踢封解 / 白名单 / IP 封禁 / 审计（含脱敏口径） |
| `core/log_watcher.py` | 日志轮询、事件派发、玩家 IP 旁路索引 |
| `core/web_console.py` | WebUI 终端：命令分级提示、执行、日志尾 |
| `core/web_api.py` | 页面接口路由（只加路由，不改业务） |
| `core/knowledge_base.py` | 知识库与预设（读坏 vs 空库要分得清） |
| `pages/mc_control/index.html` | 单文件 UI（服务器四子页签 / 终端 / 设置） |
| `tests/mutation_all.py` | **变异总入口**（当前 165 条，四路并行，支持 `--changed`） |
| `tests/ui_*.py` | UI 硬门禁（18 个文件，独立浏览器） |
| `run_release_verify.py` | 发布门禁：静态检查 + 回归 + UI（`--ci` 只跑 CI 子集，`--bundled` 换内核） |
| `docs/roadmap.md` | 三档路线图（已排期版本必须严格晚于 `metadata.yaml` 当前版本） |
| `docs/_internal/` | **本地内部文档**（派活单、交付说明、STATUS）—— 已被 gitignore，不进公开仓库 |

---

## 3. 红线（改这些文件要先说清理由）

**判定内核**：`command_result.py` · `version_caps.py` · `rcon.py` · `server_card.py` · `tool_guard.py` · `strict_bool.py`

- 可以**新增**条目（例如给某条命令补锚定成功模式），但**不许改既有判定语义**；
- 任何改动都要能被一条变异守住，且该变异必须真的能红。

**判定原则**（踩过坑才写下的）：

- 只有**确定性证据**（行首锚定 `Unknown command`、服务端用法文本）才能判「该世代不支持」；
  其余「有回包但解析不出」一律判「回执认不出」并**强制带服务器原文**。
- 失败**不许被当成空值**。已经栽过三次：回调异常被计为已交付、读盘失败当空库、
  `banlist ips` 解析失败当没人被封。

---

## 4. 验证三件套

```powershell
$py = "<AstrBot 安装目录>\backend\python\python.exe"   # 本机实际值见 docs/_internal/STATUS.md
& $py -B -X utf8 tests\mutation_all.py             # 变异（四路并行，≈5.5 min）
& $py -B -X utf8 run_release_verify.py             # Edge 门禁（≈3 min）
& $py -B -X utf8 run_release_verify.py --bundled   # Chromium 门禁（≈3 min）
```

- 完整一轮 ≈ **11.5 分钟**（优化前是 45 分钟）；
- 日常改动：`tests\mutation_all.py --changed` —— 只跑受影响的变异，约 16 秒；
  **命中共享内核文件会自动 WARN 并退回全量**（这条防护比提速本身重要）；
- 变异输出会打印**时间账单**（选区/副本/基线/执行/核对各段耗时），想再压就照它找瓶颈。

---

## 5. 铁律

1. **判据必须能「改坏变红」**。写完判据要亲手改坏一次，看它是否红；改坏不红 = 没做。
2. **变异总数不许减少**。优化流水线可以并行、可以增量，但不能少跑一条。
3. **发版前必须跑全量**。增量选区有安全网，但共享内核名单不可能完备 —— 这条不能靠工具自觉。
4. **改源码前先跑基线，改完字节级还原**（SHA-256 比对）。变异框架会做，手工调试时自己也要做。
5. **别用 `git add -A` 无脑收文件**：曾经把测试临时文件收进过发版提交。
6. **别只信插件自己的记录**：验证「命令到底发出去没有」要查**服务端日志**，不是审计 JSONL。

---

## 6. 发版流程

```
1. 工作区干净 + 三件套全绿
2. 更新 CHANGELOG（首节版本号必须 == metadata.yaml 的版本）
3. git push 分支
4. git tag -a vX.Y.Z（预发布写 vX.Y.Z-rc.N —— workflow 会自动标 pre-release）
5. git push origin vX.Y.Z  → 触发 release workflow
6. git push origin main:main   ← 把默认分支快进到 Release 提交（少这步插件市场不认）
7. 若发错了：先删 Release，再删 tag（顺序不能反）
```

- `metadata.yaml` 的 `version` 只写 `vX.Y.Z`，**预发布后缀只出现在 tag 上**；
- roadmap 的「已排期」版本必须严格晚于当前版本（门禁会查）；
- **插件市场只盯默认分支**（见下面坑表）：tag / Release 发得再漂亮，`main` 不动，市场就还是旧版本。
  市场打包时读的是**默认分支 HEAD 上的 `metadata.yaml`**。

---

## 7. 已经踩过的坑（省你一次）

| 坑 | 表现 | 对策 |
| --- | --- | --- |
| PowerShell 写文件带 BOM | 提交标题前面多个不可见字符 | 用 `io.open(..., encoding="utf-8", newline="\n")` 写 |
| CRLF / LF 混用 | 多行锚点替换"找不到" | 先探测换行再替换 |
| `Start-Process` 起的 java 会随会话被收走 | 八台测试服瞬间全灭 | 用 WMI `Win32_Process.Create` |
| `git push` 走 stderr | 明明成功却像报错 | 判成功看 `git ls-remote` 的 SHA |
| 本机代理打崩 schannel | push 握手失败 | `git -c http.version=HTTP/1.1 push` |
| unittest 省略长字符串 | 变异按 failure 文本匹配会失灵 | 用 `raise AssertionError("marker")` 显式给消息 |
| 单行锚点 `str.replace(..., 1)` | 打到了同名代码的另一处 | 锚点带上文，或按行号操作 |
| 部署了没重载 | 改的文件是新的，行为还是旧的 | 复制完必须 reload 插件 |
| 发版只推 tag、忘了推 `main` | GitHub 上 v0.25.0 的 Release 好好挂着，**AstrBot 插件市场仍显示 0.24.12**（main 停在 8 天前的 v0.24.12 提交上） | 发版后 `git push origin main:main`；市场只读默认分支 HEAD 的 `metadata.yaml`，不看 tag/Release（2026-11-24 在五个插件上核对：市场 `commit_sha` 全部 == 各自默认分支 HEAD） |
| 想「重提同一个版本号」让市场刷新 | 市场同一版本号只能上架一次，zip 冻结在索尔特家 S3 | 必须 bump 版本号（当时发 v0.23.6 绕过） |

---

## 8. 测试基准事实（真机验过，别再猜）

- 1.7.10 / 1.12.2：`There are 2/20 players online:`
- 1.13–1.15：`There are 0 of a max 20 players online:`（**没有第二个 `of`**）
- 1.13.2 的 `banlist ips`：`There are 1 bans:<ip> was banned by <src>: <reason>`
  —— 前缀末尾那个**冒号会被 IP 字符类吞掉**，必须先剥前缀
- 服务端会把玩家名**小写化**（比较一律 eye fold）
- `/list` 不给 IP；IP 只在 `Name[/IP:port] logged in with entity id` 行里
- 通过 FRP / 端口转发进来时，服务端看到的永远是**最后一跳**的地址（回环或转发机地址）

---

## 9. 角色分工

- **外部 Agent**：重活（大功能、批量改造、重构、体检）。
- **本机 Agent**：审核、验收、发版、方案讨论、修小问题与微调。

派活单与交付说明放 `docs/_internal/`，**不进公开仓库**。
