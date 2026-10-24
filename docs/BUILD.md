# 如何重新打包成 exe

本程序是 Python 源码 + PyInstaller 打包成的单文件 exe。需要改功能或重新编译时看这里。

---

## 一、环境要求

| 项 | 版本 | 说明 |
|---|---|---|
| Python | 3.10+（开发时用 3.14） | 装依赖用 |
| PyInstaller | 6.22+ | 打包工具 |
| 依赖库 | `websocket-client`、`psutil`、`pywin32`、`pillow` | 运行与图标处理 |

安装依赖：

```bat
python -m pip install pyinstaller websocket-client psutil pywin32 pillow
```

---

## 二、打包命令

在源码目录下执行：

```bat
python -m PyInstaller --onefile --noconsole --clean ^
  --name "Cherry AI 工作室" ^
  --icon "app_icon.ico" ^
  --hidden-import websocket ^
  --hidden-import psutil ^
  --hidden-import win32gui ^
  --hidden-import win32api ^
  --hidden-import win32com ^
  --hidden-import win32com.client ^
  CherryPortable.py
```

产物：`dist\Cherry AI 工作室.exe`（约 11 MB）

**参数含义：**

| 参数 | 作用 |
|---|---|
| `--onefile` | 打成单个 exe（运行时自解压到临时目录） |
| `--noconsole` | 不显示黑窗口 |
| `--clean` | 清掉上次构建缓存 |
| `--icon` | 程序图标（`app_icon.ico`，六种尺寸） |
| `--hidden-import` | 这几个库是运行时动态导入的，必须手动声明，否则打包后报 ImportError |

---

## 三、改功能常改哪里

打开 `CherryPortable.py`，几个关键位置：

| 想改什么 | 找哪里 |
|---|---|
| **自动批准哪些工具** | 顶部 `WHITELIST` 字典，加 `"工具名": True` |
| **调试端口** | `CDP_PORT = 9333` |
| **单实例锁端口** | `LOCK_PORT = 9336` |
| **扫描卡片间隔** | `SCAN_MS = 500`（毫秒） |
| **两次点击最小间隔** | `CLICK_COOLDOWN_MS = 1500` |
| **放行哪些 Cherry 限制** | `RULES` 列表（两处等长字节替换） |
| **DOM 选择器**（Cherry 升级后失效时改） | `SELECTORS` 字典 |
| **找 Cherry 的方式** | `class Locator`，五个 `_from_*` 方法 |

---

## 四、改完怎么验证

```bat
:: 1. 语法检查
python -m py_compile CherryPortable.py

:: 2. 直接跑源码（不打包，最快验证逻辑）
python CherryPortable.py
:: 此时日志在 %LOCALAPPDATA%\CherryAIStudio\logs\app-YYYYMMDD.log

:: 3. 打包后再跑一次 exe
```

---

## 五、重要技术约束（改代码前必读）

### 1. 补丁必须是「等长替换」
`RULES` 里的 `old` 和 `new` **字节数必须完全相同**。因为 app.asar 内部有偏移表和完整性哈希，长度一变整个文件结构就废了。

短了用空格补齐，例如：
```python
"new": b"!1" + b" " * (len(old) - 2)
```

### 2. 改完要重算哈希
代码里已自动处理：重算该文件的 SHA256，并按 blockSize 分块更新 asar header 里的 integrity 字段（哈希是定长 64 字符，可等长替换）。

### 3. 子进程环境必须洗干净
`clean_env()` 会从 PATH 里剔除 PyInstaller 的 `_MEI` 解包路径。
**不清理的后果**：Cherry 会从临时目录加载 `VCRUNTIME140.dll`，锁住目录导致程序退出时卡死 + 留下 22MB 垃圾。

### 4. 备份必须按指纹命名
`app.asar.<md5前16位>.orig`。若用固定文件名，Cherry 升级后退出时会把**旧版本**的 asar 覆盖回去，直接把 Cherry 搞坏。

---

## 六、Cherry 升级后选择器失效怎么办

本程序依赖这些 DOM 属性（`SELECTORS`）：

```
[data-ui="chat.permission-request-composer"]       授权卡片容器
[data-ui="chat.permission-preview-header"] div     卡片里的工具名
```

升级后如果卡片不再自动点掉，用 CDP 重新抓一遍 DOM：

1. 用带调试端口启动 Cherry
2. 访问 `http://127.0.0.1:9333/json/list` 拿到 webSocketDebuggerUrl
3. 通过 CDP 执行：
   ```js
   document.body.innerHTML.match(/data-ui="[^"]*permission[^"]*"/g)
   ```
4. 把新属性填回 `SELECTORS`

另外可以从 `resources\app.asar` 里搜 i18n 文案（`agent.toolPermission.button.allow`）确认按钮文案是否变了。
