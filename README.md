# WorkBuddy 每日积分自动领取

> 每天自动拿到 WorkBuddy「Buddy 加油站」的 **100 积分**（连签第 7 天额外 **+1000**），不用再惦记着手动点。

纯 Python 标准库实现，**零第三方依赖**；配套 Windows 计划任务注册脚本，装一次长期生效。

- 无依赖：只用 `urllib` / `json` / `ctypes` / `winreg` / `subprocess`
- 不碰密码：默认模式完全复用桌面端已登录态，脚本不读取、不解密、不保存任何凭据
- 不怕重跑：三层幂等保护，补签任务不会重复领积分
- 可核验：从客户端日志解析签到证据，成功/失败都有明确退出码

---

## 目录

- [项目简介](#项目简介)
- [功能特性](#功能特性)
- [工作原理](#工作原理)
- [环境要求](#环境要求)
- [安装](#安装)
- [使用](#使用)
- [配置说明](#配置说明)
- [签到结果判定](#签到结果判定)
- [日志与排错](#日志与排错)
- [隐私与安全](#隐私与安全)
- [常见问题](#常见问题)
- [注意事项与免责声明](#注意事项与免责声明)
- [许可证](#许可证)
- [附录：技术细节](#附录技术细节)

---

## 项目简介

WorkBuddy 桌面端每天可以签到领 100 积分，连续 7 天额外奖励 1000 积分。问题在于**必须坚持**——漏一天，连签天数清零，那 1000 分奖励就作废了。

本项目把这件事交给机器：定时唤醒客户端、核验签到结果、失败时提醒你。可选的 `token` 模式还能完全绕过界面，直接调用官方签到接口。

### 积分规则

| 项目 | 积分 | 说明 |
|---|---|---|
| 每日签到 | 100 / 天 | |
| 连续 7 天 | +1000 | 断签后重新计算 |
| 月收益 | 约 3000+ | 含连签奖励 |
| 有效期 | 约 30 天 | 过期清零，记得用掉 |

---

## 功能特性

- **双模式自动切换**：`auto` 模式有 token 走接口，没有就走客户端兜底，永不空手
- **每日定时**：注册两条 Windows 计划任务（默认 09:00 主签到 + 21:00 补签）
- **错过补跑**：任务开启 `StartWhenAvailable`，当时关机也会在下次开机后自动补执行
- **日志核验**：解析客户端 `main.log` 的 `[Checkin]` 记录，用真实证据判定成败，不靠猜
- **智能重试**：只对可恢复错误（超时 / 429 / 5xx）做指数退避重试，退避带抖动
- **幂等安全**：本地状态 + 服务端状态查询 + 服务端按天去重，三重保护
- **环境体检**：`--discover` 一条命令检查 7 项运行条件并给出结论
- **失败提醒**：需要人工确认时弹 Windows 通知，并落盘提醒文件
- **自动探测**：客户端安装路径自动发现（运行中进程 → 注册表 → 磁盘扫描），无需手工配置
- **结构化日志**：人类可读按天切分 + JSONL 结构化记录，便于统计与排错

---

## 工作原理

```
计划任务触发 (09:00 / 21:00)
        │
        ▼
读取配置 + 检查本地幂等状态 ──已领取──▶ 直接退出
        │
        ▼
     auto 模式判定
        │
   ┌────┴─────────────────────┐
   │ 有 token                 │ 无 token
   ▼                          ▼
token 模式                 client 模式
直连官方签到接口           检测进程 → 未运行则启动
   │                      窗口置前 → 触发状态刷新
   │                      轮询日志核验（最长 180s）
   └────────────┬─────────────┘
                ▼
            结果判定
        ┌───────┴────────┐
        ▼                ▼
   成功 / 今日已签      失败 / 需人工确认
   写状态，退出码 0     指数退避重试 ×3 → 弹通知
```

**为什么需要两种模式？** 客户端的领取动作绑定在界面上（点击顶部头像旁的签到气泡）。客户端启动后**常常**会自动领取，但这不是契约保证。所以要真正无人值守，需要 `token` 模式。

---

## 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | **Windows 10 / 11**（依赖任务计划程序 + Win32 窗口 API） |
| Python | **3.9+**，无需任何第三方包 |
| WorkBuddy 桌面端 | 已安装并**已登录** |
| 网络 | 能访问 `https://copilot.tencent.com`（token 模式）；client 模式由客户端自行联网 |
| 权限 | **不需要管理员权限**（计划任务以当前用户 `RunLevel=Limited` 注册） |
| 运行前提 | 计划任务以 `Interactive` 身份运行，需保持用户登录状态（锁屏可以，注销不行） |
| 磁盘 | 日志目录约每月几十 KB |

> 已在 **Windows 11 (build 26200) / AMD64 + Python 3.13** 上完整实测通过。

---

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/<your-name>/workbuddy-auto-checkin.git
cd workbuddy-auto-checkin
```

### 2. 确认 Python

```bash
python --version    # 需要 3.9 或更高
```

无需 `pip install`。若想显式确认，`pip install -r requirements.txt` 会直接成功（文件为空占位）。

### 3. 生成配置

```bash
python checkin.py --discover
```

首次运行会自动从 `config.example.json` 生成 `config.json`，并输出环境体检报告。

### 4. 注册每日定时任务

```powershell
powershell -ExecutionPolicy Bypass -File install_task.ps1
```

看到两条任务 `Ready` 即完成：

```
WorkBuddy每日积分-主签到  ->  每天 09:00
WorkBuddy每日积分-补签    ->  每天 21:00
```

---

## 使用

### 快速开始

```bash
# 环境体检（7 项检查 + 明确结论）
python checkin.py --discover

# 查今日签到状态（只读，不做任何写操作）
python checkin.py --show-status

# 按配置执行一次（默认 auto 模式）
python checkin.py
```

Windows 上也可以直接**双击 `run_checkin.bat`**。

### 常用命令

| 命令 | 说明 |
|---|---|
| `python checkin.py` | 按 `config.json` 的 mode 执行一次 |
| `python checkin.py --show-status` | 只查询今日状态，不做任何写操作 |
| `python checkin.py --mode client` | 强制走客户端模式 |
| `python checkin.py --mode token` | 强制走接口模式 |
| `python checkin.py --force` | 忽略"今日已领取"的本地幂等记录 |
| `python checkin.py --dry-run` | 演练，不真正调用领取 |
| `python checkin.py --discover` | 环境体检 |
| `python checkin.py --no-notify` | 本次不弹系统通知 |
| `python checkin.py --verbose` | 输出 DEBUG 日志 |
| `python checkin.py --help` | 完整帮助 |

### 定时任务管理

```powershell
# 自定义时间重新注册
powershell -ExecutionPolicy Bypass -File install_task.ps1 -PrimaryTime 08:30 -RetryTime 20:30

# 只注册主签到，不要补签
powershell -ExecutionPolicy Bypass -File install_task.ps1 -NoRetry

# 注册后立即试跑一次
powershell -ExecutionPolicy Bypass -File install_task.ps1 -RunNow

# 指定 Python 路径（自动探测失败时）
powershell -ExecutionPolicy Bypass -File install_task.ps1 -PythonPath "C:\Python313\python.exe"

# 卸载
powershell -ExecutionPolicy Bypass -File install_task.ps1 -Remove
```

---

## 配置说明

配置文件是 `config.json`（**已被 `.gitignore` 忽略，不会被提交**）。首次运行自动生成，改完立即生效，**不需要重新注册计划任务**。

### 模式选择

`mode` 有三个取值：

| 值 | 行为 |
|---|---|
| `auto`（默认） | 有 `access_token` 走 token 模式，否则走 client 模式 |
| `token` | 强制接口模式；缺 token 会自动降级为 client 并告警 |
| `client` | 强制客户端模式 |

### 凭据配置

`token` 模式需要官方接口的 `access_token`，按以下**优先级**解析（先命中者生效）：

1. 命令行：`--token <T> --user-id <UID>`
2. 环境变量：`WB_ACCESS_TOKEN` / `WB_USER_ID`
3. `token.txt`（第 1 行 token，第 2 行 user_id）
4. `config.json` 的 `auth.access_token` / `auth.user_id`

**获取方式**：WorkBuddy 客户端 → `Ctrl+Shift+I` 打开 DevTools → Network → 过滤 `billing/meter` → 点一次签到 → 复制请求头里的 `Authorization`（去掉 `Bearer ` 前缀）与 `X-User-Id`。

**推荐用环境变量**，避免明文落在项目里：

```powershell
setx WB_ACCESS_TOKEN "你的token"
setx WB_USER_ID "你的accountUid"
```

> `access_token` 会过期。失效时脚本报 `登录态失效`（退出码 3），自动降级把客户端拉起来兜底，并弹通知提醒更新。

### 完整配置表

| 键 | 默认 | 说明 |
|---|---|---|
| `mode` | `auto` | `auto` / `token` / `client` |
| `auth.access_token` | `""` | Bearer 令牌，留空即走 client 模式 |
| `auth.user_id` | `""` | 请求头 `X-User-Id` |
| `auth.token_file` | `token.txt` | token 文件路径（相对项目目录） |
| `auth.env_token_var` / `env_user_var` | `WB_ACCESS_TOKEN` / `WB_USER_ID` | 环境变量名 |
| `api.base_url` | `https://copilot.tencent.com` | 接口域名 |
| `api.status_path` | `/billing/meter/checkin-status` | 查询今日是否已签 |
| `api.claim_path` | `/billing/meter/daily-checkin` | 执行签到 |
| `api.timeout_seconds` | `20` | 单次请求超时 |
| `api.verify_tls` | `true` | 是否校验 TLS 证书 |
| `client.exe_path` | `""` | 客户端路径，留空自动探测 |
| `client.process_name` | `WorkBuddy` | 进程名（不含 `.exe`） |
| `client.auto_launch` | `true` | 客户端未运行是否自动启动 |
| `client.focus_window` | `true` | 是否把窗口置前以触发状态刷新 |
| `client.ready_timeout_seconds` | `90` | 等待客户端上线超时 |
| `client.log_dir` | `""` | 签到日志目录，留空自动定位到 `%USERPROFILE%\.workbuddy\logs` |
| `client.log_files` | `["main.log","main.old.log"]` | 参与解析的日志文件 |
| `client.log_tail_bytes` | `8000000` | 每个日志只读尾部 N 字节 |
| `retry.max_attempts` | `3` | 最大尝试次数 |
| `retry.initial_backoff_seconds` | `5` | 首次退避基数 |
| `retry.backoff_multiplier` | `2.0` | 退避倍数 |
| `retry.max_backoff_seconds` | `120` | 退避上限 |
| `logging.log_dir` / `level` / `keep_days` | `logs` / `INFO` / `60` | 日志目录、级别、保留天数 |
| `state.state_dir` | `state` | 幂等状态目录 |
| `notify.enabled` | `true` | 是否弹系统通知 |
| `notify.on_success` / `on_failure` | `false` / `true` | 成功 / 失败时是否通知 |

---

## 签到结果判定

### token 模式（接口返回）

| 条件 | 判定 | 处理 |
|---|---|---|
| HTTP 200 且 `code == 0` | ✅ 成功 | 记录 `credit` / `streak_days` |
| HTTP 200，`code != 0`，msg 含 `already/重复/已签` | ✅ 已签到 | 幂等命中，不报错 |
| HTTP 401 / 403 | ❌ 登录态失效 | 退出码 3，降级拉起客户端 + 弹通知 |
| HTTP 429 / 5xx / 超时 / 网络错误 | ⚠️ 可重试 | 指数退避重试 |
| HTTP 200，`code != 0` 且非"已签"语义 | ❌ 业务失败 | 退出码 4，记录 code / msg |

### client 模式（客户端日志）

解析 `%USERPROFILE%\.workbuddy\logs\main.log`（及滚动备份 `main.old.log`）中的 JSON 行，**只取本地日期等于今天的记录**：

| 日志特征 | 判定 |
|---|---|
| `[Checkin] claimDailyCheckin success` | ✅ 领取成功，取 `credit` / `streak_days` |
| `[Checkin] fetchCheckinStatus success` 且 `today_checked_in: true` | ✅ 服务端确认已签 |
| `[Checkin] refreshStatus -> active {"uiState":"claimed"}` | ✅ 已领取态 |
| 只有 `uiState":"available"` / `today_checked_in: false` | ⚠️ 今日未签，需人工点击 |

> 日志时间戳是 **UTC**，脚本会换算成本地时区再比对日期，跨零点不会误判。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 今日积分已到账（含"已签到"幂等命中） |
| 2 | 客户端已上线，但需要点击签到气泡确认 |
| 3 | 登录态失效，需重新登录 / 更新 token |
| 4 | 网络或服务端异常，重试已耗尽 |
| 5 | 环境问题（找不到客户端 / 配置错误） |

---

## 日志与排错

### 文件布局

```
logs/
├── checkin-YYYY-MM-DD.log   # 人类可读，按天切分
├── checkin.jsonl            # 结构化，每次接口尝试一行
├── notices/YYYY-MM-DD.txt   # 需要人工处理时的提醒
└── _toast.ps1               # 通知脚本临时文件
state/
├── last-run.json            # 幂等状态
└── tasks.json               # 计划任务名清单（供 --discover 按名探测）
```

`checkin.jsonl` 样例：

```json
{"event":"attempt","stage":"daily-checkin","attempt":1,"kind":"retryable","http":502,"msg":"服务端异常 HTTP 502"}
{"event":"result","mode":"token","result":"success","credit":100,"streak_days":2}
```

超过 `logging.keep_days`（默认 60 天）的历史日志在每次运行时自动清理。

### 排错顺序

1. `python checkin.py --discover` —— 先看 7 项体检哪一项带 `[!!]`
2. 看 `logs/checkin-<今天>.log` 的最后一次运行详情
3. 看退出码对应的含义（见上表）
4. `python checkin.py --show-status --verbose` —— 只读诊断，不动数据

---

## 隐私与安全

**脚本如何对待你的凭据：**

- **默认（client 模式）完全不接触凭据**。客户端登录态由 WorkBuddy 自己用 Electron `safeStorage`（Windows DPAPI）加密落盘，脚本既不读取也不解密。
- **token 模式**下 `access_token` 只存在你本地的 `config.json` / `token.txt` / 环境变量里，脚本仅在向 `copilot.tencent.com` 发请求时使用它，**不会写入日志、不会上传到任何第三方**。
- 所有日志只记录状态码、业务 code、积分与连签天数等**非敏感信息**；账号标识（`X-User-Id`）不落盘。
- 网络请求只发往配置的 `api.base_url`（默认官方域名），无任何遥测。

**仓库层面的保护：**

- `.gitignore` 已忽略 `config.json`、`token.txt`、`.env`、`logs/`、`state/`
- 提交的是 `config.example.json`（模板，凭据为空），克隆后自动生成 `config.json`

> ⚠️ **不要把自己的 `config.json` 或 `token.txt` 提交到公开仓库。** 如果不小心提交了，请立即移除文件、改写历史并**重新登录 WorkBuddy 使旧令牌失效**。

---

## 常见问题

**Q：为什么早上 09:00 有时没领到？**
看 `logs/checkin-*.log`。若退出码是 2，说明客户端没有自动签到（签到气泡需要一次点击）——把 `access_token` 填上即可彻底免点击。

**Q：会重复领取 / 领两次吗？**
不会。三层防重：① 本地 `state/last-run.json` 当天已领直接跳过；② token 模式先查 `checkin-status`，`today_checked_in=true` 就不调领取；③ 服务端本身按天幂等。

**Q：断签了怎么办？**
任务里的 `StartWhenAvailable` 会在下次开机补跑，但**跨天无法补**（签到按自然日）。想稳拿连签奖励，建议保证每天至少开机一次，或用 token 模式。

**Q：找不到客户端，`--discover` 报 `[!!]`？**
脚本会按「运行中进程 → 注册表 → 磁盘扫描」自动探测。都失败时，在 `config.json` 里显式指定 `client.exe_path`。

**Q：计划任务没跑？**
确认用户处于登录状态（注销后 `Interactive` 任务不会运行，锁屏可以）。用 `Get-ScheduledTask -TaskName 'WorkBuddy每日积分*'` 查看状态。

**Q：改了 `config.json` 要重装计划任务吗？**
不用。任务只负责调起 `checkin.py --mode auto`，行为完全由 `config.json` 决定。

**Q：支持 macOS / Linux 吗？**
暂不支持。窗口置前与每日定时都依赖 Windows 专属能力（`ctypes.windll`、任务计划程序）。`token` 模式的 HTTP 逻辑本身是跨平台的，欢迎 PR。

---

## 注意事项与免责声明

- 本项目仅供**个人学习与自动化实践**使用。
- 自动化操作自己的账号**可能违反服务条款**。使用前请自行确认并承担风险，建议仅用于自己拥有且已正常登录的设备与账号。
- 请勿用于批量注册、多账号薅取、绕过风控等滥用场景。
- 项目通过 `urllib` 调用客户端**自身使用的公开接口**，不涉及逆向破解鉴权、不绕过任何安全机制；接口路径来自客户端安装包内的明文定义。
- 作者不对因使用本项目导致的账号异常、积分损失或任何间接损失负责。

---

## 许可证

[MIT License](LICENSE) © 2026 Zhao PeiZhi

---

## 附录：技术细节

以下是从 WorkBuddy 客户端 `app.asar` 中直接读到的明文定义，供排错与二次开发参考。

### 官方接口定义

```js
/**
 * 获取每日签到状态
 * API 端点: POST /billing/meter/checkin-status
 */
async getCheckinStatus() {
    const result = await httpService.post("/billing/meter/checkin-status", {});
    if (result?.code === 0 && result?.data) return result.data;
    return null;
}

/**
 * 执行每日签到
 * API 端点: POST /billing/meter/daily-checkin
 */
async claimDailyCheckin() {
    const result = await httpService.post("/billing/meter/daily-checkin", {});
    if (result?.code === 0 && result?.data) return result.data;
    return { code: result?.code ?? -1, msg: result?.msg || "Checkin failed" };
}
```

### 请求与响应

```http
POST /billing/meter/checkin-status HTTP/1.1
Host: copilot.tencent.com
Content-Type: application/json
Authorization: Bearer <access_token>
X-User-Id: <accountUid>

{}
```

状态响应 `data`：

```json
{
  "active": true,
  "today_checked_in": false,
  "streak_days": 1,
  "is_streak_day": false,
  "next_streak_day": 0,
  "today_credit": 100,
  "streak_bonus_days": 0,
  "streak_bonus_credit": 0
}
```

领取响应 `data`：

```json
{ "credit": 100, "streak_days": 2, "is_streak_day": false }
```

### 客户端签到日志

- 路径：`%USERPROFILE%\.workbuddy\logs\main.log`（滚动备份 `main.old.log`）
- 格式：JSON Lines，`timestamp` 为 UTC，`scope = "queue-diag"`，`message = [文本, 状态字典]`

```json
{"timestamp":"2026-09-30T02:56:55.664Z","scope":"queue-diag","message":["[Checkin] handleClaim triggered",{"uiState":"available","streak_days":0,"today_credit":100}]}
{"timestamp":"2026-09-30T02:56:56.233Z","scope":"queue-diag","message":["[Checkin] claimDailyCheckin success",{"costMs":569,"credit":100,"streak_days":1,"is_streak_day":false}]}
{"timestamp":"2026-09-30T03:09:15.780Z","scope":"queue-diag","message":["[Checkin] refreshStatus -> active",{"uiState":"claimed","today_checked_in":true,"streak_days":1}]}
```

### 项目结构

```
.
├── checkin.py               # 主程序（唯一入口）
├── config.example.json      # 配置模板（提交到仓库）
├── config.json              # 本地实际配置（已被 .gitignore）
├── install_task.ps1         # 注册 / 卸载 Windows 计划任务
├── run_checkin.bat          # 手动运行入口（双击即用）
├── requirements.txt         # 空占位（零第三方依赖）
├── LICENSE                  # MIT
└── README.md
```
