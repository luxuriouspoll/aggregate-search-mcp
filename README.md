# aggregate-search-mcp

把十几家联网搜索服务打成一个 MCP，给 AI 助手用。

默认先用**不要钱的匿名搜索源**；结果不够，再自动补 3 家不同厂商；同一家可以配多个 key 自动轮换；所有厂商搜到的重复内容合并成一份带来源的证据。

```
用户问题 → 匿名搜 3 家（不花额度）→ 本地检查够不够 → 不够才补 3 家（花额度）→ 去重合并 → 交给 AI 判断
```

- **装一次，全局可用**：一条命令 `aggregate-search`，配置文件放在用户目录，和代码放哪个文件夹无关
- **三个工具**：`web_search`（搜索）、`web_fetch`（抓网页正文）、`web_status`（看状态）
- **19 家厂商**，一个 key 都不配也能用
- **依赖很少**：Python 3.10+，只要 `mcp` 和 `httpx`，stdio 传输
- **仓库里没有任何真实 key**

> 各家的免费额度和价格会变。本文的额度信息来自 2026-11 查到的官方页面，注册前请以厂商控制台为准。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [快速开始](#快速开始)
- [接入 MCP 客户端](#接入-mcp-客户端)
- [配置 key](#配置-key)
- [四种搜索模式](#四种搜索模式)
- [三个工具](#三个工具)
- [省额度与防超支](#省额度与防超支)
- [命令行](#命令行)
- [厂商注册地址](#厂商注册地址)
- [配置项完整参考](#配置项完整参考)
- [去重与证据合并规则](#去重与证据合并规则)
- [故障排查](#故障排查)
- [从 0.3 升级](#从-03-升级)
- [项目结构与开发](#项目结构与开发)
- [安全说明](#安全说明)

---

## 它解决什么问题

直接调一家搜索 API，会遇到三个麻烦：**额度会烧完、结果可能不全、不同家返回的内容重复**。

这个 MCP 的处理方式是：

1. **先省着用**。第一轮只查匿名免费的源（Parallel、Keenable、AnySearch），不发送任何账号 key。
2. **判断够不够**。本地检查相关页面数、证据条数、有没有多家共识或官方一手来源。
3. **不够才花额度**。按「国内 / 国际 / 混合」调整顺序，补 3 家**不同**厂商，而不是把一家的 3 个 key 全打一遍。
4. **合并去重**。同一网址的多份摘要、日期、来源都保留，标明每条信息的出处。
5. **交给人判断**。本地检查只能判断"要不要补搜"，不能证明内容正确；最终是否可信由调用它的 AI 读完证据后决定。

每次返回都会**如实写明成本**：哪几家是匿名调用、哪几家真的发出了带 key 的请求、用的是第几个 key。key 在冷却或到了用量上限、请求根本没发出去的，不会被算成"动用了 key"。

---

## 快速开始

### 第 1 步：安装命令

推荐用 [uv](https://docs.astral.sh/uv/getting-started/installation/)：

```bash
uv tool install git+https://github.com/luxuriouspoll/aggregate-search-mcp
```

或者用 pipx / pip：

```bash
pipx install git+https://github.com/luxuriouspoll/aggregate-search-mcp
# 或
pip install git+https://github.com/luxuriouspoll/aggregate-search-mcp
```

装完验证：

```bash
aggregate-search --version
```

> 提示找不到命令？说明安装目录不在 PATH 里。运行 `uv tool update-shell`，然后**重新打开终端**。

### 第 2 步：生成配置文件

```bash
aggregate-search init
```

它会创建配置目录并放一份带注释的 `.env` 模板：

| 系统 | 配置目录 |
|---|---|
| Windows | `C:\Users\你的用户名\.aggregate-search\` |
| macOS / Linux | `~/.aggregate-search/` |

想放到别处，设环境变量 `AGGREGATE_SEARCH_HOME=绝对路径`。随时用 `aggregate-search path` 查看当前位置。

**这一步之后就已经能用了**——不填任何 key，三家匿名源照常工作。

### 第 3 步：自检

```bash
aggregate-search selftest                # 离线自测：不联网，不碰你的配置
aggregate-search status                  # 看哪些厂商已启用
aggregate-search search "MCP 是什么"      # 真的搜一次
```

### 升级与卸载

```bash
uv tool upgrade aggregate-search-mcp      # 升级
uv tool uninstall aggregate-search-mcp    # 卸载（配置目录不会被删）
```

> **Windows 上升级前，先在 MCP 客户端里断开 aggregate-search（或退出客户端）。**
> 服务运行中时程序文件被占用，升级会报 `拒绝访问 (os error 5)`，还会把安装弄成半截——之后命令直接打不开。
> 已经遇到了：断开客户端里的连接，再执行
> `uv tool install --force --reinstall git+https://github.com/luxuriouspoll/aggregate-search-mcp` 就能恢复，配置不受影响。
>
> 不想断开也能更新（不影响正在运行的进程）：
> ```bash
> uv pip install --python "<工具环境>/Scripts/python.exe" --reinstall-package aggregate-search-mcp .
> ```
> 装完需要重连才生效。

---

## 接入 MCP 客户端

所有客户端都一样：**启动命令填 `aggregate-search`，不需要参数，也不用在这里填 key**（key 在配置目录的 `.env` 里）。

### 通用写法（Claude Desktop、Cursor、Cline 等）

```json
{
  "mcpServers": {
    "aggregate-search": { "command": "aggregate-search" }
  }
}
```

### Claude Code

```bash
claude mcp add --scope user aggregate-search -- aggregate-search
```

### Codex

```toml
# ~/.codex/config.toml
[mcp_servers.aggregate-search]
command = "aggregate-search"
```

### ZCode

```json
// ~/.zcode/cli/config.json
{
  "mcp": {
    "servers": {
      "aggregate-search": { "command": "aggregate-search", "args": [] }
    }
  }
}
```

### 客户端找不到命令怎么办

有些图形界面程序读不到终端里的 PATH。把 `command` 换成完整路径：

```bash
where aggregate-search     # Windows，例如 C:\Users\你\.local\bin\aggregate-search.exe
which aggregate-search     # macOS / Linux，例如 /Users/你/.local/bin/aggregate-search
```

### 不安装，每次临时拉起

```json
{
  "mcpServers": {
    "aggregate-search": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/luxuriouspoll/aggregate-search-mcp", "aggregate-search"]
    }
  }
}
```

改完配置后**重连或重启客户端**。以后每次改 `.env` 也要重连才生效——配置在进程启动时读取，运行中的进程不会热加载。

---

## 配置 key

打开配置目录里的 `.env`，去掉行首的 `#`，把等号后面换成自己的 key：

```dotenv
TAVILY_API_KEY=tvly-your-key
VOLCENGINE_API_KEY=your-volcengine-key
```

写错了不会让服务起不来：有问题的行会被跳过，并显示在 `aggregate-search status` 最上面（只告诉你是第几行，不显示那一行的内容）。

某一项留空（如 `SEARCH_PROXY=`）等于没写，会用默认值。文件存成 UTF-8、带 BOM 的 UTF-8、UTF-16、GBK 都能读。

### 同一家配多个 key

用带 `S` 的变量名，逗号分隔：

```dotenv
TAVILY_API_KEYS=tvly-your-first-key,tvly-your-second-key
```

多个 key 的行为：

- **轮流使用**，不会总盯着第一个 key 消耗
- **自动切换**：某个 key 额度用完、被限流或失效（401 / 402 / 403 / 429 及可识别的业务错误），换下一个，并让它冷却一段时间
- **并发分摊**：同时来多个请求时优先分到不同 key；key 不够分就共用，不会让后来的请求失败
- **只存指纹**：冷却状态文件里只有 key 的 SHA-256 摘要，没有明文

想集中管理也可以用 `keys.json`（把配置目录里的 `keys.example.json` 复制成 `keys.json` 再填）。

### 优先级

**MCP 客户端传入的环境变量 > `.env` > `keys.json`**

- 同一个变量名两边都写了：以客户端传入的为准（客户端传空串则不算）
- 同一家用了不同变量名（客户端传 `TAVILY_API_KEY`、`.env` 里写 `TAVILY_API_KEYS`）：两边的 key 会合在一起轮流用
- `keys.json` 只在环境变量和 `.env` 都没给这一家配 key 时才生效

### 匿名三家要不要配 key

Parallel、Keenable、AnySearch 不配 key 就能搜。配了 key 也**默认不用它**，只在三种情况下才带上：

1. 匿名通道被限流（结果里会注明；不想这样就设 `ANON_KEY_FALLBACK=false`）
2. 你用 `providers` 点名指定，或使用 `all` 模式
3. 补搜轮到它们时——也是先走匿名，被限流才用 key

---

## 四种搜索模式

| 模式 | 做什么 | 会不会用账号 key |
|---|---|---|
| `auto`（默认） | 先匿名查 3 家；覆盖不足才自动补 3 个不同厂商 | 简单问题不会，复杂问题限量用 |
| `free` | 只做匿名调用 | **绝不发送** |
| `escalate` | 跳过匿名阶段，直接补 3 个**还没查过**的厂商，并与此前同一问题的结果合并 | 会，每次 3 家 |
| `all` | 所有已启用的厂商一起查 | 会，最费额度 |

把 `.env` 里的 `SEARCH_MODE` 设成 `free`，等于给整台机器上锁：搜索和抓取都只做匿名调用。

### 推荐给 AI 的用法

1. 先用默认的 `auto`
2. 读返回的证据，自己判断够不够用
3. 不够：**同一个 query** 再调一次，传 `search_mode="escalate"`。它会换 3 家没查过的，并把前后结果合并去重
4. 还不够可以继续 `escalate`（每次换一批），或者换个问法

每次返回的"下一步"一行会直接告诉 AI 接下来会补哪几家、会不会花额度。几个细节：

- `escalate` 靠进程内缓存记住同一问题查过谁，有效期 15 分钟。中间穿插一次 `auto` 或点名查询，不会让它忘记
- `escalate` 不能和 `providers` 同时传（它自己挑还没查过的厂商）
- 同一问题的多个 `escalate` 会排队执行，不会把同一批厂商的额度花两遍

想让 AI 全权决定要不要花额度，把 `AUTO_ESCALATE_MAX=0`：`auto` 就只做匿名搜索，不再自动补。

### 国内 / 国际顺序

补搜时先用哪几家，由 `region` 决定。**它只影响顺序，不过滤任何结果**——中文提问不等于国内信息，国内平台也会返回国际文档，反之亦然。

| `region` | 适用 | 优先补的厂商（配了 key 的才会被选中） |
|---|---|---|
| `domestic` | 国内政策、公司、产品、本地信息 | 百度千帆、火山引擎、Querit … |
| `global` | 海外信息、国际技术文档 | Tavily、Exa、Firecrawl … |
| `mixed` | 中外对比 | 国内和国际交替排 |
| 不传 | 自动 | 按问题里的地名、机构名判断；判断不出就用通用顺序 |

判断优先级：AI 传的 `region` > `.env` 里的 `REGION_ROUTING` > 关键词识别。

补搜怎么凑满 3 家：

- 先排除**没配 key、正在冷却、已到用量上限**的厂商，再从剩下的里取前 3 家
- 有厂商调用失败、或者没搜到任何结果，就从后面递补（最多 3 轮）
- 顺序名单里没写到但已启用的厂商自动排在最后，不会被漏掉
- 一整轮全是网络错误，或快到总时限了，就停止递补，带着已有结果返回

---

## 三个工具

### `web_search` — 搜索

| 参数 | 必填 | 说明 |
|---|---|---|
| `query` | 是 | 搜索问题或关键词 |
| `search_mode` | 否 | `auto`（默认）/ `free` / `escalate` / `all` |
| `region` | 否 | `domestic` / `global` / `mixed`，不传自动判断 |
| `providers` | 否 | 只查指定厂商，如 `["tavily","exa"]`。配了 key 的会带 key；只想匿名请同时传 `search_mode="free"` |
| `max_results` | 否 | 每家取几条，默认 5 |
| `total_results` | 否 | 最终返回多少个页面，默认 12 |

```json
{"query": "MCP 是什么"}
{"query": "MCP 是什么", "search_mode": "escalate"}
{"query": "中国最新的人工智能政策", "search_mode": "escalate", "region": "domestic"}
{"query": "MCP 是什么", "search_mode": "free"}
{"query": "MCP 是什么", "providers": ["tavily", "exa", "parallel"], "max_results": 3}
```

返回内容：查了几家 / 几家有结果 / 失败几家、本地判断结论、**实际成本**、下一步建议、多源共识、去重后的证据列表（每条带来源）、每家的耗时和状态。

一次搜索有**总时限**（默认 45 秒）：到点还没返回的厂商直接放弃，带着已经拿到的结果返回，不会拖到客户端超时把结果全丢掉。

### `web_fetch` — 抓网页正文

```json
{"url": "https://example.com"}
{"url": "https://example.com", "provider": "jina"}
```

不指定后端时**免费优先**：

1. `WEB_FETCH_PROVIDER` 指定的后端（如果你设置了）
2. Parallel、Keenable（匿名，不花额度）
3. 配了 key 的后端：Tavily → Jina → Firecrawl → Exa → Ollama → Parallel / Keenable 的带 key 通道

为了不白花额度：

- 某家失败自动换下一家；抓回来是空的也算失败
- 正文太短（疑似反爬页或空壳）会再换一家复核，**只复核一次**
- 免费后端都抓不到时，最多再试 2 个带 key 的后端——连它们都抓不到，多半是地址本身有问题，不再继续消耗额度
- 抓取工具报的错（"403 Forbidden""需要登录"之类）多半是目标网页的问题，不会因此把你的 key 判成失效

只支持 http / https。

### `web_status` — 看状态

```json
{}
{"usage_days": 7}
```

显示：配置文件在哪、配置里有没有写错的行、每家是否启用、key 可用数、冷却和今日用量、抓取顺序、当前能用于补搜的厂商、走的是哪条网络。传 `usage_days` 还会附上近几天的使用统计。

---

## 省额度与防超支

### 本地用量上限

有些厂商超出免费额度后会直接计费。可以给每个 key 设每天 / 每月最多发多少次请求，到量自动停用，第二天 / 下个月恢复：

```dotenv
DAILY_LIMITS=baidu:90,tavily:30
MONTHLY_LIMITS=tavily:900,exa:900
```

- **百度千帆内置 `baidu:90`**（官方每日免费 100 次，留了余量）。不想要就写 `baidu:0`
- 按"发出的请求数"保守计数；多个客户端同时搜索时可能有个位数误差
- 这是本地软保护，**不能替代在厂商控制台设置额度告警**

### 本地使用记录

每次搜索和抓取会在配置目录的 `usage.jsonl` 里记一行：问了什么、什么模式、调了哪几家、各家耗时和条数、有没有用 key。**只存在本机，不含任何 key**；抓取的网址只记到路径，`?` 后面的参数不落盘。

```bash
aggregate-search usage 7     # 近 7 天：各家调用次数、成功率、平均耗时、动用 key 的比例
```

可以据此调整：哪家总失败、哪家最慢、匿名源够不够用。不想记录就设 `USAGE_LOG=false`。

### 已经做过的省额度优化

| 项目 | 之前 | 现在 |
|---|---|---|
| Jina 搜索 | 把每个结果页全文都抓回来，实测 20 秒、计费约 8.5 万 token | 只取标题和摘要，实测 2 秒、固定 1 万 token |
| Parallel | 每次搜索 3 次网络往返（握手 2 次 + 调用） | 1 次往返 |
| 抓取 | 默认先用 Tavily（花额度） | 默认先用匿名后端 |
| 每家的连接 | 每次调用重新加载证书，约 0.8 秒/家 | 整个进程只加载一次 |

---

## 命令行

MCP 客户端只用到不带参数的 `aggregate-search`。其余子命令是给人排查用的：

| 命令 | 作用 |
|---|---|
| `aggregate-search` | 启动 MCP 服务（stdio） |
| `aggregate-search init` | 生成配置目录和 `.env` 模板（已有的不覆盖） |
| `aggregate-search init --from 路径` | 从已有的 `.env` 导入 |
| `aggregate-search path` | 显示配置目录 |
| `aggregate-search status` | 配置告警、各家 key、冷却、用量、抓取顺序、网络 |
| `aggregate-search search 关键词` | 搜一次。可加 `--mode`、`--region`、`--providers`、`--max`、`--total` |
| `aggregate-search fetch URL [后端]` | 抓一个网页 |
| `aggregate-search check [关键词]` | 让每家各发一次真实请求，逐家报告通不通（带 key 的会消耗 1 次额度） |
| `aggregate-search usage [天数]` | 使用记录汇总，默认 7 天 |
| `aggregate-search selftest` | 离线自测，不联网，不读也不改你的配置 |

---

## 厂商注册地址

### 国内

| 厂商 | 注册 / key / 文档 | 免费口径 | 变量名 | 实测 |
|---|---|---|---|---|
| **火山引擎** | [联网搜索 API Key 控制台](https://console.volcengine.com/search-infinity/api-key) | 每月有免费额度，以控制台为准 | `VOLCENGINE_API_KEY` | ✅ 已实测 |
| **百度千帆** | [API Key 控制台](https://console.bce.baidu.com/qianfan/ais/console/apiKey) · [接入指南](https://cloud.baidu.com/doc/qianfan/s/nmweljpt3) · [计费说明](https://ai.baidu.com/ai-doc/AppBuilder/Imca18xc6) | 官方页面写明每日免费 100 次，超出计费 | `BAIDU_API_KEY` | ✅ 已实测 |
| **腾讯元宝搜索** | [API KEY 开通与创建](https://cloud.tencent.com/document/product/1806/130615) · [接入指南](https://cloud.tencent.com/document/product/1806/130616) | 新用户每月有免费额度，以控制台为准 | `TENCENTCLOUD_WSA_APIKEY` | 按文档接入，未实测 |
| **智谱** | [联网搜索文档](https://docs.bigmodel.cn/cn/guide/tools/web-search) · [开放平台](https://open.bigmodel.cn/) | 按次计费；赠送的模型 token 不等于免费搜索 | `ZHIPU_API_KEY` | 按文档接入，未实测 |
| **博查** | [开放平台](https://open.bochaai.com/) | 以控制台为准 | `BOCHA_API_KEY` | 按文档接入，未实测 |

### 国际与匿名

| 厂商 | 注册 / key | 免费口径 | 变量名 | 搜索 | 抓取 | 实测 |
|---|---|---|---|---|---|---|
| **Parallel** | 匿名直接用；[Platform](https://platform.parallel.ai/) | 匿名免费但有限流；账号每月有免费额度 | `PARALLEL_API_KEY`（可选） | ✅ | ✅ | ✅ |
| **Keenable** | 匿名直接用；[控制台](https://app.keenable.ai/console) · [价格](https://keenable.ai/pricing) | 匿名公共池；注册有月度免费额度 | `KEENABLE_API_KEY`（可选） | ✅ | ✅ | ✅ 匿名 |
| **AnySearch** | [官网](https://www.anysearch.com) | 匿名按 IP 给免费额度 | `ANYSEARCH_API_KEY`（可选） | ✅ | ❌ | ✅ 匿名 |
| **Tavily** | [控制台](https://app.tavily.com/home) | 每月有免费 credits | `TAVILY_API_KEY(S)` | ✅ | ✅ | ✅ |
| **Exa** | [API Keys](https://dashboard.exa.ai/api-keys) | 注册赠金，以控制台为准 | `EXA_API_KEY(S)` | ✅ | ✅ | ✅ |
| **Jina** | [API Dashboard](https://jina.ai/api-dashboard/) | 以控制台为准 | `JINA_API_KEY(S)` | ✅ | ✅ | ✅ |
| **Firecrawl** | [API Keys](https://www.firecrawl.dev/app/api-keys) | 有免费额度 | `FIRECRAWL_API_KEY(S)` | ✅ | ✅ | ✅ |
| **Querit** | [官网](https://www.querit.ai) | 以账户页面为准 | `QUERIT_API_KEY(S)` | ✅ | ❌ | ✅ |
| **You.com** | [API Keys](https://you.com/platform/api-keys) | 匿名免费档有限额，国内可能需代理 | `YDC_API_KEY`（可选） | ✅ | ❌ | 未实测 |
| **Ollama** | [云端 Keys](https://ollama.com/settings/keys) | 以账户页面为准；不是本地 11434 | `OLLAMA_API_KEY(S)` | ✅ | ✅ | 未实测 |
| **Brave** | [API Dashboard](https://api-dashboard.search.brave.com/app/keys) | 免费计划已调整，以官网为准 | `BRAVE_API_KEY(S)` | ✅ | ❌ | 未实测 |
| **Perplexity** | [API 价格](https://docs.perplexity.ai/docs/getting-started/pricing) | 没有通用免费套餐 | `PERPLEXITY_API_KEY(S)` | ✅ | ❌ | 未实测 |
| **SearXNG** | 自建实例 | 不需要 key，实例需开启 JSON 输出 | `SEARXNG_BASE_URL` | ✅ | ❌ | 未实测 |
| **DuckDuckGo** | 非官方 HTML 端点 | 免费但不稳定，常被反爬 | 无需 key | ✅ | ❌ | 实测常被反爬 |

**"未实测"是什么意思**：适配器按官方文档写好了，但作者没有这家的 key 或环境，**没有做过真实调用**。匿名源也可能因为共享 IP、地区或上游变更暂时不可用。

Perplexity 默认**不参与自动补搜**（它每次搜索是一次大模型调用，比普通搜索贵得多），仍可用 `providers` 点名使用。用 `ESCALATE_EXCLUDE` 调整这份名单。

---

## 配置项完整参考

全部写在配置目录的 `.env` 里，也可以由 MCP 客户端通过环境变量传入。留空等于默认值；名单类配置想明确表示"一个都不要"，写 `none`。

### 省额度

| 变量 | 默认 | 说明 |
|---|---|---|
| `SEARCH_MODE` | `auto` | 默认模式。设成 `free` 后搜索和抓取都绝不发送 key |
| `AUTO_ESCALATE_MAX` | `3` | `auto` 覆盖不足时自动补几家；`0` = 不自动补 |
| `ESCALATE_COUNT` | `3` | `escalate` 每次补几家 |
| `ANON_KEY_FALLBACK` | `true` | 匿名通道被限流时，配了该家 key 就改用 key |
| `FREE_PROVIDERS` | `parallel,keenable,anysearch` | 第一阶段的厂商 |
| `PAID_PROVIDERS` | 火山、Querit、Tavily、Exa … | 通用补搜顺序。没写进来但配了 key 的自动排最后 |
| `ESCALATE_EXCLUDE` | `perplexity` | 不参与自动补搜的厂商（仍可点名） |
| `DAILY_LIMITS` | `baidu:90` | 每个 key 每天最多请求数，如 `baidu:90,tavily:30` |
| `MONTHLY_LIMITS` | 空 | 每个 key 每月最多请求数 |
| `AUTO_MIN_RESULTS` | `4` | 判断门槛：最少证据条数 |
| `AUTO_MIN_UNIQUE` | `3` | 判断门槛：最少相关独立页面数 |
| `AUTO_MIN_CONSENSUS` | `1` | 判断门槛：复杂问题最少三源共识页面数（有官方来源也算通过）；`0` = 不要求 |

### 国内 / 国际

| 变量 | 默认 | 说明 |
|---|---|---|
| `REGION_ROUTING` | `auto` | `auto` / `off` / `cn` / `global` |
| `DOMESTIC_PROVIDERS` | `baidu,volcengine,querit,yuanbao,zhipu,bocha` | 国内优先顺序 |
| `GLOBAL_PROVIDERS` | `tavily,exa,firecrawl,jina` | 国际优先顺序 |

### 抓取

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEB_FETCH_PROVIDER` | 空 | 想固定先用某个后端就写这里，可写多个 |
| `FETCH_MIN_CHARS` | `200` | 正文短于这个长度就换一家复核（只复核一次）；`0` 关闭 |
| `FETCH_KEYED_FALLBACKS` | `2` | 免费后端都失败后，最多再试几个带 key 的后端 |
| `FETCH_MAX_CHARS` | `20000` | 正文截断长度 |

### 网络

| 变量 | 默认 | 说明 |
|---|---|---|
| `SEARCH_PROXY` | 空 | 代理地址，如 `http://127.0.0.1:7897` |
| `PROXY_PROVIDERS` | `jina,duckduckgo,youcom` | 哪几家走上面这个代理 |
| `SEARCH_PROXY_ALL` | `false` | `true` = 所有厂商都走上面这个代理 |
| `USE_SYSTEM_PROXY` | `true` | 其余厂商跟随系统代理设置；`false` = 强制直连 |
| `SEARCH_TIMEOUT` | `15` | 单家超时秒数 |
| `TOTAL_TIMEOUT` | `45` | 一次搜索 / 抓取的总时限，到点带着已有结果返回 |

**关于代理容易误会的一点**：`PROXY_PROVIDERS` 之外的厂商**不是一定直连**。默认它们跟随系统网络设置——系统开着代理（或设了 `HTTPS_PROXY`），请求就会经过它。如果代理开着全局模式，国内厂商也会被绕出去；想一律直连就设 `USE_SYSTEM_PROXY=false`。`aggregate-search status` 的"网络"一节会显示当前走的是哪种。

### 其它

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGGREGATE_SEARCH_HOME` | `~/.aggregate-search` | 配置目录（只能用系统环境变量或客户端 env 设置） |
| `SEARCH_PROVIDERS` | 空 | 限定 `all` 模式查哪几家 |
| `MAX_RESULTS` / `MAX_RESULTS_CAP` | `5` / `20` | 每家默认条数 / 硬上限 |
| `TOTAL_RESULTS` | `12` | 最终返回的页面数 |
| `NEAR_DEDUP` | `true` | 折叠高度相似的转载 |
| `ENABLE_KEYLESS` | `false` | `true` = Firecrawl 匿名、DuckDuckGo、You.com 也默认参与 |
| `QUOTA_COOLDOWN` / `RATE_COOLDOWN` | `3600` / `60` | key 额度耗尽 / 被限流后的冷却秒数 |
| `USAGE_LOG` | `true` | 是否写本地使用记录 |
| `USAGE_LOG_MAX_MB` | `5` | 使用记录超过这个大小就轮转（保留一份旧的） |
| `SEARXNG_BASE_URL` | 空 | 自建 SearXNG 地址 |
| `FIRECRAWL_BASE_URL` | 官方地址 | 自建 Firecrawl 地址 |

还有几项很少改的（地域关键词表、复杂问题关键词、官方来源域名、状态文件位置），见 `aggregate_search/config.py` 里的注释。

---

## 去重与证据合并规则

1. **网址统一**：域名小写、去掉 `www`、默认端口、跟踪参数（`utm_*`、`fbclid` 等）、结尾斜杠；查询参数排序
2. **保留有意义的差异**：`share_token` 之类有功能的参数和 hash 路由（`#/path`）保留，避免把不同页面错并成一条
3. **多份证据都留存**：同一网址的标题、摘要、日期全部保留，并标明每条来自哪家
4. **排序**：被多家搜到的页面优先，其次看各家返回的原始名次
5. **保守折叠**：只有标题和摘要都高度相似的转载才折叠；**版本号、日期、金额等数字不一致时不折叠**（`Python 3.13` 和 `Python 3.14` 的文章不会被合并）
6. **折叠后重排**：保留转载链接，重新计算来源数和排名
7. **展示预算**：每个页面最多展示 3 条不同证据，单条摘要最多 500 字；有省略会写明数量
8. **不隐瞒**：因 `total_results` 没展示的页面会在统计里注明，不会让你以为没搜到

---

## 故障排查

先看状态，再逐家测通：

```bash
aggregate-search status
aggregate-search check
```

| 现象 | 原因与处理 |
|---|---|
| 客户端里没有出现工具 | 命令没找到。把 `command` 换成 `where` / `which` 查到的完整路径，然后重连 |
| 命令突然打不开，报 `No module named aggregate_search` | Windows 上服务运行时做了升级，安装成了半截。断开客户端连接，再执行 `uv tool install --force --reinstall git+https://github.com/luxuriouspoll/aggregate-search-mcp` |
| 改了 `.env` 没生效 | 配置在进程启动时读取，要在客户端里重连 |
| 填了 key 但显示"未配置" | 看 `aggregate-search status` 最上面有没有"配置里有问题"——那一行可能写错了（忘了删行首 `#`、变量名打错） |
| 不确定读的是哪个配置文件 | `aggregate-search status` 第一段会显示 |
| `auto` 一直没用 key | 匿名结果已经过了门槛，这是正常的省额度行为。要更多来源就用 `escalate` |
| 某家显示"已到本地用量上限" | 到了 `DAILY_LIMITS` / `MONTHLY_LIMITS`，到期自动恢复；也可以调大或写 `厂商:0` |
| 某家显示"冷却中" | 上次调用被判为额度耗尽、限流或 key 失效。换了 key 后删掉配置目录里的 `key-state.json` 再重连 |
| Parallel 匿名报 rate limit | 匿名通道有频率限制。配了 `PARALLEL_API_KEY` 会自动改用 key；没配就等一分钟 |
| 结果里出现"等待超过总时限" | 那一家太慢被放弃了，其余结果照常返回。经常出现可以调大 `TOTAL_TIMEOUT`（别超过客户端的工具超时） |
| Jina / You.com / DuckDuckGo 超时 | 国内直连不稳，设置 `SEARCH_PROXY`（默认只有这三家走它） |
| 国内厂商变慢或报网络错误 | 系统代理开着全局模式，把国内厂商也绕出去了。切回规则模式，或设 `USE_SYSTEM_PROXY=false` |
| 百度 / 元宝 / 智谱 / 博查没结果 | 确认已开通搜索服务且有额度；后三家未实测 |
| SearXNG 返回的不是 JSON | 在实例 `settings.yml` 里给 `search.formats` 加上 `json` |

想亲眼看 MCP 的握手过程，仓库里有个小脚本（Windows / macOS / Linux 通用，只做匿名搜索）：

```bash
python mcp_handshake_demo.py aggregate-search
```

---

## 从 0.3 升级

0.3 及更早要求在项目文件夹里建虚拟环境，`.env` 也放在项目文件夹里。0.4 起改为全局安装：

| | 0.3 及更早 | 0.4 起 |
|---|---|---|
| 启动命令 | `项目\.venv\Scripts\python.exe 项目\server.py` | `aggregate-search` |
| `.env` 位置 | 项目文件夹 | `~/.aggregate-search/` |
| 自检 | `python check_providers.py …` | `aggregate-search check / status / selftest` |
| 冷却状态文件 | 项目里的 `.key-state.json` | 配置目录里的 `key-state.json` |
| 代码组织 | 根目录若干个 `.py` + `providers/` | 统一放进 `aggregate_search/` 包 |

升级步骤：

1. 按[快速开始](#快速开始)装好命令
2. 迁移配置：`aggregate-search init --from 旧项目路径/.env`（原样复制，旧文件不动）
3. 把客户端里的启动命令改成 `aggregate-search`，重连
4. `aggregate-search status` 确认读的是新位置

**行为上的变化**（值得留意）：

- **第一次搜索不再带 key**。旧版只要配了 Parallel / Keenable / AnySearch 的 key，"匿名阶段"其实会带着 key 请求
- **抓取默认免费优先**。旧版默认先用 Tavily。想恢复就设 `WEB_FETCH_PROVIDER=tavily`
- **`free` 模式可以点名匿名源了**。旧版里配了 key 的 Parallel 在 `free` 下会被直接拒绝
- **百度千帆默认每天最多 90 次**。不想要写 `DAILY_LIMITS=baidu:0`
- **火山引擎只认 `VOLCENGINE_API_KEY(S)`**。旧版还会把通用名 `WEB_SEARCH_API_KEY` 当成火山的 key，容易把别的工具的 key 误发过去，已去掉
- **Perplexity 不再参与自动补搜**，仍可点名使用
- **新增 `escalate` 模式和 `region` 参数**，AI 不再只能"全量烧"或"完全不烧"

暂时不想改的话，旧的启动方式（`python server.py`）和项目文件夹里的 `.env` 仍然可用，启动日志会提示迁移。

---

## 项目结构与开发

```text
aggregate_search/
  __init__.py       版本号
  __main__.py       python -m aggregate_search
  cli.py            命令行入口（不带参数 = 启动 MCP 服务）
  server.py         三个 MCP 工具、搜索模式、补搜、统一输出
  config.py         配置目录、.env 解析、全部配置项
  keypool.py        多 key 轮转、并发分配、冷却状态
  usage.py          本地使用记录与用量上限
  quality.py        本地判断：覆盖度、共识、官方来源
  routing.py        国内 / 国际 / 混合识别与补搜排序
  selftest.py       离线自测
  providers/
    __init__.py     厂商注册表、调度、匿名 / 带 key 的调用记录
    base.py         HTTP 封装、脱敏、URL 归一化、证据合并
    *.py            19 个厂商适配器
  templates/        .env 与 keys.json 模板
server.py           旧启动方式的兼容入口
mcp_handshake_demo.py   手动演示一次完整的 MCP 对话
```

### 从源码开发

```bash
git clone https://github.com/luxuriouspoll/aggregate-search-mcp.git
cd aggregate-search-mcp
python -m venv .venv
.venv/bin/python -m pip install -e .          # Windows: .venv\Scripts\python.exe
.venv/bin/python -m aggregate_search selftest
```

### 关于自测

`selftest` 在临时沙盒里跑，用的是假 key：不联网，也不读不改你的真实配置。它测两层东西：

- **调度**：把各家的网络调用换成假后端，验证搜索模式、补搜、多 key 轮换、成本记账、抓取回退等逻辑
- **适配器本身**：用不联网的假响应喂给 19 家真实的适配器代码，覆盖正常响应、各家的业务报错，以及几十种"不对劲"的响应（不是 JSON、字段缺失、字段类型不对……）。适配器遇到它们只允许正常返回或报"调用失败"，不允许冒出程序错误

改了代码先跑它。新增或修改适配器后尤其要跑——假后端测不到适配器自己的解析代码。

### 新增一家厂商

1. 在 `providers/` 里照着现有文件写 `search(client, key, query, max_results)`，需要抓取再加 `fetch(client, key, url)`
2. 在 `providers/__init__.py` 的 `REGISTRY` 里登记
3. 在 `config.py` 的 `_KEY_ENV_NAMES` 里加上它的变量名

几条约定：

- **key 只放请求头**，不要拼进 URL——URL 会出现在日志和报错里
- 接口用「HTTP 200 + 报错文案」表示失败时，搜索用 `business_error()`，抓取用 `fetch_error()`（后者不会拿目标网页的 401/403 去判我们的 key 失效）
- 把响应体放进报错前先过一遍 `brief()`（先脱敏再截断）
- 从响应里取字段用 `text_of()` / `as_items()` / `as_dict()`，不要直接 `.strip()`——上游哪天改了字段类型，适配器不该因此报错

---

## 安全说明

- **真实 key 只在本机**：存在配置目录的 `.env` / `keys.json`，或客户端的环境变量里。仓库里只有占位符模板
- **`.gitignore` 已排除**：`.env`、`.env.*`、`keys.json`、`key-state.json`、`usage.jsonl`、`usage-counters.json`、`*.tmp`
- **错误信息自动脱敏**：返回给客户端的报错、日志、冷却状态、使用记录在写出前都会把 key 抹掉
- **只记指纹**：冷却状态和用量计数只记录 key 的 SHA-256 摘要
- **不跟危险跳转**：带 key 的请求不会跟着跳到别的站点，也不会从 https 跳到不加密的 http
- **文件权限**：类 Unix 系统上配置目录和 `.env` 只有你自己能读写
- **使用记录只在本机**：里面有搜过的问题，不想留就设 `USAGE_LOG=false`
- **代理可控**：`SEARCH_PROXY` 默认只给指定厂商用；其余厂商走哪条网络见[网络](#网络)一节
- **输出内容不可信**：搜回来的网页正文会被塞进 AI 的上下文，网页里如果埋了"指令"（提示词注入）可能影响判断。聚合工具天然放大这个问题，需要时让 AI 把可疑内容当作数据而不是命令
- 使用各家搜索服务时，请遵守它们的服务条款和配额规定

---

## 许可

本仓库未附带开源许可证文件。如需引用或分发，请先与作者联系。
