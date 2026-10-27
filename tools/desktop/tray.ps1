# Soul Companion 托盘（P7-d）
# 零 pip 依赖：PowerShell 5.1 + .NET WinForms NotifyIcon。
# 启动方式（通常由 launcher 拉起）：
#   powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File tools\desktop\tray.ps1
# 单实例：命名 Mutex；重复启动直接退出。
# 通知：定时调用 `python launcher.py --notify-once`，本脚本只负责把返回的
#       JSON 显示成 balloon（通知规则/去重/夜间策略都在 Python 纯函数里）。
param(
    [string]$Python = "",
    [string]$RepoRoot = "",
    [switch]$NoNotify
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$script:RepoRoot = if ($RepoRoot) { $RepoRoot } else {
    Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
}
$script:ConsoleUrl = "http://127.0.0.1:802/"
$script:StatusUrl = "http://127.0.0.1:802/api/status"
$script:ConfigUrl = "http://127.0.0.1:802/api/config"
$script:StopUrl = "http://127.0.0.1:802/api/proactive/stop"
$script:ResumeUrl = "http://127.0.0.1:802/api/proactive/resume"
$script:ConfigPath = Join-Path $script:RepoRoot "agi\data\wechat\config.json"
$script:Launcher = Join-Path $script:RepoRoot "tools\desktop\launcher.py"
$script:IconPath = Join-Path $script:RepoRoot "tools\desktop\icon.ico"
$script:LogDirHints = @(
    $(if ($env:AGENT_HOME) { Join-Path $env:AGENT_HOME "logs" }),
    (Join-Path $script:RepoRoot "agi\data\desktop_logs")
)
$script:NotifyIcon = $null
$script:Timer = $null
$script:PythonExe = $Python

function Get-PythonExe {
    if ($script:PythonExe) { return $script:PythonExe }
    $preferred = "C:\Python314\python.exe"
    if (Test-Path $preferred) { return $preferred }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return ""
}

function Get-AdminToken {
    try {
        $raw = Get-Content -Path $script:ConfigPath -Raw -Encoding UTF8
        $cfg = $raw | ConvertFrom-Json
        if ($cfg.admin_token) { return [string]$cfg.admin_token }
    } catch {
        # 配置读不到时写操作会 401，交给界面提示
    }
    return ""
}

function Show-Balloon {
    param([string]$Title, [string]$Text, [string]$Level = "Info")
    if ($null -eq $script:NotifyIcon) { return }
    try {
        $script:NotifyIcon.BalloonTipTitle = $Title
        $script:NotifyIcon.BalloonTipText = $Text
        $icon = [System.Windows.Forms.ToolTipIcon]::Info
        if ($Level -eq "Warning") { $icon = [System.Windows.Forms.ToolTipIcon]::Warning }
        if ($Level -eq "Error") { $icon = [System.Windows.Forms.ToolTipIcon]::Error }
        $script:NotifyIcon.BalloonTipIcon = $icon
        $script:NotifyIcon.ShowBalloonTip(4000)
    } catch {
    }
}

function Open-Console {
    try {
        Start-Process $script:ConsoleUrl
    } catch {
        Show-Balloon "打开控制台失败" $_.Exception.Message "Error"
    }
}

function Invoke-ConsoleJson {
    param([string]$Url, [string]$Method = "GET", $Body = $null)
    $headers = @{}
    $token = Get-AdminToken
    if ($token) { $headers["X-Admin-Token"] = $token }
    if ($Method -eq "POST") {
        $json = if ($null -ne $Body) { ($Body | ConvertTo-Json -Compress) } else { "{}" }
        return Invoke-RestMethod -Method Post -Uri $Url -Headers $headers `
            -ContentType "application/json; charset=utf-8" -Body $json -TimeoutSec 8
    }
    return Invoke-RestMethod -Method Get -Uri $Url -Headers $headers -TimeoutSec 8
}

function Set-Proactive {
    param([bool]$Enabled)
    try {
        Invoke-ConsoleJson -Url $script:ConfigUrl -Method "POST" `
            -Body @{ proactive_enabled = $Enabled } | Out-Null
        if ($Enabled) {
            Show-Balloon "主动消息已开启" "网关会在低频节奏下主动发消息" "Info"
        } else {
            Show-Balloon "主动消息已停止" "不会主动发消息（被动回复不受影响）" "Info"
        }
    } catch {
        Show-Balloon "操作失败" ("控制台未启动或 token 不对：" + $_.Exception.Message) "Error"
    }
}

function Set-Stop {
    param([bool]$Stopped)
    $url = if ($Stopped) { $script:StopUrl } else { $script:ResumeUrl }
    try {
        Invoke-ConsoleJson -Url $url -Method "POST" -Body @{} | Out-Null
        if ($Stopped) {
            Show-Balloon "已紧急停止" "STOP 文件已创建，所有外发被拦" "Warning"
        } else {
            Show-Balloon "已恢复" "STOP 文件已删除，发送恢复" "Info"
        }
    } catch {
        Show-Balloon "操作失败" ("控制台未启动或 token 不对：" + $_.Exception.Message) "Error"
    }
}

function Show-Status {
    try {
        $resp = Invoke-ConsoleJson -Url $script:StatusUrl
        $st = $resp.status
        $proactive = if ($null -ne $st.proactive) { $st.proactive } else { $null }
        $enabled = if ($null -ne $proactive) { [bool]$proactive.enabled } else { $false }
        $stopped = if ($null -ne $proactive) { [bool]$proactive.stopped } else { $false }
        $inner = if ($null -ne $st.inner) { [string]$st.inner.mode } else { "off" }
        $sent = if ($null -ne $st.sent) { [int]$st.sent } else { 0 }
        $lines = @(
            ("主动消息：" + $(if ($enabled) { "开" } else { "关" })),
            ("STOP：" + $(if ($stopped) { "已停止" } else { "正常" })),
            ("内状态：" + $inner),
            ("已发送：" + $sent)
        )
        Show-Balloon "Soul Companion 状态" ($lines -join "`n") "Info"
    } catch {
        Show-Balloon "状态获取失败" ("控制台未启动？" + $_.Exception.Message) "Warning"
    }
}

function Open-LogDir {
    foreach ($dir in $script:LogDirHints) {
        if (Test-Path $dir) {
            Start-Process explorer.exe $dir
            return
        }
    }
    Start-Process explorer.exe $script:RepoRoot
}

function Invoke-NotifyPoll {
    try {
        $python = Get-PythonExe
        if (-not $python -or -not (Test-Path $script:Launcher)) { return }
        $raw = & $python $script:Launcher --notify-once 2>$null
        if (-not $raw) { return }
        $payload = ($raw | ConvertFrom-Json)
        foreach ($item in @($payload.notifications)) {
            if ($null -eq $item) { continue }
            $level = "Info"
            if ($item.level -eq "warn") { $level = "Warning" }
            if ($item.level -eq "error") { $level = "Error" }
            Show-Balloon ([string]$item.title) ([string]$item.body) $level
        }
    } catch {
        # 通知轮询失败静默（不要用托盘报错刷屏）
    }
}

# ---------------------------------------------------------------- 单实例 Mutex
$mutex = New-Object System.Threading.Mutex($false, "Local\SoulCompanionTray")
$created = $false
try {
    $created = $mutex.WaitOne(0, $false)
} catch [System.Threading.AbandonedMutexException] {
    $created = $true
}
if (-not $created) {
    exit 0            # 已有托盘在跑
}

# ---------------------------------------------------------------- 托盘图标/菜单
$script:NotifyIcon = New-Object System.Windows.Forms.NotifyIcon
if (Test-Path $script:IconPath) {
    try {
        $script:NotifyIcon.Icon = New-Object System.Drawing.Icon($script:IconPath)
    } catch {
        $script:NotifyIcon.Icon = [System.Drawing.SystemIcons]::Application
    }
} else {
    $script:NotifyIcon.Icon = [System.Drawing.SystemIcons]::Application
}
$script:NotifyIcon.Text = "Soul Companion（打开控制台/主动消息/STOP）"
$script:NotifyIcon.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$itemOpen = New-Object System.Windows.Forms.ToolStripMenuItem("打开控制台")
$itemOpen.add_Click({ Open-Console })
[void]$menu.Items.Add($itemOpen)

[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$itemStart = New-Object System.Windows.Forms.ToolStripMenuItem("启动主动消息")
$itemStart.add_Click({ Set-Proactive -Enabled $true })
[void]$menu.Items.Add($itemStart)

$itemStopProactive = New-Object System.Windows.Forms.ToolStripMenuItem("停止主动消息")
$itemStopProactive.add_Click({ Set-Proactive -Enabled $false })
[void]$menu.Items.Add($itemStopProactive)

[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$itemStop = New-Object System.Windows.Forms.ToolStripMenuItem("紧急停止 STOP")
$itemStop.add_Click({ Set-Stop -Stopped $true })
[void]$menu.Items.Add($itemStop)

$itemResume = New-Object System.Windows.Forms.ToolStripMenuItem("恢复")
$itemResume.add_Click({ Set-Stop -Stopped $false })
[void]$menu.Items.Add($itemResume)

[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$itemStatus = New-Object System.Windows.Forms.ToolStripMenuItem("查看状态")
$itemStatus.add_Click({ Show-Status })
[void]$menu.Items.Add($itemStatus)

$itemLogs = New-Object System.Windows.Forms.ToolStripMenuItem("打开日志目录")
$itemLogs.add_Click({ Open-LogDir })
[void]$menu.Items.Add($itemLogs)

[void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))

$itemExit = New-Object System.Windows.Forms.ToolStripMenuItem("退出托盘")
$itemExit.add_Click({
        try { $script:NotifyIcon.Visible = $false } catch { }
        [System.Windows.Forms.Application]::Exit()
    })
[void]$menu.Items.Add($itemExit)

$script:NotifyIcon.ContextMenuStrip = $menu
$script:NotifyIcon.add_MouseDoubleClick({ Open-Console })

# ---------------------------------------------------------------- 通知轮询
if (-not $NoNotify) {
    $script:Timer = New-Object System.Windows.Forms.Timer
    $script:Timer.Interval = 20000      # 20s 轮询一次 gateway.log
    $script:Timer.add_Tick({ Invoke-NotifyPoll })
    $script:Timer.Start()
    # 启动后先安静地轮询一次（只记录 offset，不回放历史；有事件则弹）
    Invoke-NotifyPoll
}

$context = New-Object System.Windows.Forms.ApplicationContext
try {
    [System.Windows.Forms.Application]::Run($context)
} finally {
    if ($null -ne $script:Timer) { try { $script:Timer.Stop() } catch { } }
    if ($null -ne $script:NotifyIcon) {
        try { $script:NotifyIcon.Visible = $false } catch { }
        try { $script:NotifyIcon.Dispose() } catch { }
    }
    try { $mutex.ReleaseMutex() } catch { }
    try { $mutex.Dispose() } catch { }
}
