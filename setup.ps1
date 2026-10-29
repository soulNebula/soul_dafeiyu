# 小鱼（SoulAgent）开源一键环境脚本（Windows）
#
# 用法（在仓库根目录右键 PowerShell 运行）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1
#   powershell ... -File .\setup.ps1 -CheckOnly        # 只检查，不安装/不启动
#   powershell ... -File .\setup.ps1 -NoOpen           # 装完不打开浏览器
#   powershell ... -File .\setup.ps1 -SkipSender       # 不装微信发送器（只做 803/802）
#   powershell ... -File .\setup.ps1 -Mirror ""        # 不用国内镜像（默认清华）
#
# 它做什么：
#   1) 检查 Windows / Python(>=3.12) / 微信 / 目录；缺 Python 时尝试 winget 或国内镜像自动安装
#   2) 建主 venv + idea venv + 发送器 venv，用国内源装依赖
#   3) 创建 AGENT_HOME 下模型/数据目录；启动 803 工作台并打开浏览器
#
# 注意：不写任何 API key；key 在 803 创建 Bot 时配置（或稍后 802 模型配置页）。
param(
    [string]$AgentHome = "",
    [switch]$CheckOnly,
    [switch]$NoOpen,
    [switch]$SkipSender,
    [string]$Mirror = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$WorkbenchPort = "803"
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
if (-not $Root) { $Root = (Get-Location).Path }
Set-Location $Root

$PyMirrorArg = @()
if ($Mirror) { $PyMirrorArg = @("-i", $Mirror) }

function Info([string]$Msg) { Write-Host "[setup] $Msg" -ForegroundColor Cyan }
function Warn([string]$Msg) { Write-Host "[setup] WARN: $Msg" -ForegroundColor Yellow }
function Fail([string]$Msg) { Write-Host "[setup] ERROR: $Msg" -ForegroundColor Red }

function Test-Python([string]$Exe) {
    try {
        $out = & $Exe -c "import sys; print(sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        return ($out -match "\(3, 1[2-9]\)" -or $out -match "\(3, [2-9][0-9]\)")
    } catch { return $false }
}

function Find-Python {
    $candidates = @()
    if ($env:SOULAGENT_PYTHON) { $candidates += $env:SOULAGENT_PYTHON }
    $candidates += @(
        "C:\Python314\python.exe",
        "C:\Python312\python.exe"
    )
    foreach ($cand in $candidates) {
        if ($cand -and (Test-Path $cand) -and (Test-Python $cand)) { return $cand }
    }
    foreach ($cmd in @("python", "py")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        if ($cmd -eq "py") {
            try {
                $ver = & py -3.12 -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $ver) { return $ver.Trim() }
            } catch { }
        } elseif (Test-Python $found.Source) { return $found.Source }
    }
    return ""
}

function Install-Python {
    Info "未找到 Python 3.12+，尝试自动安装…"
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Info "使用 winget 安装 Python 3.12（国内网络可能较慢）…"
        & winget install -e --id Python.Python.3.12 --silent `
            --accept-package-agreements --accept-source-agreements
        $py = Find-Python
        if ($py) { return $py }
    }
    $urls = @(
        "https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe",
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
    )
    $installer = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
    foreach ($url in $urls) {
        try {
            Info "下载 Python 安装包：$url"
            Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
            if (Test-Path $installer) { break }
        } catch { Warn "下载失败：$url" }
    }
    if (-not (Test-Path $installer)) { return "" }
    Info "静默安装 Python 3.12（当前用户）…"
    Start-Process -FilePath $installer -Wait -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1", "Include_launcher=1"
    )
    return (Find-Python)
}

function Ensure-Venv([string]$VenvDir, [string]$PythonExe) {
    $venvPy = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {
        Info "创建 venv：$VenvDir"
        & $PythonExe -m venv $VenvDir
    }
    if (-not (Test-Path $venvPy)) { throw "venv 创建失败：$VenvDir" }
    return $venvPy
}

function Pip-Install([string]$VenvPy, [string[]]$Args2) {
    & $VenvPy -m pip install --disable-pip-version-check --upgrade pip @PyMirrorArg | Out-Null
    & $VenvPy -m pip install --disable-pip-version-check @Args2 @PyMirrorArg
    if ($LASTEXITCODE -ne 0) { throw "pip install 失败：$($Args2 -join ' ')" }
}

# ---------------- 1. 环境检查 ----------------
Info "仓库：$Root"
if (-not ($IsWindows -or $env:OS -match "Windows")) { Fail "本脚本只支持 Windows 10/11"; exit 1 }
Info "Windows：$([System.Environment]::OSVersion.Version)"

if (-not $AgentHome) {
    if ($env:AGENT_HOME) { $AgentHome = $env:AGENT_HOME } else { $AgentHome = "E:\agent" }
}
$env:AGENT_HOME = $AgentHome
Info "AGENT_HOME：$AgentHome（模型/数据/日志放这里；可用 -AgentHome 改）"

$python = Find-Python
if (-not $python) {
    if ($CheckOnly) { Fail "没有 Python 3.12+（CheckOnly 不安装）"; exit 1 }
    $python = Install-Python
}
if (-not $python) { Fail "Python 3.12+ 未安装成功，请手动安装后重跑"; exit 1 }
Info "Python：$python"

$wechat = Get-Process -Name "Weixin", "WeChat" -ErrorAction SilentlyContinue
if ($wechat) { Info "微信进程：运行中" }
else { Warn "没有检测到微信进程；配置收发前请先登录微信 PC 客户端" }

# 可选 B站插件（仓库外；缺失仅降级 B站能力）
$biliHome = if ($env:BILI_DUTY_HOME) { $env:BILI_DUTY_HOME } else { Join-Path $HOME ".dsh/bili-duty" }
if (Test-Path (Join-Path $biliHome "config.json")) { Info "B站插件：$biliHome" }
else { Warn "未找到 B站插件（可选）：$biliHome；安装见 docs/guides/B站插件安装.md" }

$senderVenv = Join-Path $AgentHome "venv_wx312"
$workbench = Join-Path $Root "idea"
$mainVenv = Join-Path $Root ".venv"
$ideaVenv = Join-Path $workbench ".venv"

if ($CheckOnly) {
    Info "== 检查结果 =="
    Info "Python：$python"
    Info "AGENT_HOME：$AgentHome"
    Info "主 venv：$(if (Test-Path (Join-Path $mainVenv 'Scripts\python.exe')) {'已存在'} else {'缺失'})"
    Info "803 venv：$(if (Test-Path (Join-Path $ideaVenv 'Scripts\python.exe')) {'已存在'} else {'缺失'})"
    Info "发送器 venv：$(if (Test-Path (Join-Path $senderVenv 'Scripts\python.exe')) {'已存在'} else {'缺失'})"
    Info "微信：$(if ($wechat) {'运行中'} else {'未运行'})"
    exit 0
}

# ---------------- 2. 目录 ----------------
foreach ($dir in @($AgentHome, (Join-Path $AgentHome "models"), (Join-Path $AgentHome "datasets"),
                   (Join-Path $AgentHome "output"), (Join-Path $AgentHome "logs"),
                   (Join-Path $Root "agi\data"))) {
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
}

# ---------------- 3. 主 venv ----------------
$mainPy = Ensure-Venv $mainVenv $python
Info "安装主进程依赖（国内源：$Mirror）…"
Pip-Install $mainPy @("-r", (Join-Path $Root "requirements.txt"))

# ---------------- 4. 803 venv ----------------
$ideaPy = Ensure-Venv $ideaVenv $python
Info "安装 803 工作台依赖…"
Pip-Install $ideaPy @("-r", (Join-Path $workbench "requirements.txt"))

# ---------------- 5. 发送器 venv ----------------
if (-not $SkipSender) {
    $senderPy = Ensure-Venv $senderVenv $python
    Info "安装微信发送器 wechatauto（失败不影响 803，但 802 发消息会不可用）…"
    try {
        Pip-Install $senderPy @("wechatauto")
    } catch {
        Warn "pip 安装 wechatauto 失败，尝试从源码安装…"
        $vendor = Join-Path $AgentHome "vendor\wechatauto-replica"
        if (-not (Test-Path $vendor)) {
            try {
                git clone --depth 1 https://github.com/fanyuantaier/wechatauto-replica.git $vendor
            } catch {
                Warn "源码 clone 失败（需要 git/网络）：$vendor"
            }
        }
        if (Test-Path $vendor) {
            Pip-Install $senderPy @("-e", $vendor)
        } else {
            Warn "发送器未安装：请手动装 wechatauto，并在 config.json 配 wxauto_python"
        }
    }
}

# ---------------- 6. 启动 803 ----------------
$listening = Get-NetTCPConnection -LocalPort ([int]$WorkbenchPort) -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Info "803 已在运行（端口 $WorkbenchPort）"
} else {
    Info "启动 803 工作台…"
    $log = Join-Path $AgentHome "logs\workbench_setup.log"
    Start-Process -FilePath $ideaPy -WindowStyle Hidden -ArgumentList @(
        "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", $WorkbenchPort
    ) -WorkingDirectory $workbench -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    Start-Sleep -Seconds 5
}

$url = "http://127.0.0.1:$WorkbenchPort/"
Info "环境就绪：打开 $url"
if (-not $NoOpen) { Start-Process $url }
Info "下一步：在 803「Bot 管理」创建 Bot（云端方案，填 DeepSeek/OpenAI 兼容 key）→ 打开 802"
