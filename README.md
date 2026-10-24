# Cherry Studio Agent 自由交流助手

> 让 [Cherry Studio](https://github.com/CherryHQ/cherry-studio) 里的 AI Agent 之间**自由互发消息、多轮协作**，全程无需人工点击确认。
>
> 便携单文件版 · 用完自动还原 · 不改动你的数据

---

## 它解决什么问题

Cherry Studio 支持多个 Agent 会话之间互相发消息（`session_send` / `session_create`），但默认体验被两道限制卡住：

### 限制一：每次发消息都要人工点「允许」

Agent 之间每通信一次，就弹一张审批卡片。开会话、发消息、转达任务……手点几十次。

### 限制二：被唤醒的 Agent 根本发不出消息

更硬的限制：一个 Agent 收到别的 Agent 发来的消息后（官方称 *headless turn*），它想再发消息给别的 Agent，会被直接拒绝：

```
PreToolUse hook error: This channel or scheduled turn has no interactive responder
SESSION_TOOL_FORBIDDEN: Cross-Session discovery and delegation require an interactive user turn
```

连审批卡片都不会弹——不是没人点，是**系统不让它发**。结果就是 Agent 之间只能走「一跳」，无法多轮往来、无法串联协作。

**本工具把两道限制都解开了。** Agent A → B → C → A，想怎么聊怎么聊。

---

## 特性

| 特性 | 说明 |
|---|---|
| 🎯 **自动定位** | 自动查找本机 Cherry Studio（注册表 / 常见路径 / 快捷方式 / 运行进程 / 磁盘扫描），无需配置 |
| 🪶 **单文件便携** | 一个 11MB 的 exe，自带 Python 运行时，目标机不用装任何环境 |
| ♻️ **用完即还原** | 关闭 Cherry Studio 时自动恢复被修改的文件，不在系统里留改动 |
| 🩺 **崩溃自愈** | 被强杀或断电也能在下次启动时自动还原干净 |
| 🔒 **最小放行** | 只自动批准「会话通信 / 建会话 / 装技能」三类，其它审批仍需人工确认 |
| 🖱️ **零侵入** | 不移动鼠标、不抢焦点、不碰数据库、不碰会话记录 |
| 🛡️ **升级安全** | 备份按文件指纹命名，Cherry 升级后不会用旧备份覆盖新版本 |

---

## 快速开始

### 方式一：直接下载（推荐）

1. 到 [Releases](../../releases) 下载 `Cherry AI 工作室.exe`
2. 双击它

没了。程序会自己找 Cherry Studio、启动、接管，然后安静地待在后台。

**关闭 Cherry Studio 后，它会自动还原文件并退出。**

### 方式二：从源码运行

```bash
pip install websocket-client psutil pywin32 pillow
python CherryPortable.py
```

### 方式三：自己打包

见 [docs/BUILD.md](docs/BUILD.md)。

---

## 工作流程

```
双击 exe
  │
  ├─ 1. 定位 Cherry Studio
  │      注册表 → 常见安装路径 → 开始菜单快捷方式 → 运行中进程 → 磁盘扫描
  │
  ├─ 2. 备份 app.asar（按 MD5 指纹命名，同版本只备一次）
  │
  ├─ 3. 打临时补丁（两处等长字节替换 + 重算完整性哈希）
  │
  ├─ 4. 带调试端口启动 Cherry Studio
  │
  ├─ 5. 通过 CDP 注入守护脚本 → 自动点掉「允许」卡片
  │
  └─ 6. 监控进程 → 你一关 Cherry，立刻还原 app.asar 并退出
```

---

## 原理

### 为什么改 app.asar？

Cherry Studio 把跨会话消息的限制硬编码在 Electron 主进程里：

```javascript
// 内置工具策略表：session_send 被定为"不可绕过"档
cherrySessionSend: tool(..., "required", "enforce"),
cherrySessionList: tool(..., "auto"),        // 对比：列表查询是免审批的

// 路由层：非交互回合直接抛错
if (interaction.currentTurn === "headless" ||
    interaction.userResponse === "unavailable")
  throw new AgentSessionDeliveryRoutingError("SESSION_TOOL_FORBIDDEN", ...);
```

没有配置项、没有环境变量可以放开它。所以本工具做两处**等长字节替换**：

| # | 原值 | 改为 | 效果 |
|---|---|---|---|
| 1 | `"required", "enforce"` | `"auto",     "enforce"` | 不再弹审批卡片 |
| 2 | `if (headless \|\| unavailable)` | `if (!1)` | 被唤醒的 Agent 也能发消息 |

两处都保持字节长度不变，因此 asar 内部偏移表无需重建；同时重算该文件的 SHA256 并等长更新 header 里的 integrity 字段。

### 为什么还要 CDP 注入？

补齐第 1 处后大部分审批不再弹卡，但为防止其它路径仍有授权交互，程序同时通过 Chrome DevTools Protocol 在渲染进程注入守护脚本：轮询 + MutationObserver 双通道监听授权卡片，命中白名单工具就点「允许」。

**代码里不存在任何点击「拒绝」的路径。**

---

## 配置

改 `CherryPortable.py` 顶部常量即可：

```python
WHITELIST = {
    "session_send": True,      # Agent 之间发消息
    "session_create": True,    # 新建 Agent 会话
    "install_skill": True,     # 安装技能
}

CDP_PORT = 9333                # 调试端口
LOCK_PORT = 9336               # 单实例锁
SCAN_MS = 500                  # 卡片扫描间隔
CLICK_COOLDOWN_MS = 1500       # 两次点击最小间隔
```

想放行别的工具：往 `WHITELIST` 里加一条。

---

## 文件位置

```
%LOCALAPPDATA%\CherryAIStudio\
├─ backup\  app.asar.<md5前16位>.orig   ← 原始文件备份（约 297MB，最多保留 2 份）
├─ logs\    app-YYYYMMDD.log            ← 运行日志
└─ state.json                            ← 仅运行时存在，正常退出会删除
```

**完全卸载**：删掉 exe + 删掉 `%LOCALAPPDATA%\CherryAIStudio` 目录即可，系统其它地方没有任何改动。

---

## 常见问题

**Q：需要管理员权限吗？**
修改 `C:\Program Files` 下的文件需要。程序会自己申请 UAC；如果你拒绝，会降级为「只做自动授权、不改文件」模式（多轮互通不可用）。

**Q：Cherry 升级后要重新做什么吗？**
不需要。下次双击自动按新版本重新备份、重新打补丁。旧版本备份永远不会覆盖新版本。

**Q：杀毒软件报毒？**
PyInstaller 打包的 exe 常被误报。可以自行从源码打包，或用源码直接运行。

**Q：会不会误点「拒绝」？**
不会。代码只查找文本以「允许」/「Allow」开头的按钮，并且显式排除了含「拒绝/deny/reject/取消/cancel/关闭/close」的按钮。

**Q：会话记录会丢吗？**
不会。程序只改 app.asar 里两处字节，完全不碰数据库和会话数据。

**Q：为什么关掉后要还原？**
保持 Cherry Studio 原样是设计底线。需要时再启动即可，不留长期改动。

---

## 安全与免责

- 本工具修改的是**你本机安装的** Cherry Studio 程序文件，修改前会完整备份，退出时还原。
- 仅用于个人学习、研究与效率提升。请遵守 Cherry Studio 的许可协议与当地法律法规。
- 自动批准功能会**跳过人工确认环节**，请自行评估风险；建议只在你信任的 Agent 与任务中使用。
- 使用本工具产生的任何后果由使用者自行承担。

---

## 开发

```
CherryPortable.py     主程序（单文件，含全部功能）
app_icon.ico          程序图标（6 种尺寸）
docs/BUILD.md         打包说明 / 改动指南 / 升级兼容排查
docs/USAGE-zh.md      详细使用说明
```

技术栈：Python 3.14 · PyInstaller · websocket-client · psutil · pywin32

贡献欢迎，提 Issue 或 PR 都行。如果 Cherry 大版本升级导致选择器失效，请附上 `logs\app-*.log` 和 Cherry 版本号。

---

## License

MIT

---

## 致谢

- [Cherry Studio](https://github.com/CherryHQ/cherry-studio) — 优秀的桌面 AI 客户端
- 所有让 Agent 协作变得更顺畅的开源项目
