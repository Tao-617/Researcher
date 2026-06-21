# Researcher 交接文档（HANDOFF）

> 新接手的开发者从这里开始。
> 配套文档：[README.md](../README.md)（怎么跑） · [DESIGN.md](./DESIGN.md)（为什么这么设计）。
> 本文回答三件事：**①这是什么 ②是不是自依赖、怎么迁移 ③各模块职责与数据契约**。

---

## 1. 这是什么

面向内容运营的「媒体内容采集工具」原型（选题1）：输入一段自然语言需求 → 多平台发现候选帖
→ 配置化硬筛选 → LLM 语义去重 → 评分+加权+LLM 精排 Top N → 标准化 `result.json` → 网页浏览每条原帖。

一句话数据流：
```
prompt ─(LLM扩展)→ 关键词 → search_all(多平台+cid精确去重)
      → filters(配置化硬筛, 带原因) → llm_dedup(语义去重)
      → rank_top(逐条评分+加权+LLM精排) → schema(标准化, 含全量原帖) → result.json → web
```

---

## 2. 自依赖 & 迁移（重点）

### 2.1 结论：核心自依赖，可直接搬

整个 `researcher/` 的**核心管线零外部包依赖**——只 import 自己（`pipeline`/`platforms`/`llm`/`vendor`）
和三个 pip 包（`httpx`/`Pillow`/`python-dotenv`/`PyYAML`）。**不依赖** `agent.*` / `examples.*`。

实测（隔离目录、PYTHONPATH 不含原项目根）import 全通过、11 平台正常注册：
```bash
SRC=.; DST=/tmp/mig
mkdir -p $DST && (cd $SRC && tar --exclude='__pycache__' --exclude='runs' -cf - .) | (cd $DST && tar -xf -)
cd $DST && env -u PYTHONPATH PYTHONIOENCODING=utf-8 python -c "import server; from pipeline.run import run_pipeline; print('ok')"
```

### 2.2 残留的 agent.*/examples.* 引用 —— 都是可选增强，不阻塞迁移

代码里还有 20 处 `agent.*` / `examples.*` 引用，但**全部是函数内、`try/except` 包裹的懒加载**，
ImportError 时降级（置 `None` / 打 warning / 走重试）。**没有一处在顶层 import，没有一处在主流程关键路径。**

| 懒加载模块 | 出现位置 | 作用（增强功能） | 缺失时 |
|-----------|---------|-----------------|--------|
| `examples...schema_manager` | `llm/llm_helper.py` | 结构化输出 schema 校验 | researcher 不传 `schema_name`，**根本不触发** |
| `examples...fix_json_quotes` | `llm/llm_helper.py` | LLM 坏 JSON 自动修复 | 跳过修复、走正常重试（轻微降级） |
| `examples...evaluate_source_quality` | `platforms/*` | 搜索摘要的额外质量打分 | `evaluator=None`，跳过 |
| `agent...content.transcription` | `platforms/*` | 视频时长探测 / 字幕转写 | 跳过，正文不含字幕 |
| `agent...content.media` | `platforms/youtube.py` | YouTube 视频下载 | 跳过 |
| `agent...content.cache` | `platforms/*` | 搜索结果磁盘缓存 | 每次实时搜（researcher 本就直调，不需要） |
| `agent...file.image_cdn` | `platforms/*` | 图片上传 OSS | 跳过上传，前端直接用平台原图 URL |

> **迁移就这么做**：整个目录拷走即可，上面这些功能在新环境自动降级，主流程不受影响。
> **若想彻底零残留**（可选）：把这些 `try/except` 分支删掉/注释——它们对应的增强（OSS 上传、视频转写、
> 额外质量分、JSON 修复）researcher 主流程当前都没用到。删了不影响 `search→filter→dedup→rank→schema`。

### 2.3 迁移后验证清单

1. `pip install -r requirements.txt`
2. 项目根放 `.env`（至少 `GEMINI_API_KEY`，见 §5）
3. `python -c "import server"` 不报错（核心 import 通）
4. `python -m llm.providers`（LLM 冒烟）+ `python tests/smoke_search.py`（搜索冒烟，需远端后端可达）
5. 跑一次 `python -m pipeline.run --query "测试" --platforms xhs --output-dir runs/smoke`

---

## 3. 目录结构与模块职责

```
researcher/
├── pipeline/            ★核心管线（编排在 run.py）
│   ├── query.py         prompt → 关键词（可选 LLM 扩展）          #1
│   ├── search.py        多平台×关键词 并发搜 + (platform,cid) 精确去重  #1
│   ├── filters.py       配置化硬筛选（声明式规则，每条淘汰带原因）  #2
│   ├── dedup.py         LLM 语义去重（跨平台/换皮同选题）          #3
│   ├── rank.py          逐条评分 → 加权 → LLM 精排 Top N          #3
│   ├── schema.py        标准化 result.json（含全量原帖 posts[]）  #4
│   └── run.py           串起来：run_pipeline() / CLI 入口
├── platforms/           平台接入（复制自 agent content/，import 已改指 vendor）
│   ├── registry.py      PlatformDef + register/get_platform
│   ├── aigc_channel.py  9 个中文平台(xhs/gzh/sph/douyin/bili/zhihu/weibo/toutiao/github)
│   ├── youtube.py / x.py
├── llm/
│   ├── llm_helper.py    call_llm_with_retry（JSON 校验 + 重试）
│   └── providers.py     build_llm_call()：OpenAI 兼容，gemini/openrouter/qwen 一个 caller 通吃
├── vendor/              从 agent.* 裁剪的叶子件：tool_result.py / image.py
├── server.py            后端：http.server + 后台线程 + 轮询（:8780）         #5
├── web/index.html       前端 SPA：控制台 + 帖子网格 + 详情弹窗（暖色纸张主题） #5
├── config.yaml          默认配置（平台/筛选规则/排序权重/精排开关）
├── requirements.txt / .env.example
├── tests/smoke_search.py 搜索冒烟脚本
├── docs/                DESIGN.md（设计）· HANDOFF.md（本文）
└── runs/                每次 run 输出 result.json（已 gitignore）
```

子 Agent / 分层细节见 DESIGN.md §2~§4。

---

## 4. 数据契约（改下游前必读）

### 4.1 `source_dict`（管线内部流转的单条帖子）
```jsonc
{
  "case_id": "xhs_<cid>", "platform": "xhs", "channel_content_id": "<cid>",
  "source_url": "...", "post": { /* 平台原始详情 title/body_text/images/like_count... */ },
  "comments": [...], "found_by_queries": ["关键词", ...],
  // 管线途中原地挂上：
  "dedup": {"group_id": n, "merged_from": ["case_id", ...]},   // dedup.py
  "scores": {"relevance","quality","engagement","final","reason","eval_reason","reranked"},  // rank.py
  "rank": 1                                                     // rank.py（仅 Top N）
}
```
> 关键机制：`filters/dedup/rank` 操作的是**同一批 dict 对象**（kept 是子集引用），所以 `run.py` 里
> `all_sources` 始终持有全量帖子；schema 末尾一遍扫描即可还原每条帖子的归宿。

### 4.2 `result.json`（对外契约，前端 & 下游只认这份）
```jsonc
{
  "query", "requirement", "config_snapshot", "platforms", "eval_model", "cost", "stats",
  "top":   [ /* Top N 精简卡片，向后兼容 */ ],
  "posts": [ {                       // ★前端列表+详情页的数据源：每条搜到的原帖
     "case_id","platform","title","source_url","author","metrics","publish_time",
     "found_by_queries",
     "stage": "top|candidate|filtered|deduped",   // 在管线里的归宿
     "rank", "scores": {...}|null, "reason", "drop_reason",
     "body_text","images":[...],"comments":[...], "dedup":{...}
  } ],
  "dropped": [ {"case_id","stage","reason"} ]
}
```

---

## 5. 配置与外部依赖

**`config.yaml`**：`platforms` / `max_count_per_query` / `expand_query` / `eval_model` / `eval_concurrent`
/ `filters[]`（算子见 `filters.py`）/ `rank.top_n` / `rank.weights` / `rank.rerank` / `rank.rerank_pool`。

**搜索后端（可插拔，见 `platforms/_backends.py`）**：
- **公开默认 = MediaCrawler**（开源，git 子模块 `external/MediaCrawler`）。适配层 `platforms/mediacrawler.py`
  以子进程跑它的关键词搜索 → 读 JSON → 映射成统一 post。需 `uv sync` + Playwright Chromium +
  各平台首次扫码登录（登录态持久化复用）。安装见 `setup.ps1`/`setup.sh`。
- **内部后端**（均依赖公司内部接口，**已 .gitignore，不随公开 repo 发布**；本地存在时仍可用）：
  - `platforms/aigc_channel.py`（`aigc-channel.aiddit.com`）：9 个中文平台，保留其独有的 gzh/sph/toutiao/github；与 MediaCrawler 重叠的平台由后者覆盖。
  - `platforms/youtube.py` / `platforms/x.py`（`crawler.aiddit.com`）：YouTube / X。
- 因此**公开 repo 实际可用平台 = MediaCrawler 的 7 个**（xhs/douyin/kuaishou/bili/weibo/zhihu/tieba）；
  前端平台勾选项由 `/api/platforms` 动态生成，自动只显示已注册（即可用）的平台。

**API Key**（项目根 `.env`，名见 `.env.example`）：`GEMINI_API_KEY`（当前唯一可用，默认）。
`OPEN_ROUTER_API_KEY` / `QWEN_API_KEY` 实测已失效（401）。`DEEPGRAM_KEY` 仅转写增强用。

---

## 6. 已知边界 / 待办（交给后人）

1. **评论 & 作者为空**：当前只取平台**搜索摘要**，不含 comments / author 详情。要补需加一步**逐帖详情抓取**
   （参考 agent `extract_sources` 的 detail fetch）。前端已对空值优雅降级（"无评论"/"-"）。
2. **账号聚合**：MVP 以「内容(post)」为一等公民。`author` 字段已预留；可加 `aggregate_by_author` 视图把
   同作者内容聚成「候选账号」（DESIGN §5.4 注、§9.5 列为扩展）。
3. **LLM 稳定性**：Gemini 端高并发会掐连接（`EndOfStream`），评分并发已限 `eval_concurrent≤3`；偶发失败
   有重试 + fail-open（评分失败给 0 分、精排失败回退加权），不会整条挂掉。
4. **dedup 无预聚类**：候选量大（数百条）时单次 LLM prompt 会过长，需补 MinHash/n-gram 预聚类（DESIGN §5.2）。
5. **筛选规则**：仅固定算子集，无 AND/OR 嵌套布尔（DESIGN §9.6）。

---

## 7. 关键设计决策（一句话版，细节见 DESIGN.md §5）

- **硬筛 vs LLM 分工**：数值/时间/长度/关键词等确定性逻辑走 `filters.py`（廉价、可解释、零成本初筛）；
  主观相关性/质量才交 LLM。不把"赞数门槛"塞进 prompt。
- **排序两段式**：加权快稳地砍到候选池（`rerank_pool`，须 > `top_n` 才能"提拔"被加权低估者）→ LLM 只在
  小集合做点对点精排，纠偏 + 出推荐理由。既控成本又拿到人能看懂的排序理由。
- **全量原帖持久化**：`result.json.posts[]` 保留每条原帖全文/图片/评论 + 其 `stage`，前端因此能展示"原帖原文"
  和"为什么被滤掉"。
