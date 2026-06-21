# Researcher 一键安装（Windows / PowerShell）
# 作用：装 Researcher 依赖 → 拉 MediaCrawler 子模块 → uv sync + 装 Chromium → 关 CDP 模式
# 用法：  powershell -ExecutionPolicy Bypass -File .\setup.ps1

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

function Need($name) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        throw "缺少依赖：$name。请先安装后重试（uv: https://docs.astral.sh/uv/ ）。"
    }
}
Need git; Need uv; Need python

Write-Host "==> [1/4] 安装 Researcher 依赖" -ForegroundColor Cyan
python -m pip install -r "$root\requirements.txt"

Write-Host "==> [2/4] 拉取 MediaCrawler 子模块" -ForegroundColor Cyan
git -C $root submodule update --init --recursive

$mc = Join-Path $root "external\MediaCrawler"

Write-Host "==> [3/4] 安装 MediaCrawler 依赖 + Playwright Chromium" -ForegroundColor Cyan
Push-Location $mc
try {
    uv sync
    uv run playwright install chromium
} finally {
    Pop-Location
}

Write-Host "==> [4/4] 关闭 MediaCrawler CDP 模式（改用标准 Playwright 自启浏览器）" -ForegroundColor Cyan
$cfg = Join-Path $mc "config\base_config.py"
(Get-Content $cfg -Raw) -replace 'ENABLE_CDP_MODE = True', 'ENABLE_CDP_MODE = False' |
    Set-Content $cfg -Encoding utf8

Write-Host ""
Write-Host "✅ 安装完成。后续：" -ForegroundColor Green
Write-Host "   1) copy .env.example .env  并填入 GEMINI_API_KEY"
Write-Host "   2) `$env:PYTHONIOENCODING='utf-8'; python server.py  打开 http://127.0.0.1:8780"
Write-Host "   3) 首次每个平台会弹浏览器扫码登录一次，登录态自动保存，之后免扫码"
