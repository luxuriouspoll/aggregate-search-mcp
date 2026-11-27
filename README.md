# aggregate-search-mcp

[English](README_EN.md) · 简体中文

[![CI](https://github.com/luxuriouspoll/aggregate-search-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/luxuriouspoll/aggregate-search-mcp/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/luxuriouspoll/aggregate-search-mcp)](https://github.com/luxuriouspoll/aggregate-search-mcp/releases)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**免费优先的多源聚合联网搜索 MCP 服务** —— 标准 MCP 协议通用实现，把 19 家国内外搜索与网页抓取通道收拢成统一入口，交给 Claude Code、Codex、DeepSeek Harness（dsh）、ZCode、Cursor、Cline 等任意 AI 客户端直接共用。

[快速开始](#快速开始) · [新手与升级指南](docs/usage-guide.md) · [接入客户端](#接入-mcp-客户端) · [支持厂商](#厂商注册地址) · [版本发布](https://github.com/luxuriouspoll/aggregate-search-mcp/releases) · [变更记录](CHANGELOG.md)

```text
用户提问 ──▶ 1. 免费匿名源先查（0 成本） ──▶ 2. 本地站点/时间过滤与去重
         ──▶ 3. 覆盖度门禁判断 ──▶ 4. 不足才按国内外路由补搜 3 家 ──▶ 5. 输出带来源与成本账单的合并证据
```

| 核心特性 | 说明 |
|---|---|
| **零 Key 开箱即用** | 内置 Parallel、Keenable、AnySearch 匿名通道，不申请任何 API Key 也能立刻联网搜索与抓正文 |
| **按需补搜省额度** | 简单问题不烧账号额度；覆盖不足才按「国内 / 国际 / 混合」软路由补查 3 家不同厂商 |
| **多 Key 轮换与硬限额** | 同厂商多 Key 自动轮询与故障冷却；SQLite WAL 多进程原子计费，支持单 Key 日/月上限与服务端禁 Key 硬锁 |
| **高信噪比与原文阅读** | 同网址跨源合并、近似转载折叠、官方来源加权、共享摘录预算；长文支持 `start_index` 分段续读与 `focus` 聚焦定位；外部内容包裹 `<untrusted_content>` 防提示词注入 |
| **轻量易部署** | 仅依赖 `mcp` 与 `httpx`（Python 3.10+）；一次全局安装到处可用，同时支持本机 `stdio` 与远程 [Streamable HTTP](#可选-http-传输) |

> **已在用 `0.5.0` ~ `0.5.3`？** 不用删旧 MCP，也不用重填 Key。按 [使用指南 · 就地升级](docs/usage-guide.md#1-已有-050不删旧-mcp就地升级) 更新程序，验收后重连原来的注册项。
>
> **第一次使用？** 推荐直接看 [手把手使用指南](docs/usage-guide.md)，几分钟内完成安装和第一次匿名实搜。各家免费额度以厂商控制台实时说明为准。

---

## 目录

- [使用指南（安装、升级与首次搜索）](docs/usage-guide.md)
- [它解决什么问题](#它解决什么问题)
- [快速开始](#快速开始)
  - [升级与卸载](#升级与卸载)
- [接入 MCP 客户端](#接入-mcp-客户端)
  - [可选 HTTP 传输](#可选-http-传输)
- [配置 key](#配置-key)
- [四种搜索模式](#四种搜索模式)
- [三个工具](#三个工具)
  - [时间与站点过滤](#时间与站点过滤)
- [省额度与防超支](#省额度与防超支)
- [命令行](#命令行)
- [厂商注册地址](#厂商注册地址)
- [配置项完整参考](#配置项完整参考)
  - [配置热加载](#配置热加载)
- [外部内容隔离](#外部内容隔离防提示词注入)
- [去重与证据合并规则](#去重与证据合并规则)
- [故障排查](#故障排查)
- [从旧版升级](#从旧版升级)
- [项目结构与开发](#项目结构与开发)
- [离线评测（evals）](evals/README.md)
- [安全说明](#安全说明)
- [变更记录](#变更记录)
- [许可](#许可)

---

## 它解决什么问题

直接调一家搜索 API，会遇到三个麻烦：**额度会烧完、结果可能不全、不同家返回的内容重复**。

这个 MCP 的处理方式是：

1. **匿名优先**。先查免费的 Parallel、Keenable、AnySearch；某家匿名通道限流时，`auto` 可按配置切换到同厂商的账号通道。
2. **判断够不够**。先按站点、发布日期过滤（如有指定），再折叠近似重复；用留下的结果判断相关独立页面数、证据条数、多渠道命中同页或官方一手来源。
3. **不够再补查**。按「国内 / 国际 / 混合」调整顺序，默认补 3 家**不同**厂商，而不是把一家的 3 个 key 全打一遍。
4. **合并证据**。同一网址的多份摘要、日期、来源都保留，标明每条信息的出处。
5. **核验信息**。本地检查只决定要不要补搜，不证明内容正确；AI 还要读证据，核验来源。

每次搜索都会说明**实际成本**：哪些请求匿名发出、哪些发出了带 key 的请求、用的是第几个 key。key 在冷却或达到用量上限、请求没发出去的，不算动用了 key。

---

## 快速开始

> **升级代码**或修改[启动配置](#配置热加载)后需要重启服务；stdio 客户端通常断开再重连即可。Windows 上请在升级前先断开，避免程序被占用而报 `拒绝访问 (os error 5)`。
>
> **修改运行配置**后通常不用重连，从下一次调用生效。如果国内厂商因系统代理的全局模式变慢，可切回规则模式，或设 `USE_SYSTEM_PROXY=false` 让未指定 `SEARCH_PROXY` 的厂商直连。

### 第 1 步：安装命令

推荐从固定发布标签安装（也可从 [Releases](https://github.com/luxuriouspoll/aggregate-search-mcp/releases) 下载 `.whl` 安装）：

```bash
uv tool install 'git+https://github.com/luxuriouspoll/aggregate-search-mcp@v0.5.4'
uv tool update-shell                # 首次用 uv 装全局工具必跑：把 ~/.local/bin 写入系统 PATH
```

> **Windows / 新装 `uv` 防坑提示**：首次用 `uv tool install` 安装工具时，末尾常提示 `` `C:\Users\...\.local\bin` is not on your PATH ``，此时直接在当前窗口运行 `aggregate-search` 会报“无法将‘aggregate-search’项识别为 cmdlet…”。执行一次上面的 `uv tool update-shell`，然后**关掉并重新打开终端窗口**（已打开的旧窗口和桌面客户端不会自动刷新 `PATH`），再验证版本：

```bash
aggregate-search --version          # 显示 aggregate-search 0.5.4
```

想跟随开发主分支可用 `@master`；也可克隆仓库后在根目录用 `uv tool install .` 或 `pipx install .`（虚拟环境内用 `python -m pip install .`）。

### 第 2 步：生成配置文件

```bash
aggregate-search init
```

默认配置目录：

| 系统 | 配置目录 |
|---|---|
| Windows | `C:\Users\你的用户名\.aggregate-search\` |
| macOS / Linux | `~/.aggregate-search/` |

`init` 会放入带注释的 `.env` 和 `keys.example.json`，不覆盖已有配置。运行时，冷却和用量计数存入 `state.db`；启用使用记录时，默认写 `usage.jsonl`。

想换配置目录，在启动服务前设环境变量 `AGGREGATE_SEARCH_HOME=绝对路径`；用 `aggregate-search path` 查看当前目录。

**这一步之后就已经能用了**——不填任何 key，也能用匿名源。

### 第 3 步：自检

```bash
aggregate-search selftest                # 离线自测：不联网，不碰你的配置
aggregate-search status                  # 看哪些厂商已启用
aggregate-search search "MCP 是什么"      # 真的搜一次
```

### 升级与卸载

已有 uv 全局安装的用户，只需就地升级；保留 MCP 注册项和 `aggregate-search path` 指向的配置目录。**Windows 上请先断开所有连着它的客户端（如 ZCode、Codex、dsh 等）**，避免文件占用报 `拒绝访问 (os error 5)`，然后任选其一：

```bash
# 升级到最新固定发布版（推荐，不需要先克隆）
uv tool install --force --reinstall 'git+https://github.com/luxuriouspoll/aggregate-search-mcp@v0.5.4'

# 或在已更新的本地源码仓库根目录执行
uv tool install --force --reinstall .
```

用 `aggregate-search --version` 确认新版，再重连客户端。遇到 `拒绝访问 (os error 5)`，先停止仍占用程序的旧进程，不要删配置或重复注册。完整步骤见 [使用指南](docs/usage-guide.md)。

只有确定不再使用时才卸载：`uv tool uninstall aggregate-search-mcp`。它不会删除配置目录。

---

## 接入 MCP 客户端

本服务遵循标准 MCP 协议，**不绑定特定客户端或大模型**（Claude、GPT、DeepSeek、Gemini 等均可调用）。同一台电脑上同时在多个 Agent 里挂载时，各进程会通过 `~/.aggregate-search/state.db`（SQLite WAL）安全共享同一份 `.env` 密钥池、冷却状态与日/月用量上限，无需重复填 Key。

下面的配置示例使用默认的 stdio：**启动命令填 `aggregate-search`，不需要参数，也不用在这里填 key**。跨机器、容器或工作流平台（OpenClaw、Dify、n8n 等）可改用 [Streamable HTTP](#可选-http-传输)；不支持 MCP 但能跑终端命令的 Agent 可直接调 `aggregate-search search "关键词"`。

### 通用写法（Claude Desktop、Cursor、Cline、Windsurf 等）

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

### DeepSeek Harness（dsh）

可在设置界面添加 stdio MCP（命令填 `aggregate-search`，参数留空），或在对应 Profile（如 `~/.dsh/profiles/desktop/cordis.patch.yml` 或 `web/cordis.patch.yml`）追加：

```yaml
- insert:
    - id: mcp-aggregate-search
      name: "@deepseek-ai/dsh-mcp-client"
      config:
        serverName: "aggregate-search"
        transport: "stdio"
        command: "aggregate-search"
        args: []
```

### ZCode

在 `~/.zcode/cli/config.json` 中，保留其它 MCP，并将这项合并到 `mcp.servers`：

```json
{
  "mcp": {
    "servers": {
      "aggregate-search": { "command": "aggregate-search", "args": [] }
    }
  }
}
```

### 客户端找不到命令怎么办

刚跑完 `uv tool update-shell` 时，**已打开的图形界面客户端（ZCode、Cursor、dsh 桌面版等）仍缓存着旧的环境变量**；部分 GUI 程序也不读取终端 Shell 的 PATH。完全退出重开客户端，或直接把 `command` 换成查出来的绝对路径最稳：

```bash
where.exe aggregate-search # Windows PowerShell/CMD，例如 C:\Users\你\.local\bin\aggregate-search.exe
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

改完**客户端连接配置**后请重连。服务端 `.env` / `keys.json` 中的运行配置从下一次调用生效，例外见[配置热加载](#配置热加载)。

### 可选 HTTP 传输

不想由客户端拉起本机的 stdio 子进程时，可以单独启动 Streamable HTTP 服务：

```bash
aggregate-search serve --transport streamable-http --host 127.0.0.1 --port 8000
```

`127.0.0.1` 仅本机可访问。HTTP 客户端填写服务启动日志里的 MCP 地址（默认 `http://127.0.0.1:8000/mcp`）。给局域网或远程客户端用时，把 `--host` 改为服务机的局域网 IP 或 `0.0.0.0`；客户端连接服务机的实际 IP 或域名，不填 `0.0.0.0`。

绑定非回环地址时，须在服务端配置 `MCP_HTTP_TOKEN`（至少 16 个字符），否则服务拒绝启动。可用 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成随机 Token。只要配置了 Token，客户端就必须发送 `Authorization: Bearer <token>`，否则返回 401。通过不受信任的网络访问时，请用 HTTPS，避免明文传输 Token。

---

## 配置 key

打开配置目录里的 `.env`，去掉行首的 `#`，把等号后面换成自己的 key：

```dotenv
TAVILY_API_KEY=tvly-your-key
VOLCENGINE_API_KEY=your-volcengine-key
```

写错了不会让服务起不来：有问题的行会被跳过，并显示在 `aggregate-search status` 最上面（只告诉你是第几行，不显示那一行的内容）。

某一项留空（如 `SEARCH_PROXY=`）按未设置处理。配置文件支持 UTF-8、带 BOM 的 UTF-8、UTF-16、GBK。

### 同一家配多个 key

用带 `S` 的变量名，逗号分隔：

```dotenv
TAVILY_API_KEYS=tvly-your-first-key,tvly-your-second-key
```

多个 key 会轮流使用。额度耗尽、被限流或失效时自动切换，原 key 到期后可继续尝试。同一进程内的并发请求优先分到不同 key；所有可用 key 都忙时可以共用，仍受用量上限约束。冷却状态存入 `state.db`，不保存 key 明文。

也可以用 `keys.json` 集中管理（把配置目录里的 `keys.example.json` 复制成 `keys.json` 再填）。

### 优先级

**MCP 客户端传入的非空环境变量 > `.env` > `keys.json`**。

对同一家厂商，只取提供了 key 的最高一层，不跨层合并单数、复数或别名变量。例如客户端传了 `TAVILY_API_KEY`，就不会再把 `.env` 的 `TAVILY_API_KEYS` 加进来；客户端没有提供 Tavily 的 key 才读 `.env`，两处都没有才用 `keys.json`。其它配置项按变量名优先取客户端非空值，再取 `.env`。

### 匿名三家要不要配 key

Parallel、Keenable、AnySearch 不配 key 就能搜。即使配了，`auto` 的第一阶段也优先匿名；匿名通道限流且 `ANON_KEY_FALLBACK=true` 时，才可能用同厂商的 key 重试。点名 `providers`、用 `all` 或补搜时也可能使用账号 key。需要始终匿名时开启下文的服务端硬锁。

---

## 四种搜索模式

| 模式 | 做什么 | 会不会用账号 key |
|---|---|---|
| `auto`（出厂默认） | 先匿名查；覆盖不足才自动补厂商（默认 3 家） | 可能：匿名限流回退或自动补搜时 |
| `free` | 本次搜索只走匿名通道，即使点名厂商也不带 key | **不会** |
| `escalate` | 跳过初次匿名阶段，补查尚未查询的厂商（默认 3 家），并合并同一问题的近期结果 | 可能 |
| `all` | 查所有已启用的厂商 | 可能，通常最费额度 |

**`SEARCH_MODE=free` 是默认免费模式，不是硬锁**。不传模式时搜索只走匿名，调用方仍可用 `search_mode` 或 CLI `--mode` 显式切换。抓取没有模式参数，跟随默认模式：设为 `free` 时，无论是否指定后端，都只走匿名。`aggregate-search check` 不受默认模式限制。

**要禁止服务发送账号 key**，在服务端 `.env` 设 `SEARCH_NO_ACCOUNT_KEYS=true`。硬锁开启后，搜索、抓取和 `aggregate-search check` 都只允许匿名；指定 `search_mode`、厂商或抓取后端都不能绕过。改动从下一次调用生效；要解除硬锁请明确设为 `false`，误删该行不会在运行中的服务上解锁。若客户端传入了同名非空环境变量，仍以客户端值为准。

### 推荐给 AI 的用法

1. 先用默认的 `auto`
2. 读返回的证据，自己判断够不够用
3. 不够：**同一个 query** 再调一次，传 `search_mode="escalate"`。它会换 3 家没查过的，并把前后结果合并去重
4. 还不够可以继续 `escalate`（每次换一批），或者换个问法

每次返回的"下一步"一行会直接告诉 AI 接下来会补哪几家、会不会花额度。几个细节：

- `escalate` 靠进程内缓存记住同一问题查过谁，有效期 15 分钟。中间穿插一次 `auto` 或点名查询，不会让它忘记
- 带 `time_range` / `domains` 搜索后，如需合并此前结果，`escalate` 时要传同样的筛选条件
- `escalate` 不能和 `providers` 同时传（它自己挑还没查过的厂商）
- 同一进程内，问题和筛选条件都相同的 `escalate` 会排队，避免重复补同一批厂商；筛选条件不同则可并行

想让 AI 决定要不要补搜，可把 `AUTO_ESCALATE_MAX=0`；这不会禁用 `ANON_KEY_FALLBACK`，也不能代替服务端硬锁。

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
| `search_mode` | 否 | `auto` / `free` / `escalate` / `all`；未指定时用 `SEARCH_MODE`（出厂默认 `auto`） |
| `region` | 否 | `domestic` / `global` / `mixed`，不传自动判断 |
| `time_range` | 否 | `day` / `week` / `month` / `year`（也认 `24h`、`7d`、`本月`）；不传则不限制日期 |
| `domains` | 否 | 只返回指定站点及其子域名，如 `["docs.python.org","github.com"]`，最多 20 个 |
| `providers` | 否 | 只查指定厂商，如 `["tavily","exa"]`。配了 key 的一般会带 key；本次只想匿名可传 `search_mode="free"`，服务端硬锁始终优先 |
| `max_results` | 否 | 每家取几条，默认 5 |
| `total_results` | 否 | 最终返回多少个页面，默认 12 |

```json
{"query": "MCP 是什么"}
{"query": "MCP 是什么", "search_mode": "escalate"}
{"query": "中国最新的人工智能政策", "search_mode": "escalate", "region": "domestic"}
{"query": "MCP 是什么", "search_mode": "free"}
{"query": "MCP 是什么", "providers": ["tavily", "exa", "parallel"], "max_results": 3}
{"query": "Go 泛型", "time_range": "month", "domains": ["go.dev"]}
```

返回内容：检索渠道数与转载折叠后的独立原始页面数、查了几家 / 几家有结果 / 失败几家、本地判断结论、**实际成本**、下一步建议、多渠道命中同页、去重后的证据列表（含共享预算摘录、来源与 SearXNG 底层引擎溯源）、每家的耗时和状态。

一次搜索有**总时限**（默认 45 秒）：到点还没返回的厂商直接放弃，带着已经拿到的结果返回，不会拖到客户端超时把结果全丢掉。

#### 时间与站点过滤

显式指定 `time_range` 时，以 UTC 当前时间为基准，`day` / `week` / `month` / `year` 分别是最近 **1 / 7 / 30 / 365 天**。只返回发布日期可核验且落在窗口内的结果；缺少可靠发布日期或超出窗口的结果不保留。不传 `time_range` 则不限制日期。

本地会复核站点和日期条件；近似去重开启时，也会先折叠转载。这些步骤都在覆盖度判断之前完成，不合条件或重复的页面不会抬高 `auto` 的覆盖度。

发布日期支持 ISO 日期、中文年月日（如 `2026年11月25日`），以及带明确时区的 ISO 或 RFC 时间戳。不会把无时区时间戳、相对时间、网页更新时间或爬取时间换算成发布日期。Perplexity 是按厂商契约处理的例子：[Sonar 的 OpenAPI](https://docs.perplexity.ai/openapi.json) 明确区分 `search_results[].date`（发布日期）与 `last_updated`（更新时间）；其他厂商仅名为 `date` 的字段不能据此类推。

### `web_fetch` — 抓网页正文、聚焦定位与分段续读

| 参数 | 必填 | 说明 |
|---|---|---|
| `url` | 是 | 要抓取的 `http` / `https` 网页地址（调用前校验格式、主机名与端口合法性） |
| `provider` | 否 | 显式指定单一抓取后端；不传按免费优先顺序尝试 |
| `start_index` | 否 | 非负字符位置，默认 `0`；用于接着上一次末尾顺序续读缓存原文 |
| `focus` | 否 | 可选关键词或版本号/数字（如 `"配置代理"`、`"0.5.3"`、`"429"`）；按相关性提取段落并输出原文区间位置 |

```json
{"url": "https://example.com"}
{"url": "https://example.com", "provider": "jina"}
{"url": "https://example.com/long-doc", "focus": "配置代理"}
{"url": "https://example.com/long-doc", "start_index": 20000}
```

阅读与抓取策略：

- **分段续读与缓存边界**：单页展示上限 `FETCH_MAX_CHARS=20000`，单文档缓存及上游抓取上限 `FETCH_CACHE_MAX_CHARS=100000`（两项均支持热加载）。位置按缓存中已脱敏文本的 Python 字符计数（中英文一致），输出给出半开区间 `[start, end)` 和下一页 `start_index`。缓存最多保留 64 篇 / 总正文 400 万字符，默认有效期 `300` 秒；**只有命中仍在有效期内且满足当前账号策略的缓存时，翻页才不发额外 HTTP 请求**（过期、淘汰或配置变化后重新抓取，原文位置可能变化）。
- **聚焦定位（`focus`）**：支持中文词汇、英文词与纯数字/版本号（如 `0.5.3`、`429`）。定位时保留 `start_index` 之前的 Markdown 章节标题上下文，超长段落按 80 字符重叠切分并去重合并，围绕命中关键词保留上下文，且跨边界按全文完整数字校验（不会把 `1429` 拆成 `429`）。未命中时退回从 `start_index` 起顺序输出。
- **GitHub 仓库首页直取**：抓取标准 HTTPS 仓库首页（如 `https://github.com/owner/repo` 或 `.git` 结尾）且未指定 `provider` 时，优先从 `raw.githubusercontent.com` 的 `HEAD` 分支匿名直取 README（依次尝试 `README.md`、`readme.md`、`README.rst`、`README`、`README.MD`），不调用 GitHub REST API、拒绝重定向、下载上限 `2 MB`（其余后端解码响应上限 `8 MB`），保留原始 `github.com` 引用。Issue、PR、Release、具体文件与目录链接仍按原目标抓取。
- **匿名优先回退链**：

1. `WEB_FETCH_PROVIDER` 指定的后端（如果你设置了）
2. Parallel、Keenable（匿名，不花额度）
3. 配了 key 的后端：Tavily → Jina → Firecrawl → Exa → Ollama → Parallel / Keenable 的带 key 通道

为了不白花额度：

- 某家失败自动换下一家；抓回来是空的也算失败
- 正文太短（疑似反爬页或空壳）会再换一家复核，**只复核一次**
- 免费后端都抓不到时，最多再试 2 个带 key 的后端——连它们都抓不到，多半是地址本身有问题，不再继续消耗额度
- 抓取工具报的错（"403 Forbidden""需要登录"之类）多半是目标网页的问题，不会因此把你的 key 判成失效

`web_fetch` 不接受 `search_mode`。默认模式为 `free` 或开启 `SEARCH_NO_ACCOUNT_KEYS=true` 时，只允许匿名后端；显式指定后端也不能用于发送账号 key。抓取命中缓存时不会发出请求；如果启用了使用记录，会记一次缓存命中，出站请求与额度消耗都记为零。只支持 `http` / `https`。

### `web_status` — 看状态

```json
{}
{"usage_days": 7}
```

显示：配置文件在哪、配置里有没有写错的行、每家是否启用、key 可用数、冷却和今日用量、抓取顺序、当前能用于补搜的厂商、走的是哪条网络，以及当前单页展示 / 单文档缓存 / 共享摘录字符预算。传 `usage_days` 还会附上近几天的使用统计。

---

## 省额度与防超支

### 本地用量上限

有些厂商超出免费额度后会直接计费。可以给每个 key 设每天 / 每月最多发多少次请求，到量自动停用，第二天 / 下个月恢复：

```dotenv
DAILY_LIMITS=baidu:90,tavily:30
MONTHLY_LIMITS=tavily:900,exa:900
```

- **百度千帆内置 `baidu:90`**（官方每日免费 100 次，留了余量）。不想要就写 `baidu:0`
- 同一配置目录下的服务进程通过 `state.db`（SQLite WAL）共享用量和冷却状态。设了上限的 key 要先在同一事务内检查日 / 月用量并占额，才能发出请求，避免并发请求超用同一份额度
- 计数针对本服务发出的请求，不是厂商的账单；它管不到其它应用或使用其他配置目录的服务，不能替代厂商控制台的额度告警
- 冷却到期自动解除。确认上游账号已恢复且不想等到期时，用 `aggregate-search reset-cooldown tavily` 预览目标厂商，再用 `aggregate-search reset-cooldown tavily --apply` 清除当时仍有效的账号冷却。**不要删除 `state.db`**：重置冷却不会清账，已达本地上限的 Key 仍不可用。

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
| Parallel | 每次搜索 3 次网络往返（握手 2 次 + 调用） | 通常 1 次；需要握手时，匿名会话与账号认证会话分开复用 |
| 抓取 | 默认先用 Tavily（花额度） | 默认先用匿名后端 |
| 每家的连接 | 每次调用重新加载证书，约 0.8 秒/家 | 整个进程只加载一次 |

---

## 命令行

不带参数的 `aggregate-search` 用于 stdio 客户端接入；`serve` 可单独启动 HTTP 服务。其余子命令可用于本地试用和排查：

| 命令 | 作用 |
|---|---|
| `aggregate-search` | 启动 MCP 服务（stdio） |
| `aggregate-search serve --transport streamable-http --host 127.0.0.1 --port 8000` | 用 [Streamable HTTP](#可选-http-传输) 启动（此地址仅本机可访问） |
| `aggregate-search init` | 生成配置目录和 `.env` 模板（已有的不覆盖） |
| `aggregate-search init --from 路径` | 从已有的 `.env` 导入 |
| `aggregate-search path` | 显示配置目录 |
| `aggregate-search status` | 显示这个新进程读取的配置、Key、用量和抓取顺序；确认常驻服务当前限额请用 `web_status` |
| `aggregate-search reset-cooldown <厂商> [--apply]` | 默认只预览；加 `--apply` 才清除该厂商有效的账号 Key 冷却，包括已移出配置的旧指纹。不会清除日/月用量或匿名限流 |
| `aggregate-search search 关键词` | 搜一次。可加 `--mode`、`--region`、`--providers`、`--max`、`--total`、`--time-range`、`--domains` |
| `aggregate-search fetch URL [后端] [--start-index N] [--focus 关键词]` | 抓网页正文，支持按字符位置续读与关键词聚焦（`--start-index` 为负数时退出码为 `2`） |
| `aggregate-search check [关键词]` | 发送真实请求逐家诊断；带 key 的请求会计入用量，开启 `SEARCH_NO_ACCOUNT_KEYS=true` 后只测匿名通道 |
| `aggregate-search usage [天数]` | 使用记录汇总，默认 7 天 |
| `aggregate-search selftest` | 离线自测，不联网，不读也不改你的配置 |

例如，搜索最近 30 天的指定站点（`--domains` 用逗号隔开），或聚焦抓取某个仓库：

```bash
aggregate-search search "Python 版本更新" --time-range month --domains docs.python.org,python.org
aggregate-search fetch "https://github.com/modelcontextprotocol/modelcontextprotocol" --focus "stdio"
```

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

**未实测的厂商不会被禁用**，但在补搜顺序里排在已实测厂商之后（国内/国际的分组顺序不变），`aggregate-search status` 里也会标 `⚠️ 未实测`。想调整这份名单，用 `UNVERIFIED_PROVIDERS`。

Perplexity 默认**不参与自动补搜**（它每次搜索是一次大模型调用，比普通搜索贵得多），仍可用 `providers` 点名使用。用 `ESCALATE_EXCLUDE` 调整这份名单。

---

## 配置项完整参考

通常写在配置目录的 `.env` 里，也可以由 MCP 客户端通过环境变量传入。留空按未设置处理；名单类配置要明确表示「一个都不要」，写 `none`。

### 配置热加载

保存服务端的 `.env` 或 `keys.json` 后，运行配置在**下一次调用**生效：包括 key 与其变量别名、日 / 月限额、搜索和抓取的路由与开关、`SEARCH_*` 代理设置（以及 `PROXY_PROVIDERS`、`USE_SYSTEM_PROXY`）、抓取缓存和超时。临时读不到文件时保留该文件上一份有效配置，另一个文件的正常更新仍可生效。

为防止保存中途误放宽额度，运行中的服务不会因省略某家限额就清除它。要取消某家上限，在对应的日 / 月配置写 `厂商:0`；要改回百度的内置日上限，写 `baidu:90`。

下列设置仍需**重启服务**：配置目录 `AGGREGATE_SEARCH_HOME`、使用记录文件路径 `USAGE_LOG_FILE`、传输方式和监听地址 / 端口，以及 HTTP Bearer Token `MCP_HTTP_TOKEN`。修改客户端注入的环境变量后也要重启子进程；旧的非空客户端 env 仍优先于 `.env`。

### 省额度

| 变量 | 默认 | 说明 |
|---|---|---|
| `SEARCH_MODE` | `auto` | 默认搜索模式；`free` 时默认搜索和抓取都走匿名，只有搜索可显式改模式；不限制 `check` |
| `SEARCH_NO_ACCOUNT_KEYS` | `false` | `true` = 服务端硬锁：搜索、抓取和 `check` 只允许匿名，工具参数不可覆盖 |
| `AUTO_ESCALATE_MAX` | `3` | `auto` 覆盖不足时自动补几家；`0` = 不自动补 |
| `ESCALATE_COUNT` | `3` | `escalate` 每次补几家 |
| `ANON_KEY_FALLBACK` | `true` | `auto` 的匿名通道被限流时可改用同厂商的 key；硬锁开启时不回退 |
| `FREE_PROVIDERS` | `parallel,keenable,anysearch` | 第一阶段的厂商 |
| `PAID_PROVIDERS` | 火山、Querit、Tavily、Exa … | 通用补搜顺序。没写进来但配了 key 的自动排最后 |
| `ESCALATE_EXCLUDE` | `perplexity` | 不参与自动补搜的厂商（仍可点名） |
| `UNVERIFIED_PROVIDERS` | `yuanbao,zhipu,bocha,brave,ollama,youcom,perplexity,searxng` | 未做真实调用验证的厂商：补搜排在已实测之后，状态页标注，**不禁用** |
| `ADAPTIVE_ROUTING` | `true` | 按近 7 天表现微调补搜顺序（只在本组内，不跨区、不剔除） |
| `ADAPTIVE_MIN_CALLS` | `5` | 至少这么多次真实调用才参与调权 |
| `DAILY_LIMITS` | `baidu:90` | 每个 key 每天最多请求数，如 `baidu:90,tavily:30` |
| `MONTHLY_LIMITS` | 空 | 每个 key 每月最多请求数 |
| `AUTO_MIN_RESULTS` | `4` | 判断门槛：最少证据条数 |
| `AUTO_MIN_UNIQUE` | `3` | 判断门槛：最少相关独立页面数 |
| `AUTO_MIN_CONSENSUS` | `1` | 判断门槛：复杂问题最少三渠道命中同页数（有官方来源也算通过）；`0` = 不要求 |

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
| `FETCH_CACHE_TTL` | `300` | 抓取缓存有效期（秒）；`0` = 不缓存 |
| `FETCH_MAX_CHARS` | `20000` | 单次原文展示上限（热加载） |
| `FETCH_CACHE_MAX_CHARS` | `100000` | 单文档缓存与上游抓取上限（支持该参数的上游同步截断），热加载 |
| `SEARCH_EXCERPT_MAX_CHARS` | `6000` | 所有页面共享的摘录字符预算，热加载 |

### 网络

| 变量 | 默认 | 说明 |
|---|---|---|
| `SEARCH_PROXY` | 空 | 代理地址，如 `http://127.0.0.1:7897` |
| `PROXY_PROVIDERS` | `jina,duckduckgo,youcom` | 哪几家走上面这个代理（直连 GitHub README 不稳时也可追加 `github`） |
| `SEARCH_PROXY_ALL` | `false` | `true` = 所有厂商都走上面这个代理 |
| `USE_SYSTEM_PROXY` | `true` | 其余厂商跟随系统代理设置；`false` = 强制直连 |
| `SEARCH_TIMEOUT` | `15` | 单家超时秒数 |
| `TOTAL_TIMEOUT` | `45` | 一次搜索 / 抓取的总时限，到点带着已有结果返回 |

**关于代理容易误会的一点**：`PROXY_PROVIDERS` 之外的厂商**不是一定直连**。默认它们跟随系统网络设置——系统开着代理（或设了 `HTTPS_PROXY`），请求就会经过它。如果代理开着全局模式，国内厂商也会被绕出去；想一律直连就设 `USE_SYSTEM_PROXY=false`。`aggregate-search status` 的"网络"一节会显示当前走的是哪种。

### 其它

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGGREGATE_SEARCH_HOME` | `~/.aggregate-search` | 配置目录，只能通过进程环境变量设置，改动需重启 |
| `USAGE_LOG_FILE` | 配置目录下的 `usage.jsonl` | 本地使用记录路径，改动需重启 |
| `MCP_HTTP_TOKEN` | 空 | HTTP Bearer Token；绑定非回环地址时必填（至少 16 个字符），改动需重启 |
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

还有几项很少改的（地域关键词表、复杂问题关键词、官方来源域名），见 `aggregate_search/config.py` 里的注释。

---

## 外部内容隔离（防提示词注入）

搜回来的网页标题、摘要、正文都来自互联网，里面可能藏着"忽略以上指令"之类的话术。所以所有外部内容都会被包进明确的边界标签：

```text
<untrusted_content source="tavily" url="https://example.com/a">……原文……</untrusted_content>
```

结果开头还会有一句提示，告诉读它的 AI：标签里是外部不可信数据，只能当素材，不能当指令执行。

防逃逸：如果网页内容里自己伪造了一个 `</untrusted_content>`，会被转义成 `&lt;/untrusted_content&gt;`，无法提前闭合标签把后面的内容"放出来"。

`web_fetch` 返回的标题和正文同样会包进去。

## 去重与证据合并规则

1. **网址统一**：域名小写、去掉 `www`、默认端口、跟踪参数（`utm_*`、`fbclid` 等）、结尾斜杠；查询参数排序
2. **保留有意义的差异**：`share_token` 之类有功能的参数和 hash 路由（`#/path`）保留，避免把不同页面错并成一条
3. **多份证据都留存**：同一网址的标题、摘要、日期全部保留，并标明每条来自哪家
4. **排序**：等效来源数优先。官方一手来源（gov.cn、官方文档等）额外加 **1.2**（单渠道官网等效 `1 + 1.2 = 2.2`，压过两家低质转载，但压不过三渠道独立命中）；结果里会标注"官方一手来源"。其次看各家原始名次
5. **保守折叠**：只有标题和摘要都高度相似的转载才折叠；**版本号、日期、金额、端口等数字及词项顺序不一致时不折叠**（`Python 3.13` 和 `Python 3.14`、或互换端口的文章不会被误合并）
6. **渠道区分与重排**：保留转载链接并重新计算等效来源数与排名，分别统计"检索渠道数"与"独立原始页面数"；SearXNG 会在摘要处标注底层引擎名称供溯源，但计费与 `auto` 判断仍算作一个上游渠道
7. **共享摘录预算**：单次搜索的全部摘要共享 `SEARCH_EXCERPT_MAX_CHARS`（默认 6000 字符），按「先为每个独立页面分配保底主摘要 → 如有富余按排名优先回填被截断的主摘要 → 再按信息增量分配差异补充摘要」的顺序裁剪（日期前缀计入预算，标题、来源与审计信息另计）；预算用尽会注明用 `web_fetch` 阅读原文
8. **不隐瞒**：因 `total_results` 没展示的页面会在统计里注明，不会让你以为没搜到

---

## 故障排查

先看状态；需要验证连通性时再运行 `check`。它会发出真实请求，关闭硬锁时可能消耗账号额度。

```bash
aggregate-search status
aggregate-search check
```

| 现象 | 原因与处理 |
|---|---|
| 客户端里没有出现工具 | 命令没找到。把 `command` 换成 `where` / `which` 查到的完整路径，然后重连 |
| 命令突然打不开，报 `No module named aggregate_search` | Windows 上服务运行时做了升级，安装可能不完整。先停止服务，再按[升级与卸载](#升级与卸载)中的方法重装 |
| 改了 `.env` 没生效 | 先用 `aggregate-search status` 确认文件位置与告警，再查同一设置是否被客户端非空环境变量覆盖；目录、日志路径、HTTP 监听与 Token 仍需重启 |
| 填了 key 但显示「未配置」 | 检查 `aggregate-search status` 顶部的配置告警，确认变量名正确、已去掉行首 `#` |
| 不确定读的是哪个配置文件 | `aggregate-search status` 第一段会显示 |
| `auto` 一直没用 key | 匿名结果可能已够用；允许消耗账号额度时可显式用 `escalate`。开启硬锁时，改搜索模式也不会动用 key |
| 某家显示「已到本地用量上限」 | 到了 `DAILY_LIMITS` / `MONTHLY_LIMITS`，到下一天 / 下个月自动恢复；也可以调大或写 `厂商:0` |
| 某家显示「冷却中」 | 可能是额度用完、限流或 key 失效。查看预计恢复时间并等冷却到期；key 失效时可换新 key，不需要删除 `state.db` 或重启 |
| Parallel 匿名报 rate limit | 匿名通道有频率限制，应等冷却到期再试。若未开启硬锁，`auto` 在允许 `ANON_KEY_FALLBACK` 且配有可用 key 时可回退到账号通道 |
| 结果里出现「等待超过总时限」 | 慢的厂商被放弃，其余结果照常返回。频繁超时时可调大 `TOTAL_TIMEOUT`（别超过客户端的工具超时） |
| Jina / You.com / DuckDuckGo 超时 | 国内直连不稳，设置 `SEARCH_PROXY`（默认只有这三家走它） |
| GitHub 仓库首页直取超时 | 若关闭了系统代理（`USE_SYSTEM_PROXY=false`），`raw.githubusercontent.com` 默认不走 `SEARCH_PROXY`；在 `PROXY_PROVIDERS` 追加 `github` 即可走代理 |
| 国内厂商变慢或报网络错误 | 系统代理开着全局模式，把国内厂商也绕出去了。切回规则模式，或设 `USE_SYSTEM_PROXY=false` |
| 百度 / 元宝 / 智谱 / 博查没结果 | 确认已开通搜索服务且有额度；后三家未实测 |
| SearXNG 返回的不是 JSON | 在实例 `settings.yml` 里给 `search.formats` 加上 `json` |

想亲眼看 MCP 的握手过程，仓库里有个小脚本（Windows / macOS / Linux 通用，只做匿名搜索）：

```bash
python mcp_handshake_demo.py aggregate-search
```

---

## 从旧版升级

### 从 0.5.0 升级

- 如果原来用 `SEARCH_MODE=free` 来禁止服务发送账号 key，请在升级时设 `SEARCH_NO_ACCOUNT_KEYS=true`；要保留默认免费搜索，也可同时保留 `SEARCH_MODE=free`。
- 同一家厂商的 key 不再跨配置层合并。如果之前有意把它们分在多层，请把需要轮换的 key 集中到该厂商优先级最高的一层。
- 继续使用原配置目录下的 `state.db`，不要清库。若目录里还有旧版的 `key-state.json` 或 `usage-counters.json`，首次使用状态库时会自动迁移，原文件会改名为 `.migrated` 备份，无需手动处理。

### 从 0.3 及更早版本升级

0.3 及更早要求在项目文件夹里建虚拟环境，`.env` 也放在项目文件夹里。0.4 起改为全局安装：

| | 0.3 及更早 | 当前版本 |
|---|---|---|
| 启动命令 | `项目\.venv\Scripts\python.exe 项目\server.py` | `aggregate-search` |
| `.env` 位置 | 项目文件夹 | `~/.aggregate-search/` |
| 自检 | `python check_providers.py …` | `aggregate-search check / status / selftest` |
| 状态存储 | 项目目录 | 配置目录下的 `state.db`（冷却与用量计数） |
| 代码组织 | 根目录若干个 `.py` + `providers/` | 统一放进 `aggregate_search/` 包 |

升级步骤：

1. 按[快速开始](#快速开始)装好命令
2. 在运行普通 `init` 之前，执行 `aggregate-search init --from "旧项目的 .env 路径"`。它只复制 `.env`，不会覆盖已存在的配置；如还使用旧 `keys.json`，请另外迁移至 `aggregate-search path` 指向的目录。
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
  keypool.py        多 key 轮转、并发分配、冷却逻辑
  store.py          state.db：冷却与用量计数、旧状态迁移
  usage.py          本地使用记录与用量上限
  quality.py        本地判断：覆盖度、多渠道命中同页、官方来源
  routing.py        国内 / 国际 / 混合识别与补搜排序
  reading.py        长文分段/聚焦阅读与 GitHub 仓库 README 直取
  excerpts.py       共享摘录预算三段式分配与差异片段合并
  selftest.py       离线自测
  providers/
    __init__.py     厂商注册表、调度、匿名 / 带 key 的调用记录
    base.py         HTTP 封装、脱敏、URL 归一化、证据合并
    *.py            19 个厂商适配器
  templates/        .env 与 keys.json 模板
README_EN.md        英文说明（安装、客户端接入、配置速查与离线评测概述）
docs/usage-guide.md 安装、升级与首次实搜手把手指南
evals/              34 题离线快照评测集、基线与结果报告
scripts/            发行包（sdist / wheel）校验与离线评测脚本
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

- **调度**：把各家的网络调用换成假后端，验证搜索模式、补搜、多 key 轮换、成本记账、抓取回退、预算回填与聚焦定位等逻辑
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

- **真实 key 只在本机配置**：放在 `.env`、`keys.json` 或客户端环境变量里，不提交到仓库；仓库里的模板只含占位符
- **本地文件不入库**：`.gitignore` 排除了密钥配置、`state.db` 及其 WAL 相关文件、迁移备份和使用记录
- **错误信息脱敏**：返回客户端的错误和使用记录不会写入 key 明文；`state.db` 中用 SHA-256 指纹标识 key
- **服务端硬锁**：`SEARCH_NO_ACCOUNT_KEYS=true` 禁止搜索、抓取和诊断发送账号 key；HTTP 对外服务仍需配置 Bearer Token
- **不跟危险跳转**：带 key 的请求不会跟着跳到别的站点，也不会从 https 跳到不加密的 http
- **文件权限**：类 Unix 系统上配置目录和 `.env` 只有你自己能读写
- **使用记录只在本机**：里面有搜过的问题，不想留就设 `USAGE_LOG=false`
- **代理可控**：`SEARCH_PROXY` 默认只给指定厂商用；其余厂商走哪条网络见[网络](#网络)一节
- **输出内容不可信**：搜回来的网页正文会被塞进 AI 的上下文，网页里如果埋了"指令"（提示词注入）可能影响判断。聚合工具天然放大这个问题，需要时让 AI 把可疑内容当作数据而不是命令
- 使用各家搜索服务时，请遵守它们的服务条款和配额规定

---

## 变更记录

每个版本改了什么，见 [CHANGELOG.md](CHANGELOG.md)。

`0.5.4` 改进共享摘录预算回填与去重顺序保持，完善 `focus` 数字定位、前置标题与跨块重叠处理，兼容 GitHub `.git` 地址直取、`PROXY_PROVIDERS=github` 校验与 CLI `fetch` 参数，并扩充 34 题离线快照评测。`0.5.3` 加入 `start_index` 分段续读、`focus` 段落筛选、GitHub README 直取、共享摘录预算、SearXNG 溯源与英文文档。`0.5.2` 修复 Perplexity 网页证据丢失和中文关键词漏配，扩大可靠发布日期格式支持并新增冷却维护命令。`0.5.1` 将默认免费模式与服务端硬锁分开，修正热加载、筛选、会话隔离及多进程限额。

## 许可

[MIT](LICENSE) © 2026 luxuriouspoll

可以自由使用、修改、分发，包括商用；保留版权声明即可。软件按"现状"提供，不附带任何担保。
