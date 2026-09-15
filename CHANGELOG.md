# 更新日志

本项目的所有重要变更都记录在这里。格式参考
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

> **写作约定**：每个版本小节的第一行请用一行引用块写「本版一句话」摘要
> （`> 一句话：…`）。发布 Release 时会自动把小节正文当作说明，
> 这句摘要就会出现在最外层，访客不展开细节也能看懂这一版干了什么。

## [v0.21.39] - 2026-09-15

> 一句话：权限文案全链路说清楚了、修了两个文件头的 BOM、把发版流程自动化了。

### 变更
- 权限策略口径统一：白名单下「非管理员完全不能使用**口头命令工具**（执行指令 / 发物品 / 广播），插件自带功能照旧」。
  覆盖 README、WebUI 设置页策略卡片、`mcs 帮助`、配置说明（`docs/configure.md` 与 `_conf_schema.json`）与市场文案（`metadata.yaml`）。

### 修复
- `core/agent_llm.py`、`core/workflow.py` 文件头的 UTF-8 BOM（会让 `ast.parse`、linter 等静态工具直接报
  `invalid non-printable character U+FEFF`）。
- `requirements.txt` 注释里的反斜杠笔误（`\mcs` → `mcs`）。

### 工程
- 新增 GitHub Actions：`tests.yml` 在 push / PR 时自动跑回归测试；`release.yml` 推送 `v*` 标签即自动打包并发布 Release。
- 新增 `.editorconfig`（UTF-8 无 BOM + LF），防止 BOM / CRLF 再次混入。
- 新增 Issue 模板（Bug 反馈 / 功能建议）与 README 状态徽章。
- Release 包不再携带 `tests/`（`.gitattributes` 的 `export-ignore`），包体更小。

## [v0.21.38] - 2026-09-13

### 新增
- 上架 AstrBot 插件市场（审核通过，VirusTotal 零检出）。
- 文档新增四个页面：界面效果、配置详解、使用指南、常见问题；安装段补充「应用市场安装（推荐）」与 Releases 入口。

### 变更
- 元数据补齐 `short_desc` / `astrbot_version` / `support_platforms`；权限策略表述不再依赖行内代码渲染。

## [v0.21.37] - 2026-09-13

### 新增
- 首个公开版本：RCON 远程指令与 LLM 工具、`mcs` 指令组（绑定 / 喊话 / 状态 / 踢人封禁）、
  玩家事件播报、物品词典与整合包指纹、多 Agent 工作流与知识库、十组可视化设置与六页插件 WebUI（含深浅主题）。
