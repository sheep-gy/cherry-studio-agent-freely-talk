#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Cherry AI 工作室 · 便携外挂版（单文件）
================================================================================
作用：让 Cherry Studio 里的 AI Agent 之间可以自由互发消息、多轮对话，无需人工点确认。

工作流程（全自动，无窗口）：
  1. 自动定位本机的 Cherry Studio（注册表 → 常见安装路径 → 开始菜单快捷方式 → 运行中进程 → 磁盘搜索）
  2. 备份并临时修改其 app.asar（放开跨会话消息限制）
  3. 带调试端口启动 Cherry Studio
  4. 后台注入守护脚本，自动点掉「允许」授权卡片
  5. 监控 Cherry Studio；一旦你关掉它，立即还原 app.asar 并自行退出

设计原则：
  * 用完即还原：退出时把 app.asar 恢复原样，不在你电脑上留下改动
  * 异常自愈：如果程序被强杀/断电，下次启动会先把残留改动还原干净
  * 便携：不写注册表、不装服务、不依赖 Python 环境（已打包）
  * 只读不改数据：数据库、会话记录、配置一律不碰

作者：sheep-gy ｜ 版本 3.0（便携版）
"""
import atexit
import glob
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request

# ============================================================== 常量
APP_NAME = "Cherry AI 工作室"

LOCAL = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
STATE_DIR = os.path.join(LOCAL, "CherryAIStudio")
BACKUP_DIR = os.path.join(STATE_DIR, "backup")
LOG_DIR = os.path.join(STATE_DIR, "logs")
STATE_FILE = os.path.join(STATE_DIR, "state.json")

CDP_PORT = 9333
LOCK_PORT = 9336          # 单实例锁端口（避开 9333 CDP、9334 旧版锁）
CDP_HOST = "127.0.0.1"

# 允许自动批准的工具（可自由增删）
WHITELIST = {
    "session_send": True,      # Agent 之间发消息
    "session_create": True,    # 新建 Agent 会话
    "install_skill": True,     # 安装技能
}

SELECTORS = {
    "card": '[data-ui="chat.permission-request-composer"]',
    "tool_name": '[data-ui="chat.permission-preview-header"] div',
    "allow_button_keywords": ["允许", "Allow"],
}
SCAN_MS = 500
CLICK_COOLDOWN_MS = 1500
ARGS_PREVIEW_CHARS = 200

# 补丁规则：都是等长替换，不改文件结构
RULES = [
    {
        "name": "审批档位 enforce→auto",
        "old": b'"required", "enforce"',
        "new": b'"auto",     "enforce"',
        "expect": 2,
    },
    {
        "name": "路由层 headless 拦截→恒假",
        "old": b'interaction.currentTurn === "headless" || interaction.userResponse === "unavailable"',
        "new": None,   # 下面按等长自动填充
        "expect": 1,
    },
]
RULES[1]["new"] = b"!1" + b" " * (len(RULES[1]["old"]) - 2)

CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008

CREATE_NO_WINDOW, DETACHED = 0x08000000, 0x00000008

# ============================================================== 注入脚本
INJECTED_JS = r"""
(function () {
  'use strict';
  try {
    if (window.__CHERRY_AA__ && typeof window.__CHERRY_AA__.stop === 'function') {
      window.__CHERRY_AA__.stop('replaced');
    }
  } catch (e) {}

  var CFG = window.__CHERRY_AA_CFG__ || {};
  var SEL = CFG.selectors || {};
  var BEH = CFG.behavior || {};

  var CARD_SEL = SEL.card || '[data-ui="chat.permission-request-composer"]';
  var HEADER_SEL = SEL.tool_name || '[data-ui="chat.permission-preview-header"] div';
  var ALLOW_KWS = SEL.allow_button_keywords || ['允许', 'Allow'];
  var WHITELIST = CFG.whitelist || {};
  var DRY_RUN = BEH.dry_run === true;
  var SCAN_MS = Math.max(200, BEH.scan_interval_ms || 500);
  var COOLDOWN_MS = Math.max(500, BEH.click_cooldown_ms || 1500);
  var ARGS_MAX = BEH.log_args_preview_chars || 200;

  var stats = { scans: 0, cards_seen: 0, approvals: 0, blocked: 0, errors: 0, last_click_at: null };
  var handled = new WeakSet();
  var warned = new WeakSet();
  var lastClickAt = 0;

  function report(evt) {
    try {
      var s = JSON.stringify(evt);
      if (typeof window.__cherryAA_report === 'function') window.__cherryAA_report(s);
      else if (window.console) console.log('[CHERRY-AA] ' + s);
    } catch (e) {}
  }

  function normalizeTool(raw) {
    var s = (raw || '').trim();
    var m = /^mcp__(.+?)__(.+)$/.exec(s);
    return (m ? m[2] : s).trim();
  }

  function readToolName(card) {
    try {
      var nodes = card.querySelectorAll(HEADER_SEL);
      for (var i = 0; i < nodes.length; i++) {
        var t = (nodes[i].textContent || '').trim();
        if (t) return t;
      }
    } catch (e) {}
    var all = (card.textContent || '');
    var names = Object.keys(WHITELIST);
    for (var j = 0; j < names.length; j++) if (all.indexOf(names[j]) >= 0) return names[j];
    return '';
  }

  function readArgs(card) {
    try {
      var box = card.querySelector('[data-ui="chat.permission-request-composer.permission-preview"]');
      var t = ((box || card).textContent || '').replace(/\s+/g, ' ').trim();
      return t.length > ARGS_MAX ? t.slice(0, ARGS_MAX) + '…' : t;
    } catch (e) { return ''; }
  }

  function findAllowButton(card) {
    var btns;
    try { btns = Array.prototype.slice.call(card.querySelectorAll('button')); }
    catch (e) { return { ok: false, reason: 'query_failed' }; }
    var exact = [], loose = [];
    for (var i = 0; i < btns.length; i++) {
      var b = btns[i];
      var txt = (b.textContent || '').replace(/\s+/g, ' ').trim();
      if (/拒绝|deny|reject|取消|cancel|关闭|close/i.test(txt)) continue;
      for (var k = 0; k < ALLOW_KWS.length; k++) {
        var kw = ALLOW_KWS[k];
        if (txt.indexOf(kw) === 0) { exact.push({ btn: b, txt: txt }); break; }
        if (txt.indexOf(kw) > 0) { loose.push({ btn: b, txt: txt }); break; }
      }
    }
    var pick = exact.length ? exact[exact.length - 1] : (loose.length ? loose[loose.length - 1] : null);
    if (!pick) return { ok: false, reason: 'no_allow_button', buttons: btns.map(function (b) { return (b.textContent || '').trim(); }) };
    if (pick.btn.disabled || pick.btn.getAttribute('aria-disabled') === 'true') return { ok: false, reason: 'disabled' };
    return { ok: true, btn: pick.btn, text: pick.txt };
  }

  function handleCard(card) {
    stats.cards_seen++;
    var rawName = readToolName(card);
    var tool = normalizeTool(rawName);
    var allowed = (tool && WHITELIST[tool] === true) || (rawName && WHITELIST[rawName] === true);
    if (!allowed) {
      handled.add(card);
      stats.blocked++;
      report({ ev: 'blocked', tool: tool || '(未识别)', raw: rawName, args: readArgs(card) });
      return;
    }
    var found = findAllowButton(card);
    if (!found.ok) {
      if (!warned.has(card)) { warned.add(card); stats.errors++; report({ ev: 'no_button', tool: tool, reason: found.reason }); }
      return;
    }
    if (DRY_RUN) { handled.add(card); report({ ev: 'dry_run', tool: tool, button: found.text }); return; }
    var now = Date.now();
    if (now - lastClickAt < COOLDOWN_MS) return;
    handled.add(card);
    lastClickAt = now;
    stats.approvals++;
    stats.last_click_at = new Date().toISOString();
    try {
      found.btn.click();
      report({ ev: 'clicked', tool: tool, raw: rawName, button: found.text, args: readArgs(card) });
    } catch (e) {
      stats.errors++;
      report({ ev: 'click_failed', tool: tool, err: String(e) });
    }
  }

  function scan() {
    stats.scans++;
    var cards;
    try { cards = document.querySelectorAll(CARD_SEL); }
    catch (e) { report({ ev: 'selector_error', err: String(e) }); return; }
    for (var i = 0; i < cards.length; i++) {
      if (handled.has(cards[i])) continue;
      try { handleCard(cards[i]); }
      catch (e) { stats.errors++; report({ ev: 'handle_error', err: String(e) }); }
    }
  }

  var timer = setInterval(scan, SCAN_MS);
  var observer = null;
  try {
    observer = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) if (muts[i].addedNodes && muts[i].addedNodes.length) { scan(); return; }
    });
    observer.observe(document.body || document.documentElement, { childList: true, subtree: true });
  } catch (e) {}

  window.__CHERRY_AA__ = {
    version: '3.0',
    status: function () {
      var n = 0; try { n = document.querySelectorAll(CARD_SEL).length; } catch (e) {}
      return { dry_run: DRY_RUN, whitelist: Object.keys(WHITELIST).filter(function (k) { return WHITELIST[k]; }), cards: n, stats: stats };
    },
    scan_now: scan,
    stop: function (why) { clearInterval(timer); if (observer) observer.disconnect(); report({ ev: 'stopped', why: why }); }
  };

  report({ ev: 'injected', whitelist: Object.keys(WHITELIST).filter(function (k) { return WHITELIST[k]; }) });
})();
"""

# ============================================================== 日志
_log_lock = threading.Lock()


def log(msg, level="INFO"):
    line = "[%s] [%-5s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), level, msg)
    with _log_lock:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(os.path.join(LOG_DIR, "app-%s.log" % time.strftime("%Y%m%d")),
                      "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass


# ============================================================== 子进程环境
def clean_env():
    """给子进程干净的环境变量。
    PyInstaller 单文件包会把解包目录塞进 PATH，子进程若从那里加载 VCRUNTIME140.dll，
    会锁住该 dll 导致主程序退出时临时目录删不掉（表现为进程卡死 + 报错）。"""
    e = dict(os.environ)
    for k in list(e):
        if k.startswith("_MEI") or k.startswith("_PYI"):
            e.pop(k, None)
    p = e.get("PATH") or e.get("Path") or ""
    parts = [x for x in p.split(";") if x and "_MEI" not in x and "_PYI" not in x]
    e["PATH"] = ";".join(parts)
    e.pop("Path", None)
    return e


def spawn(args, cwd, wait=False):
    kw = dict(cwd=cwd, env=clean_env(), close_fds=True,
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              creationflags=CREATE_NO_WINDOW)
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kw["startupinfo"] = si
    except Exception:
        pass
    return subprocess.Popen(args, **kw)


# ============================================================== 单实例
_lock_sock = None


def acquire_lock():
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((CDP_HOST, LOCK_PORT))
        s.listen(1)
        _lock_sock = s
        return True
    except OSError:
        try:
            s.close()
        except Exception:
            pass
        return False


# ============================================================== 定位 Cherry
class Locator(object):
    @staticmethod
    def _from_registry():
        try:
            import winreg
        except ImportError:
            return []
        found = []
        roots = [
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, path in roots:
            try:
                key = winreg.OpenKey(hive, path)
            except OSError:
                continue
            try:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        k = winreg.OpenKey(key, sub)
                        name, _ = winreg.QueryValueEx(k, "DisplayName")
                        if "cherry" not in str(name).lower():
                            winreg.CloseKey(k)
                            continue
                        for vn in ("DisplayIcon", "InstallLocation"):
                            try:
                                val, _ = winreg.QueryValueEx(k, vn)
                            except OSError:
                                continue
                            val = str(val).strip('"').split(",")[0]
                            if val.lower().endswith(".exe") and os.path.exists(val):
                                found.append(val)
                            elif os.path.isdir(val):
                                found.append(os.path.join(val, "Cherry Studio.exe"))
                        winreg.CloseKey(k)
                    except OSError:
                        continue
            finally:
                winreg.CloseKey(key)
        return found

    @staticmethod
    def _from_paths():
        env = os.environ
        cands = []
        for base in (env.get("LOCALAPPDATA"), env.get("ProgramFiles"),
                     env.get("ProgramFiles(x86)"), env.get("APPDATA")):
            if not base:
                continue
            for sub in ("Programs\\cherry-studio", "Programs\\Cherry Studio",
                        "Cherry Studio", "cherry-studio", "CherryStudio"):
                cands.append(os.path.join(base, sub, "Cherry Studio.exe"))
        for d in ("C:\\Program Files\\Cherry Studio", "C:\\Program Files (x86)\\Cherry Studio",
                  "D:\\Program Files\\Cherry Studio", "E:\\Program Files\\Cherry Studio"):
            cands.append(os.path.join(d, "Cherry Studio.exe"))
        return cands

    @staticmethod
    def _from_shortcuts():
        try:
            import win32com.client
            from win32com.shell import shell, shellcon
        except ImportError:
            return []
        dirs = []
        for flag in (shellcon.CSIDL_PROGRAMS, shellcon.CSIDL_COMMON_PROGRAMS,
                     shellcon.CSIDL_DESKTOPDIRECTORY, shellcon.CSIDL_COMMON_DESKTOPDIRECTORY):
            try:
                d = shell.SHGetFolderPath(0, flag, None, 0)
                if d:
                    dirs.append(d)
            except Exception:
                pass
        sh = win32com.client.Dispatch("WScript.Shell")
        out = []
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for root, _dirs, files in os.walk(d):
                for f in files:
                    if not f.lower().endswith(".lnk"):
                        continue
                    try:
                        t = sh.CreateShortcut(os.path.join(root, f)).TargetPath
                        if "cherry" in (t or "").lower() and t.lower().endswith(".exe"):
                            out.append(t)
                    except Exception:
                        pass
        return out

    @staticmethod
    def _from_process():
        try:
            import psutil
        except ImportError:
            return []
        out = []
        for p in psutil.process_iter(["name", "exe"]):
            try:
                if (p.info.get("name") or "").lower() == "cherry studio.exe":
                    exe = p.info.get("exe")
                    if exe:
                        out.append(exe)
            except Exception:
                pass
        return out

    @staticmethod
    def _from_disk():
        """最后手段：常见盘符浅层扫描（限深度，避免太慢）"""
        out = []
        roots = []
        for drv in "CDEFG":
            r = drv + ":\\"
            if os.path.exists(r):
                roots.append(r)
        skip = ("Windows", "ProgramData", "$Recycle.Bin", "System Volume Information",
                "node_modules", ".git", "AppData\\Local\\Temp")
        for root in roots:
            base_depth = root.rstrip("\\").count("\\")
            for cur, dirs, files in os.walk(root):
                if cur.count("\\") - base_depth > 2:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if d not in skip and not d.startswith("$")]
                for f in files:
                    if f.lower() == "cherry studio.exe":
                        out.append(os.path.join(cur, f))
                if out:
                    return out
        return out

    @classmethod
    def find(cls):
        for label, fn in (("注册表", cls._from_registry),
                          ("常见安装路径", cls._from_paths),
                          ("快捷方式", cls._from_shortcuts),
                          ("运行中进程", cls._from_process),
                          ("磁盘扫描", cls._from_disk)):
            try:
                for p in fn():
                    if p and os.path.exists(p):
                        log("定位到 Cherry Studio [%s]：%s" % (label, p))
                        return os.path.abspath(p)
            except Exception as e:
                log("定位方式[%s]出错：%s" % (label, e), "WARN")
        return None


# ============================================================== 补丁器
class Patcher(object):
    def __init__(self, exe_path):
        self.exe = exe_path
        res = os.path.join(os.path.dirname(exe_path), "resources")
        self.asar = os.path.join(res, "app.asar")
        self.ok = os.path.exists(self.asar)

    # ---------- 备份 ----------
    def _md5(self, path, chunk=1 << 20):
        h = hashlib.md5()
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()

    def backup_path(self):
        if not os.path.exists(self.asar):
            return None
        return os.path.join(BACKUP_DIR, "app.asar.%s.orig" % self._md5(self.asar)[:16])

    def ensure_backup(self):
        """按原始文件 MD5 命名备份。
        关键：如果 Cherry 升级过，asar 内容变了，旧备份绝不能用来覆盖新版本。"""
        os.makedirs(BACKUP_DIR, exist_ok=True)
        cur = self._md5(self.asar)
        bp = os.path.join(BACKUP_DIR, "app.asar.%s.orig" % cur[:16])
        if os.path.exists(bp):
            log("备份已存在（同指纹 %s）" % cur[:16])
            return bp, cur
        if self.inspect() != "pristine":
            log("当前 asar 非原始态，且没有对应指纹的备份 → 无法安全备份", "WARN")
            return None, cur
        log("备份 app.asar（约 %.0f MB，请稍候）…" % (os.path.getsize(self.asar) / 1048576))
        shutil.copy2(self.asar, bp)
        log("备份完成：%s" % bp)
        # 顺手清理其它版本的旧备份（仅保留最近 2 份）
        try:
            others = sorted(glob.glob(os.path.join(BACKUP_DIR, "app.asar.*.orig")),
                            key=os.path.getmtime, reverse=True)
            for old in others[2:]:
                os.remove(old)
                log("清理旧备份：%s" % os.path.basename(old))
        except Exception:
            pass
        return bp, cur

    # ---------- 状态 ----------
    def _load(self):
        with open(self.asar, "rb") as f:
            a, b, c, d = struct.unpack("<4I", f.read(16))
            data_off = 8 + b
            f.seek(16)
            raw = f.read(d)
        return data_off, json.loads(raw.decode("utf-8")), raw.decode("utf-8")

    def _target(self, index):
        def walk(node, prefix=""):
            for name, meta in node.get("files", {}).items():
                p = prefix + "/" + name
                if "files" in meta:
                    for x in walk(meta, p):
                        yield x
                else:
                    yield p, meta
        for p, meta in walk(index):
            if "automationSkills" in p and p.endswith(".js"):
                return p, meta
        return None, None

    def inspect(self):
        """返回 'pristine' / 'patched' / 'mixed' / 'unknown'"""
        try:
            data_off, index, _ = self._load()
            path, meta = self._target(index)
            if not path:
                return "unknown"
            with open(self.asar, "rb") as f:
                f.seek(data_off + int(meta["offset"]))
                data = f.read(int(meta["size"]))
            old_ok = all(data.count(r["old"]) == r["expect"] for r in RULES)
            new_ok = all(data.count(r["new"]) == r["expect"] for r in RULES)
            if old_ok:
                return "pristine"
            if new_ok:
                return "patched"
            return "mixed"
        except Exception as e:
            log("检查补丁状态出错：%s" % e, "WARN")
            return "unknown"

    # ---------- 打补丁 ----------
    def apply(self):
        data_off, index, header_txt = self._load()
        path, meta = self._target(index)
        if not path:
            log("找不到目标文件，跳过补丁", "WARN")
            return False

        size = int(meta["size"])
        off = data_off + int(meta["offset"])
        with open(self.asar, "rb") as f:
            f.seek(off)
            data = bytearray(f.read(size))

        for r in RULES:
            n = data.count(r["old"])
            if n != r["expect"]:
                log("规则[%s]预期 %d 处，实际 %d 处，中止" % (r["name"], r["expect"], n), "ERROR")
                return False
            if len(r["old"]) != len(r["new"]):
                log("规则[%s]长度不等，中止" % r["name"], "ERROR")
                return False
            pos = 0
            while True:
                i = data.find(r["old"], pos)
                if i < 0:
                    break
                data[i:i + len(r["old"])] = r["new"]
                pos = i + len(r["new"])

        # 重算完整性哈希
        integ = meta.get("integrity") or {}
        old_hash = integ.get("hash")
        bs = integ.get("blockSize") or 4194304
        h = hashlib.sha256()
        for i in range(0, len(data), bs):
            h.update(data[i:i + bs])
        new_hash = h.hexdigest()

        new_header = header_txt
        if old_hash:
            ki = header_txt.find('"%s"' % os.path.basename(path))
            if ki >= 0:
                region = header_txt[ki:ki + 800]
                if old_hash in region:
                    cand = header_txt[:ki] + region.replace(old_hash, new_hash) + header_txt[ki + 800:]
                    if len(cand) == len(header_txt):
                        new_header = cand

        with open(self.asar, "r+b") as f:
            if new_header != header_txt:
                f.seek(16)
                f.write(new_header.encode("utf-8"))
            f.seek(off)
            f.write(bytes(data))

        st = self.inspect()
        log("补丁结果：%s" % st)
        return st == "patched"

    # ---------- 还原 ----------
    def restore(self, backup=None):
        bp = backup
        if not bp or not os.path.exists(bp):
            log("没有指定或找不到备份，无法还原", "ERROR")
            return False
        try:
            shutil.copy2(bp, self.asar)
            st = self.inspect()
            log("已从 %s 还原 app.asar（当前状态：%s）" % (os.path.basename(bp), st))
            return st == "pristine"
        except Exception as e:
            log("还原失败（Cherry 可能仍在运行）：%s" % e, "ERROR")
            return False


# ============================================================== 状态文件（异常自愈）
def save_state(d):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def clear_state():
    try:
        os.remove(STATE_FILE)
    except Exception:
        pass


# ============================================================== 自动授权
def fetch_targets(timeout=3):
    with urllib.request.urlopen("http://%s:%d/json/list" % (CDP_HOST, CDP_PORT), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def pick_pages(targets):
    pages = []
    for t in targets:
        if t.get("type") != "page" or not t.get("webSocketDebuggerUrl"):
            continue
        url = t.get("url") or ""
        if not url or url.startswith("about:") or url.startswith("devtools://"):
            continue
        if any(x in url for x in ("webview", "miniApp", "miniapp")):
            continue
        pages.append(t)
    pages.sort(key=lambda t: 0 if any(k in (t.get("url") or "") for k in ("window.html", "index.html")) else 1)
    return pages


class Session(object):
    def __init__(self, target, stop_event):
        self.target = target
        self.stop = stop_event
        self.ws = None
        self._id = 0
        self.title = target.get("title") or target.get("url") or "?"

    def _send(self, method, params=None):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        return self._id

    def inject(self):
        cfg = {
            "selectors": SELECTORS,
            "whitelist": WHITELIST,
            "behavior": {"dry_run": False, "scan_interval_ms": SCAN_MS,
                         "click_cooldown_ms": CLICK_COOLDOWN_MS,
                         "log_args_preview_chars": ARGS_PREVIEW_CHARS},
        }
        expr = "window.__CHERRY_AA_CFG__ = %s;\n%s" % (json.dumps(cfg, ensure_ascii=False), INJECTED_JS)
        self._id += 1
        self.inject_id = self._id
        self.ws.send(json.dumps({"id": self._id, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "returnByValue": True}}))
        log("已注入守护脚本 → %s" % self.title)

    def run(self):
        import websocket
        self.ws = websocket.create_connection(self.target["webSocketDebuggerUrl"],
                                              timeout=6, suppress_origin=True)
        self.ws.settimeout(1.5)
        self._send("Runtime.enable")
        self._send("Page.enable")
        try:
            self._send("Runtime.addBinding", {"name": "__cherryAA_report"})
        except Exception:
            pass
        time.sleep(0.3)
        self.inject()

        last_hb = 0.0
        while not self.stop.is_set():
            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                raw = None
            except Exception:
                break
            if raw:
                try:
                    msg = json.loads(raw)
                except Exception:
                    msg = {}
                m = msg.get("method")
                p = msg.get("params") or {}
                if m == "Runtime.bindingCalled" and p.get("name") == "__cherryAA_report":
                    self._on_event(p.get("payload") or "")
                elif m == "Page.frameNavigated" and not (p.get("frame") or {}).get("parentId"):
                    threading.Timer(1.5, self._reinject).start()
                elif msg.get("id") == getattr(self, "inject_id", None) and msg.get("exceptionDetails"):
                    log("注入失败：%s" % msg["exceptionDetails"], "ERROR")
            now = time.time()
            if now - last_hb >= 10:
                last_hb = now
                self._id += 1
                self.ws.send(json.dumps({"id": 990001, "method": "Runtime.evaluate",
                                         "params": {"expression":
                                                    "typeof window.__CHERRY_AA__==='object'&&window.__CHERRY_AA__?'alive':'missing'",
                                                    "returnByValue": True}}))
                try:
                    r = json.loads(self.ws.recv() or "{}")
                    if (r.get("result") or {}).get("result", {}).get("value") == "missing":
                        self._reinject()
                except Exception:
                    pass
        try:
            self.ws.close()
        except Exception:
            pass

    def _reinject(self):
        try:
            if self.ws:
                self.inject()
        except Exception:
            pass

    def _on_event(self, payload):
        try:
            e = json.loads(payload)
        except Exception:
            return
        ev = e.get("ev")
        if ev == "clicked":
            log("★ 已自动批准：%s" % (e.get("tool")), "ACT")
        elif ev == "blocked":
            log("非白名单·保持人工：%s" % (e.get("raw") or e.get("tool")), "GUARD")
        elif ev == "injected":
            log("守护脚本就绪：白名单=%s" % e.get("whitelist"))


class Approver(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self.stop = threading.Event()

    def halt(self):
        self.stop.set()

    def run(self):
        import websocket  # noqa
        warned = False
        while not self.stop.is_set():
            try:
                pages = pick_pages(fetch_targets())
            except Exception:
                if not warned:
                    log("调试端口 %d 暂不可达，等待 Cherry 起来…" % CDP_PORT, "WARN")
                    warned = True
                time.sleep(2)
                continue
            warned = False
            if not pages:
                time.sleep(2)
                continue
            log("接管 %d 个窗口：%s" % (len(pages), [p.get("title") or p.get("url") for p in pages]))
            threads = []
            for p in pages:
                s = Session(p, self.stop)

                def runner(sess=s):
                    try:
                        sess.run()
                    except Exception as e:
                        log("会话异常：%s" % e, "WARN")
                t = threading.Thread(target=runner)
                t.daemon = True
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            if not self.stop.is_set():
                time.sleep(2)


# ============================================================== 进程工具
def cherry_alive():
    try:
        import psutil
    except ImportError:
        return False
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() == "cherry studio.exe":
                return True
        except Exception:
            pass
    return False


def port_up(timeout=1.5):
    try:
        with urllib.request.urlopen("http://%s:%d/json/version" % (CDP_HOST, CDP_PORT), timeout=timeout) as r:
            return json.loads(r.read().decode()).get("Browser", "up")
    except Exception:
        return ""


def activate_window():
    try:
        import win32gui
    except ImportError:
        return

    def cb(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd):
                t = win32gui.GetWindowText(hwnd) or ""
                c = win32gui.GetClassName(hwnd) or ""
                if "Cherry Studio" in t and "Chrome_WidgetWin" in c:
                    win32gui.ShowWindow(hwnd, 9)
                    try:
                        win32gui.SetForegroundWindow(hwnd)
                    except Exception:
                        pass
        except Exception:
            pass
        return True
    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass


def notify(title, text):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x40)
    except Exception:
        pass


# ============================================================== 清理历史垃圾
def cleanup_stale_mei():
    """清理 PyInstaller 单文件包遗留的临时解包目录（含别的程序留下的）。
    自己正在用的那个目录会跳过；被占用的删不掉也无妨。"""
    td = tempfile.gettempdir()
    cur = os.path.abspath(getattr(sys, "_MEIPASS", "") or "")
    removed = 0
    freed = 0
    for p in glob.glob(os.path.join(td, "_MEI*")):
        if not os.path.isdir(p):
            continue
        if cur and os.path.abspath(p) == cur:
            continue
        try:
            sz = 0
            for r, _d, fs in os.walk(p):
                for f in fs:
                    try:
                        sz += os.path.getsize(os.path.join(r, f))
                    except Exception:
                        pass
            shutil.rmtree(p)
            removed += 1
            freed += sz
        except Exception:
            pass
    if removed:
        log("清理历史临时目录 %d 个，释放 %.0f MB" % (removed, freed / 1048576))


# ============================================================== 权限
def ensure_privilege(asar):
    """确保能写 asar。不行就提权重启自己（用户拒绝则降级）。
    返回 True=可写 / 'escalated'=已提权交给新进程 / False=不可写"""
    if not asar or not os.path.exists(asar):
        return True
    probe = asar + ".wtest"
    try:
        with open(probe, "wb") as f:
            f.write(b"x")
        os.remove(probe)
        return True
    except Exception:
        pass
    try:
        import ctypes
        if ctypes.windll.shell32.IsUserAnAdmin():
            log("已是管理员但 asar 仍不可写（可能被安全软件锁），降级运行", "WARN")
            return False
        log("修改 app.asar 需要管理员权限，正在申请…")
        params = subprocess.list2cmdline(sys.argv)
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        if int(r) > 32:
            return "escalated"
        log("用户取消了提权（返回值 %s）" % r, "WARN")
    except Exception as e:
        log("提权失败：%s" % e, "WARN")
    return False


# ============================================================== 异常自愈
def recover_previous():
    """上次被强杀/断电，可能留下未还原的 asar。启动时先收拾干净。"""
    st = load_state()
    if not st:
        return
    asar = st.get("asar")
    if not asar or not os.path.exists(asar):
        clear_state()
        return
    if cherry_alive():
        log("上次未正常退出，但 Cherry 仍在运行 → 本次不动文件")
        clear_state()
        return
    bp = st.get("backup") or os.path.join(BACKUP_DIR, "app.asar.pristine.orig")
    if not os.path.exists(bp):
        cands = glob.glob(os.path.join(BACKUP_DIR, "app.asar.*.orig"))
        bp = max(cands, key=os.path.getmtime) if cands else None
    if st.get("phase") in ("patching", "running"):
        if bp and os.path.exists(bp):
            try:
                shutil.copy2(bp, asar)
                log("自愈：已还原上次残留的 app.asar 修改（%s）" % os.path.basename(bp))
            except Exception as e:
                log("自愈还原失败：%s" % e, "ERROR")
        else:
            log("自愈：没有备份，无法还原（asar 可能仍带补丁）", "WARN")
    clear_state()


# ============================================================== 主流程
def main():
    if not acquire_lock():
        log("已有实例在运行，本进程退出", "WARN")
        return 0

    log("=" * 66)
    log("%s 便携版启动  PID=%d" % (APP_NAME, os.getpid()))

    cleanup_stale_mei()
    recover_previous()

    exe = Locator.find()
    if not exe:
        log("未在本机找到 Cherry Studio", "ERROR")
        notify(APP_NAME, "没有在本机找到 Cherry Studio。\n\n"
                         "请先安装 Cherry Studio 后再运行本程序。\n"
                         "（已尝试：注册表、常见安装路径、开始菜单快捷方式、运行中进程、磁盘扫描）")
        return 1

    patcher = Patcher(exe)
    log("asar: %s" % patcher.asar)

    running = cherry_alive()
    has_port = bool(port_up())

    if running and not has_port:
        log("Cherry 正在运行但没有调试端口，无法接管", "WARN")
        activate_window()
        notify(APP_NAME, "检测到 Cherry Studio 正在运行，但没有开启调试端口。\n\n"
                         "请先完全退出 Cherry Studio（含右下角托盘图标），\n"
                         "再双击本程序重新打开即可。")
        return 2

    if running and has_port:
        log("Cherry 已在运行且调试端口可用 → 直接接管（不改文件）")
        mode = "attach"
    else:
        mode = "launch"
        if patcher.ok:
            pv = ensure_privilege(patcher.asar)
            if pv == "escalated":
                log("已申请管理员权限，新进程接手（本进程退出）")
                return 0
            if pv is False:
                log("没有写入权限 → 降级为「仅自动授权」，不改程序文件", "WARN")
                patcher.ok = False
        if patcher.ok:
            st = patcher.inspect()
            log("当前 asar 状态：%s" % st)
            if st == "pristine":
                bp, cur = patcher.ensure_backup()
                if not bp:
                    log("无法建立备份 → 降级为「仅自动授权」", "WARN")
                    patcher.ok = False
                else:
                    save_state({"asar": patcher.asar, "backup": bp, "pristine_md5": cur,
                                "phase": "patching", "ts": time.time()})
                    if patcher.apply():
                        save_state({"asar": patcher.asar, "backup": bp, "pristine_md5": cur,
                                    "phase": "running", "ts": time.time()})
                        log("补丁已生效（退出时自动还原）")
                    else:
                        log("补丁失败 → 降级为「仅自动授权」模式", "WARN")
                        clear_state()
            elif st == "patched":
                cands = sorted(glob.glob(os.path.join(BACKUP_DIR, "app.asar.*.orig")),
                               key=os.path.getmtime, reverse=True)
                bp = cands[0] if cands else None
                if bp:
                    log("asar 已是补丁态，沿用已有备份：%s" % os.path.basename(bp))
                    save_state({"asar": patcher.asar, "backup": bp,
                                "phase": "running", "ts": time.time()})
                else:
                    log("asar 是补丁态但找不到任何备份 → 退出时不会动文件（需手动处理）", "WARN")
                    save_state({"asar": patcher.asar, "phase": "running_nobackup", "ts": time.time()})
            else:
                log("asar 状态异常（%s），跳过补丁，仅做自动授权" % st, "WARN")
        else:
            log("找不到 app.asar，仅做自动授权", "WARN")

        log("启动 Cherry Studio（调试端口 %d）…" % CDP_PORT)
        try:
            spawn([exe, "--remote-debugging-port=%d" % CDP_PORT,
                   "--remote-debugging-address=%s" % CDP_HOST],
                  cwd=os.path.dirname(exe))
        except Exception as e:
            log("启动失败：%s" % e, "ERROR")
            notify(APP_NAME, "启动 Cherry Studio 失败：\n%s" % e)
            if mode == "launch" and patcher.ok:
                patcher.restore()
            clear_state()
            return 3

        for i in range(40):
            time.sleep(1.5)
            if port_up():
                log("调试端口就绪（第 %d 次探测）" % (i + 1))
                break
        else:
            log("60 秒内端口未就绪，可能被系统拦截", "WARN")

    approver = Approver()
    approver.start()

    log("进入监控：等你关闭 Cherry Studio…")
    idle_since = None
    while True:
        time.sleep(2)
        if cherry_alive():
            idle_since = None
            continue
        if idle_since is None:
            idle_since = time.time()
        elif time.time() - idle_since >= 4:   # 连续 4 秒无进程 = 真的关了
            log("Cherry Studio 已关闭")
            break

    approver.halt()
    time.sleep(1.0)

    restored = True
    if mode == "launch" and patcher.ok:
        bp = (load_state() or {}).get("backup")
        log("还原 app.asar…")
        restored = False
        for attempt in range(12):          # Cherry 刚退出时文件可能还被锁，重试
            if patcher.restore(bp):
                restored = True
                break
            time.sleep(1.5)
        if not restored:
            log("还原失败 → 保留状态文件，下次启动自愈", "WARN")
    if restored:
        clear_state()
    log("已退出，清理完毕")
    time.sleep(2.0)   # 留点时间让系统释放句柄，便于临时解包目录被清理
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except KeyboardInterrupt:
        log("收到中断信号")
    except Exception:
        log("未捕获异常：\n" + traceback.format_exc(), "ERROR")
    finally:
        # 兜底：任何退出路径都尝试还原
        try:
            st = load_state()
            if st and st.get("phase") in ("patching", "running"):
                asar = st.get("asar")
                bp = st.get("backup")
                if not bp or not os.path.exists(bp):
                    cands = glob.glob(os.path.join(BACKUP_DIR, "app.asar.*.orig"))
                    bp = max(cands, key=os.path.getmtime) if cands else None
                if asar and os.path.exists(asar):
                    if not cherry_alive() and bp and os.path.exists(bp):
                        shutil.copy2(bp, asar)
                        log("退出兜底：已还原 app.asar")
                        clear_state()
                    else:
                        log("退出兜底：Cherry 仍在运行或无备份，保留状态文件供下次自愈", "WARN")
        except Exception:
            pass
    sys.exit(code)
