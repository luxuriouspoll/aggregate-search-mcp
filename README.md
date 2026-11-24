# aggregate-search MCP

聚合搜索 MCP Server（stdio 传输）：把 **Tavily / 火山引擎 / Querit / Firecrawl / Ollama Web Search /
Brave / Exa / Jina / Perplexity / SearXNG / DuckDuckGo** 聚合到一个 MCP 工具里——并发调用、
**多 key 轮询**、URL 归一化去重、多来源合并标记；另提供 `web_fetch` 抓取网页正文。

## 核心能力

| 能力 | 说明 |
|---|---|
| **多 key 轮询** | 同一家可配多个 key，按顺序轮询；401/402/403/429 自动换下一个 key 重试 |
| **限额状态持久化** | 额度耗尽的 key 打冷却标记（带 `reset_at`），到期自动恢复；状态存 `.key-state.json`，只存 key 哈希不存明文 |
| **跨家去重合并** | 同一 URL（归一化：去 www/默认端口/锚点/跟踪参数、参数排序、去尾斜杠）只留一条，`来源` 字段列出命中的所有家 |
| **失败隔离** | 单家失败只记入"提供商状态"，不影响其他家 |

## 对外工具

### `web_search(query, max_results?, providers?)`

并发调用所有已启用的搜索提供商，合并去重后返回 Markdown 结果列表。

- 排序：多家都返回的在前，同分按最佳原始名次
- `max_results`：每家返回条数上限（默认 5），合并后总条数约不超过它的 2 倍
- `providers`：可选，临时限定子集，如 `["tavily", "volcengine"]`

### `web_fetch(url, provider?)`

抓取网页正文，后端五选一：

| 后端 | 端点 | 说明 |
|---|---|---|
| tavily（默认） | `POST api.tavily.com/extract` | raw_content |
| firecrawl | `POST api.firecrawl.dev/v2/scrape` | Markdown |
| ollama | `POST ollama.com/api/web_fetch` | 正文 + 链接 |
| exa | `POST api.exa.ai/contents` | 正文 |
| jina | `GET r.jina.ai/<url>` | Markdown，提取能力强 |

默认后端由 `WEB_FETCH_PROVIDER` 控制，正文超过 `FETCH_MAX_CHARS` 会截断。

### `web_status()`

查看各家的 key 数量、可用数与冷却情况，用来排查"某家为什么没结果"。

## 支持的提供商

| 名称 | 环境变量 | 抓取 | 免费额度 |
|---|---|---|---|
| `tavily` | `TAVILY_API_KEY(S)` | ✅ | 1000 次/月 |
| `volcengine` | `VOLCENGINE_API_KEY(S)` / `WEB_SEARCH_API_KEY(S)` | ❌ | 500 次/月 |
| `querit` | `QUERIT_API_KEY(S)` | ❌ | — |
| `firecrawl` | `FIRECRAWL_API_KEY(S)` | ✅ | 少额试用 |
| `ollama` | `OLLAMA_API_KEY(S)` | ✅ | 少量 |
| `brave` | `BRAVE_API_KEY(S)` | ❌ | 2000 次/月 |
| `exa` | `EXA_API_KEY(S)` | ✅ | 有免费额度 |
| `jina` | `JINA_API_KEY(S)` | ✅ | 有免费额度 |
| `perplexity` | `PERPLEXITY_API_KEY(S)` | ❌ | 需付费 |
| `searxng` | `SEARXNG_BASE_URL`（无需 key） | ❌ | 自建无限 |
| `duckduckgo` | 无需 key | ❌ | 免费（国内需代理） |

> 配了哪家的 key 哪家就自动启用。免 key 源有 `duckduckgo`、`firecrawl`（官方支持无 key 调用，
> 每日按 IP 限额）、`searxng`（填实例地址即启用）。
> 免 key 源默认**不自动参与**聚合（境外源在国内直连易超时，会拖慢整次搜索）；
> 用 `providers: ["firecrawl"]` 显式点名，或设 `ENABLE_KEYLESS=true` 让它默认参与。

## 多 key 配置

三种方式，优先级从高到低：

1. **MCP 客户端 env**（注册时传的环境变量）
2. **项目目录 `.env`**
3. **项目目录 `keys.json`**（模板见 `keys.example.json`）

```dotenv
# 复数变量 = 多个 key，逗号 / 分号 / 换行分隔均可
TAVILY_API_KEYS=tvly-key1,tvly-key2,tvly-key3
VOLCENGINE_API_KEYS=key1,key2
```

单数变量（`TAVILY_API_KEY`）依然兼容，写一个 key 即可。

**轮询行为**：一次搜索里，一个 key 失败（额度耗尽/限流/失效）就换下一个，同一 key 不重复试；
某家的 key 全在冷却期时，这家会被记为失败并在状态里给出最早恢复时间。

## 快速开始

```bash
cd F:\Zcode\日常与测试\search-mcp
python -m venv .venv
.venv\Scripts\python -m pip install "mcp>=1.2,<2" httpx
copy .env.example .env   # 然后填入真实 key
.venv\Scripts\python check_providers.py --status    # 看各家配置情况
.venv\Scripts\python check_providers.py             # 实测搜索
.venv\Scripts\python check_providers.py --demo      # 不用 key，测合并去重 + key 轮询
```

## 配置（环境变量）

| 变量 | 必填 | 说明 |
|---|---|---|
| `<NAME>_API_KEY(S)` | 否 | 配了即启用该家搜索，复数形式支持多 key |
| `SEARXNG_BASE_URL` | 否 | SearXNG 实例地址，填了即启用（需允许 json 输出） |
| `SEARCH_PROVIDERS` | 否 | 逗号分隔，限定默认启用子集 |
| `WEB_FETCH_PROVIDER` | 否 | 抓取后端 `tavily`（默认）/`firecrawl`/`ollama`/`exa`/`jina` |
| `SEARCH_PROXY` | 否 | 搜索请求走的 HTTP 代理，如 `http://127.0.0.1:7897` |
| `ENABLE_KEYLESS` | 否 | `true` 时让 DuckDuckGo / SearXNG 默认参与聚合 |
| `MAX_RESULTS` | 否 | 每家默认返回条数，默认 5 |
| `SEARCH_TIMEOUT` | 否 | 单家超时秒数，默认 15 |
| `FETCH_MAX_CHARS` | 否 | 抓取正文截断长度，默认 20000 |
| `MAX_RESULTS_CAP` | 否 | 单家结果条数硬上限，默认 20（防上游 400） |
| `QUOTA_COOLDOWN` | 否 | 额度耗尽冷却秒数，默认 3600 |
| `RATE_COOLDOWN` | 否 | 限流冷却秒数，默认 60 |
| `KEY_STATE_FILE` | 否 | 冷却状态文件路径，默认 `.key-state.json` |

## 客户端注册

```json
{
  "mcpServers": {
    "aggregate-search": {
      "command": "F:\\Zcode\\日常与测试\\search-mcp\\.venv\\Scripts\\python.exe",
      "args": ["F:\\Zcode\\日常与测试\\search-mcp\\server.py"],
      "env": {
        "TAVILY_API_KEYS": "tvly-k1,tvly-k2",
        "VOLCENGINE_API_KEY": "…"
      }
    }
  }
}
```

不写 `env` 也可以——把 key 填进项目目录的 `.env` 或 `keys.json` 即可。

## 目录结构

```
search-mcp/
├── server.py             # MCP 入口：web_search / web_fetch / web_status
├── config.py             # 环境变量与多 key 解析
├── keypool.py            # 多 key 轮询 + 限额状态持久化
├── check_providers.py    # 自检：--demo / --status / 直连 / --fetch
├── providers/
│   ├── base.py           # 结果模型、HTTP 封装、URL 归一化、合并去重
│   ├── __init__.py       # 注册表、key 轮询调度、并发
│   ├── tavily.py / querit.py / firecrawl.py / ollama.py
│   ├── brave.py / exa.py / jina.py / perplexity.py
│   ├── volcengine.py / searxng.py / duckduckgo.py
├── .env.example          # key 配置模板
├── keys.example.json     # 多 key 集中管理模板
└── pyproject.toml
```

## 常见问题

- **某家一直失败**：看返回末尾"提供商状态"和"key 状态"，多为 key 无效/欠费/限流（401/402/429）。
  设了无效 key 会冷却 24 小时。改完 key 后删掉 `.key-state.json` 里的对应条目（或整个文件）即可立刻重试——
  运行中的进程会检测文件变动并重新加载，不需要重启。
- **DuckDuckGo 超时**：国内直连不通，配 `SEARCH_PROXY=http://127.0.0.1:7897` 后设 `ENABLE_KEYLESS=true`。
- **Ollama 搜索报 401**：确认 key 是在 ollama.com 的 keys 设置页创建的云服务 key，不是本地 `11434`。
- **SearXNG 报"响应不是 JSON"**：实例没开 json 输出格式，改 `settings.yml` 的 `search.formats` 加上 `json`。
- **Perplexity 结果少**：它只返回一个答案 + 引用链接，没有逐条标题摘要，属正常。
- **想临时只用一家**：`web_search` 传 `providers: ["tavily"]`。
