# Researcher —— 媒体内容抓取工具 设计文档

> 选题 1 · 面向内容运营团队的「关键词发现 → 筛选 → 去重排序 → 结构化输出」采集模块原型。
> 本文档只做**分析与设计**，不含实现。代码落在本仓库根目录下，**自依赖**（所需代码复制进来，不再 `import agent.*` / `import examples.*`）。

---

## 1. 需求 → 能力 → 复用 映射

图中 5 条任务目标，逐条拆到模块，并标注「能复用 / 需新写」：

| # | 任务目标 | 落到模块 | 复用来源 | 状态 |
|---|---------|---------|---------|------|
| 1 | 运营输入查询 prompt（自然语言）→ 平台发现候选账号/内容 | `query.py`（prompt→关键词）+ `search.py`（多平台搜索） | `search_and_evaluate.py::generate_queries` + `search_all`；`content/platforms/*` | **复用为主** |
| 2 | 对候选执行筛选规则（规则**可配置**） | `filters.py`（规则引擎）+ `config.yaml` | 现有只有「LLM 逐条打分」，**没有配置化硬筛选** | **需新写** |
| 3 | 借助 LLM 去重，并按规则排序给出最相关 Top 5 | `dedup.py`（LLM 语义去重）+ `rank.py`（逐条评分→加权→LLM 精排 → Top 5） | 现有有「按 cid 精确去重」+「逐条打分」，**没有跨结果语义去重 / 全局排序到 Top N** | **已实现** |
| 4 | 输出标准化结构化结果 | `schema.py` + `result.json` | `evaluated.json` 结构 | **复用+收敛** |
| 5 | 运营同事可直接使用（Nice to have） | `server.py` + `web/index.html` | `search_eval/server.py`（http.server + SPA + 轮询）模式 | **复用模式** |

**一句话结论**：搜索+评估的「发动机」已经存在且经过实战（`search_and_evaluate.py`），本任务的真正增量是 **(a) 配置化筛选规则层**、**(b) LLM 语义去重 + 全局排序到 Top 5**、**(c) 一个自包含的轻前后端**。其余靠复用。

---

## 2. 两个来源文件夹分析

### 2.1 `search_eval/search_and_evaluate.py` —— 搜+评 发动机（614 行）

自包含的「搜 + 评」管线，**绕开 agent**，直接调平台 `search_impl`。核心阶段（`run()`）：

```
load_queries → [可选] generate_queries(LLM 改写/扩展)
            → build_query_overrides(英文平台 youtube/x 把中文 query 翻成英文)
            → search_all(平台×query 并发搜索 + 按 (platform,cid) 去重)
            → _convert_timestamps
            → [可选] transcribe_video_posts(视频转字幕并入正文)
            → evaluate_posts(rubric 逐条多模态打分)
            → 写 evaluated.json
```

可直接搬走的函数（与 agent 框架解耦）：
- `generate_queries(...)` —— prompt/需求 → 搜索词列表（任务目标 #1）
- `build_query_overrides(...)` —— 英文平台译词
- `search_all(...)` —— **多平台并发 + cid 去重**，产出标准 `source_dict`（任务目标 #1）
- `_post_cid / _collect_post_image_urls / _attach_image_refs` —— 图片收集（多模态评估用）
- `evaluate_posts(...)` —— 逐条打分（部分覆盖 #2/#3，但只是「逐条」，非「全局排序」）

`source_dict` 标准结构（下游一切的契约）：
```jsonc
{
  "case_id": "xhs_<cid>", "platform": "xhs",
  "channel_content_id": "<cid>", "source_url": "...",
  "post": { /* 平台原始帖详情：title/body_text/images/author/互动数... */ },
  "comments": [...],
  "found_by_queries": ["query1", ...]   // 命中它的 query，可回溯 query 质量
}
```

### 2.2 `agent/tools/builtin/content/` —— 平台接入层

| 模块 | 行 | 作用 | 关键外部依赖 |
|------|----|------|-------------|
| `registry.py` | 125 | `PlatformDef` / `ParamSpec` / 注册表（`register_platform`/`get_platform`） | 仅 `ToolResult` |
| `platforms/aigc_channel.py` | 450 | **9 个中文平台**：xhs/gzh/sph/douyin/bili/zhihu/weibo/toutiao/github，后端 `aigc-channel.aiddit.com` | httpx + `utils.image` |
| `platforms/youtube.py` | 463 | YouTube 搜索/详情，后端 `crawler.aiddit.com` | httpx + `utils.image` |
| `platforms/x.py` | 315 | X/Twitter，后端 `crawler.aiddit.com` | httpx + `utils.image`（+ 懒加载转写/OSS） |
| `cache.py` | 175 | 搜索结果磁盘缓存（按 trace 隔离）；`search_all` 走直调**不需要它** | 0 |
| `transcription.py` | 353 | 视频下载+Deepgram 转写 | httpx / ffmpeg / `DEEPGRAM_KEY` |
| `tools.py` | 305 | `@tool` 包装（content_search 等）；**本工具不走这层** | 整套 `@tool` 框架 |

**平台 `search_impl` 签名**（researcher 内部统一契约）：
```python
async def search_impl(platform_id: str, keyword: str, max_count: int,
                      cursor: str, extras: Optional[dict]) -> ToolResult
# 命中结果在 result.metadata["posts"] -> List[dict]
```

---

## 3. 依赖闭包 与 Vendoring 边界

> 目标：`researcher/` 内 `import` 只指向自己；`agent.*` / `examples.*` 一律复制进来或裁掉。

### 3.1 分层（按耦合度）

**Tier 0 — 纯叶子（复制即用，0 内部依赖）**
- `agent/tools/models.py::ToolResult`（纯 dataclass）→ 落 `researcher/vendor/tool_result.py`
- `examples/.../llm_helper.py`（167 行，0 agent 依赖）→ `researcher/llm/llm_helper.py`
- `content/cache.py`、`content/transcription.py`（各 0 agent 依赖，按需复制）

**Tier 1 — 轻耦合（改 1~2 行 import 指向 vendor 即可）**
- `content/registry.py`（仅依赖 `ToolResult`）→ `researcher/platforms/registry.py`
- `agent/tools/utils/image.py`（依赖 httpx + Pillow）→ `researcher/vendor/image.py`
- `content/platforms/{aigc_channel,youtube,x}.py` → `researcher/platforms/`
  - x.py 里 `evaluate_source_quality` / `transcription` / `image_cdn` / `cache` 都是**函数内懒加载**，原型阶段可裁剪为「能力降级」分支，不阻塞导入。
- `extract_sources.py`（只依赖 transcription）→ 按需，主要取 `_normalize_post_in_place`/`_convert_timestamps`
- `generate_case.py`（只依赖 image_cdn）→ 只需 `_extract_raw_images`，可直接抽函数，避免拖入 OSS

**Tier 2 — 评估/Provider**
- `llm_evaluate_sources.py`（628 行）→ `researcher/eval/`
  - 顶层只 import `llm_helper`；provider（`agent.llm.*`）是**懒加载**。
  - 复制时把 `from agent.llm import ...` 改指 vendor 的 provider 适配器，或直接重写一个 `build_eval_llm_call()`（推荐：原型只接 OpenRouter + Qwen 两个）。
  - `eval_prompt_template.md` / `eval_prompt_sample-mod.md`（rubric）一并复制。

**Tier 3 — 明确不搬**
- `agent/tools/registry.py`（569 行 `@tool` 框架）—— **不在关键路径**，跳过。
- `content/tools.py`（`@tool` 包装）—— 不走这层。

### 3.2 真正搬不走的东西（外部/远端，需在 README 标注）

| 类别 | 具体 | 处理 |
|------|------|------|
| 远端爬虫后端 | `aigc-channel.aiddit.com`、`crawler.aiddit.com` | 平台搜索实际打这些 HTTP 服务，原型依赖其可达 |
| 专有 SDK | `cyber_sdk.ali_oss`（仅图片**上传** OSS 用到） | 原型裁掉上传，只做图片**下载**（纯 httpx） |
| pip 包 | `httpx`、`Pillow`、`python-dotenv` | `requirements.txt` 列明 |
| API Key（`.env`） | `OPEN_ROUTER_API_KEY`、`QWEN_API_KEY`/`QWEN_BASE_URL`、`GEMINI_API_KEY`、`DEEPGRAM_KEY` | `.env.example` 列名，不带值 |

---

## 4. 目标目录结构（researcher/）

```
researcher/                   # 仓库根
├── README.md                 # 跑通步骤 + 外部依赖说明
├── docs/                      # DESIGN.md（本文档）+ HANDOFF.md（交接）
├── tests/                    # smoke_search.py（搜索冒烟）
├── requirements.txt          # httpx / Pillow / python-dotenv ...
├── .env.example              # 需要的 key 名（无值）
├── config.yaml               # ★筛选规则 + 平台 + 排序权重（可配置，任务目标 #2）
│
├── vendor/                   # 从 agent.* 原样复制的叶子件
│   ├── tool_result.py        #   ToolResult
│   └── image.py              #   build_image_grid/encode_base64/load_images
│
├── platforms/                # 平台接入（复制自 content/）
│   ├── registry.py
│   ├── aigc_channel.py       #   xhs/gzh/douyin/bili/zhihu...
│   ├── youtube.py
│   └── x.py
│
├── llm/
│   ├── llm_helper.py         #   call_llm_with_retry（叶子）
│   └── providers.py          #   build_llm_call()：OpenRouter + Qwen（轻量重写）
│
├── pipeline/                 # ★核心管线（编排 = 复用 search_and_evaluate 的阶段）
│   ├── query.py              #   prompt → 关键词（generate_queries）          #1
│   ├── search.py             #   多平台并发搜索 + cid 去重（search_all）        #1
│   ├── filters.py            #   ★配置化硬筛选（新写）                          #2
│   ├── dedup.py              #   ★LLM 语义去重（新写）                          #3
│   ├── rank.py               #   ★逐条评分 + 加权排序 + LLM 精排 → Top 5（新写）#3
│   ├── schema.py             #   ★标准化输出 schema（新写）                     #4
│   └── run.py                #   串起来：一个 run(query, config) -> result.json
│
│   # 注：设计初稿的独立 eval/（复制精简 llm_evaluate_sources + rubric 文件）已并入
│   #     rank.py：逐条 rubric 打分内联为 rank.py::_score_one 的 system prompt，
│   #     原型只需 relevance/quality 两维，无需搬整套评估器与外部 rubric markdown。
│
├── server.py                 # ★后端（http.server，复刻 search_eval/server.py 模式） #5
├── web/
│   └── index.html            # ★前端 SPA（输入框 + 平台勾选 + 规则编辑 + Top5 卡片） #5
└── runs/                     # 每次 run 的输出（result.json + 日志）
```

---

## 5. 关键增量设计（现有代码没有的部分）

### 5.1 配置化筛选规则（`filters.py` + `config.yaml`）— 任务目标 #2

现状：`search_and_evaluate` 只有 LLM 逐条软打分，没有「可配置的硬规则」。设计一个**声明式规则**层，在送 LLM 之前先做廉价过滤（省钱、可解释）：

```yaml
# config.yaml （节选）
platforms: [xhs, douyin, bili]          # 采哪些平台
max_count_per_query: 20

filters:                                 # 按顺序执行，全部通过才保留
  - field: post.like_count               # 支持点号路径取值
    op: gte
    value: 100                           #   赞 >= 100
  - field: post.publish_timestamp
    op: within_days
    value: 365                           #   一年内
  - field: post.body_text
    op: min_len
    value: 20                            #   正文 >= 20 字（滤空壳/纯 hashtag）
  - field: post.title
    op: not_contains_any
    value: ["广告", "代写"]

rank:
  top_n: 5
  weights:                               # 排序权重（喂给 rank.py 的加权或作 LLM 提示）
    relevance: 0.5
    quality: 0.3
    engagement: 0.2
```

`filters.py` 提供 `apply_filters(sources, rules) -> (kept, dropped_with_reason)`，每条淘汰都记原因（前端可展示「为什么被滤」）。算子集合：`gte/lte/eq/within_days/min_len/contains_any/not_contains_any/regex`。

> 设计取舍：**硬规则**做客观、可解释、零成本的初筛（数值/时间/长度/关键词）；**LLM**只做主观相关性/质量判断。两者不混淆——避免把「赞数门槛」这种确定性逻辑塞进 prompt。

### 5.2 LLM 语义去重（`dedup.py`）— 任务目标 #3

现状：`search_all` 只按 `(platform, cid)` **精确**去重；跨平台/换皮的「同一条内容」抓不到。新增一步**语义去重**：
- 廉价预聚类：标题/正文做归一化 + MinHash/字符 n-gram 相似度，聚成候选簇；
- LLM 终判：对高相似候选对，让 LLM 判「是否同一内容/同一选题」，保留信息量最高的一条，其余记为 `merged_into`。

输出保留去重痕迹（`dup_group_id` / `merged_from`），便于运营复核。

> **实现状态**：`dedup.py::llm_dedup` 已落地 LLM 终判 + 去重痕迹（`dedup.group_id` / `merged_from`，组内保留正文最长的一条）。
> **MinHash/n-gram 预聚类暂未实现**——候选集已被硬筛选砍小，目前把全清单一次性交给 LLM 分组（单次调用、成本低）。
> 当候选量很大（数百条）时再补预聚类，避免单次 prompt 过长。

### 5.3 评估 + 全局排序到 Top 5（`rank.py`）— 任务目标 #3

复用 `evaluate_posts` 的逐条 rubric 打分拿到每条的 `relevance/quality` 分，然后：
1. 按 `config.rank.weights` 把各维度分 + 互动数（归一化）加权成 `final_score`；
2. 取 `top_n`（默认 5）；
3. 让 LLM 对这 Top-K **做一次 rerank/复核**（点对点比较，纠正纯加权的偏差），给出最终顺序 + 一句话推荐理由。

> 为什么两段式（先加权后 LLM rerank）：加权快且稳定地把候选从几百条砍到 5~8 条，LLM 只在小集合上做精排——既控成本又拿到「人能看懂的排序理由」。

> **实现状态（已落地）**：`rank.py::rank_top` 完整实现三步——
> 1. `_score_one` 并发逐条打 relevance/quality + 一句话理由；
> 2. 加权（含 log1p 归一化的互动数）得 `final_score`，砍到候选池 `rerank_pool`（默认 `top_n+3`，**略大于 top_n** 才能让精排「提拔」被加权低估的候选）；
> 3. `_llm_rerank` 对候选池做一次点对点精排，输出最终顺序 + 推荐理由（覆盖逐条 `reason`，原值存 `scores.eval_reason`，并打 `scores.reranked=true`）。
>
> **fail-open**：精排调用失败或被关闭时自动回退纯加权顺序，不阻断管线。
> **开关**：`config.yaml` 的 `rank.rerank` / `rank.rerank_pool`；CLI `--no-rerank`；前端「LLM 精排」复选框（经 `/api/run` 透传）。

### 5.4 标准化输出（`schema.py`）— 任务目标 #4

```jsonc
{
  "query": "健身 增肌 计划",
  "requirement": "竞品分析：找增肌内容选题参考",
  "config_snapshot": { /* 本次用的 filters/weights，可复现 */ },
  "platforms": ["xhs", "douyin"],
  "stats": { "searched": 213, "after_filter": 64, "after_dedup": 51, "top_n": 5 },
  "top": [
    {
      "rank": 1,
      "case_id": "xhs_xxx",
      "platform": "xhs",
      "title": "...", "source_url": "...",
      "author": { "id": "...", "name": "...", "follower_count": 0 },
      "metrics": { "like": 0, "comment": 0, "collect": 0 },
      "scores": { "relevance": 9, "quality": 8, "final": 8.6 },
      "reason": "LLM 给出的一句话推荐理由",
      "cover_images": ["..."],
      "found_by_queries": ["..."],
      "dedup": { "dup_group_id": "g3", "merged_from": ["dy_yyy"] }
    }
    // ... 共 top_n 条
  ],
  "dropped": [ { "case_id": "...", "stage": "filter|dedup", "reason": "赞数 32 < 100" } ]
}
```

> 注：图里第 1 条提到「候选**账号**或内容」。原型先做**内容（post）**为一等公民；`author` 字段已带账号信息，后续可加一个 `aggregate_by_author` 视图把同作者内容聚合成「候选账号」，作为扩展点而非 MVP。

---

## 6. 前后端设计（任务目标 #5）

**复刻 `search_eval/server.py` 的成熟模式**（零额外框架依赖，适合原型）：

- **后端**：`http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler`，单文件 `server.py`。
- **任务执行**：POST 触发 → 起后台 `threading.Thread` 跑 `pipeline/run.py`（**直接 import 调用**，比 subprocess 干净，因为已自依赖）→ 内存 dict 记 `status`，HTTP 立即返回。
- **进度**：前端**轮询** `/api/status?run_id=` 与 `/api/log?run_id=`（沿用现有 polling，不上 WebSocket/SSE）。
- **前端**：单页 `web/index.html`（原生 JS + fetch），SPA 调 JSON API。

路由设计：

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/` | 返回 `web/index.html` |
| GET | `/api/config` | 返回默认 `config.yaml`（前端渲染成可编辑表单） |
| GET | `/api/platforms` | 列可用平台（`all_platforms()`） |
| POST | `/api/run` | body `{query, requirement, platforms, filters, weights}` → 起任务，返回 `run_id` |
| GET | `/api/status?run_id=` | `{status: running/success/failed, error}` |
| GET | `/api/log?run_id=` | 实时日志文本 |
| GET | `/api/result?run_id=` | 返回 `result.json`（Top5 + dropped） |
| GET | `/api/runs` | 历史 run 列表（扫 `runs/`） |

前端 UI 区块：
1. **输入区**：查询 prompt 文本框 + 需求描述 + 平台多选 + 「[可选] 让 LLM 扩展关键词」开关。
2. **规则区**：把 `config.filters` 渲染成可增删的规则行（字段/算子/值）+ 排序权重滑块。
3. **运行/进度区**：运行按钮 + 状态徽章 + 实时日志（轮询）。
4. **结果区**：**Top 5 卡片**（封面图条 + 标题 + 平台徽章 + 互动数 + final_score + 一句话推荐理由）；折叠区展示「被滤掉的 N 条及原因」。

---

## 7. 端到端数据流

```
运营在网页输入 prompt + 选平台 + 调规则
        │  POST /api/run
        ▼
server.py 起后台线程 → pipeline/run.py
        ▼
query.py   prompt ─(可选 LLM 扩展)→ 关键词集
        ▼
search.py  平台×关键词 并发搜索 → cid 精确去重 → source_dict[]
        ▼
filters.py 配置化硬规则初筛 → (kept, dropped:reason)        # #2
        ▼
dedup.py   LLM 语义去重 → 合并同一内容                       # #3
        ▼
eval+rank  逐条 rubric 打分 → 加权 → LLM rerank → Top 5      # #3
        ▼
schema.py  组装 result.json（top + dropped + stats）         # #4
        ▼
前端轮询 /api/result → 渲染 Top5 卡片                         # #5
```

---

## 8. 分阶段实施计划

- **M0 自依赖骨架**：建目录；复制 `ToolResult`/`image.py`/`registry.py`/平台三件套/`llm_helper`；写最小 `providers.py`；`python -c "import platforms; search xhs"` 跑通**一次真实搜索**（验证远端后端可达 + key 生效）。
- **M1 管线打通（CLI）**：`query→search→evaluate→schema`，复用 `search_and_evaluate` 阶段，先不做 filters/dedup，输出 `result.json`。
- **M2 增量能力**：`filters.py`（配置化）+ `rank.py`（加权+LLM rerank Top5）+ `dedup.py`（语义去重）。
- **M3 前后端**：`server.py` + `web/index.html`，复刻轮询模式，运营可点开即用。
- **M4 打磨**：`config.yaml` 默认值、`README`、`.env.example`、被滤原因展示、账号聚合视图（扩展）。

---

## 9. 风险 / 开放问题

1. **远端后端可达性**：`aigc-channel.aiddit.com` / `crawler.aiddit.com` 是搜索的真正数据源，原型强依赖；需在 M0 验证并在 README 标注（内网/鉴权？）。
2. **平台 post schema 不齐**：youtube/sph 字段与通用 schema 不同，`search_and_evaluate` 用 `_normalize_post_in_place` 抹平——vendoring 时这段必须一起搬，否则下游取字段会漏。
3. **成本**：LLM 出现在 3 处（扩展 query / 逐条打分 / Top-K rerank）。`config` 要能关掉「query 扩展」「多模态图片」以省钱；评估模型默认用便宜档（gemini-flash-lite 量级）。
4. **`cyber_sdk.ali_oss`**：仅图片上传用到，原型**裁掉上传**只做下载即可绕开；若前端要显示图片，直接用平台原图 URL（xhs 实测可直连）。
5. **「账号」vs「内容」**：图里要"账号或内容"，MVP 先内容；账号聚合作为 `aggregate_by_author` 扩展，不进 M1~M3。
6. **筛选规则表达力**：先支持固定算子集；复杂布尔组合（AND/OR 嵌套）留作后续，避免过早做成 DSL。

---

## 附：复用 / 新写 清单速查

| 文件 | 来源 | 动作 |
|------|------|------|
| `vendor/tool_result.py` | `agent/tools/models.py` | 复制 `ToolResult` |
| `vendor/image.py` | `agent/tools/utils/image.py` | 复制 |
| `platforms/*` | `content/registry.py` + `content/platforms/*` | 复制，改 import 指向 vendor |
| `llm/llm_helper.py` | `examples/.../llm_helper.py` | 复制 |
| `llm/providers.py` | `agent/llm/*`（精简） | 轻量重写（OpenRouter+Qwen） |
| ~~`eval/evaluate.py` + `prompts/`~~ | `llm_evaluate_sources.py` + `eval_prompt_*.md` | **已并入 `rank.py`**（逐条打分内联，不再单设目录/外部 rubric） |
| `pipeline/query.py` `search.py` | `search_and_evaluate.py` | 抽函数复用 |
| `pipeline/filters.py` `dedup.py` `rank.py` `schema.py` | —— | **新写** |
| `server.py` + `web/index.html` | `search_eval/server.py` + `index.html` | 按模式新写（精简） |
