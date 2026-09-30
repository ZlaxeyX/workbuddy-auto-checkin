#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日积分自动领取（Buddy 加油站签到 100 分/天，连签 7 天额外 1000 分）

两种工作模式
------------
token  : 直连官方签到接口（最可靠、与 UI 无关）
         POST {base_url}/billing/meter/checkin-status   查询今日是否已签
         POST {base_url}/billing/meter/daily-checkin    执行签到
         Header: Authorization: Bearer <access_token> / X-User-Id: <user_id>
client : 复用桌面端已登录态，确保 WorkBuddy 客户端在线，再通过客户端日志核验签到结果
         （不接触任何凭据；若客户端未自动领取，则弹通知提示手动点一次）
auto   : 有 token 走 token，否则走 client

仅使用 Python 标准库，无需 pip 安装任何依赖。

常用命令
--------
  python checkin.py                    # 按 config.json 的 mode 执行一次
  python checkin.py --show-status      # 只看今日签到状态，不做任何写操作
  python checkin.py --dry-run          # 全流程演练，不真正调用领取
  python checkin.py --mode client      # 强制走客户端模式
  python checkin.py --force            # 忽略"今日已领取"的本地幂等记录
  python checkin.py --discover         # 自动探测客户端路径 / 日志目录
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import glob
import json
import logging
import os
import random
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:  # 仅 Windows 提供
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")
IS_WINDOWS = os.name == "nt"

EXIT_OK = 0            # 今日积分已到账（含"今日已签到"幂等命中）
EXIT_NEED_HAND = 2     # 客户端模式：已保上线，但需要人工点一次
EXIT_AUTH_ERROR = 3    # 登录态失效，需要重新登录
EXIT_NETWORK = 4       # 网络异常 / 重试耗尽
EXIT_ENV_ERROR = 5     # 客户端不可用、配置错误等环境问题

# 接口业务错误里代表"今天已经签过了"的关键字（幂等，不视为失败）
ALREADY_HINTS = ("already", "duplicate", "重复", "已签", "已领取", "已签到", "今日已")

LOG = logging.getLogger("wb-checkin")

DEFAULT_CONFIG_DATA: Dict[str, Any] = {
    "mode": "auto",
    "auth": {
        "access_token": "",
        "user_id": "",
        "token_file": "token.txt",
        "env_token_var": "WB_ACCESS_TOKEN",
        "env_user_var": "WB_USER_ID",
    },
    "api": {
        "base_url": "https://copilot.tencent.com",
        "status_path": "/billing/meter/checkin-status",
        "claim_path": "/billing/meter/daily-checkin",
        "timeout_seconds": 20,
        "verify_tls": True,
    },
    "client": {
        "exe_path": "",
        "process_name": "WorkBuddy",
        "auto_launch": True,
        "focus_window": True,
        "ready_timeout_seconds": 90,
        "log_dir": "",
        "log_files": ["main.log", "main.old.log"],
        "log_tail_bytes": 8000000,
    },
    "retry": {
        "max_attempts": 3,
        "initial_backoff_seconds": 5,
        "backoff_multiplier": 2.0,
        "max_backoff_seconds": 120,
    },
    "logging": {"log_dir": "logs", "level": "INFO", "keep_days": 60},
    "state": {"state_dir": "state"},
    "notify": {"enabled": True, "on_success": False, "on_failure": True},
}


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------

def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """把 override 递归合并进 base 的副本。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if key.startswith("_"):  # _comment / _generated 之类的元字段
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def expand(path: str) -> str:
    """展开 %VAR% / ~ 并转绝对路径。空串保持空串。"""
    if not path:
        return ""
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return os.path.normpath(path)


def ensure_dir(path: str) -> str:
    if path and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    return path


def now_local() -> datetime:
    return datetime.now().astimezone()


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def sleep_backoff(seconds: float) -> None:
    """带 ±20% 抖动，避免固定节奏重试。"""
    jitter = seconds * random.uniform(-0.2, 0.2)
    time.sleep(max(0.0, seconds + jitter))


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_config(path: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    elif path == DEFAULT_CONFIG:
        # 首次运行：从模板生成 config.json，保证"克隆即可用"
        example = os.path.join(BASE_DIR, "config.example.json")
        if os.path.exists(example):
            try:
                with open(example, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                data["_generated"] = True
                LOG.info("已从 config.example.json 生成 config.json")
            except (OSError, ValueError) as exc:
                LOG.warning("生成 config.json 失败（改用内置默认值）: %s", exc)
                data = {}
        else:
            LOG.warning("未找到 config.json，使用内置默认配置")
    else:
        raise SystemExit("配置文件不存在: %s" % path)
    cfg = deep_merge(DEFAULT_CONFIG_DATA, data)
    if data.get("_generated"):
        LOG.warning("提示：config.json 是本地配置，已加入 .gitignore，请勿提交（可能含 access_token）")
    return cfg


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

class JsonlWriter:
    """结构化日志：每次尝试一行 JSON，方便事后统计。"""

    def __init__(self, path: str) -> None:
        self.path = path

    def write(self, record: Dict[str, Any]) -> None:
        record.setdefault("ts", now_local().isoformat(timespec="seconds"))
        try:
            ensure_dir(os.path.dirname(self.path))
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:  # 日志失败不能影响主流程
            LOG.debug("写入 jsonl 失败: %s", exc)


def setup_logging(cfg: Dict[str, Any]) -> Tuple[JsonlWriter, str]:
    log_dir = ensure_dir(expand(cfg["logging"]["log_dir"]))
    level = getattr(logging, str(cfg["logging"].get("level", "INFO")).upper(), logging.INFO)

    root = logging.getLogger("wb-checkin")
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    daily = os.path.join(log_dir, "checkin-%s.log" % today_str())
    fh = logging.FileHandler(daily, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # 有控制台时同时输出（pythonw / 计划任务下没有控制台）
    if sys.stdout is not None and sys.stderr is not None:
        try:
            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(fmt)
            root.addHandler(sh)
        except Exception:
            pass

    root.propagate = False
    return JsonlWriter(os.path.join(log_dir, "checkin.jsonl")), log_dir


def purge_old_logs(log_dir: str, keep_days: int) -> None:
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    try:
        for name in os.listdir(log_dir):
            if not name.startswith("checkin-"):
                continue
            fp = os.path.join(log_dir, name)
            if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                os.remove(fp)
                LOG.info("清理过期日志: %s", name)
    except OSError as exc:
        LOG.debug("清理日志失败: %s", exc)


# --------------------------------------------------------------------------
# 幂等状态
# --------------------------------------------------------------------------

class StateStore:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        self.dir = ensure_dir(expand(cfg["state"]["state_dir"]))
        self.path = os.path.join(self.dir, "last-run.json")
        self.data: Dict[str, Any] = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    self.data = json.load(fh)
            except (OSError, ValueError):
                self.data = {}

    def claimed_today(self) -> bool:
        return bool(self.data.get("claimed")) and self.data.get("date") == today_str()

    def save(self, **fields: Any) -> None:
        self.data.update(fields)
        self.data["date"] = today_str()
        self.data["updated_at"] = now_local().isoformat(timespec="seconds")
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            LOG.warning("保存状态失败: %s", exc)


# --------------------------------------------------------------------------
# 登录态解析
# --------------------------------------------------------------------------

class Credentials:
    def __init__(self, token: str, user_id: str, source: str) -> None:
        self.token = token
        self.user_id = user_id
        self.source = source

    @property
    def valid(self) -> bool:
        return bool(self.token)


def resolve_credentials(cfg: Dict[str, Any], cli_token: str = "", cli_uid: str = "") -> Credentials:
    """按 命令行 > 环境变量 > token 文件 > config.json 的顺序取凭据。"""
    auth = cfg["auth"]
    token_file = expand(auth.get("token_file") or "")

    if cli_token:
        return Credentials(cli_token.strip(), (cli_uid or "").strip(), "命令行参数")

    env_token = os.environ.get(auth.get("env_token_var") or "WB_ACCESS_TOKEN", "").strip()
    if env_token:
        env_uid = os.environ.get(auth.get("env_user_var") or "WB_USER_ID", "").strip()
        return Credentials(env_token, env_uid, "环境变量")

    if token_file and os.path.exists(token_file):
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
            if lines:
                token = lines[0]
                uid = lines[1] if len(lines) > 1 else ""
                for ln in lines:
                    if ln.lower().startswith("user_id"):
                        uid = ln.split("=", 1)[-1].strip()
                return Credentials(token, uid, "token 文件 %s" % os.path.basename(token_file))
        except OSError as exc:
            LOG.warning("读取 token 文件失败: %s", exc)

    cfg_token = (auth.get("access_token") or "").strip()
    if cfg_token:
        return Credentials(cfg_token, (auth.get("user_id") or "").strip(), "config.json")

    return Credentials("", (auth.get("user_id") or "").strip(), "无")


# --------------------------------------------------------------------------
# 官方接口客户端
# --------------------------------------------------------------------------

class ApiResult:
    """统一的结果对象。kind 取值：ok / already / skipped / auth_error / retryable / business_error"""

    def __init__(self, kind: str, http_status: Optional[int] = None, code: Optional[int] = None,
                 msg: str = "", data: Optional[Dict[str, Any]] = None) -> None:
        self.kind = kind
        self.http_status = http_status
        self.code = code
        self.msg = msg
        self.data = data or {}

    def __repr__(self) -> str:
        return "ApiResult(kind=%s, http=%s, code=%s, msg=%s, data=%s)" % (
            self.kind, self.http_status, self.code, self.msg, self.data)

    @property
    def is_success(self) -> bool:
        return self.kind == "ok"

    @property
    def is_already(self) -> bool:
        return self.kind == "already"


def _looks_already(msg: str) -> bool:
    low = (msg or "").lower()
    return any(hint.lower() in low for hint in ALREADY_HINTS)


class ApiClient:
    def __init__(self, cfg: Dict[str, Any], cred: Credentials) -> None:
        self.base = cfg["api"]["base_url"].rstrip("/")
        self.status_path = cfg["api"]["status_path"]
        self.claim_path = cfg["api"]["claim_path"]
        self.timeout = int(cfg["api"].get("timeout_seconds", 20))
        self.cred = cred
        self.ctx = None
        if not cfg["api"].get("verify_tls", True):
            self.ctx = ssl.create_default_context()
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def _post(self, path: str) -> ApiResult:
        url = self.base + path
        body = json.dumps({}).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("Authorization", "Bearer %s" % self.cred.token)
        if self.cred.user_id:
            req.add_header("X-User-Id", self.cred.user_id)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                status = resp.status
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            return ApiResult("retryable", msg="网络异常: %s" % exc)

        payload: Dict[str, Any] = {}
        if raw.strip():
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {}

        code = payload.get("code")
        msg = str(payload.get("msg") or payload.get("message") or "")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

        if status in (401, 403):
            return ApiResult("auth_error", status, code, msg or "登录态失效", data)
        if status == 429 or status >= 500:
            return ApiResult("retryable", status, code, msg or "服务端异常 HTTP %s" % status, data)
        if status >= 400:
            return ApiResult("business_error", status, code, msg or "HTTP %s" % status, data)

        if code == 0:
            return ApiResult("ok", status, code, msg, data)
        if _looks_already(msg):
            return ApiResult("already", status, code, msg, data)
        return ApiResult("business_error", status, code, msg or raw[:200], data)

    def status(self) -> ApiResult:
        return self._post(self.status_path)

    def claim(self) -> ApiResult:
        return self._post(self.claim_path)


# --------------------------------------------------------------------------
# 客户端驱动：进程检测 / 启动 / 窗口置前
# --------------------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DRIVE_FIXED = 3


def _exe_from_running_process(process_name: str) -> str:
    """客户端已在运行时，直接从进程拿真实路径——最准的一招。"""
    if not IS_WINDOWS:
        return ""
    candidates: List[int] = []
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s.exe" % process_name, "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        for line in out.splitlines():
            parts = [p.strip().strip('"') for p in line.strip().split('","')]
            if len(parts) >= 2 and parts[0].lower() == ("%s.exe" % process_name).lower():
                try:
                    candidates.append(int(parts[1]))
                except ValueError:
                    pass
    except (OSError, subprocess.SubprocessError):
        return ""

    kernel32 = ctypes.windll.kernel32
    for pid in candidates[:6]:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                path = buf.value
                if path and os.path.isfile(path):
                    return path
        finally:
            kernel32.CloseHandle(handle)
    return ""


def _exe_from_registry() -> str:
    """查注册表 App Paths 与卸载项，覆盖标准安装。"""
    if winreg is None:
        return ""
    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WorkBuddy.exe"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WorkBuddy.exe"),
    ]
    for hive, sub in keys:
        try:
            with winreg.OpenKey(hive, sub) as key:
                value, _ = winreg.QueryValueEx(key, None)
                if value and os.path.isfile(value):
                    return value
        except OSError:
            pass

    uninstall_roots = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for root in uninstall_roots:
            try:
                with winreg.OpenKey(hive, root) as base:
                    for i in range(winreg.QueryInfoKey(base)[0]):
                        try:
                            name = winreg.EnumKey(base, i)
                            with winreg.OpenKey(base, name) as item:
                                display = str(winreg.QueryValueEx(item, "DisplayName")[0])
                                if "workbuddy" not in display.lower():
                                    continue
                                loc = str(winreg.QueryValueEx(item, "InstallLocation")[0])
                                exe = os.path.join(loc, "WorkBuddy.exe")
                                if os.path.isfile(exe):
                                    return exe
                        except (OSError, ValueError):
                            continue
            except OSError:
                continue
    return ""


def _drive_roots() -> List[str]:
    if not IS_WINDOWS:
        return []
    try:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return []
    roots = []
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        root = "%s:\\" % chr(ord("A") + i)
        try:
            if ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == DRIVE_FIXED:
                roots.append(root)
        except Exception:
            continue
    return roots


def _exe_from_common_dirs() -> str:
    """扫描固定盘的常见层级，不依赖任何本机专属路径。"""
    env_dirs = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "WorkBuddy"),
        os.path.join(os.environ.get("ProgramFiles", ""), "WorkBuddy"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "WorkBuddy"),
    ]
    for d in env_dirs:
        exe = os.path.join(d, "WorkBuddy.exe")
        if os.path.isfile(exe):
            return exe

    patterns = ["*\\WorkBuddy\\WorkBuddy.exe", "*\\*\\WorkBuddy\\WorkBuddy.exe",
                "*\\*\\*\\WorkBuddy\\WorkBuddy.exe"]
    for root in _drive_roots():
        for pattern in patterns:
            try:
                hits = glob.glob(os.path.join(root, pattern))
            except OSError:
                continue
            hits = [h for h in hits if os.path.isfile(h)]
            if hits:
                return hits[0]
    return ""


def discover_client_exe(configured: str, process_name: str) -> Tuple[str, str]:
    """返回 (路径, 来源说明)。顺序：显式配置 > 运行中进程 > 注册表 > 常见目录。"""
    if configured:
        return expand(configured), "config.json 指定"

    from_process = _exe_from_running_process(process_name)
    if from_process:
        return from_process, "运行中的进程"

    from_registry = _exe_from_registry()
    if from_registry:
        return from_registry, "注册表安装信息"

    from_dirs = _exe_from_common_dirs()
    if from_dirs:
        return from_dirs, "常见安装目录扫描"

    return "", "未找到"


class ClientDriver:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        cc = cfg["client"]
        self.process_name = cc.get("process_name") or "WorkBuddy"
        self.exe_path, self.exe_source = discover_client_exe(
            cc.get("exe_path") or "", self.process_name)
        self.auto_launch = bool(cc.get("auto_launch", True))
        self.focus_window = bool(cc.get("focus_window", True))
        self.ready_timeout = int(cc.get("ready_timeout_seconds", 90))

    # ---- 进程 ----
    def pids(self) -> List[int]:
        if not IS_WINDOWS:
            return []
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq %s.exe" % self.process_name, "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("tasklist 执行失败: %s", exc)
            return []
        result: List[int] = []
        for line in out.splitlines():
            line = line.strip().strip('"')
            if not line or line.lower().startswith("info:"):
                continue
            parts = [p.strip().strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == ("%s.exe" % self.process_name).lower():
                try:
                    result.append(int(parts[1]))
                except ValueError:
                    pass
        return result

    def is_running(self) -> bool:
        return bool(self.pids())

    def launch(self) -> bool:
        if not self.exe_path or not os.path.isfile(self.exe_path):
            LOG.error("找不到客户端可执行文件，无法自动上线")
            return False
        LOG.info("启动 WorkBuddy 客户端: %s", self.exe_path)
        try:
            flags = 0
            if IS_WINDOWS:
                flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen([self.exe_path], cwd=os.path.dirname(self.exe_path),
                             creationflags=flags,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True)
            return True
        except OSError as exc:
            LOG.error("启动客户端失败: %s", exc)
            return False

    def ensure_online(self) -> bool:
        """确保客户端在运行；返回是否最终在线。"""
        if self.is_running():
            LOG.info("客户端已在运行（PID: %s）", ",".join(str(p) for p in self.pids()[:3]))
            return True
        LOG.info("客户端未运行")
        if not self.auto_launch:
            return False
        if not self.launch():
            return False
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            if self.is_running():
                LOG.info("客户端已上线")
                return True
            time.sleep(2)
        LOG.error("等待客户端上线超时（%ss）", self.ready_timeout)
        return False

    # ---- 窗口 ----
    def focus(self) -> bool:
        """把客户端主窗口置前，触发 Checkin 模块刷新。"""
        if not IS_WINDOWS or not self.focus_window:
            return False
        pids = set(self.pids())
        if not pids:
            return False
        user32 = ctypes.windll.user32
        found: List[int] = []

        WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd, _lparam):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    found.append(hwnd)
            return True

        user32.EnumWindows(WNDENUMPROC(callback), 0)
        if not found:
            LOG.info("未找到客户端可见窗口（可能已最小化到托盘）")
            return False

        hwnd = found[0]
        user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        # SetForegroundWindow 有前台锁定限制，先按一下 ALT 解除
        VK_MENU, KEYEVENTF_KEYUP = 0x12, 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        ok = bool(user32.SetForegroundWindow(hwnd))
        LOG.info("窗口置前: %s", "成功" if ok else "未生效（不影响后台签到）")
        return ok


# --------------------------------------------------------------------------
# 日志核验：从客户端日志判定今日是否已签到
# --------------------------------------------------------------------------

class LogVerifier:
    """
    客户端把签到链路写进 ~/.workbuddy/logs/main.log，格式为 JSON Lines：
      {"timestamp":"2026-09-30T02:56:55.664Z","scope":"queue-diag",
       "message":["[Checkin] claimDailyCheckin success",{"credit":100,"streak_days":1}]}
    时间戳是 UTC，需要换算成本地日期再比对。
    """

    def __init__(self, cfg: Dict[str, Any]) -> None:
        cc = cfg["client"]
        self.log_dir = expand(cc.get("log_dir") or os.path.join(
            os.environ.get("USERPROFILE", ""), ".workbuddy", "logs"))
        self.log_files = cc.get("log_files") or ["main.log"]
        self.tail_bytes = int(cc.get("log_tail_bytes", 8000000))

    def _iter_lines(self):
        for name in self.log_files:
            path = os.path.join(self.log_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                size = os.path.getsize(path)
                with open(path, "rb") as fh:
                    if size > self.tail_bytes:
                        fh.seek(size - self.tail_bytes)
                        fh.readline()          # 丢掉半行
                    for raw in fh:
                        yield path, raw.decode("utf-8", "replace")
            except OSError as exc:
                LOG.debug("读取日志失败 %s: %s", path, exc)

    def scan(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        """返回 {claimed, today_checked_in, credit, streak_days, evidence[], last_refresh_at}"""
        target = date_str or today_str()
        result: Dict[str, Any] = {
            "claimed": False,
            "today_checked_in": None,
            "credit": None,
            "streak_days": None,
            "evidence": [],
            "last_refresh_at": None,
            "log_dir": self.log_dir,
            "log_found": False,
        }

        for path, line in self._iter_lines():
            if "[Checkin]" not in line:
                continue
            result["log_found"] = True
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            ts_raw = rec.get("timestamp")
            if not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone()
            except ValueError:
                continue
            if ts.strftime("%Y-%m-%d") != target:
                continue

            msg_field = rec.get("message") or []
            if not msg_field:
                continue
            text = str(msg_field[0])
            payload = msg_field[1] if len(msg_field) > 1 and isinstance(msg_field[1], dict) else {}
            stamp = ts.strftime("%H:%M:%S")

            if "claimDailyCheckin success" in text:
                result["claimed"] = True
                result["today_checked_in"] = True
                result["credit"] = payload.get("credit", result["credit"])
                result["streak_days"] = payload.get("streak_days", result["streak_days"])
                result["evidence"].append("%s 领取成功 +%s 分（连签 %s 天）"
                                          % (stamp, payload.get("credit"), payload.get("streak_days")))
            elif "fetchCheckinStatus success" in text:
                result["last_refresh_at"] = stamp
                if payload.get("today_checked_in") is True:
                    result["today_checked_in"] = True
                    result["streak_days"] = payload.get("streak_days", result["streak_days"])
                    result["evidence"].append("%s 服务端状态: 今日已签到（连签 %s 天）"
                                              % (stamp, payload.get("streak_days")))
            elif "handleClaim" in text and "error" in text.lower():
                result["evidence"].append("%s %s %s" % (stamp, text, payload))

        # 有"服务端状态为已签到"也算完成
        if result["today_checked_in"] is True:
            result["claimed"] = True
        return result


# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------

_TOAST_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime]
[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType=WindowsRuntime]
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode($env:WB_TOAST_TITLE)) | Out-Null
$n.Item(1).AppendChild($t.CreateTextNode($env:WB_TOAST_BODY)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('WorkBuddy 签到助手').Show($toast)
"""


def notify(title: str, body: str, enabled: bool, log_dir: str) -> None:
    """写一条提醒文件（始终），并尽力弹一条 Windows 通知。"""
    try:
        notice_dir = ensure_dir(os.path.join(log_dir, "notices"))
        stamp = now_local().strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(notice_dir, "%s.txt" % today_str()), "a", encoding="utf-8") as fh:
            fh.write("[%s] %s\n%s\n\n" % (stamp, title, body))
    except OSError as exc:
        LOG.debug("写提醒文件失败: %s", exc)

    if not enabled or not IS_WINDOWS:
        return

    ps1 = os.path.join(log_dir, "_toast.ps1")
    try:
        with open(ps1, "w", encoding="utf-8-sig") as fh:
            fh.write(_TOAST_PS)
        env = dict(os.environ, WB_TOAST_TITLE=title[:120], WB_TOAST_BODY=body[:300])
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
            env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        LOG.info("已尝试弹出系统通知")
    except OSError as exc:
        LOG.debug("系统通知失败: %s", exc)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

class Runner:
    def __init__(self, cfg: Dict[str, Any], args: argparse.Namespace) -> None:
        self.cfg = cfg
        self.args = args
        self.jsonl, self.log_dir = setup_logging(cfg)
        self.state = StateStore(cfg)
        if getattr(args, "no_notify", False):
            cfg["notify"]["enabled"] = False
        self.cred = resolve_credentials(cfg, args.token, args.user_id)
        self.client = ClientDriver(cfg)
        self.verifier = LogVerifier(cfg)
        self.retry_cfg = cfg["retry"]

    # ---- 通用重试包装 ----
    def call_with_retry(self, label: str, fn) -> ApiResult:
        attempts = max(1, int(self.retry_cfg.get("max_attempts", 3)))
        delay = float(self.retry_cfg.get("initial_backoff_seconds", 5))
        mult = float(self.retry_cfg.get("backoff_multiplier", 2.0))
        cap = float(self.retry_cfg.get("max_backoff_seconds", 120))

        last = ApiResult("business_error", msg="未执行")
        for attempt in range(1, attempts + 1):
            last = fn()
            LOG.info("%s 第 %d/%d 次 -> kind=%s http=%s code=%s msg=%s",
                     label, attempt, attempts, last.kind, last.http_status, last.code, last.msg)
            self.jsonl.write({"event": "attempt", "stage": label, "attempt": attempt,
                              "kind": last.kind, "http": last.http_status,
                              "code": last.code, "msg": last.msg, "data": last.data})
            if last.kind in ("ok", "already", "auth_error", "business_error"):
                return last
            if attempt < attempts:
                wait = min(delay, cap)
                LOG.warning("%s 可重试失败，%.1fs 后重试", label, wait)
                sleep_backoff(wait)
                delay = min(delay * mult, cap)
        return last

    # ---- token 模式 ----
    def run_token_mode(self) -> int:
        LOG.info("== token 模式：直连官方签到接口 ==")
        api = ApiClient(self.cfg, self.cred)

        status = self.call_with_retry("checkin-status", api.status)
        if status.kind == "auth_error":
            LOG.error("登录态失效（HTTP %s），需要重新登录或更新 access_token", status.http_status)
            self.jsonl.write({"event": "result", "mode": "token", "result": "auth_error"})
            return EXIT_AUTH_ERROR
        if status.kind == "retryable":
            LOG.error("查询签到状态失败（网络/服务端），放弃本次")
            return EXIT_NETWORK
        if status.kind == "ok" and status.data.get("today_checked_in"):
            LOG.info("今日已签到（连签 %s 天），无需重复领取", status.data.get("streak_days"))
            self.state.save(claimed=True, mode="token", credit=0,
                            streak_days=status.data.get("streak_days"), note="status:already")
            self.jsonl.write({"event": "result", "mode": "token", "result": "already"})
            return EXIT_OK

        if self.args.dry_run:
            LOG.info("[dry-run] 跳过实际领取调用")
            return EXIT_OK

        claim = self.call_with_retry("daily-checkin", api.claim)
        if claim.kind == "auth_error":
            LOG.error("领取时登录态失效，需要重新登录")
            return EXIT_AUTH_ERROR
        if claim.kind in ("ok", "already"):
            credit = claim.data.get("credit")
            streak = claim.data.get("streak_days")
            LOG.info("领取成功：+%s 积分，连签 %s 天", credit, streak)
            self.state.save(claimed=True, mode="token", credit=credit, streak_days=streak,
                            note="claim:" + claim.kind)
            self.jsonl.write({"event": "result", "mode": "token", "result": "success",
                              "credit": credit, "streak_days": streak})
            if self.cfg["notify"].get("on_success"):
                notify("WorkBuddy 签到成功", "已领取 %s 积分，连签 %s 天" % (credit, streak),
                       bool(self.cfg["notify"].get("enabled")), self.log_dir)
            return EXIT_OK
        if claim.kind == "retryable":
            LOG.error("领取失败（网络/服务端），已重试 %s 次", self.retry_cfg.get("max_attempts"))
            self.jsonl.write({"event": "result", "mode": "token", "result": "network_failed"})
            return EXIT_NETWORK

        LOG.error("领取被服务端拒绝：code=%s msg=%s", claim.code, claim.msg)
        self.jsonl.write({"event": "result", "mode": "token", "result": "business_error",
                          "code": claim.code, "msg": claim.msg})
        return EXIT_NETWORK

    # ---- client 模式 ----
    def run_client_mode(self) -> int:
        LOG.info("== client 模式：确保客户端在线 + 日志核验 ==")
        LOG.info("客户端可执行文件: %s", self.client.exe_path or "(未探测到)")
        LOG.info("签到日志目录: %s", self.verifier.log_dir)

        before = self.verifier.scan()
        if before["claimed"]:
            LOG.info("客户端日志显示今日已签到，无需处理")
            for item in before["evidence"]:
                LOG.info("  证据: %s", item)
            self.state.save(claimed=True, mode="client", streak_days=before.get("streak_days"),
                            note="log:already")
            self.jsonl.write({"event": "result", "mode": "client", "result": "already",
                              "streak_days": before.get("streak_days")})
            return EXIT_OK

        if not self.client.ensure_online():
            LOG.error("客户端不可用且无法自动启动")
            return EXIT_ENV_ERROR

        # 窗口置前 -> 触发 Checkin 模块刷新状态
        self.client.focus()

        # 等待客户端把状态刷新/领取写入日志
        deadline = time.time() + max(30, min(self.client.ready_timeout, 180))
        poll = 0
        while time.time() < deadline:
            time.sleep(10)
            poll += 1
            snap = self.verifier.scan()
            if snap["claimed"]:
                LOG.info("检测到今日签到完成")
                for item in snap["evidence"]:
                    LOG.info("  证据: %s", item)
                self.state.save(claimed=True, mode="client", credit=snap.get("credit"),
                                streak_days=snap.get("streak_days"), note="log:claimed")
                self.jsonl.write({"event": "result", "mode": "client", "result": "success",
                                  "credit": snap.get("credit"),
                                  "streak_days": snap.get("streak_days"), "poll": poll})
                if self.cfg["notify"].get("on_success"):
                    notify("WorkBuddy 签到成功",
                           "已领取 %s 积分" % (snap.get("credit") or "100"),
                           bool(self.cfg["notify"].get("enabled")), self.log_dir)
                return EXIT_OK
            LOG.info("第 %d 次核验：尚未检测到签到记录，继续等待", poll)

        # 未能自动完成：给出明确提示（客户端签到需要一次点击）
        LOG.warning("客户端已在线，但今日签到尚未完成")
        LOG.warning("提示：WorkBuddy 客户端的签到需要点击右上角头像旁的签到气泡确认")
        if self.cfg["notify"].get("on_failure"):
            notify("WorkBuddy 今日签到待确认",
                   "客户端已自动上线，请点击右上角头像旁的签到气泡领取今日 100 积分。"
                   "或在 config.json 填入 access_token 实现全自动。",
                   bool(self.cfg["notify"].get("enabled")), self.log_dir)
        self.jsonl.write({"event": "result", "mode": "client", "result": "need_manual_click"})
        return EXIT_NEED_HAND

    # ---- 总入口 ----
    def run(self) -> int:
        mode = (self.args.mode or self.cfg.get("mode") or "auto").lower()
        LOG.info("=" * 62)
        LOG.info("WorkBuddy 每日积分自动领取 | 模式=%s | 日期=%s", mode, today_str())
        LOG.info("凭据来源: %s", self.cred.source)

        if self.args.show_status:
            return self.show_status(mode)

        if self.state.claimed_today() and not self.args.force:
            LOG.info("本地记录显示今日已领取（%s），跳过。需要强制执行请加 --force",
                     self.state.data.get("updated_at"))
            return EXIT_OK

        if mode == "auto":
            mode = "token" if self.cred.valid else "client"
            LOG.info("auto 模式解析为: %s", mode)

        if mode == "token" and not self.cred.valid:
            LOG.warning("token 模式缺少 access_token，自动降级为 client 模式")
            mode = "client"

        if mode == "token":
            code = self.run_token_mode()
            if code == EXIT_AUTH_ERROR:
                # 兜底一：客户端可能已经签到成功
                snap = self.verifier.scan()
                if snap["claimed"]:
                    LOG.info("access_token 已失效，但客户端日志显示今日签到已完成，按成功处理")
                    self.state.save(claimed=True, mode="client-fallback",
                                    streak_days=snap.get("streak_days"), note="fallback")
                    return EXIT_OK
                # 兜底二：把客户端拉起来，至少保证今天能拿到分
                if self.client.ensure_online():
                    self.client.focus()
                    LOG.warning("已通过客户端兜底上线，请更新 access_token 以恢复全自动")
                    notify("WorkBuddy 签到需要重新登录",
                           "access_token 已失效。客户端已上线，请点击右上角签到气泡完成今日签到，"
                           "并更新 config.json 中的 token。",
                           bool(self.cfg["notify"].get("enabled")), self.log_dir)
            return code

        return self.run_client_mode()

    def show_status(self, mode: str) -> int:
        LOG.info("-- 只读状态查询 --")
        if self.cred.valid:
            api = ApiClient(self.cfg, self.cred)
            res = api.status()
            LOG.info("接口返回: kind=%s http=%s code=%s msg=%s",
                     res.kind, res.http_status, res.code, res.msg)
            if res.kind == "ok":
                print(json.dumps(res.data, ensure_ascii=False, indent=2))
                return EXIT_OK
            if res.kind == "auth_error":
                LOG.error("access_token 无效或已过期")
                return EXIT_AUTH_ERROR
            LOG.warning("接口不可用，改用客户端日志判断")
        snap = self.verifier.scan()
        ok = bool(snap["claimed"])
        print(json.dumps({k: v for k, v in snap.items() if k != "evidence"},
                         ensure_ascii=False, indent=2))
        for item in snap["evidence"]:
            print("  证据: %s" % item)
        if not snap["log_found"]:
            print("  警告: 未找到客户端签到日志，请确认 --log-dir 或客户端是否运行过")
        return EXIT_OK if ok else EXIT_NEED_HAND


# --------------------------------------------------------------------------
# 自检 / 探测
# --------------------------------------------------------------------------

TASK_XML_DIR = r"C:\Windows\System32\Tasks"
TASK_PREFIX = "WorkBuddy每日积分"
DEFAULT_TASK_NAMES = ["WorkBuddy每日积分-主签到", "WorkBuddy每日积分-补签"]


def _task_names(cfg: Dict[str, Any]) -> List[str]:
    """
    C:\\Windows\\System32\\Tasks 未提权时不允许列举（PermissionError），
    但按已知文件名直接读取是允许的。所以任务名由 install_task.ps1 登记到 state/tasks.json。
    """
    names = list(DEFAULT_TASK_NAMES)
    manifest = os.path.join(expand(cfg["state"]["state_dir"]), "tasks.json")
    if os.path.isfile(manifest):
        try:
            # PowerShell 的 Set-Content -Encoding UTF8 会带 BOM，用 utf-8-sig 兼容
            with open(manifest, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
            for item in data.get("tasks", []):
                name = item if isinstance(item, str) else item.get("name", "")
                if name and name not in names:
                    names.append(name)
        except (OSError, ValueError) as exc:
            LOG.debug("读取 tasks.json 失败: %s", exc)
    return names


def _task_definitions(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按名字探测任务计划程序的 XML 定义，不依赖 PowerShell，也不需要管理员权限。"""
    out: List[Dict[str, Any]] = []

    candidates = _task_names(cfg)
    try:  # 万一有权限列举，就把非默认命名的任务也补上
        for name in os.listdir(TASK_XML_DIR):
            if name.startswith(TASK_PREFIX) and name not in candidates:
                candidates.append(name)
    except OSError:
        pass

    for name in candidates:
        path = os.path.join(TASK_XML_DIR, name)
        if not os.path.isfile(path):
            continue
        info: Dict[str, Any] = {"name": name, "start": "", "enabled": True}
        try:
            with open(path, "r", encoding="utf-16", errors="replace") as fh:
                xml = fh.read(20000)
        except OSError:
            out.append(info)
            continue
        m = re.search(r"<StartBoundary>([^<]+)</StartBoundary>", xml)
        if m:
            # StartBoundary 以 UTC 存储，换算成本地时间展示
            try:
                utc = datetime.fromisoformat(m.group(1))
                local = utc.replace(tzinfo=timezone.utc).astimezone()
                info["start"] = local.strftime("%H:%M")
            except ValueError:
                info["start"] = m.group(1)
        info["enabled"] = "<Enabled>false</Enabled>" not in xml
        out.append(info)
    return sorted(out, key=lambda x: x["name"])


def _tcp_ok(host: str, port: int = 443, timeout: float = 8.0) -> Tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "TCP %d 可达" % port
    except OSError as exc:
        return False, str(exc)


def _writable(path: str) -> Tuple[bool, str]:
    try:
        ensure_dir(path)
        probe = os.path.join(path, ".wb_write_probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True, "可写"
    except OSError as exc:
        return False, str(exc)


def run_discover(cfg: Dict[str, Any]) -> int:
    blockers: List[str] = []

    def line(tag: str, label: str, detail: str) -> None:
        print("  %-6s %-14s %s" % (tag, label, detail))

    def check(ok: bool, label: str, detail: str, blocking: bool = True) -> None:
        line("[OK]" if ok else ("[!!]" if blocking else "[--]"), label, detail)
        if not ok and blocking:
            blockers.append(label)

    print("")
    print("=========== WorkBuddy 每日积分 · 环境体检 ===========")
    print("")

    print("[1] 系统与运行时")
    if IS_WINDOWS:
        try:
            import platform as _pf
            check(True, "操作系统", "Windows %s (build %s) / %s" % (
                _pf.release(), _pf.version().split(".")[-1], _pf.machine()))
        except Exception:
            check(True, "操作系统", "Windows")
    else:
        check(False, "操作系统", "非 Windows（%s），窗口置前与计划任务不可用" % os.name)
    check(sys.version_info >= (3, 9), "Python 版本",
          "%s（要求 >= 3.9）" % sys.version.split()[0])
    print("         %-14s %s" % ("解释器", sys.executable))
    missing = []
    for mod in ("urllib.request", "json", "ssl", "subprocess", "ctypes"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    check(not missing, "标准库依赖", "全部可用（无需 pip 安装）" if not missing else "缺失: %s" % missing)
    check(os.name == "nt" and hasattr(ctypes, "windll"), "Win32 API",
          "ctypes.windll 可用" if (os.name == "nt" and hasattr(ctypes, "windll")) else "不可用")

    print("")
    print("[2] WorkBuddy 桌面端")
    driver = ClientDriver(cfg)
    check(bool(driver.exe_path), "客户端程序",
          ("%s（来源: %s）" % (driver.exe_path, driver.exe_source)) if driver.exe_path
          else "未找到，请在 config.json 的 client.exe_path 手工指定")
    if driver.exe_path:
        check(os.path.isfile(driver.exe_path), "文件存在", driver.exe_path)
    running = driver.is_running()
    pids = driver.pids()
    check(True, "运行状态",
          "在运行（PID %s）" % ",".join(str(p) for p in pids[:4]) if running
          else "未运行（脚本会自动启动它）", blocking=False)

    print("")
    print("[3] 签到日志（核验依据）")
    verifier = LogVerifier(cfg)
    check(os.path.isdir(verifier.log_dir), "日志目录", verifier.log_dir)
    snap = verifier.scan()
    check(snap["log_found"], "可解析签到记录",
          "找到 [Checkin] 记录" if snap["log_found"] else "未找到，今日还没启动过客户端？")
    check(True, "今日签到",
          ("已完成" if snap["claimed"] else "未完成")
          + "（服务端 today_checked_in=%s，连签 %s 天，最近刷新 %s）"
          % (snap["today_checked_in"], snap["streak_days"], snap["last_refresh_at"]),
          blocking=False)
    for item in snap["evidence"][:3]:
        print("         %-14s %s" % ("证据", item))

    print("")
    print("[4] 网络")
    host = cfg["api"]["base_url"].split("//")[-1].split("/")[0]
    reachable, detail = _tcp_ok(host)
    check(reachable, "接口域名", "%s -> %s" % (host, detail), blocking=False)
    if not reachable:
        print("         %-14s %s" % ("说明", "token 模式不可用；client 模式由客户端自行联网，仍可工作"))

    print("")
    print("[5] 凭据")
    cred = resolve_credentials(cfg)
    check(cred.valid, "access_token",
          "已配置（来源: %s）" % cred.source if cred.valid
          else "未配置 -> 走 client 模式；填入可切换为全自动 token 模式", blocking=False)
    check(bool(cred.user_id) or cred.valid, "X-User-Id",
          cred.user_id or "未配置（token 模式建议一并填写）", blocking=False)

    print("")
    print("[6] 每日定时任务")
    tasks = _task_definitions(cfg)
    if not tasks:
        check(False, "计划任务", "未注册。执行 install_task.ps1 后生效", blocking=False)
    for t in tasks:
        line("[OK]" if t["enabled"] else "[--]", "计划任务",
             "%s  每天 %s%s" % (t["name"], t["start"] or "?", "" if t["enabled"] else "（已禁用）"))

    print("")
    print("[7] 读写权限")
    for label, sub in (("日志目录", cfg["logging"]["log_dir"]), ("状态目录", cfg["state"]["state_dir"])):
        ok, detail = _writable(expand(sub))
        check(ok, label, "%s -> %s" % (expand(sub), detail))

    print("")
    print("=" * 56)
    if blockers:
        print("结论：存在 %d 项阻塞问题 -> %s" % (len(blockers), "、".join(blockers)))
        print("请按上面的 [!!] 行修复后重跑 --discover。")
        return EXIT_ENV_ERROR

    if cred.valid:
        print("结论：环境就绪，token 模式可全自动领取。")
    else:
        print("结论：环境就绪，当前以 client 模式运行（保证客户端上线 + 核验签到结果）。")
        print("      想彻底免点击，填上 access_token 即可切换全自动。")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="WorkBuddy 每日积分自动领取",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python checkin.py --show-status\n"
               "  python checkin.py --mode client --force\n"
               "  python checkin.py --discover\n")
    p.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    p.add_argument("--mode", choices=["auto", "token", "client"], help="覆盖 config.json 的 mode")
    p.add_argument("--token", default="", help="临时指定 access_token")
    p.add_argument("--user-id", default="", help="临时指定 X-User-Id")
    p.add_argument("--force", action="store_true", help="忽略本地幂等记录，强制执行")
    p.add_argument("--dry-run", action="store_true", help="演练：不真正调用领取接口")
    p.add_argument("--show-status", action="store_true", help="只查询今日签到状态")
    p.add_argument("--no-notify", action="store_true", help="本次不弹系统通知（只写提醒文件）")
    p.add_argument("--discover", action="store_true", help="自检运行环境并输出探测结果")
    p.add_argument("--verbose", action="store_true", help="输出 DEBUG 日志")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(os.path.abspath(args.config))
    if args.verbose:
        cfg["logging"]["level"] = "DEBUG"

    if args.discover:
        setup_logging(cfg)
        return run_discover(cfg)

    runner = Runner(cfg, args)
    purge_old_logs(runner.log_dir, int(cfg["logging"].get("keep_days", 60)))
    try:
        return runner.run()
    except KeyboardInterrupt:
        LOG.warning("被用户中断")
        return EXIT_NETWORK
    except Exception as exc:  # 兜底，保证日志里能看到堆栈
        LOG.exception("未预期的异常: %s", exc)
        return EXIT_ENV_ERROR


if __name__ == "__main__":
    sys.exit(main())
