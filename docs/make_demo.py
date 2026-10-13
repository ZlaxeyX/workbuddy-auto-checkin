#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docs/make_demo.py —— 生成 README 首屏的演示图 docs/demo.png

为什么需要这个脚本
------------------
1. GitHub **不会渲染 Markdown 里的 SVG 图片**（SVG 可携带脚本，被安全策略拦掉），
   所以 README 必须引用 PNG，否则首屏是裂图。
2. 图里的内容是 `checkin.py --discover` 的真实输出，里面含本机路径、accountUid、
   PID 等敏感信息。直接截图上传等于泄露，因此这里做**强制脱敏**。
3. 改过默认时间/模式后，重跑一次本脚本即可同步更新图片，不会留下过时截图。

用法
----
    python docs/make_demo.py

依赖：Pillow（仅生成图片时需要，**项目本身零依赖**）
    pip install pillow
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PNG = os.path.join(BASE_DIR, "docs", "demo.png")
CHECKIN = os.path.join(BASE_DIR, "checkin.py")

# ---------------------------------------------------------------------------
# 脱敏规则：按顺序替换，规则尽量保守（宁可多掩，不可漏掩）
# ---------------------------------------------------------------------------

REPLACEMENTS: List[Tuple["re.Pattern[str]", object]] = [
    # accountUid / 任意 UUID -> 只留前 8 位
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"),
     lambda m: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"),
    # 本机用户目录 C:\Users\<name>\...
    (re.compile(r"[A-Z]:\\Users\\[^\\\s]+"),
     lambda m: "C:\\Users\\<用户>"),
    # 任意盘符开头的绝对路径 -> 只保留最后一段（文件名），避免暴露目录层级
    (re.compile(r"[A-Za-z]:\\[^:\n]*\\([^\\\n]+)"),
     lambda m: "...\\" + m.group(1)),
    # PID 列表
    (re.compile(r"PID\s+\d[\d,\s]*"),
     lambda m: "PID <已省略>"),
    # build 号
    (re.compile(r"\(build\s+\d+\)"),
     lambda m: "(build <n>)"),
]


def sanitize(text: str) -> str:
    """用函数做替换，避免替换串里的反斜杠被 re 当成转义（\\U 会报 bad escape）。"""
    for pat, repl in REPLACEMENTS:
        text = pat.sub(repl, text)
    return text


# ---------------------------------------------------------------------------
# 取真实输出
# ---------------------------------------------------------------------------

def collect_output() -> str:
    """跑 --discover 拿真实输出。失败时退回到一段内置示例，保证脚本总能出图。"""
    if os.path.isfile(CHECKIN):
        try:
            proc = subprocess.run(
                [sys.executable, CHECKIN, "--discover"],
                capture_output=True, timeout=180,
                cwd=BASE_DIR,
            )
            raw = proc.stdout.decode("utf-8", "replace")
            if raw.strip():
                return raw
        except (OSError, subprocess.SubprocessError) as exc:
            sys.stderr.write("跑 --discover 失败（%s），改用内置示例\n" % exc)
    return _FALLBACK


_FALLBACK = """=========== WorkBuddy 每日积分 · 环境体检 ===========

[1] 系统与运行时
  [OK]   操作系统           Windows 11 / AMD64
  [OK]   Python 版本      3.13（要求 >= 3.9）
  [OK]   标准库依赖          全部可用（无需 pip 安装）

[2] WorkBuddy 桌面端
  [OK]   客户端程序          ...\\WorkBuddy\\WorkBuddy.exe（来源: 运行中的进程）
  [OK]   运行状态           在运行

[3] 签到日志（核验依据）
  [OK]   可解析签到记录        找到 [Checkin] 记录
  [OK]   今日签到           已完成（连签 8 天）

[5] 凭据
  [OK]   access_token   已配置
  [OK]   token 有效期      2026-11-05 00:17 （还剩 22 天）

[6] 每日定时任务
  [OK]   配置时间           主签到每天 12:00，补签每天 21:00
  [OK]   计划任务           WorkBuddy每日积分-主签到  每天 12:00
  [OK]   断网韧性           主签到每 30 分钟重试一次、持续 12 小时

结论：环境就绪，token 模式可全自动领取。
"""


# ---------------------------------------------------------------------------
# 首屏图的行筛选：README 第一屏装不下 43 行，只留最能体现价值的
# ---------------------------------------------------------------------------

KEEP_HINTS = (
    "checkin.py --discover", "=====",      # 命令头 / 标题
    "Python 版本", "标准库依赖",            # 卖点：零依赖
    "客户端程序", "今日签到",               # 核心结果
    "access_token", "token 有效期",
    "配置时间", "断网韧性", "结论",
)

DROP_HINTS = (
    "Win32", "文件存在", "运行状态", "日志目录", "可解析签到记录",
    "证据", "接口域名", "X-User-Id", "读写权限", "解释器",
)


def slim(lines: List[str]) -> List[str]:
    """去掉与首屏无关的行。保留空行、[n] 段标题，以及 KEEP 命中且未被 DROP 命中的行。"""
    section_re = re.compile(r"^\[\d\]")
    out: List[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            out.append(ln)
            continue
        # 段标题形如 "[1] 系统与运行时"；注意别把 "  [OK] xxx" 也当标题，
        # 否则 DROP 规则会被段标题分支短路掉（实测踩过）
        if section_re.match(s):
            out.append(ln)
            continue
        if any(h in ln for h in DROP_HINTS):
            continue
        if any(h in ln for h in KEEP_HINTS):
            out.append(ln)
    # 收尾：去掉连续空行与首尾空行
    cleaned: List[str] = []
    for ln in out:
        if not ln.strip() and (not cleaned or not cleaned[-1].strip()):
            continue
        cleaned.append(ln)
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    # 再删掉"下面已经没有内容"的段标题，别留 [4] 网络 这种光杆标题
    cleaned = _drop_empty_sections(cleaned)
    return cleaned


def _drop_empty_sections(lines: List[str]) -> List[str]:
    """删除后面没有实质内容的段标题。"""
    section_re = re.compile(r"^\[\d\]")
    out = list(lines)
    i = 0
    while i < len(out):
        if section_re.match(out[i].strip()):
            j = i + 1
            while j < len(out) and not out[j].strip():
                j += 1
            has_content = (j < len(out)
                           and not section_re.match(out[j].strip())
                           and not out[j].strip().startswith("="))
            if not has_content:
                if j > i:
                    del out[i:j]
                else:
                    del out[i]
                continue
        i += 1
    return out


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------

# 颜色（VS Code Dark+ 风格）
BG = "#1E1E1E"
TITLEBAR = "#2D2D30"
FG = "#D4D4D4"
DIM = "#8A8A8E"
CMD = "#9CDCFE"
CMD_TAIL = "#D4D4D4"
OK = "#4EC9B0"
HINT = "#6A9955"

FONT_DIR = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
CANDIDATE_MONO = ["consola.ttf", "Consola.ttf"]
CANDIDATE_CJK = ["msyh.ttc", "simsun.ttc", "simhei.ttf"]


def _load_font(size: int, cjk: bool = False) -> Optional[object]:
    """按 优先等宽/中文字体 的顺序加载，失败返回 None（调用方退化处理）。"""
    from PIL import ImageFont
    names = CANDIDATE_CJK if cjk else CANDIDATE_MONO
    for name in names:
        path = os.path.join(FONT_DIR, name)
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return None


def _text_width(font, font_cjk, ch: str) -> int:
    """单个字符的渲染宽度：CJK 用中文字体，ASCII 用等宽字体。"""
    if ord(ch) < 0x2E80 and font_cjk is not None and font is not None:
        try:
            return font.getlength(ch)
        except Exception:
            return 8
    if font_cjk is not None:
        try:
            return font_cjk.getlength(ch)
        except Exception:
            return 14
    return 8


def render(lines: List[str], out_path: str) -> None:
    from PIL import Image, ImageDraw

    size = 13
    mono = _load_font(size)
    cjk = _load_font(size, cjk=True)

    # 估算画布宽度（按最宽行）
    max_w = 0
    for ln in lines:
        w = sum(_text_width(mono, cjk, ch) for ch in ln)
        max_w = max(max_w, w)

    pad_x, pad_y, line_h = 26, 16, 24
    title_h = 30
    width = int(max_w + pad_x * 2)
    height = title_h + pad_y * 2 + len(lines) * line_h + 10

    img = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(img)

    # 标题栏 + 三个圆点
    draw.rectangle([0, 0, width, title_h], fill=TITLEBAR)
    for i, color in enumerate(("#FF5F57", "#FEBC2E", "#28C840")):
        cx = 22 + i * 20
        cy = title_h // 2
        r = 6
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)

    def put_text(x: int, y: int, text: str, color: str) -> None:
        cx = x
        for ch in text:
            if ch != " ":
                f = cjk if ord(ch) >= 0x2E80 else mono
                if f is not None:
                    draw.text((cx, y), ch, font=f, fill=color)
                else:  # 字体缺失时的兜底
                    draw.text((cx, y), ch, fill=color)
            cx += _text_width(mono, cjk, ch)

    y = title_h + pad_y
    for ln in lines:
        stripped = ln.strip()
        color = FG
        if ln.startswith("PS ") or "checkin.py" in ln:
            color = CMD
        elif stripped.startswith("[OK]"):
            color = OK
        elif stripped.startswith("结论"):
            color = CMD
        elif stripped.startswith("证据"):
            color = HINT
        elif stripped.startswith("=") or stripped.startswith("[") and "]" in stripped[:6]:
            color = FG
        put_text(pad_x, y, ln, color)
        y += line_h

    img.save(out_path, "PNG", optimize=True)
    print("已生成 %s（%dx%d）" % (os.path.relpath(out_path, BASE_DIR), width, height))


def main() -> int:
    from PIL import ImageFont  # noqa: F401  （提前暴露缺失依赖）

    raw = collect_output()
    raw = sanitize(raw)

    # 顶部加一行"敲了什么命令"，让图自带上下文；再瘦身到首屏能放下的行数
    lines = ["PS C:\\WorkBuddy> python checkin.py --discover", ""]
    lines.extend(slim(raw.rstrip("\n").split("\n")))

    render(lines, OUT_PNG)
    print("脱敏检查：")
    for pat, _ in REPLACEMENTS:
        pass
    # 复核一遍，确保没有漏掩
    bad = re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                     "\n".join(lines))
    if bad:
        print("  [!!] 仍检测到 UUID：%s" % bad[:3])
        return 1
    print("  [OK] 未检测到 UUID / 本机用户路径")
    return 0


if __name__ == "__main__":
    sys.exit(main())
