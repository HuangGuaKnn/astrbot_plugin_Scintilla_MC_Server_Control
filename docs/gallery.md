# 界面效果

[返回 README](../README.md) · [配置详解](configure.md) · [使用指南](usage.md) · [常见问题](faq.md)

插件自带一套完整的 WebUI：**六个功能页签、十个配置分组、深浅两套主题**（顶栏右上角一键切换）。
下面全部是实际运行的截图。

> 截图为演示数据（玩家 Steve、示例知识条目等），仅用于展示界面形态。

## 概览

四个模块状态卡（物品词典 / 知识库 / 多 Agent 工作流 / 事件转发）、服务器连接自检、最近事件流。

![概览页](images/overview-dark.png)

<details><summary>浅色模式</summary>

![概览页 · 浅色](images/overview-light.png)

</details>

## 服务器

在线玩家与服务器状态（版本、MSPT、TPS、运行时长、内存、CPU），以及聊天栏 / 全屏标题 / 模拟任务输出的文本颜色与渐变设置。

![服务器页](images/server-dark.png)

<details><summary>浅色模式</summary>

![服务器页 · 浅色](images/server-light.png)

</details>

## 知识库

按服务端指纹隔离的经验库；条目可编辑、可校验、可标记纠错，换服时弹窗提醒而不会串库。

![知识库页](images/knowledge-dark.png)

<details><summary>浅色模式</summary>

![知识库页 · 浅色](images/knowledge-light.png)

</details>

## 工作流

多 Agent 流水线实况：每个任务走了哪个 Agent、耗时多久、成功还是失败。

![工作流页](images/workflow-dark.png)

<details><summary>浅色模式</summary>

![工作流页 · 浅色](images/workflow-light.png)

</details>

## 提示词

五个 Agent（分类 / 模板判断 / 模板工程师 / 实现器 / 纠错器）的系统提示词，可逐条改写，留空即跟随内置默认。

![提示词页](images/prompts-dark.png)

<details><summary>浅色模式</summary>

![提示词页 · 浅色](images/prompts-light.png)

</details>

## 设置页

设置页内容较多，下面按分组截取。每一项的详细说明见 [配置详解](configure.md)。

### ① 基础 · 连接与安全

异地 RCON 模式、服务端目录、RCON 连接参数、管理员名单、命令工具白/黑名单、权限前置提醒、拦截闩锁、封禁默认理由。

![设置页 · ① 基础 · 连接与安全](images/settings-conn-dark.png)

<details><summary>浅色模式</summary>

![设置页 · ① 基础 · 连接与安全 · 浅色](images/settings-conn-light.png)

</details>

### ② 消息互通 · 播报与桥接

六类服务器事件（进出 / 聊天 / 死亡 / 成就 / 指令）转发到会话，以及游戏内聊天 ↔ 群聊双向桥接。

![设置页 · ② 消息互通 · 播报与桥接](images/settings-notify-dark.png)

<details><summary>浅色模式</summary>

![设置页 · ② 消息互通 · 播报与桥接 · 浅色](images/settings-notify-light.png)

</details>

### ③ 外部平台指令 · mcs 指令组

绑定、喊话、状态、踢人、封禁、解封、封禁列表、帮助、全屏喊话，逐条独立开关。

![设置页 · ③ 外部平台指令 · mcs 指令组](images/settings-cmd-dark.png)

<details><summary>浅色模式</summary>

![设置页 · ③ 外部平台指令 · mcs 指令组 · 浅色](images/settings-cmd-light.png)

</details>

### ④ AI 能力 · LLM 工具与知识库

12 个 LLM 工具逐个开关，以及物品词典、学习型知识库、检索 / 沉淀 / 纠错三件套。

![设置页 · ④ AI 能力 · LLM 工具与知识库](images/settings-tools-dark.png)

<details><summary>浅色模式</summary>

![设置页 · ④ AI 能力 · LLM 工具与知识库 · 浅色](images/settings-tools-light.png)

</details>

### ⑤ 外观 · 反馈与颜色

反馈署名、公屏回执、渐变色总开关与四种输出格式，聊天栏 / 全屏标题 / 模拟任务输出三处颜色与渐变锚点。

![设置页 · ⑤ 外观 · 反馈与颜色](images/settings-look-dark.png)

<details><summary>浅色模式</summary>

![设置页 · ⑤ 外观 · 反馈与颜色 · 浅色](images/settings-look-light.png)

</details>

### ⑥ 自动化 · 多 Agent 工作流

工作流总开关、五个 Agent 各自指定 Provider、实现器最大尝试轮数、纠错循环最大轮数。

![设置页 · ⑥ 自动化 · 多 Agent 工作流](images/settings-workflow-dark.png)

<details><summary>浅色模式</summary>

![设置页 · ⑥ 自动化 · 多 Agent 工作流 · 浅色](images/settings-workflow-light.png)

</details>

---

更多： [返回 README](../README.md) · [界面效果](gallery.md) · [使用指南](usage.md) · [常见问题](faq.md)
