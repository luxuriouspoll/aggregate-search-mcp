# aggregate-search-mcp

一个本地运行的聚合联网搜索 MCP：**匿名源优先、结果不足时自动补 3 个不同厂商、同厂商多 key 均衡轮转、跨厂商去重与多证据汇总、网页正文多后端抓取**。

默认情况下，简单问题先使用 Parallel、Keenable、AnySearch 匿名搜索，不消耗账号 key；复杂或覆盖不足的查询，最多追加 3 个不同厂商，而不是一开始就把所有付费/额度源全部调用一遍。

- 传输：stdio
- 运行环境：Python 3.10+
- 依赖：`mcp>=1.2,<2`、`httpx`
- 工具：`web_search`、`web_fetch`、`web_status`
- 适配器：19 家搜索服务（是否可用取决于网络、key、额度和上游服务）
- 密钥：仅本机配置，仓库不包含真实 key

> 免费额度与价格会变化。本文档的免费信息基于 2026-11-25 检索到的官方页面，注册前请再次以厂商控制台为准。

## 目录

1. [核心工作方式](#核心工作方式)
2. [三种搜索模式](#三种搜索模式)
3. [安装与启动](#安装与启动)
4. [厂商注册地址与配置](#厂商注册地址与配置)
5. [多 key 配置与轮询](#多-key-配置与轮询)
6. [接入 MCP 客户端](#接入-mcp-客户端)
7. [工具调用示例](#工具调用示例)
8. [去重与证据汇总规则](#去重与证据汇总规则)
9. [环境变量参考](#环境变量参考)
10. [测试与故障排查](#测试与故障排查)
11. [密钥与仓库安全](#密钥与仓库安全)

## 核心工作方式

默认 `auto` 模式：

```text
用户问题
  ↓
Parallel + Keenable + AnySearch 匿名搜索
  ↓
本地质量门禁：相关页面数、证据数量、跨源共识、官方一手来源
  ↓
覆盖充足？
  ├─ 是：不再调用任何 key 厂商
  └─ 否：按国内/国际/混合实体，追加 3 个不同厂商
             ↓
        URL 归一化与去重、近似转载折叠、多来源证据汇总
             ↓
        当前 Agent 审核证据的语义、时效性与完整性后回答
```

**“覆盖充足”不等于“事实证明正确”。**本地门禁只负责判断是否需要补搜；最终回答是否可靠，仍由当前 Agent 阅读证据后判断。门禁不足时会返回缺口，不会谎称审核通过。

### 关键能力

- 匿名搜索优先，降低账号积分消耗。
- 覆盖不足时追加 **3 个不同厂商**，不是用同一家的 3 个 key 凑数。
- **地域软路由**：只调整第二阶段补源顺序；国内优先国内源、国际优先国际源、跨地区混排，不过滤任何地区的信息。
- 同一家可保存多个 key，跨请求均衡轮转；失效、限流、额度耗尽自动切换。
- 同一 URL 聚合多个来源和摘要；近似转载折叠但保留 URL 和证据关联。
- 版本号、日期、金额等关键数字不同，禁止误合并。
- 某一家失败不影响其他家；输出明确区分有效返回、空结果和失败。
- 网页抓取支持多后端自动回退。
- 可按源配置代理，国内服务无需全部绕行代理。

## 三种搜索模式

| 模式 | 行为 | 成本保证 |
|---|---|---|
| `auto`（默认） | 先匿名 3 家；覆盖不足追加 3 个不同厂商 | 简单问题可零 key；复杂问题限量使用 |
| `free` | 只用匿名源；显式指定带 key 厂商也拒绝 | **硬保证不调用账号 key** |
| `all` | 并发查询全部已启用厂商 | 各厂商按账号规则消耗额度 |

显式 `providers` 时按用户指定厂商执行；`free` 的禁止 key 约束仍然优先。

### 地域软路由（不是硬分流）

第一阶段始终使用匿名混合搜索，不因中文或英文限制信息来源。只有需要补搜时，才根据问题里的明确地域实体调整顺序：

| 问题类型 | 示例 | 默认补源顺序 |
|---|---|---|
| 国内 | `中国最新的人工智能政策` | 百度千帆、火山引擎、Querit |
| 国际 | `OpenAI 最新 API 价格` | Tavily、Exa、Firecrawl |
| 跨地区 | `中美云计算政策对比` | 百度千帆、Tavily、火山引擎（混排） |
| 无明确地域 | 通用问题 | 火山引擎、Querit、Tavily |

**不会按地区过滤结果。**中文提问不自动等于国内信息；国内平台也可能返回国际技术文档，国际平台同样可能有优质中文资料。这样避免硬分流导致证据缺失。

复杂查询除三源共识外，也接受官方一手来源作为补充证据；官方网站只是原始出处提示，最终仍由 Agent 审核内容、日期与问题匹配度。

可用 `REGION_ROUTING=off` 关闭地域排序，或设 `cn` / `global` 手动指定补源偏好。

## 安装与启动

### Windows PowerShell

```powershell
git clone https://github.com/luxuriouspoll/aggregate-search-mcp.git
cd aggregate-search-mcp
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install "mcp>=1.2,<2" httpx
Copy-Item .env.example .env
# 编辑 .env，填入自己的 key；匿名模式可以不填
.\.venv\Scripts\python.exe check_providers.py --demo
.\.venv\Scripts\python.exe check_providers.py --status
.\.venv\Scripts\python.exe check_providers.py "MCP 是什么"
```

### macOS / Linux

```bash
git clone https://github.com/luxuriouspoll/aggregate-search-mcp.git
cd aggregate-search-mcp
python3 -m venv .venv
.venv/bin/python -m pip install "mcp>=1.2,<2" httpx
cp .env.example .env
# 编辑 .env
.venv/bin/python check_providers.py --demo
.venv/bin/python check_providers.py --status
.venv/bin/python check_providers.py "MCP 是什么"
```

**零 key 也能开始**：不填任何 key，Parallel、Keenable、AnySearch 匿名源默认可用（仍受网络和公共池限流影响）。

## 厂商注册地址与配置

### 国内厂商

| 厂商 | 官方注册 / key / 文档地址 | 免费口径 | 环境变量 | 实测状态 |
|---|---|---|---|---|
| **腾讯元宝搜索** | [服务 API KEY 开通与创建](https://cloud.tencent.com/document/product/1806/130615) · [API 接入指南](https://cloud.tencent.com/document/product/1806/130616) | 新接入用户每月有免费搜索额度，以控制台活动为准 | `TENCENTCLOUD_WSA_APIKEY` | 已按官方文档接入，待 key 实测 |
| **百度千帆搜索** | [千帆 API Key 控制台](https://console.bce.baidu.com/qianfan/ais/console/apiKey) · [搜索接入指南](https://cloud.baidu.com/doc/qianfan/s/nmweljpt3) · [计费与免费额度](https://ai.baidu.com/ai-doc/AppBuilder/Imca18xc6) | 官方页面当前写明每日免费 100 次，默认优先抵扣；以控制台为准 | `BAIDU_API_KEY` | 已实测 |
| **智谱** | [联网搜索文档](https://docs.bigmodel.cn/cn/guide/tools/web-search) · [开放平台](https://open.bigmodel.cn/) | 搜索按次计费；赠送模型 token 不等于免费搜索 | `ZHIPU_API_KEY` | 已按官方文档接入，待 key 实测 |
| **博查** | [开放平台与控制台](https://open.bochaai.com/) | 以控制台额度和当前价格为准 | `BOCHA_API_KEY` | 已按官方文档接入，待 key 实测 |
| **火山引擎** | [联网搜索 API Key 控制台](https://console.volcengine.com/search-infinity/api-key) | 每月有免费额度，以控制台为准 | `VOLCENGINE_API_KEY` | 已实测 |

### 国际与匿名搜索源

| 厂商 | 注册 / key 地址 | 免费口径 | 环境变量 | 搜索 | 抓取 |
|---|---|---|---|---|---|
| **Parallel** | 匿名直接用；[Platform](https://platform.parallel.ai/) | 匿名 MCP 免费有限流；账号每月有免费额度 | `PARALLEL_API_KEY`（可选） | ✅ | ✅ |
| **Keenable** | 匿名直接用；[控制台](https://app.keenable.ai/console) · [价格](https://keenable.ai/pricing) | 匿名公共池；注册 key 当前有月度免费额度 | `KEENABLE_API_KEY`（可选） | ✅ | ✅ |
| **AnySearch** | [官网](https://www.anysearch.com) | 匿名按 IP 免费额度，可选 key | `ANYSEARCH_API_KEY`（可选） | ✅ | ❌ |
| **Tavily** | [控制台](https://app.tavily.com/home) | 每月有免费 credits，以控制台为准 | `TAVILY_API_KEY(S)` | ✅ | ✅ |
| **Exa** | [API Keys](https://dashboard.exa.ai/api-keys) | 注册/月度赠金以控制台为准 | `EXA_API_KEY(S)` | ✅ | ✅ |
| **Jina** | [API Dashboard](https://jina.ai/api-dashboard/) | 免费额度以控制台为准 | `JINA_API_KEY(S)` | ✅ | ✅ |
| **Firecrawl** | [API Keys](https://www.firecrawl.dev/app/api-keys) | 有免费额度；也支持匿名按 IP 限额 | `FIRECRAWL_API_KEY(S)` | ✅ | ✅ |
| **Querit** | [官网](https://www.querit.ai) | 以账户页面为准 | `QUERIT_API_KEY(S)` | ✅ | ❌ |
| **You.com** | [API Keys](https://you.com/platform/api-keys) | 匿名免费档有限额，国内可能需代理 | `YDC_API_KEY`（可选） | ✅ | ❌ |
| **Ollama** | [云端 Keys](https://ollama.com/settings/keys) | 以账户页面为准；不是本地 11434 | `OLLAMA_API_KEY(S)` | ✅ | ✅ |
| **Brave** | [API Dashboard](https://api-dashboard.search.brave.com/app/keys) | 原免费计划已调整；绑卡和计费以官网为准 | `BRAVE_API_KEY(S)` | ✅ | ❌ |
| **Perplexity** | [API 价格](https://docs.perplexity.ai/docs/getting-started/pricing) | 无通用免费 API 套餐 | `PERPLEXITY_API_KEY(S)` | ✅ | ❌ |
| **SearXNG** | 自建实例 | 不需要 key，需开放 JSON 输出 | `SEARXNG_BASE_URL` | ✅ | ❌ |
| **DuckDuckGo** | 非官方 HTML 端点 | 免费但不稳定，可能反爬/超时 | 无需 key，建议手动指定 | ✅ | ❌ |

“已接入”表示代码适配器存在；**没有 key 的厂商未声称已完成真实调用验证**。公共匿名源也可能因共享 IP、地区、限流或上游变更暂时不可用。

## 多 key 配置与轮询

配置优先级：**MCP 客户端 env > 项目 `.env` > 项目 `keys.json`**。

### `.env` 写法

```dotenv
# 单 key
EXA_API_KEY=your-exa-key

# 同一家多个 key，使用复数变量，逗号/分号/换行分隔
TAVILY_API_KEYS=tvly-your-first-key,tvly-your-second-key
JINA_API_KEYS=jina_your-first-key,jina_your-second-key
```

### `keys.json` 写法

复制 `keys.example.json` 为 `keys.json`：

```json
{
  "tavily": ["tvly-your-first-key", "tvly-your-second-key"],
  "exa": ["your-exa-key"],
  "volcengine": ["your-volcengine-key"]
}
```

### 轮询和故障处理

- 跨请求轮转 key，避免每次先消耗第一个账号。
- 同一次请求中，同一个 key 不重复尝试。
- 401/402/403/429 以及可识别的业务错误会触发换 key。
- key 失效、额度、限流分别采用不同冷却时间。
- `.key-state.json` 只保存 key 的 SHA-256 摘要与冷却状态，不保存明文。
- 更换坏 key 后可删除对应冷却条目，进程会检测文件变化并重新加载。

## 接入 MCP 客户端

将路径替换为实际克隆目录。key 推荐放 `.env`，不要写进公开客户端配置。

### Windows

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

### macOS / Linux

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

保存后重启或重连 MCP 客户端。`.env` 在进程启动时读取，修改后需要重连。

## 工具调用示例

### `web_search`

| 参数 | 必填 | 说明 |
|---|---|---|
| `query` | 是 | 搜索问题或关键词 |
| `search_mode` | 否 | `auto`（默认）/`free`/`all` |
| `max_results` | 否 | 每家抓取条数，默认 5 |
| `total_results` | 否 | 汇总后交付页面数，默认 12 |
| `providers` | 否 | 显式指定厂商，如 `["tavily","exa"]` |

默认省钱搜索：

```json
{"query":"MCP 是什么","search_mode":"auto"}
```

严格不消耗账号 key：

```json
{"query":"MCP 是什么","search_mode":"free"}
```

全厂商聚合：

```json
{"query":"MCP 最新版本与旧版差异","search_mode":"all","max_results":3,"total_results":12}
```

指定三家：

```json
{"query":"MCP 是什么","providers":["tavily","exa","parallel"],"max_results":3}
```

输出包含：查询厂商数、有效返回、空结果、失败数、覆盖门禁、成本审计、多源共识、带来源的证据列表及省略说明。

### `web_fetch`

```json
{"url":"https://example.com"}
```

指定 Jina：

```json
{"url":"https://example.com","provider":"jina"}
```

未指定后端时先使用 `WEB_FETCH_PROVIDER`，失败后自动尝试其他已配置后端。仅允许 http/https。

### `web_status`

无参数。查看各厂商配置、key 可用数、冷却状态、匿名优先池、自动升级顺序和当前搜索模式。

## 去重与证据汇总规则

1. URL 统一小写域名、去 `www`、默认端口、跟踪参数、尾斜杠；查询参数排序。
2. `share_token`、功能性参数和 hash 路由保留，避免误合并不同资源。
3. 同 URL 的标题、摘要、日期和来源均保留，并记录每条证据的原始 URL。
4. 多源命中和原始名次共同决定排序。
5. 高度相似转载才折叠；关键数字、版本、日期不同禁止折叠。
6. 折叠后重新计算来源数、RRF 分数和排名。
7. 每页默认展示前 3 条不同证据、单条摘要最多 500 字；省略时明确报告数量。
8. `total_results` 未展示的页面会在统计中注明，不冒充“没有搜到”。

## 环境变量参考

### 成本与质量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SEARCH_MODE` | `auto` | 默认成本模式 |
| `FREE_PROVIDERS` | `parallel,keenable,anysearch` | 第一阶段匿名源 |
| `PAID_PROVIDERS` | 火山、Querit、Tavily、Exa 等 | 自动升级顺序 |
| `AUTO_ESCALATE_MAX` | `3` | 不足时追加的不同厂商数，设 0 禁止升级 |
| `AUTO_MIN_RESULTS` | `4` | 最少原始证据数 |
| `AUTO_MIN_UNIQUE` | `3` | 最少相关独立页面数 |
| `AUTO_MIN_CONSENSUS` | `1` | 复杂查询最少三源共识页面数；官方一手来源可作为补充证据 |
| `REGION_ROUTING` | `auto` | 补源地域软路由：auto / off / cn / global；不过滤结果 |
| `DOMESTIC_PROVIDERS` | `baidu,volcengine,querit,...` | 国内优先补源列表 |
| `GLOBAL_PROVIDERS` | `tavily,exa,firecrawl,jina` | 国际优先补源列表 |

### 搜索、抓取与网络

| 变量 | 默认值 | 说明 |
|---|---|---|
| `SEARCH_PROVIDERS` | 空 | `all` 模式限定默认搜索源 |
| `MAX_RESULTS` | `5` | 每家默认抓取条数 |
| `MAX_RESULTS_CAP` | `20` | 单家条数硬上限 |
| `TOTAL_RESULTS` | `12` | 最终交付页面数 |
| `NEAR_DEDUP` | `true` | 是否折叠高度近似转载 |
| `WEB_FETCH_PROVIDER` | `tavily` | 首选抓取后端 |
| `SEARCH_TIMEOUT` | `15` | 普通源超时秒数；Jina 单独放宽 |
| `FETCH_MAX_CHARS` | `20000` | 抓取正文截断长度 |
| `SEARCH_PROXY` | 空 | 代理地址，如 `http://127.0.0.1:7897` |
| `PROXY_PROVIDERS` | `jina,duckduckgo,youcom` | 哪些源走代理 |
| `SEARCH_PROXY_ALL` | `false` | 是否所有源走代理 |
| `ENABLE_KEYLESS` | `false` | 是否默认加入 Firecrawl 匿名、DDG、You.com；前三家匿名源不受影响 |
| `QUOTA_COOLDOWN` | `3600` | 额度耗尽冷却秒数 |
| `RATE_COOLDOWN` | `60` | 限流冷却秒数 |
| `KEY_STATE_FILE` | `.key-state.json` | 冷却状态文件 |

## 测试与故障排查

```bash
# 离线测试：URL/近似去重、版本冲突、多 key、成本模式
python check_providers.py --demo

# 查看厂商与 key 状态
python check_providers.py --status

# 实际联网搜索
python check_providers.py "MCP 是什么"

# 测试抓取
python check_providers.py --fetch https://example.com tavily
```

- **auto 没有追加 key**：匿名结果已达到本地覆盖门禁，属正常节省行为。
- **复杂查询追加 3 家**：匿名结果缺少三源共识和官方一手来源，系统按设计补搜；仍不足时会返回缺口，不会自动烧完所有厂商。
- **某家失败**：查看结果末尾错误与 `web_status`；401/402/403/429 分别检查 key、余额、权限、限流。
- **Jina 慢**：其搜索返回全文，超时比普通搜索长；可按源配置代理。
- **DuckDuckGo 202/超时**：非官方端点反爬或网络问题，建议使用 Parallel/Keenable/AnySearch。
- **百度/元宝/智谱/博查未出结果**：先确认已配置对应 key、开通服务并有额度；这些适配器需要各自账户验证。
- **SearXNG 非 JSON**：在实例 `settings.yml` 的 `search.formats` 开启 `json`。

## 项目结构

```text
server.py                 MCP 工具、成本模式、统一交付
config.py                 key、网络与门禁配置
keypool.py                多 key 轮转、并发占用、冷却状态
quality.py                覆盖度、三源共识与官方来源门禁
routing.py                国内/国际/混合实体识别与补源软路由
check_providers.py        离线自测与联网自检
providers/
  base.py                 HTTP/MCP 解码、URL 归一化、证据合并
  __init__.py             注册表、按源代理、key 调度
  *.py                    各厂商适配器
.env.example              无密钥配置模板
keys.example.json         多 key 配置模板
```

## 密钥与仓库安全

- 真实 key 仅放本机 `.env`、`keys.json` 或客户端环境变量。
- `.gitignore` 排除 `.env`、`keys.json`、`.key-state.json`、虚拟环境与缓存。
- 错误信息在落盘和返回客户端前会脱敏。
- 仓库内示例只使用占位符，不附带任何用户账号 key。
- 请遵守各搜索厂商服务条款、配额和数据使用规定。
