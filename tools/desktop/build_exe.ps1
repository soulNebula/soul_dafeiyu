# 构建 SoulAgent Windows 桌面版（PyInstaller 单目录）
# 用法：右键 PowerShell 运行，或在仓库根执行：
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\desktop\build_exe.ps1
$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot

$Py = "C:\Python314\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }

Write-Host "[build] 检查 PyInstaller ..."
& $Py -m PyInstaller --version | Out-Host

$Dist = Join-Path $RepoRoot "tools\desktop\dist"
$Build = Join-Path $RepoRoot "tools\desktop\build"
Remove-Item -Recurse -Force $Dist, $Build -ErrorAction SilentlyContinue

Write-Host "[build] PyInstaller 打包 ..."
& $Py -m PyInstaller `
    --noconfirm --clean --windowed `
    --name "SoulAgentDesktop" `
    --icon (Join-Path $RepoRoot "tools\desktop\icon.ico") `
    --paths $RepoRoot `
    --distpath $Dist `
    --workpath $Build `
    --specpath $Build `
    --hidden-import agi.paths `
    (Join-Path $RepoRoot "tools\desktop\desktop_entry.py")

$Exe = Join-Path $Dist "SoulAgentDesktop\SoulAgentDesktop.exe"
if (-not (Test-Path $Exe)) { throw "构建失败：$Exe 不存在" }
Write-Host "[build] 完成：$Exe"
Write-Host "[build] 双击 exe 即可；要托盘/开机自启用 启动桌面版.bat 或："
Write-Host "        $Exe --tray --autostart install"
