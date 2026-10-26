# Soul Companion 开机自启（P7-d）
# 只动用户级：默认在「启动」目录放一个 bat（不需要管理员）；
# -UseRegistry 则写 HKCU\Software\Microsoft\Windows\CurrentVersion\Run。
# 真正的实现与状态都在 python launcher.py --autostart ... 里（便于单测/复用）。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File tools\desktop\autostart.ps1 install
#   powershell -ExecutionPolicy Bypass -File tools\desktop\autostart.ps1 install -UseRegistry
#   powershell -ExecutionPolicy Bypass -File tools\desktop\autostart.ps1 status
#   powershell -ExecutionPolicy Bypass -File tools\desktop\autostart.ps1 uninstall
param(
    [Parameter(Position = 0)]
    [ValidateSet("install", "uninstall", "status")]
    [string]$Action = "status",
    [switch]$UseRegistry,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$launcher = Join-Path $repo "tools\desktop\launcher.py"

if (-not $Python) {
    $preferred = "C:\Python314\python.exe"
    if (Test-Path $preferred) {
        $Python = $preferred
    } else {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $Python = $cmd.Source }
    }
}
if (-not $Python -or -not (Test-Path $Python)) {
    Write-Host "[错误] 没有找到 Python：请安装 Python 3.10+ 或在 PATH 中提供 python。" -ForegroundColor Red
    exit 1
}

$argv = @($launcher, "--autostart", $Action)
if ($UseRegistry) { $argv += "--autostart-registry" }

& $Python @argv
exit $LASTEXITCODE
