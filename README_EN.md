# aggregate-search-mcp

English | [简体中文](README.md)

[![CI](https://github.com/luxuriouspoll/aggregate-search-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/luxuriouspoll/aggregate-search-mcp/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/luxuriouspoll/aggregate-search-mcp)](https://github.com/luxuriouspoll/aggregate-search-mcp/releases)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A free-first, multi-provider web search and content retrieval MCP server for Codex, Claude Code, Cursor, DeepSeek Harness, ZCode, and other MCP clients. Supports 19 search/fetch channels on Python 3.10+ with only two direct runtime dependencies (`mcp` and `httpx`).

Search starts with anonymous providers (`parallel`, `keenable`, `anysearch`). Results are filtered, normalized, and deduplicated before a local coverage check decides whether to escalate to keyed providers. Each response reports per-provider status, channel vs. page counts, and a request-cost audit. Account keys can be disabled at the server level with `SEARCH_NO_ACCOUNT_KEYS=true`.

## Install and upgrade

Install the pinned release with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install 'git+https://github.com/luxuriouspoll/aggregate-search-mcp@v0.5.4'
uv tool update-shell
aggregate-search init
```

Restart your terminal after updating `PATH` so `aggregate-search --version` prints `aggregate-search 0.5.4` (use `which aggregate-search` or `where.exe aggregate-search` if a GUI client still caches an old `PATH`).

Before upgrading on Windows, disconnect MCP clients (such as ZCode or Codex) to release `aggregate-search.exe`:

```sh
uv tool install --force --reinstall 'git+https://github.com/luxuriouspoll/aggregate-search-mcp@v0.5.4'
aggregate-search --version
aggregate-search selftest
aggregate-search status
```

## Quick client setup

Launch `aggregate-search` with no arguments for `stdio` transport (no keys need to be placed in client config unless overriding `.env`):

```json
{
  "mcpServers": {
    "aggregate-search": {
      "command": "aggregate-search"
    }
  }
}
```

Or add via Claude Code CLI:

```sh
claude mcp add --scope user aggregate-search -- aggregate-search
```

For remote / container access over Streamable HTTP (non-loopback bindings require `MCP_HTTP_TOKEN` with at least 16 characters):

```sh
MCP_HTTP_TOKEN="your-long-random-token" aggregate-search serve --transport streamable-http --host 0.0.0.0 --port 8000
```

## Core capabilities

| Feature | Behavior |
|---|---|
| Three tools | `web_search`, `web_fetch`, `web_status` |
| Anonymous-first search | Start without keys, escalate when coverage is insufficient (`auto` / `free` / `escalate` / `all`) |
| Cost & quota control | Multiple keys per provider, cooldowns, atomic SQLite WAL quota reservations, `SEARCH_NO_ACCOUNT_KEYS=true` hard lock |
| Evidence ranking & folding | URL normalization, conservative reprint folding, `+1.2` extra weight for official sources (`2.2` for single official channel), separate channel and page counts |
| Paginated & focused fetch | `web_fetch(url, start_index=20000)` continues reading; `focus="..."` selects relevant paragraphs (including numbers/versions, section headings before `start_index`, and overlapping long blocks) around hit offsets |
| GitHub README fast-path | Repository homepages (`https://github.com/owner/repo`, normalizing `.git`) fetch README anonymously from `raw.githubusercontent.com` (`HEAD`, no REST API, no redirects, 2 MB cap) with original citation retained |
| Shared excerpt budget | `SEARCH_EXCERPT_MAX_CHARS=6000` reserves primary excerpts per page first, backfills clipped primaries by rank, then adds novelty-ordered supplements |
| SearXNG provenance | Underlying SearXNG engine names are displayed per excerpt while still counting as one upstream channel for billing and `auto` gates |

## Configuration highlights (`~/.aggregate-search/.env`)

Run `aggregate-search path` to see your active config directory (or set `AGGREGATE_SEARCH_HOME`). Most runtime settings hot-reload on the next call; `AGGREGATE_SEARCH_HOME`, `USAGE_LOG_FILE`, HTTP host/port, and `MCP_HTTP_TOKEN` require a restart.

- **Account-key hard lock**: `SEARCH_NO_ACCOUNT_KEYS=true` blocks account keys across `web_search`, `web_fetch`, and `aggregate-search check` (tool arguments cannot override it).
- **Quotas & multiple keys**: `DAILY_LIMITS=baidu:90,tavily:30`, `MONTHLY_LIMITS=tavily:900`; use plural env vars for key rotation (e.g. `TAVILY_API_KEYS=k1,k2`).
- **Supported providers**: Anonymous `parallel`, `keenable`, `anysearch`, `duckduckgo` (`ENABLE_KEYLESS=true`), self-hosted `searxng` (`SEARXNG_BASE_URL`), international `tavily`, `exa`, `firecrawl`, `jina`, `querit`, `ollama`, `brave`, `perplexity`, `youcom`, and CN providers `volcengine`, `baidu`, `yuanbao` (`TENCENTCLOUD_WSA_APIKEY`), `zhipu`, `bocha`.
- **Fetch & excerpt budgets**: `FETCH_MAX_CHARS=20000` (displayed slice), `FETCH_CACHE_MAX_CHARS=100000` (cached doc & supported upstream fetch limit; 64 entries / 4M chars total, TTL `300`s), `SEARCH_EXCERPT_MAX_CHARS=6000`. Cache hits avoid HTTP requests while valid and allowed by current account policy. Other provider decoded responses are capped at 8 MB. External text remains wrapped in `<untrusted_content>`.
- **Proxy**: `SEARCH_PROXY=http://127.0.0.1:7897`, `PROXY_PROVIDERS=jina,duckduckgo,youcom` (also accepts `github` for direct README retrieval), `SEARCH_PROXY_ALL=false`, `USE_SYSTEM_PROXY=true`.
- **CLI helpers**: `aggregate-search status`, `aggregate-search check`, `aggregate-search fetch URL [backend] [--start-index N] [--focus QUERY]`, `aggregate-search reset-cooldown PROVIDER [--apply]`, `aggregate-search selftest`.

> Note: The generated `env.example` template and [usage guide](docs/usage-guide.md) are currently written in Chinese.

## Offline evaluation

The repository includes 34 synthetic offline replay cases in `evals/cases.json` (plus committed baseline/result reports for `0.5.3` vs. `0.5.4`). Across those snapshots, Top-5 hit rate remains `34/34`, focused document fact retention improves from `28/34` to `34/34`, and search fact retention improves from `32/34` to `34/34`. The `--live` switch is a reserved placeholder until a separately budgeted live recorder is configured. See [evaluation notes](evals/README.md) for commands and scope.
