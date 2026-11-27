# aggregate-search 使用指南

适用于 **v0.5.3**。本指南从升级或安装开始，走到在 MCP 客户端里完成第一次搜索。想先看项目原理、支持的厂商和完整配置，见 [项目介绍（README）](../README.md)。

## 1. 已有 0.5.0：不删旧 MCP，就地升级

这套步骤同样适用于从 0.5.1 或 0.5.2 升级。

如果客户端已经把启动命令设成 `aggregate-search`，**保留原来的 MCP 注册项**。升级只替换程序，不需要重新填 Key，也不要删除 `.env`、`keys.json`、`state.db` 或 `usage.jsonl`。

1. 在 ZCode 等客户端里停用或断开 `aggregate-search`。Windows 上如果断开后仍占用文件，退出使用这个 MCP 的客户端再升级；远程 HTTP 常驻实例则要停掉实际的服务进程。
2. 在 PowerShell 进入**已更新到 0.5.3 的本地仓库根目录**（里面有 `pyproject.toml`）。把下面示例路径换成你自己的：

   ```powershell
   Set-Location 'D:\projects\aggregate-search-mcp'
   uv tool install --force --reinstall .
   ```

   如果不使用本地源码，而是直接安装 GitHub 上的发布版，用下面这条替代（不要两条都执行；想跟随开发主分支可把 `@v0.5.3` 换成 `@master`）：

   ```powershell
   uv tool install --force --reinstall 'git+https://github.com/luxuriouspoll/aggregate-search-mcp@v0.5.3'
   ```

3. **先在终端验收，再回客户端重连原来的注册项**：

   ```powershell
   aggregate-search --version
   aggregate-search path
   aggregate-search selftest
   aggregate-search status
   ```

   该版本应打印 `aggregate-search 0.5.3`；`path` 应是原来的配置目录；`selftest` 应全部通过；`status` 用来确认原配置仍被读取，并查看厂商、用量、硬锁和代理模式。`selftest` 不联网也不读真实配置；`status` 不向厂商发请求，但可能在本地初始化或迁移状态库。

> **什么时候才需要改 MCP 注册项？** 如果旧命令是 `uvx`，它不会使用刚装好的 `aggregate-search`：可以更新原有 `uvx` 指向的 Git 版本，或直接把原注册项改成 `aggregate-search`。如果客户端写死了旧虚拟环境的路径，也只需把命令改为新版入口；无需先删再建。Windows 用 `where.exe aggregate-search` 查看程序路径。使用 `pipx` 或自建虚拟环境安装过的，优先用原安装工具更新，避免 PATH 上同时出现两个版本。

**想备份的话**：先停用所有指向同一配置目录的实例，再把 `aggregate-search path` 显示的目录整个复制到用户私有目录（不要复制进 Git 仓库）。同一目录中的 SQLite 文件要一起保留，别只挑一个 `state.db` 复制。若客户端设置了自定义 `AGGREGATE_SEARCH_HOME`，以该客户端实际使用的目录为准。

## 2. 从未安装：先让命令可用

使用 [uv](https://docs.astral.sh/uv/getting-started/installation/)，在 PowerShell 或终端先装好命令并写入系统 `PATH`：

```powershell
uv tool install 'git+https://github.com/luxuriouspoll/aggregate-search-mcp@v0.5.3'
uv tool update-shell
```

> **必看防坑（尤其是 Windows 首次用 `uv` 装工具）**：`uv` 默认把命令装在 `%USERPROFILE%\.local\bin`（macOS / Linux 为 `~/.local/bin`），首次安装往往不在系统 `PATH` 里，直接运行会报“无法将‘aggregate-search’项识别为 cmdlet…”。跑完 `uv tool update-shell` 后，**关掉当前终端并新开一个窗口**（不想关窗口可临时运行 `$env:PATH = "$HOME\.local\bin;$env:PATH"`），再继续执行初始化与自检：

```powershell
aggregate-search --version
aggregate-search init
aggregate-search selftest
aggregate-search status
```

也可以克隆仓库，在根目录执行 `uv tool install .`。`init` 会创建配置模板，不会覆盖旧 `.env`。不知道配置在哪里就运行 `aggregate-search path`。**第一次使用不用申请 API Key**，先试匿名源即可。

## 3. 接入任意 MCP 客户端（ZCode / Codex / dsh / Claude Code / Cursor 等）

本服务基于标准 MCP 协议，所有支持 MCP 的 Agent 都能直接共用同一份 `~/.aggregate-search` 配置与额度账本。本机使用 **stdio**：在客户端的 MCP 服务管理里新增一项（已有项就复用），把以下三项填好：

| 字段 | 值 |
|---|---|
| 名称 | `aggregate-search` |
| 启动命令 | `aggregate-search` |
| 启动参数 | 留空 |

如果客户端在执行 `uv tool update-shell` 之前就已经打开，它缓存的还是旧 `PATH`：要么完全退出重开客户端，要么把启动命令直接填成 `where.exe aggregate-search`（macOS / Linux 用 `which aggregate-search`）查到的完整绝对路径。配置文件写法可参照 README 中的 [Codex](../README.md#codex)、[DeepSeek Harness（dsh）](../README.md#deepseek-harnessdsh)、[ZCode](../README.md#zcode) 或 [通用 MCP 示例](../README.md#通用写法claude-desktopcursorclinewindsurf-等)。本机 stdio **不用手工先启动服务器**，客户端会按需拉起进程；跨机器或容器访问再考虑 [Streamable HTTP](../README.md#可选-http-传输)。

重连后，在工具列表里应该看到：

| 工具 | 用来做什么 |
|---|---|
| `web_search` | 搜多家来源，合并同一页面和高度重复的转载，返回带出处的证据 |
| `web_fetch` | 阅读指定网页正文；默认先尝试匿名可用的后端 |
| `web_status` | 查看已启用厂商、代理、限额、冷却状态和本机使用记录 |

可以复制这段到聊天里做第一次实搜：

> 请显式使用 aggregate-search 的 `web_search`，传 `search_mode="free"` 搜索「MCP Streamable HTTP 与 stdio 的区别」。给出两条有出处的结论。若工具不可用，请告诉我错误，不要换成其它联网工具。

也可以在终端发一次匿名实搜（会联网，但不发送账号 Key）：

```powershell
aggregate-search search 'MCP Streamable HTTP 和 stdio 的区别' --mode free --domains modelcontextprotocol.io
```

如果需要在同一个主题上继续补搜，可以让 AI 用同样的 `query` 和筛选条件传 `search_mode="escalate"`；这依赖同一 MCP 进程中的近期结果。两条独立的 CLI 命令不会共享这段临时历史。拿到链接后，让 AI 用 `web_fetch` 阅读正文，再核对原文和发布日期。

## 4. 选一个费用策略

- **只是默认省额度**：保留默认 `SEARCH_MODE=auto`。先匿名，证据不足才补搜；如果配置了 Key，匿名限流回退或补搜可能消耗额度。
- **默认只查免费来源，但允许反过来点名带 Key**：设 `SEARCH_MODE=free`。调用方仍可在搜索时显式切换模式；单独运行 CLI `check` 也可能用 Key。
- **不允许这个服务发送任何账号 Key**：在服务器实际读取的 `.env` 里加入：

  ```dotenv
  SEARCH_NO_ACCOUNT_KEYS=true
  ```

  保存后运行 `aggregate-search status` 或调用 `web_status`，确认硬锁显示“已启用”。工具参数不能覆盖它。要解除请明确改为 `false`；如果客户端注册项注入了同名非空环境变量，需在客户端同步修正并重连。

同一家厂商的 Key 只取配置优先级最高的有效一层：**客户端 env > `.env` > `keys.json`**，不同别名也不跨层合并。希望 `.env` 的 Key 热换生效时，先确认客户端没有配置该厂商的 Key。

如需限制账号用量，以下是示例（按你自己的套餐填写）：

```dotenv
DAILY_LIMITS=tavily:30
MONTHLY_LIMITS=tavily:300
```

上限按**每个 Key**计。运行中取消某家上限请显式写 `厂商:0`，不要靠删行；不要删除 `state.db` 来清冷却，否则也会丢失用量账本。更多细节见 [省额度与防超支](../README.md#省额度与防超支) 和 [配置热加载](../README.md#配置热加载)。

**Key 仍在冷却怎么办？** 普通网络失败不会自动把账号 Key 封为无效；先在常驻服务里调用 `web_status`，区分匿名限流、账号冷却和用量达限。只有确认账号问题已解决时才运行下面的本地命令：

```powershell
aggregate-search reset-cooldown tavily           # 只预览，不修改
aggregate-search reset-cooldown tavily --apply   # 确认后才执行
```

它按厂商清理本地仍有效的账号 Key 冷却，包括已移出配置的旧 Key 指纹；不能恢复上游额度，也不会清匿名冷却或日/月用量。CLI `status` 是新进程；要确认长驻 MCP 实际保留的限额，请看该实例的 `web_status`。

## 5. 常见卡点

| 看到什么 | 先做什么 |
|---|---|
| 报“无法将 `aggregate-search` 项识别为 cmdlet…”或 `command not found` | `~/.local/bin` 还没进 `PATH`。运行 `uv tool update-shell`，然后**重新打开终端**；桌面客户端报找不到命令时，重开客户端或直接填 `where.exe aggregate-search` 查到的绝对路径。 |
| 终端仍显示旧版本，或客户端表现像旧版 | `where.exe aggregate-search` 查看是否有多个入口；必要时给客户端填新版 `.exe` 绝对路径。升级后别忘了断开再重连。 |
| `拒绝访问 (os error 5)` | Windows 锁住了正在运行的 `.exe`。把所有连着它的客户端（ZCode、Codex、dsh 等）先断开或退出，再重跑安装命令。不要删除配置目录。 |
| 上游超时或无结果 | 先用 `status` 检查匿名后端、代理和冷却；再用 `--mode free` 测一次。`check` 会真实发请求，未开硬锁时可能消耗已配置的账号额度。 |
| 开了 `--time-range week` 结果很少 | 这是严格筛选：只保留有可核验且落在窗口内的发布日期；缺时间的内容会被排除。如果不需要严格时效，去掉 `--time-range` 再搜。 |
| 保存 `.env` 后没效果 | 用 `path` 和 `status` 核对读取的是哪个目录，检查客户端是否注入了同名配置。目录、日志路径、HTTP 鉴权 Token 或监听参数仍需要重启服务。 |

版本变化见 [CHANGELOG](../CHANGELOG.md)。请勿把实际 `.env`、`keys.json`、日志或状态库提交到 GitHub。


## 0.5.3 原文阅读与证据摘录

三个 MCP 工具继续为 `web_search`、`web_fetch`、`web_status`。

`web_fetch(url, provider=None, start_index=0, focus="")` 支持按字符位置续读。默认返回正文前 20000 字符，输出给出半开区间和下一页起点。将返回的起点传入 `start_index` 即可顺序阅读。位置按缓存中已脱敏文本的 Python 字符计数，适用于中文。超出末尾会明确提示。

显式传入 `focus="配置代理"` 时，用标准库词频相关性筛选段落并标出原文位置。省略 focus 时按原文顺序输出。选择位置可再用 start_index 阅读上下文。

`FETCH_CACHE_MAX_CHARS=100000` 控制每份缓存正文与支持该参数的上游抓取长度，`FETCH_MAX_CHARS=20000` 控制每次展示长度。达到缓存上限时提示可能还有内容。上游服务也可能仅提供部分正文，已获取长度代表本次可读文本。缓存最多 64 条、总正文 400 万字符，TTL 默认 300 秒。只有命中仍有效且满足当前账号策略的缓存时，续读才零额外请求；过期、淘汰或配置变化会重新抓取，网页更新后位置可能变化。

标准 HTTPS GitHub 仓库首页优先从 raw.githubusercontent.com 的 HEAD 分支直取 README，兼容 README.md、readme.md、README.rst、README 和 README.MD。原始 github.com 引用保留。指定 provider 时尊重该后端；Issue、PR、Release、blob 和 tree 页面按原目标抓取。直取不调用 GitHub REST API，拒绝跳转，限制 2 MB 下载；其他后端解码响应上限为 8 MB。

`SEARCH_EXCERPT_MAX_CHARS=6000` 是单次搜索共享摘录预算。先给独立页面分配主摘要，再补充有差异的摘要，保留版本、日期与全部命中渠道。预算只限制摘录文本，标题、来源和审计信息另计。检索渠道数与转载折叠后的原始页面数分别展示，跨渠道命中同页继续沿用原有 auto 判断标准。SearXNG 可展示底层引擎名称，调度和计费仍算单个上游。

上述三个长度配置均支持热加载。离线评测见仓库 evals/README.md。
