# aggregate-search 使用指南

适用于 **v0.5.2**。本指南从升级或安装开始，走到在 MCP 客户端里完成第一次搜索。想先看项目原理、支持的厂商和完整配置，见 [项目介绍（README）](../README.md)。

## 1. 已有 0.5.0：不删旧 MCP，就地升级

这套步骤同样适用于从 0.5.1 升级。

如果客户端已经把启动命令设成 `aggregate-search`，**保留原来的 MCP 注册项**。升级只替换程序，不需要重新填 Key，也不要删除 `.env`、`keys.json`、`state.db` 或 `usage.jsonl`。

1. 在 ZCode 等客户端里停用或断开 `aggregate-search`。Windows 上如果断开后仍占用文件，退出使用这个 MCP 的客户端再升级；远程 HTTP 常驻实例则要停掉实际的服务进程。
2. 在 PowerShell 进入**已更新到 0.5.2 的本地仓库根目录**（里面有 `pyproject.toml`）。把下面示例路径换成你自己的：

   ```powershell
   Set-Location 'D:\projects\aggregate-search-mcp'
   uv tool install --force --reinstall .
   ```

   如果不使用本地源码，而是直接安装 GitHub 上的发布版，用下面这条替代（不要两条都执行；想跟随开发主分支可把 `@v0.5.2` 换成 `@master`）：

   ```powershell
   uv tool install --force --reinstall 'git+https://github.com/luxuriouspoll/aggregate-search-mcp@v0.5.2'
   ```

3. **先在终端验收，再回客户端重连原来的注册项**：

   ```powershell
   aggregate-search --version
   aggregate-search path
   aggregate-search selftest
   aggregate-search status
   ```

   该版本应打印 `aggregate-search 0.5.2`；`path` 应是原来的配置目录；`selftest` 应全部通过；`status` 用来确认原配置仍被读取，并查看厂商、用量、硬锁和代理模式。`selftest` 不联网也不读真实配置；`status` 不向厂商发请求，但可能在本地初始化或迁移状态库。

> **什么时候才需要改 MCP 注册项？** 如果旧命令是 `uvx`，它不会使用刚装好的 `aggregate-search`：可以更新原有 `uvx` 指向的 Git 版本，或直接把原注册项改成 `aggregate-search`。如果客户端写死了旧虚拟环境的路径，也只需把命令改为新版入口；无需先删再建。Windows 用 `where.exe aggregate-search` 查看程序路径。使用 `pipx` 或自建虚拟环境安装过的，优先用原安装工具更新，避免 PATH 上同时出现两个版本。

**想备份的话**：先停用所有指向同一配置目录的实例，再把 `aggregate-search path` 显示的目录整个复制到用户私有目录（不要复制进 Git 仓库）。同一目录中的 SQLite 文件要一起保留，别只挑一个 `state.db` 复制。若客户端设置了自定义 `AGGREGATE_SEARCH_HOME`，以该客户端实际使用的目录为准。

## 2. 从未安装：先让命令可用

使用 [uv](https://docs.astral.sh/uv/getting-started/installation/)，在 PowerShell 或终端运行：

```powershell
uv tool install 'git+https://github.com/luxuriouspoll/aggregate-search-mcp@master'
aggregate-search --version
aggregate-search init
aggregate-search selftest
aggregate-search status
```

也可以克隆仓库，在根目录执行 `uv tool install .`。`init` 会创建配置模板，不会覆盖旧 `.env`。不知道配置在哪里就运行 `aggregate-search path`；默认是 Windows 的 `%USERPROFILE%\.aggregate-search`，macOS / Linux 的 `~/.aggregate-search`。**第一次使用不用申请 API Key**，先试匿名源即可。

如果 `uv` 已安装但终端找不到 `aggregate-search`，先执行 `uv tool update-shell`，再重新打开终端；图形客户端也可能需要重启一次才能读取新 PATH。

## 3. 接入 ZCode 或其它 MCP 客户端

本机使用 **stdio**：在 MCP 服务管理里新增一项（已有项就复用），把以下三项填好：

| 字段 | 值 |
|---|---|
| 名称 | `aggregate-search` |
| 启动命令 | `aggregate-search` |
| 启动参数 | 留空 |

如果界面提示找不到命令，把启动命令改为 `where.exe aggregate-search` 得到的 `.exe` 完整路径。对于文件配置，可参照 [ZCode 示例](../README.md#zcode) 或 [通用 MCP 示例](../README.md#通用写法claude-desktopcursorcline-等)。本机 stdio **不用手工先启动服务器**，客户端会拉起进程。只有跨机器访问才考虑 [Streamable HTTP](../README.md#可选-http-传输)。

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
| 终端仍显示 `0.5.0`，或客户端表现像旧版 | `where.exe aggregate-search` 查看是否有多个入口；必要时给客户端填新版 `.exe` 绝对路径。升级后别忘了断开再重连。 |
| `拒绝访问 (os error 5)` | 停掉仍在用旧程序的 MCP 实例或退出客户端，再重跑安装命令。不要删除配置目录。 |
| 上游超时或无结果 | 先用 `status` 检查匿名后端、代理和冷却；再用 `--mode free` 测一次。`check` 会真实发请求，未开硬锁时可能消耗已配置的账号额度。 |
| 开了 `--time-range week` 结果很少 | 这是严格筛选：只保留有可核验且落在窗口内的发布日期；缺时间的内容会被排除。如果不需要严格时效，去掉 `--time-range` 再搜。 |
| 保存 `.env` 后没效果 | 用 `path` 和 `status` 核对读取的是哪个目录，检查客户端是否注入了同名配置。目录、日志路径、HTTP 鉴权 Token 或监听参数仍需要重启服务。 |

版本变化见 [CHANGELOG](../CHANGELOG.md)。请勿把实际 `.env`、`keys.json`、日志或状态库提交到 GitHub。
