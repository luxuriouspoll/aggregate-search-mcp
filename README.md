# aggregate-search-mcp

一个本地运行的聚合搜索 MCP：一次调用并发查询多家搜索引擎，**同一厂商多 key 自动轮转、额度/限流自动切换、跨厂商 URL 去重合并**，并提供多后端网页正文抓取。

支持 Tavily、火山引擎、Querit、Firecrawl、Exa、Jina、Parallel、Brave、Ollama、Perplexity、Serper、SerpApi、Linkup、SearXNG、DuckDuckGo。没有 key 也能先用 Parallel 匿名搜索。

> 传输方式：stdio。运行时仅依赖 Python 3.10+、`mcp>=1.2,<2` 和 `httpx`。
> 免费额度和价格会变化，下表为 2026-11-24 调研口径，注册前请以厂商官网为准。

## 功能

- **跨厂商聚合**：并发搜索，同一 URL 合并为一条，并标注所有命中来源。
- **真正的多 key 轮转**：跨请求均衡消耗多个 key；同一次请求失败自动换下一个，不重复尝试。
- **额度状态管理**：401/402/403/429 和业务错误码分类处理，冷却状态持久化；状态文件只保存 key 的 SHA-256 摘要，不保存明文。
- **失败隔离**：某一家超时或限流，不影响其他来源返回。
- **正文抓取回退**：默认抓取后端失败后，可自动尝试其他已配置后端。
- **按源代理**：只让 Jina、DuckDuckGo 等指定源走代理，国内源保持直连。
- **密钥安全**：`.env`、`keys.json`、`.key-state.json` 默认被 Git 忽略；示例配置只有占位符。

## 快速安装

### Windows PowerShell

```powershell
git clone https://github.com/luxuriouspoll/aggregate-search-mcp.git
cd aggregate-search-mcp
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install "mcp>=1.2,<2" httpx
Copy-Item .env.example .env
# 用记事本打开 .env，填入你的 key
.\.venv\Scripts\python.exe check_providers.py --status
.\.venv\Scripts\python.exe check_providers.py --demo
.\.venv\Scripts\python.exe check_providers.py "MCP 是什么"
```

### macOS / Linux

```bash
git clone https://github.com/luxuriouspoll/aggregate-search-mcp.git
cd aggregate-search-mcp
python3 -m venv .venv
.venv/bin/python -m pip install "mcp>=1.2,<2" httpx
cp .env.example .env
# 编辑 .env 填入 key
.venv/bin/python check_providers.py --status
.venv/bin/python check_providers.py --demo
.venv/bin/python check_providers.py "MCP 是什么"
```

**零 key 试用**：不配置任何 key 时，Parallel 匿名 MCP 默认可用；其他有 key 的厂商配置后自动加入。

## 在哪里注册和获取 key

### 已适配、可直接使用

| 厂商 | 注册 / 获取 key | 免费口径 | 环境变量 | 搜索 | 抓取 |
|---|---|---|---|---|---|
| **Parallel** | 匿名直接用；需要更高额度去 [Platform](https://platform.parallel.ai/) | 匿名 MCP 免费有限流；账号每月免费额度，Fast/Turbo 最多约 5,000 次 | `PARALLEL_API_KEY`（可选） | ✅ | ✅ |
| **Tavily** | [控制台](https://app.tavily.com/home) | 1,000 credits/月 | `TAVILY_API_KEY` / `TAVILY_API_KEYS` | ✅ | ✅ |
| **Exa** | [API Keys](https://dashboard.exa.ai/api-keys) | 有注册和月度赠金，以控制台为准 | `EXA_API_KEY(S)` | ✅ | ✅ |
| **Jina** | [API Dashboard](https://jina.ai/api-dashboard/) | 有免费额度，以控制台为准 | `JINA_API_KEY(S)` | ✅ | ✅ |
| **Firecrawl** | [API Keys](https://www.firecrawl.dev/app/api-keys) | 1,000 credits/月；也支持匿名按 IP 限额 | `FIRECRAWL_API_KEY(S)` | ✅ | ✅ |
| **火山引擎** | [联网搜索控制台](https://console.volcengine.com/search-infinity/api-key) | 每月有免费额度，以控制台为准 | `VOLCENGINE_API_KEY(S)`，兼容 `WEB_SEARCH_API_KEY` | ✅ | ❌ |
| **Querit** | [官网](https://www.querit.ai) | 以账户页面为准 | `QUERIT_API_KEY(S)` | ✅ | ❌ |
| **Serper** | [官网注册](https://serper.dev/) | 注册一次性 2,500 次，非每月重置 | `SERPER_API_KEY(S)` | ✅ | ❌ |
| **SerpApi** | [价格与注册](https://serpapi.com/pricing) | 250 次/月 | `SERPAPI_API_KEY(S)` | ✅ | ❌ |
| **Linkup** | [官网与价格](https://www.linkup.so/pricing) | 有免费试用额度，以官网为准 | `LINKUP_API_KEY(S)` | ✅ | ❌ |
| **Ollama Web Search** | [云端 Keys](https://ollama.com/settings/keys) | 以账户页面为准；不是本地 11434 | `OLLAMA_API_KEY(S)` | ✅ | ✅ |
| **Brave** | [API Dashboard](https://api-dashboard.search.brave.com/app/keys) | 原免费计划已取消；现为月度赠金且需绑卡，注册前核对官网 | `BRAVE_API_KEY(S)` | ✅ | ❌ |
| **Perplexity** | [API 文档与价格](https://docs.perplexity.ai/docs/getting-started/pricing) | 无通用免费 API 套餐，按量付费 | `PERPLEXITY_API_KEY(S)` | ✅ | ❌ |

### 不需要 key

| 来源 | 配置方式 | 注意事项 |
|---|---|---|
| **Parallel** | 默认启用，无需配置 | 官方匿名 MCP，有限流；实测国内可直连 |
| **Firecrawl 匿名** | 不配 key，并设 `ENABLE_KEYLESS=true`；或在调用时指定 `providers:["firecrawl"]` | 每日按 IP 限额；已配 key 时优先使用 key |
| **SearXNG** | `SEARXNG_BASE_URL=http://127.0.0.1:8080` | 自建实例需在 `settings.yml` 的 `search.formats` 开启 `json` |
| **DuckDuckGo** | 不设 key；建议仅手动指定 | 非官方 HTML 端点，容易超时或返回反爬页，不适合稳定生产使用 |

## 配置多个 key

优先级：**MCP 客户端 `env` > 项目 `.env` > 项目 `keys.json`**。

```dotenv
# 单个 key
EXA_API_KEY=your-exa-key

# 同一家多个 key，用复数变量，逗号/分号/换行分隔
TAVILY_API_KEYS=tvly-your-first-key,tvly-your-second-key
JINA_API_KEYS=jina_your-first-key,jina_your-second-key
```

也可复制 `keys.example.json` 为 `keys.json`：

```json
{
  "tavily": ["tvly-your-first-key", "tvly-your-second-key"],
  "exa": ["your-exa-key"]
}
```

**不要提交真实 key。** `.env`、`keys.json` 已写入 `.gitignore`。

## 在 MCP 客户端中使用

把下面路径替换成你的实际克隆路径。key 推荐放在项目 `.env`，而不是客户端配置中。

```json
{
  "mcpServers": {
    "aggregate-search": {
      "command": "C:\\path\\to\\aggregate-search-mcp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\aggregate-search-mcp\\server.py"]
    }
  }
}
```

macOS / Linux 示例：

```json
{
  "mcpServers": {
    "aggregate-search": {
      "command": "/absolute/path/aggregate-search-mcp/.venv/bin/python",
      "args": ["/absolute/path/aggregate-search-mcp/server.py"]
    }
  }
}
```

保存后重启客户端。不同客户端的 MCP 设置入口不同，但 stdio 的 `command` 和 `args` 含义相同。

## 三个工具怎么用

### 1. `web_search`：聚合联网搜索

参数：

- `query`（必填）：搜索关键词或问题。
- `max_results`（可选）：每家返回条数，默认 5，总上限由 `MAX_RESULTS_CAP` 控制。
- `providers`（可选）：只查指定厂商；未指定时查询所有已启用来源。

对 AI 直接说：

```text
用 aggregate-search 搜索“2026 年 MCP 协议最新变化”，每家返回 5 条。
```

等价工具调用：

```json
{"query":"2026 年 MCP 协议最新变化","max_results":5}
```

只查 Tavily、Exa 和 Parallel：

```json
{"query":"MCP 是什么","providers":["tavily","exa","parallel"],"max_results":3}
```

结果末尾会列出各家的成功/失败状态和 key 可用数量；同一 URL 被多家命中时，会显示 `来源: tavily, exa, parallel`。

### 2. `web_fetch`：抓取网页正文

```json
{"url":"https://example.com"}
```

指定 Jina 抓取：

```json
{"url":"https://example.com","provider":"jina"}
```

未指定 `provider` 时先使用 `WEB_FETCH_PROVIDER`（默认 Tavily），失败后自动尝试其他已配置后端。正文超过 `FETCH_MAX_CHARS` 会截断。

### 3. `web_status`：查看配置和 key 健康状态

无参数。用于检查哪些厂商已启用、几个 key 可用、哪些处于冷却状态。

## 常用环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SEARCH_PROVIDERS` | 空 | 限定默认搜索源，例如 `tavily,exa,parallel` |
| `WEB_FETCH_PROVIDER` | `tavily` | 首选抓取后端 |
| `MAX_RESULTS` | `5` | 每家默认返回条数 |
| `MAX_RESULTS_CAP` | `20` | 单家条数硬上限 |
| `SEARCH_TIMEOUT` | `15` | 普通源超时秒数；Jina 搜索单独允许更长时间 |
| `FETCH_MAX_CHARS` | `20000` | 正文截断长度 |
| `ENABLE_KEYLESS` | `false` | 是否默认启用 Firecrawl 匿名、DuckDuckGo；Parallel 不受影响 |
| `SEARCH_PROXY` | 空 | 指定源使用的代理，如 `http://127.0.0.1:7897` |
| `PROXY_PROVIDERS` | `jina,duckduckgo` | 哪些源走代理，可改成 `jina,duckduckgo,exa` |
| `SEARCH_PROXY_ALL` | `false` | `true` 时所有源都走代理 |
| `QUOTA_COOLDOWN` | `3600` | 额度耗尽后的默认冷却秒数 |
| `RATE_COOLDOWN` | `60` | 限流后的默认冷却秒数 |
| `KEY_STATE_FILE` | `.key-state.json` | key 冷却状态文件；相对路径基于项目目录 |

## 自检与故障排查

```bash
# 不访问网络，验证 URL 去重、错误分类、key 轮转与并发占用
python check_providers.py --demo

# 只看各家配置状态
python check_providers.py --status

# 实际联网搜索
python check_providers.py "Python 3.13 新特性"

# 测试正文抓取
python check_providers.py --fetch https://example.com tavily
```

- **某家一直失败**：先看 `web_status` 和搜索结果末尾的错误；401/402/403/429 分别检查 key、余额、权限、限流。
- **更换了失效 key**：删除 `.key-state.json` 中对应条目即可；运行中的进程会发现文件变化并重载。
- **Jina 超时**：它返回全文，速度慢于普通搜索；国内网络可设置 `SEARCH_PROXY`，默认只给 Jina 和 DuckDuckGo 使用。
- **DuckDuckGo 返回 202**：遇到反爬验证，属该非官方端点的限制，建议换 Parallel。
- **SearXNG 不是 JSON**：实例未开放 JSON 输出，检查 `search.formats`。
- **Perplexity 结果形态不同**：它返回答案和引用链接，不是传统逐条搜索摘要。

## 项目结构

```text
server.py                 MCP 工具入口
config.py                 .env / 环境变量 / keys.json 解析
keypool.py                key 轮转、并发占用、冷却状态
check_providers.py        离线自测和联网自检
providers/
  base.py                 HTTP、错误模型、URL 归一化与合并
  __init__.py             注册表、按源代理、并发调度
  *.py                    各厂商 API 适配器
.env.example              配置模板（无真实密钥）
keys.example.json         多 key 配置模板
```

## 安全说明

- 真实密钥只应存在本机 `.env`、`keys.json` 或客户端环境变量中。
- `.gitignore` 排除 `.env`、`keys.json`、`.key-state.json`、虚拟环境和缓存。
- 错误消息写入状态文件或返回客户端前，会替换已配置的明文 key。
- 本项目不自带任何第三方 API key；请使用自己的账号，并遵守各厂商服务条款。
