#!/usr/bin/env bash
# Researcher 一键安装（macOS / Linux）
# 作用：装 Researcher 依赖 → 拉 MediaCrawler 子模块 → uv sync + 装 Chromium → 关 CDP 模式
# 用法：  bash setup.sh
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少依赖：$1（uv: https://docs.astral.sh/uv/ ）"; exit 1; }; }
need git; need uv; need python3

echo "==> [1/4] 安装 Researcher 依赖"
python3 -m pip install -r "$root/requirements.txt"

echo "==> [2/4] 拉取 MediaCrawler 子模块"
git -C "$root" submodule update --init --recursive

mc="$root/external/MediaCrawler"

echo "==> [3/4] 安装 MediaCrawler 依赖 + Playwright Chromium"
( cd "$mc" && uv sync && uv run playwright install chromium )

echo "==> [4/4] 关闭 MediaCrawler CDP 模式（改用标准 Playwright 自启浏览器）"
sed -i.bak 's/ENABLE_CDP_MODE = True/ENABLE_CDP_MODE = False/' "$mc/config/base_config.py"
rm -f "$mc/config/base_config.py.bak"

cat <<'EOF'

✅ 安装完成。后续：
   1) cp .env.example .env  并填入 GEMINI_API_KEY
   2) PYTHONIOENCODING=utf-8 python server.py   打开 http://127.0.0.1:8780
   3) 首次每个平台会弹浏览器扫码登录一次，登录态自动保存，之后免扫码
EOF
