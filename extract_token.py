# -*- coding: utf-8 -*-
"""
extract_token.py —— 从运行中的 WorkBuddy 客户端提取签到所需的 access_token / userId。

背景
----
WorkBuddy 桌面端把登录态放在 daemon 进程内存里（磁盘上是加密的，且没有明文 JWT），
所以本工具直接从进程内存里捞。只依赖 Python 标准库 + Windows API。

用途
----
把 token 写进 config.json，让 checkin.py 能走 token 模式（直连官方接口），
从而摆脱 "client 模式必须手动点签到气泡" 的限制。

安全
----
* 只读进程内存，不写入、不注入、不修改客户端任何状态。
* 默认不打印 token 明文，只显示脱敏摘要（前 6 位 + 长度 + 过期时间）。
* 结果只落到 config.json（已在 .gitignore 中）或 token.txt。

用法
----
    python extract_token.py                # 扫描并写回 config.json
    python extract_token.py --dry-run      # 只探测，不写任何文件
    python extract_token.py --show         # 打印 token 明文（谨慎，仅本地排错）
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import re
import sys
from ctypes import wintypes
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Win32 常量与结构体
# ---------------------------------------------------------------------------

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

MEM_COMMIT = 0x1000
MEM_FREE = 0x10000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100

MAX_SCAN_BYTES = 512 * 1024 * 1024  # 单个进程最多扫 512MB，防止卡死
CHUNK = 1 << 20                      # 每次读 1MB

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    try:
        k32.ReadProcessMemory.argtypes = [
            wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID,
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        k32.ReadProcessMemory.restype = wintypes.BOOL
        k32.VirtualQueryEx.argtypes = [
            wintypes.HANDLE, wintypes.LPCVOID,
            ctypes.c_void_p, ctypes.c_size_t]
        k32.VirtualQueryEx.restype = ctypes.c_size_t
    except AttributeError:  # pragma: no cover
        pass

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("PartitionId", wintypes.WORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    try:
        psapi.EnumProcesses.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD)]
        psapi.EnumProcesses.restype = wintypes.BOOL
        psapi.GetProcessImageFileNameW.argtypes = [
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
        psapi.GetProcessImageFileNameW.restype = wintypes.DWORD
        _HAS_PSAPI = True
    except AttributeError:  # pragma: no cover
        _HAS_PSAPI = False


# ---------------------------------------------------------------------------
# 内存扫描
# ---------------------------------------------------------------------------

# 标准 JWT：eyJ....  .eyJ....  .<sig>
JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{6,}")

# 形如 "access_token":"xxxx" / "accessToken":"xxxx" 的 JSON 片段
KV_RE = re.compile(
    rb"""["']?(?:access_?[Tt]oken|token)["']?\s*[:=]\s*["']([A-Za-z0-9_\-\.=]{16,512})["']"""
)

# 客户端日志里出现过 accountUid，用来配对 userId
UUID_RE = re.compile(rb"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _decode_jwt_payload(tok: str) -> Optional[Dict[str, Any]]:
    """解 JWT 第二段（payload），返回字典；失败返回 None。"""
    parts = tok.split(".")
    if len(parts) != 3:
        return None
    seg = parts[1]
    seg += "=" * (-len(seg) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(seg).decode("utf-8", "replace"))
    except Exception:
        return None


def iter_committed_regions(pid: int):
    """产出 (base, size)，只给已提交且可读的内存区。"""
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not h:
        return
    try:
        mbi = MEMORY_BASIC_INFORMATION()
        addr = 0
        total = 0
        while addr < 0x7FFFFFFF0000 and total < MAX_SCAN_BYTES:
            n = k32.VirtualQueryEx(h, ctypes.c_void_p(addr),
                                   ctypes.byref(mbi), ctypes.sizeof(mbi))
            if not n:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if (mbi.State == MEM_COMMIT
                    and not (mbi.Protect & PAGE_NOACCESS)
                    and not (mbi.Protect & PAGE_GUARD)
                    and size > 0):
                yield int(base), int(size), h
                total += size
            addr = int(base) + int(size)
            if size == 0:
                break
    finally:
        k32.CloseHandle(h)


def scan_pid(pid: int) -> List[Dict[str, Any]]:
    """扫描一个进程，返回候选 token 列表（已去重、带 payload 解析）。"""
    found: Dict[str, Dict[str, Any]] = {}
    try:
        for base, size, h in iter_committed_regions(pid):
            off = 0
            while off < size:
                want = min(CHUNK, size - off)
                buf = ctypes.create_string_buffer(want)
                read = ctypes.c_size_t(0)
                ok = k32.ReadProcessMemory(
                    h, ctypes.c_void_p(base + off), buf, want, ctypes.byref(read))
                if not ok or read.value == 0:
                    break
                chunk = buf.raw[: read.value]

                for m in JWT_RE.finditer(chunk):
                    raw = m.group()
                    try:
                        tok = raw.decode("ascii")
                    except UnicodeDecodeError:
                        continue
                    payload = _decode_jwt_payload(tok) or {}
                    found.setdefault(tok, {
                        "token": tok,
                        "kind": "jwt",
                        "payload": payload,
                        "pid": pid,
                    })
                off += want
    except (OSError, ValueError):
        pass
    return list(found.values())


def scan_pid_uid(pid: int) -> List[str]:
    """顺带捞 userId（UUID 形态），用于填 X-User-Id。"""
    uids: Dict[str, int] = {}
    try:
        for base, size, h in iter_committed_regions(pid):
            off = 0
            while off < size:
                want = min(CHUNK, size - off)
                buf = ctypes.create_string_buffer(want)
                read = ctypes.c_size_t(0)
                ok = k32.ReadProcessMemory(
                    h, ctypes.c_void_p(base + off), buf, want, ctypes.byref(read))
                if not ok or read.value == 0:
                    break
                chunk = buf.raw[: read.value]
                for m in UUID_RE.finditer(chunk):
                    uids[m.group().decode("ascii")] = uids.get(
                        m.group().decode("ascii"), 0) + 1
                off += want
    except (OSError, ValueError):
        pass
    return [u for u, _ in sorted(uids.items(), key=lambda kv: -kv[1])]


def list_target_pids(name_hint: str = "WorkBuddy") -> List[Tuple[int, str]]:
    """列出名字含 hint 的进程。"""
    out: List[Tuple[int, str]] = []
    if not IS_WINDOWS:
        return out
    arr = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD(0)
    if not psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
        return out
    count = min(needed.value // ctypes.sizeof(wintypes.DWORD), 4096)
    for i in range(count):
        pid = arr[i]
        if not pid:
            continue
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            buf = ctypes.create_unicode_buffer(1024)
            if psapi.GetProcessImageFileNameW(h, buf, 1024):
                path = buf.value
                if name_hint.lower() in path.lower():
                    out.append((pid, path))
        finally:
            k32.CloseHandle(h)
    return out


# ---------------------------------------------------------------------------
# 打分：挑出"最像签到用的那个 token"
# ---------------------------------------------------------------------------

GOOD_KEYS = ("exp", "iat", "uid", "sub", "userId", "accountUid", "user_id", "nickname")


def score(cand: Dict[str, Any]) -> int:
    p = cand.get("payload") or {}
    s = 0
    if "exp" in p:
        s += 5
    for k in GOOD_KEYS:
        if k in p:
            s += 2
    # 太短的不像
    s += min(len(cand["token"]) // 64, 4)
    return s


def mask(tok: str) -> str:
    return "%s...(%d chars)" % (tok[:6], len(tok))


def exp_desc(payload: Dict[str, Any]) -> str:
    import datetime
    exp = payload.get("exp")
    if not exp:
        return "无 exp 字段"
    try:
        dt = datetime.datetime.fromtimestamp(int(exp))
        delta = dt - datetime.datetime.now()
        return "%s（还有 %d 天）" % (dt.strftime("%Y-%m-%d %H:%M"), delta.days)
    except Exception:
        return "exp=%s" % exp


# ---------------------------------------------------------------------------
# 写回配置
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config() -> Dict[str, Any]:
    path = os.path.join(BASE_DIR, "config.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def save_config(cfg: Dict[str, Any]) -> str:
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path):
        bak = path + ".bak"
        try:
            with open(path, "rb") as a, open(bak, "wb") as b:
                b.write(a.read())
        except OSError:
            pass
    with open(path, "w", encoding="utf-8") as fh:  # 无 BOM，checkin.py 用 utf-8-sig 读
        json.dump(cfg, fh, ensure_ascii=False, indent=4)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="从 WorkBuddy 进程内存提取 access_token")
    ap.add_argument("--dry-run", action="store_true", help="只探测，不写文件")
    ap.add_argument("--show", action="store_true", help="打印 token 明文（谨慎）")
    ap.add_argument("--pid", type=int, default=0, help="只扫指定 PID")
    ap.add_argument("--all", action="store_true", help="列出全部候选而不止最优一个")
    args = ap.parse_args()

    if not IS_WINDOWS:
        print("本工具仅支持 Windows。")
        return 5

    targets = ([ (args.pid, "manual") ] if args.pid else list_target_pids())
    if not targets:
        print("没有找到运行中的 WorkBuddy 进程。请先启动客户端并登录。")
        return 5

    print("=" * 62)
    print("WorkBuddy access_token 提取器")
    print("=" * 62)
    print("目标进程 %d 个：%s" % (len(targets),
                                ", ".join("PID %d" % p for p, _ in targets)))
    print()

    candidates: List[Dict[str, Any]] = []
    uid_hits: Dict[str, int] = {}

    for pid, _path in targets:
        cands = scan_pid(pid)
        if cands:
            print("  PID %d -> 找到 %d 个 JWT 候选" % (pid, len(cands)))
        candidates.extend(cands)
        for u in scan_pid_uid(pid)[:20]:
            uid_hits[u] = uid_hits.get(u, 0) + 1

    # 去重
    uniq: Dict[str, Dict[str, Any]] = {}
    for c in candidates:
        uniq[c["token"]] = c
    candidates = sorted(uniq.values(), key=score, reverse=True)

    if not candidates:
        print()
        print("未能在进程内存中找到 JWT。可能原因：")
        print("  1. 客户端未运行或未登录")
        print("  2. 权限不足（请以当前登录用户身份运行，不要跨用户）")
        print("  3. token 不是 JWT 格式（可改用 --show 配合人工排查）")
        return 5

    print()
    print("候选 token（按匹配度排序，脱敏显示）：")
    print("-" * 62)
    show_list = candidates if args.all else candidates[:5]
    for i, c in enumerate(show_list, 1):
        p = c.get("payload") or {}
        print("%2d. %s" % (i, mask(c["token"])))
        print("    过期  : %s" % exp_desc(p))
        keys = [k for k in ("uid", "userId", "accountUid", "sub", "nickname", "type")
                if k in p]
        if keys:
            print("    字段  : %s" % ", ".join("%s=%s" % (k, p[k]) for k in keys[:6]))
        print("    来源  : PID %d" % c["pid"])
        print()

    best = candidates[0]

    # 推断 userId：优先用 JWT payload 里的，其次用内存里出现最多的 UUID
    uid = ""
    p = best.get("payload") or {}
    for k in ("uid", "userId", "accountUid", "sub", "user_id"):
        v = p.get(k)
        if isinstance(v, str) and len(v) >= 8:
            uid = v
            break
    if not uid and uid_hits:
        uid = sorted(uid_hits.items(), key=lambda kv: -kv[1])[0][0]

    if args.show:
        print("[--show] token 明文：")
        print(best["token"])
        print()

    print("选定：%s" % mask(best["token"]))
    print("userId：%s" % (uid or "(未识别)"))

    if args.dry_run:
        print()
        print("--dry-run：未写入任何文件。")
        return 0

    cfg = load_config()
    cfg.setdefault("auth", {})
    cfg["auth"]["access_token"] = best["token"]
    if uid:
        cfg["auth"]["user_id"] = uid
    cfg["mode"] = "token"
    path = save_config(cfg)

    print()
    print("已写入 %s" % path)
    print("  auth.access_token = %s" % mask(best["token"]))
    print("  auth.user_id      = %s" % (uid or ""))
    print("  mode              = token   （原文件已备份为 config.json.bak）")
    print()
    print("下一步验证：python checkin.py --mode token")
    return 0


if __name__ == "__main__":
    sys.exit(main())
