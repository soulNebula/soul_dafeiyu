#!/usr/bin/env pwsh
# llama-server 启动脚本（JSON schema 常驻约束 + torch CUDA DLL 路径）
#
# 用法（在仓库根目录）:
#   pwsh -File tools/soul_serve/start_server.ps1 -Model E:\agent\output\gguf\soul_qwen25_05b_v1-q4_k_m.gguf
#   pwsh -File tools/soul_serve/start_server.ps1 -Model <gguf> -Port 8124 -Cpu   # 不占 GPU 的调试
#
# 关键点（踩坑）：
# - PATH 必须包含 E:\agent\venv312\Lib\site-packages\torch\lib，否则 ggml-cuda.dll 加载失败；
# - 必须带 --jinja（用 GGUF 内自带 chat template 渲染 messages）；
# - JSON schema 约束默认走**请求级** response_format（b11857 的服务端 -jf 在 Qwen jinja
#   template 下会 400：Unexpected empty grammar stack after accepting piece: <|im_start|>）；
#   修复后可用 -ServerSchema 切回服务端常驻约束；
# - -ngl 99 把全部层放 GPU；训练占卡时用 -Cpu 走 CPU（-ngl 0）。

param(
    [Parameter(Mandatory = $true)][string]$Model,
    [int]$Port = 8123,
    [int]$Ctx = 2048,
    [int]$Ngl = 99,
    [string]$HostAddress = "127.0.0.1",
    [switch]$Cpu,
    [switch]$ServerSchema,
    [string]$Schema = ""
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if ([string]::IsNullOrWhiteSpace($Schema)) {
    $Schema = Join-Path $repo "agi\soul\inner_state_schema.json"
}
$llamaBin = "E:\agent\llama\cuda"
$torchLib = "E:\agent\venv312\Lib\site-packages\torch\lib"
$exe = Join-Path $llamaBin "llama-server.exe"

if (-not (Test-Path $exe)) { throw "找不到 llama-server.exe: $exe" }
if (-not (Test-Path $Model)) { throw "找不到 GGUF 模型: $Model" }
if (-not (Test-Path $Schema)) { throw "找不到 JSON schema: $Schema" }

$env:PATH = "$torchLib;$env:PATH"
if ($Cpu) { $Ngl = 0 }

Write-Host "[soul_serve] llama-server model=$Model port=$Port ngl=$Ngl ctx=$Ctx schema=$Schema"
Write-Host "[soul_serve] PATH += $torchLib"

$serverArgs = @(
    "-m", $Model,
    "-ngl", "$Ngl",
    "--host", $HostAddress,
    "--port", "$Port",
    "-c", "$Ctx",
    "-np", "1",
    "--jinja"
)
if ($ServerSchema) { $serverArgs += @("--json-schema-file", $Schema) }

& $exe @serverArgs
