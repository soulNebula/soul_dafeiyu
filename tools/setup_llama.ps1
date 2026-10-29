# 小鱼 · llama.cpp 一键安装（Windows）
#
# 用法（PowerShell）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\setup_llama.ps1 -CheckOnly
#   powershell ... -File .\tools\setup_llama.ps1
#   powershell ... -File .\tools\setup_llama.ps1 -CpuFallback     # 无 GPU 也装 CPU 版
#   powershell ... -File .\tools\setup_llama.ps1 -AssetUrl <zip> -CudartUrl <zip>
#
# 做什么：
#   - 检测 NVIDIA GPU / 显存；无 GPU 且未 -CpuFallback 时提示走云端
#   - 从 GitHub llama.cpp release 下载 Windows CUDA（或 CPU）预编译包 + cudart
#   - 解压到 <AGENT_HOME>\llama\cuda（CPU 版放 llama\cpu）
#   - 写 install.json；不会自动启动模型，也不会下载 GGUF
#
# 国内网络：GitHub 慢时用 -AssetUrl / -CudartUrl 指到镜像直链（文件需同名 zip）。
param(
    [string]$AgentHome = "",
    [switch]$CheckOnly,
    [switch]$CpuFallback,
    [switch]$Force,
    [string]$Version = "latest",
    [string]$AssetUrl = "",
    [string]$CudartUrl = ""
)

$ErrorActionPreference = "Stop"
function Info([string]$m) { Write-Host "[llama] $m" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "[llama] WARN: $m" -ForegroundColor Yellow }
function Fail([string]$m) { Write-Host "[llama] ERROR: $m" -ForegroundColor Red }

if (-not $AgentHome) {
    if ($env:AGENT_HOME) { $AgentHome = $env:AGENT_HOME } else { $AgentHome = "E:\agent" }
}
$env:AGENT_HOME = $AgentHome
$gpu = $null
try {
    $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
    if ($smi) {
        $out = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { $gpu = ($out | Select-Object -First 1) }
    }
} catch { $gpu = $null }

$useCpu = $false
if (-not $gpu) {
    if ($CpuFallback) { $useCpu = $true; Warn "未检测到 NVIDIA GPU，按 -CpuFallback 安装 CPU 版" }
    else { Fail "未检测到 NVIDIA GPU；按决策应走云端方案（或加 -CpuFallback 装 CPU 版）"; exit 2 }
} else {
    Info "GPU：$gpu"
}

$target = Join-Path $AgentHome $(if ($useCpu) { "llama\cpu" } else { "llama\cuda" })
$server = Join-Path $target "llama-server.exe"
if ((Test-Path $server) -and (-not $Force)) {
    Info "已安装：$server（-Force 可重装）"
    if ($CheckOnly) { exit 0 }
    exit 0
}
if ($CheckOnly) {
    Info "目标目录：$target"
    Info "模式：$(if ($useCpu) {'CPU'} else {'CUDA'})；版本：$Version"
    Info "未安装（CheckOnly 不下载）"
    exit 0
}

function Get-Assets([string]$tag) {
    $api = if ($tag -eq "latest") {
        "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"
    } else {
        "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/$tag"
    }
    $release = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "soulagent-setup" }
    return @($release.assets)
}

try {
    $assets = Get-Assets $Version
} catch {
    if ($AssetUrl) {
        $assets = @()
    } else {
        Fail "拉取 llama.cpp release 失败：$($_.Exception.Message)"
        exit 1
    }
}

$wanted = @()
if ($AssetUrl) {
    $wanted += @{ name = "llama.zip"; url = $AssetUrl }
    if ($CudartUrl -and -not $useCpu) { $wanted += @{ name = "cudart.zip"; url = $CudartUrl } }
} else {
    foreach ($a in $assets) {
        $n = [string]$a.name
        if ($useCpu -and $n -match '^llama-.*bin-win-cpu-x64\.zip$') { $wanted += @{ name = $n; url = $a.browser_download_url } }
        if ((-not $useCpu) -and $n -match '^llama-.*bin-win-cuda-.*\.zip$') { $wanted += @{ name = $n; url = $a.browser_download_url } }
        if ((-not $useCpu) -and $n -match '^cudart-llama-bin-win-cuda-.*\.zip$') { $wanted += @{ name = $n; url = $a.browser_download_url } }
    }
}
if (-not $wanted.Count) { Fail "release 中没有找到 Windows $(if ($useCpu) {'CPU'} else {'CUDA'}) 资产"; exit 1 }

$tmp = Join-Path $env:TEMP ("soulagent_llama_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    foreach ($item in $wanted) {
        $zip = Join-Path $tmp $item.name
        Info "下载 $($item.name)"
        Invoke-WebRequest -Uri $item.url -OutFile $zip -UseBasicParsing
        if (-not (Test-Path $zip)) { throw "下载失败：$($item.url)" }
        $extract = Join-Path $tmp ("x_" + [guid]::NewGuid().ToString("N"))
        Expand-Archive -Path $zip -DestinationPath $extract -Force
        Get-ChildItem -Path $extract -Recurse -File | ForEach-Object {
            $dest = Join-Path $target $_.Name
            Copy-Item $_.FullName $dest -Force
        }
    }
    $versionFile = Join-Path $target "install.json"
    @{
        installed_at = (Get-Date).ToString("s")
        mode = $(if ($useCpu) { "cpu" } else { "cuda" })
        gpu = $gpu
        version = $Version
        source = $wanted
    } | ConvertTo-Json -Depth 4 | Set-Content -Path $versionFile -Encoding UTF8
    Info "安装完成：$server"
    Info "下一步：用模型中心/脚本下载 GGUF；启动示例："
    Info "  `"$server`" -m <model.gguf> --host 127.0.0.1 --port 8125 -c 8192 -ngl 99 --jinja"
} catch {
    Fail "安装失败：$($_.Exception.Message)"
    exit 1
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
