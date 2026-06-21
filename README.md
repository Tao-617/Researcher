# Researcher —— 媒体内容采集工具（原型）

面向内容运营：输入一段自然语言需求 → 自动在多个内容平台**发现候选 → 配置化硬筛选 →
LLM 语义去重 + 评分排序 → 给出最相关 Top N → 标准化结构化输出**，并配一个运营可直接用的网页。

```
prompt ─(LLM 扩展)→ 关键词 ─→ search_all(多平台并发 + cid 精确去重)
      ─→ filters(配置化硬筛, 带原因) ─→ llm_dedup(语义去重)
      ─→ rank_top(逐条评分 + 加权 + LLM 精排) ─→ schema(标准化) ─→ result.json ─→ web
```

> **搜索后端可插拔**：通过 `platforms/` 注册表抽象，默认使用开源的
> [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)（作为 git 子模块接入）。
> 管线核心（query / filter / dedup / rank / schema）零外部包依赖，只 import 自己 + 4 个 pip 包。
> 设计见 [docs/DESIGN.md](./docs/DESIGN.md)，交接见 [docs/HANDOFF.md](./docs/HANDOFF.md)。

---

## 能力 → 实现

| 任务目标 | 实现 |
|---------|------|
| ① prompt → 平台发现候选 | `pipeline/query.py`（LLM 扩展关键词）+ `pipeline/search.py`（多平台并发搜 + 精确去重） |
| ② 候选执行筛选规则（可配置） | `pipeline/filters.py` + `config.yaml`（声明式规则，每条淘汰带原因） |
| ③ LLM 去重 + 排序 Top N | `pipeline/dedup.py`（LLM 语义去重）+ `pipeline/rank.py`（逐条评分 + 加权 + LLM 精排纠偏） |
| ④ 标准化结构化输出 | `pipeline/schema.py` → `runs/<id>/result.json` |
| ⑤ 运营可直接使用 | `server.py`（http.server）+ `web/index.html`（SPA） |

支持平台（经 MediaCrawler）：**小红书 / 抖音 / 快手 / B站 / 微博 / 知乎 / 贴吧**。

---

## 前置要求

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)**（MediaCrawler 用它管理依赖）
- **git**（用于拉取子模块）
- 一个 **LLM API Key**（默认 `GEMINI_API_KEY`，见下）
- 各平台一个**可登录的账号**（首次扫码用，见「登录」一节）

---

## 快速开始

### 1. 克隆（含子模块）

```bash
git clone --recursive https://github.com/Tao-617/Researcher.git
cd Researcher
```

> 忘了 `--recursive`？补一句：`git submodule update --init --recursive`

### 2. 一键安装

装 Researcher 依赖 + MediaCrawler 子模块依赖 + Playwright Chromium，并自动关掉 MediaCrawler 的 CDP 模式。

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```
```bash
# macOS / Linux
bash setup.sh
```

### 3. 配置密钥

```bash
cp .env.example .env      # 然后编辑 .env 填入 GEMINI_API_KEY
```

> ⚠️ `.env` 已被 `.gitignore` 排除，**不会**被提交。请勿把真实密钥写进任何会入库的文件。

### 4. 运行

**A) 网页版（推荐）**

```powershell
# Windows
$env:PYTHONIOENCODING="utf-8"; python server.py
```
```bash
# macOS / Linux
PYTHONIOENCODING=utf-8 python server.py
```

浏览器打开 **http://127.0.0.1:8780** —— 填 prompt、勾平台、调筛选规则/权重、点「开始采集」。

**B) 命令行**

```bash
PYTHONIOENCODING=utf-8 python -m pipeline.run \
    --query "AI 健身 增肌 计划" --platforms xhs --output-dir runs/demo
```
（Windows 同样需先 `$env:PYTHONIOENCODING="utf-8"`，否则中文打印乱码。）

### 5. 登录（首次每平台一次）

第一次采集某个平台时，会**弹出浏览器显示二维码**，用对应 App 扫码登录。登录态由 MediaCrawler
自动持久化（`external/MediaCrawler/browser_data/<平台>_user_data_dir`），**之后免扫码**。

登录成功后，可在 `.env` 设 `MEDIACRAWLER_HEADLESS=1`，让后续采集**后台静默**运行（不再弹窗）。
登录态过期（通常数天～数周）后会再弹一次二维码，重扫即可。

---

## MediaCrawler 相关环境变量（`.env`）

| 变量 | 默认 | 说明 |
|------|------|------|
| `MEDIACRAWLER_HOME` | `external/MediaCrawler` | MediaCrawler 仓库根（缺省用子模块） |
| `MEDIACRAWLER_HEADLESS` | `0` | 无头模式；**首次登录必须 `0`** 以便扫码，登录后可设 `1` |
| `MEDIACRAWLER_GET_COMMENT` | `0` | 是否同时抓一级评论（开了更慢，但能补全评论/作者） |
| `MEDIACRAWLER_LOGIN_TYPE` | `qrcode` | `qrcode` / `phone` / `cookie` |
| `MEDIACRAWLER_TIMEOUT` | `300` | 单次搜索子进程超时秒数 |
| `RESEARCHER_ENABLE_INTERNAL` | 空 | 仅内部：=1 时额外加载内部后端，公开环境留空 |

> **每个平台都要各自登录一次**（不是登录一个就全通）。某些平台（尤其抖音）反爬较强，可能出现
> `ERR_CONNECTION_RESET`/登录超时——属平台风控，非本项目问题，建议先从小红书/B站/知乎等验证。
> 视频内容的评估目前基于**封面帧 + 文案**（不下载视频做逐帧/字幕理解）。

---

## 配置（`config.yaml`）

| 字段 | 说明 |
|------|------|
| `platforms` | 采哪些平台（CLI 用 `--platforms` 覆盖） |
| `max_count_per_query` | 每个 (平台, 关键词) 返回上限 |
| `expand_query` / `expand_count` | 是否用 LLM 把 prompt 扩展成多关键词、扩展数量 |
| `eval_model` | 评估/排序模型（`gemini-flash-lite` 默认 / `qwen` / `or-flash-lite` / `sonnet`） |
| `eval_concurrent` | LLM 评分并发（Gemini 端高并发会掐连接，建议 ≤3） |
| `filters` | 硬筛选规则列表（算子见 `pipeline/filters.py`） |
| `rank.top_n` / `rank.weights` | 取前几 + 相关性/质量/热度权重 |
| `rank.rerank` / `rank.rerank_pool` | 加权粗排后再让 LLM 做 Top-K 精排纠偏；CLI `--no-rerank` 关 |
| `eval_multimodal` / `eval_max_images` | 评分时把**封面/图片发给 LLM**判断视觉（视频取封面帧）；失败自动回退纯文本 |
| `aggregate_by_author` | 额外产出**「候选账号」聚合视图**（按 `user_id` 归并同作者内容，前端有「账号」标签页） |

---

## 目录结构

```
Researcher/
├── pipeline/            ★核心管线（编排在 run.py）
│   ├── query.py         prompt → 关键词（可选 LLM 扩展）
│   ├── search.py        多平台×关键词 并发搜 + (platform,cid) 精确去重
│   ├── filters.py       配置化硬筛选（声明式规则，每条淘汰带原因）
│   ├── dedup.py         LLM 语义去重（跨平台/换皮同选题）
│   ├── rank.py          逐条评分 → 加权 → LLM 精排 Top N
│   ├── schema.py        标准化 result.json（含全量原帖 posts[]）
│   └── run.py           串起来：run_pipeline() / CLI 入口
├── platforms/           平台接入（可插拔后端）
│   ├── registry.py      PlatformDef + register/get_platform
│   ├── _backends.py     容错加载所有可用后端
│   └── mediacrawler.py  ★MediaCrawler 适配（子进程 → JSON → 统一 post 字段）
├── llm/                 llm_helper（叶子）+ providers（OpenAI 兼容，多后端通吃）
├── vendor/              叶子件：tool_result.py / image.py
├── web/index.html       前端 SPA（控制台 + 帖子网格 + 详情弹窗）
├── server.py            后端：http.server + 后台线程 + 轮询（:8780）
├── config.yaml          默认配置（平台 / 筛选规则 / 排序权重 / 精排开关）
├── setup.ps1 / setup.sh 一键安装脚本
├── external/MediaCrawler  git 子模块（开源搜索后端）
├── tests/smoke_search.py 搜索冒烟脚本
├── docs/                DESIGN.md（设计）· HANDOFF.md（交接）
└── runs/                每次 run 输出 result.json（已 gitignore）
```

---

## 架构说明：搜索后端为什么可插拔

`platforms/registry.py` 把「平台」抽象成 `PlatformDef.search_impl`，`_backends.py` 按可用性加载。
`mediacrawler.py` 实现了统一的 `search(keyword) -> posts[]` 契约：以子进程跑一次 MediaCrawler
关键词搜索 → 读它产出的 JSON → 把各平台字段映射成管线认的统一 `post`（title/body_text/
like_count/images/...）。因此**换搜索源不动管线一行**。

---

## 已知边界（原型）

- **登录不可省**：任何需登录的爬虫都做不到「clone 即出数据」——每个使用者需用自己的账号扫码一次。
  这是开源替代服务端 API 的固有代价。
- **采集变重**：每次搜索要起浏览器、按登录态批量爬，比纯 HTTP API 慢；prompt 扩成多关键词即多次爬取。
- **评论/作者**：默认只取搜索摘要；设 `MEDIACRAWLER_GET_COMMENT=1` 可补一级评论（更慢）。
- **LLM 稳定性**：Gemini 端高并发会掐连接，评分并发已限 `eval_concurrent ≤ 3`，并带重试 + fail-open。
- **合规**：MediaCrawler 采用「仅学习研究、禁止商用」许可；本项目仅作技术原型，使用者须遵守目标平台条款与当地法律。

更多细节见 [docs/DESIGN.md](./docs/DESIGN.md) 与 [docs/HANDOFF.md](./docs/HANDOFF.md)。
