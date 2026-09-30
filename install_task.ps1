<#
.SYNOPSIS
    注册 / 卸载 WorkBuddy 每日积分自动领取的 Windows 计划任务。

.DESCRIPTION
    注册两条每日任务（幂等，重复执行不会重复领积分）：
      主任务  默认 10:00   <前缀>-主签到
      补签    默认 21:00   <前缀>-补签
    勾选 “StartWhenAvailable”，因此错过时间点（如当时电脑关机）会在下次开机后自动补跑。

    时间的**唯一真源是 config.json 的 schedule 段**：不传 -PrimaryTime / -RetryTime 时，
    本脚本会自动读取 config.json。因此日常改时间建议直接用：
        python checkin.py --set-time 10:00      (改配置 + 自动同步计划任务)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install_task.ps1                  # 用 config.json 里的时间
    powershell -ExecutionPolicy Bypass -File install_task.ps1 -PrimaryTime 08:30 -RetryTime 20:30
    powershell -ExecutionPolicy Bypass -File install_task.ps1 -RunNow
    powershell -ExecutionPolicy Bypass -File install_task.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$PrimaryTime = "",
    [string]$RetryTime   = "",
    [string]$TaskPrefix  = "",
    [string]$PythonPath  = "",
    [switch]$NoRetry,
    [switch]$RunNow,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$MainPy    = Join-Path $ScriptDir "checkin.py"

# ---------------- 读取 config.json（定时时间的唯一真源） ----------------
$CfgPath = Join-Path $ScriptDir "config.json"
$Cfg = $null
if (Test-Path $CfgPath) {
    try { $Cfg = Get-Content $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $Cfg = $null }
}
$CfgSchedule = if ($Cfg -and $Cfg.schedule) { $Cfg.schedule } else { $null }

if (-not $TaskPrefix) {
    if ($CfgSchedule -and $CfgSchedule.task_prefix) { $TaskPrefix = $CfgSchedule.task_prefix }
    else { $TaskPrefix = "WorkBuddy每日积分" }
}
if (-not $PrimaryTime) {
    if ($CfgSchedule -and $CfgSchedule.primary_time) { $PrimaryTime = [string]$CfgSchedule.primary_time }
    else { $PrimaryTime = "10:00" }
}
if (-not $RetryTime) {
    if ($CfgSchedule -and $CfgSchedule.retry_time) { $RetryTime = [string]$CfgSchedule.retry_time }
    else { $RetryTime = "21:00" }
}

# 补签是否启用：命令行 -NoRetry 优先，否则跟随 config.json
$RetryOn = $true
if ($CfgSchedule -and $CfgSchedule.retry_enabled -eq $false) { $RetryOn = $false }
if ($PSBoundParameters.ContainsKey("NoRetry") -and $NoRetry) { $RetryOn = $false }

function Write-Step($msg) { Write-Host "  $msg" }
function Fail($msg) { Write-Host "  [×] $msg" -ForegroundColor Red; exit 1 }
function Assert-Time($value, $label) {
    if ($value -notmatch '^\s*([01]?\d|2[0-3])\s*[:：]\s*([0-5]?\d)\s*$') {
        Fail "$label 时间格式无效: '$value'（应为 HH:MM，24 小时制，例如 10:00）"
    }
}

Write-Host ""
Write-Host "=== WorkBuddy 每日积分自动领取 · 计划任务 ===" -ForegroundColor Cyan
Write-Host "脚本目录: $ScriptDir"
if (Test-Path $CfgPath) { Write-Host "配置来源: config.json" } else { Write-Host "配置来源: 内置默认值（config.json 尚未生成）" }
Write-Host ""

if (-not (Test-Path $MainPy)) { Fail "找不到 checkin.py，请确认脚本与本文件在同一目录" }
Assert-Time $PrimaryTime "主签到"
if ($RetryOn) { Assert-Time $RetryTime "补签" }

# ---------------- 卸载 ----------------
if ($Remove) {
    $removed = 0
    Get-ScheduledTask -TaskName "$TaskPrefix*" -ErrorAction SilentlyContinue | ForEach-Object {
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
        Write-Step "已删除计划任务: $($_.TaskName)"
        $removed++
    }
    if ($removed -eq 0) { Write-Step "没有找到已注册的任务" }
    $manifest = Join-Path $ScriptDir "state\tasks.json"
    if (Test-Path $manifest) { Remove-Item $manifest -Force; Write-Step "已清理任务清单 state\tasks.json" }
    Write-Host ""
    Write-Host "完成。" -ForegroundColor Green
    exit 0
}

# ---------------- 找 Python ----------------
function Resolve-Python {
    param([string]$Explicit)

    if ($Explicit) {
        if (Test-Path $Explicit) { return (Resolve-Path $Explicit).Path }
        Fail "指定的 PythonPath 不存在: $Explicit"
    }

    $found = @()
    $roots = @(
        (Join-Path $env:USERPROFILE ".workbuddy\binaries\python\versions"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        "C:\Python313", "C:\Python312", "C:\Python311"
    )
    foreach ($root in $roots) {
        if (-not (Test-Path $root)) { continue }
        $found += Get-ChildItem -Path $root -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
                  Select-Object -ExpandProperty FullName
    }
    if ($found.Count -gt 0) {
        # 版本号降序，取最新
        $best = $found | Sort-Object -Descending { ($_ -split 'versions\\|Python')[1] } | Select-Object -First 1
        return $best
    }

    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

$py = Resolve-Python -Explicit $PythonPath
if (-not $py) { Fail "未找到 python.exe，请用 -PythonPath 参数指定，例如 -PythonPath 'C:\Python313\python.exe'" }
Write-Step "Python: $py"

# 后台静默运行用 pythonw（无控制台黑框）
$pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
$runner = if (Test-Path $pyw) { $pyw } else { $py }
Write-Step "任务执行器: $runner"

# 快速自检：确认脚本能跑
try {
    & $py $MainPy --config (Join-Path $ScriptDir "config.json") --show-status *> $null
    Write-Step "checkin.py 自检通过"
} catch {
    Write-Step "警告: checkin.py 自检未通过（不影响注册，可在任务运行时排查）"
}

# ---------------- 注册 ----------------
function New-CheckinTask {
    param([string]$Name, [string]$Time, [string]$Tag)

    $hh, $mm = $Time.Split(":")
    $at = (Get-Date).Date.AddHours([int]$hh).AddMinutes([int]$mm)

    $action = New-ScheduledTaskAction `
        -Execute $runner `
        -Argument ('"{0}" --mode auto --config "{1}"' -f $MainPy, (Join-Path $ScriptDir "config.json")) `
        -WorkingDirectory $ScriptDir

    $trigger = New-ScheduledTaskTrigger -Daily -At $at

    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
        -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

    $principal = New-ScheduledTaskPrincipal `
        -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description "WorkBuddy 每日积分自动领取（$Tag）。幂等：当天已领取会自动跳过。" `
        -Force | Out-Null

    Write-Step "已注册: $Name  ->  每天 $Time"
    return [pscustomobject]@{ name = $Name; time = $Time; tag = $Tag }
}

# C:\Windows\System32\Tasks 未提权不能列举，把任务名登记下来供 checkin.py --discover 按名读取
function Save-TaskManifest($items) {
    $stateDir = Join-Path $ScriptDir "state"
    if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
    $payload = [pscustomobject]@{
        updated_at  = (Get-Date).ToString("s")
        task_prefix = $TaskPrefix
        tasks       = @($items)
    }
    $payload | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $stateDir "tasks.json") -Encoding UTF8
    Write-Step "任务清单已登记到 state\tasks.json"
}

# 若命令行显式传了时间，回写 config.json，避免配置与计划任务不一致
function Save-ScheduleToConfig($primary, $retry, $retryOn, $explicit) {
    if (-not $explicit) { return }
    if (-not (Test-Path $CfgPath)) { Write-Step "config.json 尚不存在，跳过回写"; return }
    try {
        $doc = Get-Content $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $doc.schedule) {
            $doc | Add-Member -NotePropertyName schedule -NotePropertyValue ([pscustomobject]@{}) -Force
        }
        $doc.schedule | Add-Member -NotePropertyName primary_time  -NotePropertyValue $primary -Force
        $doc.schedule | Add-Member -NotePropertyName retry_time    -NotePropertyValue $retry   -Force
        $doc.schedule | Add-Member -NotePropertyName retry_enabled -NotePropertyValue $retryOn -Force

        Copy-Item $CfgPath "$CfgPath.bak" -Force
        $json = $doc | ConvertTo-Json -Depth 10
        # 不写 BOM，否则 Python 侧 json.load 会因 BOM 报错
        [System.IO.File]::WriteAllText($CfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))
        Write-Step "已把时间回写到 config.json（备份: config.json.bak）"
    } catch {
        Write-Step "回写 config.json 失败（不影响计划任务）: $($_.Exception.Message)"
    }
}

$created = @()
$created += New-CheckinTask -Name "$TaskPrefix-主签到" -Time $PrimaryTime -Tag "主签到"
if ($RetryOn) {
    $created += New-CheckinTask -Name "$TaskPrefix-补签" -Time $RetryTime -Tag "补签兜底"
} else {
    Write-Step "补签任务已跳过（config.json 中 schedule.retry_enabled = false）"
    $stale = Join-Path $ScriptDir "state\tasks.json"
    if (Test-Path $stale) {
        $old = (Get-Content $stale -Raw -Encoding UTF8 | ConvertFrom-Json).tasks
        foreach ($t in @($old)) {
            $nm = if ($t -is [string]) { $t } else { $t.name }
            if ($nm -and $nm -like "*-补签") {
                Get-ScheduledTask -TaskName $nm -ErrorAction SilentlyContinue |
                    Unregister-ScheduledTask -Confirm:$false
                Write-Step "已移除不再需要的补签任务: $nm"
            }
        }
    }
}
Save-TaskManifest $created
Save-ScheduleToConfig -primary $PrimaryTime -retry $RetryTime -retryOn $RetryOn -explicit (
    $PSBoundParameters.ContainsKey("PrimaryTime") -or
    $PSBoundParameters.ContainsKey("RetryTime") -or
    $PSBoundParameters.ContainsKey("NoRetry"))

Write-Host ""
Write-Host "完成。已注册的计划任务：" -ForegroundColor Green
Get-ScheduledTask -TaskName "$TaskPrefix*" |
    Select-Object TaskName, State |
    Format-Table -AutoSize | Out-String | Write-Host

Write-Host "常用操作：" -ForegroundColor Cyan
Write-Host "  立即试跑一次 : powershell -ExecutionPolicy Bypass -File install_task.ps1 -RunNow"
Write-Host "  查看任务     : Get-ScheduledTask -TaskName '$TaskPrefix*'"
Write-Host "  手动触发     : Start-ScheduledTask -TaskName '$TaskPrefix-主签到'"
Write-Host "  卸载         : powershell -ExecutionPolicy Bypass -File install_task.ps1 -Remove"
Write-Host ""

if ($RunNow) {
    Write-Host "立即试跑一次..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskName "$TaskPrefix-主签到"
    Start-Sleep -Seconds 3
    Get-ScheduledTaskInfo -TaskName "$TaskPrefix-主签到" |
        Select-Object LastRunTime, LastTaskResult, NextRunTime |
        Format-List | Out-String | Write-Host
}
