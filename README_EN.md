# aggregate-search-mcp

English · [简体中文](README.md)

A multi-provider web search and content retrieval MCP server for Codex, Claude Code, Cursor and other MCP clients. Python 3.10+ with two direct runtime dependencies, `mcp` and `httpx`.

Search starts with anonymous providers. Results are filtered and deduplicated before a coverage check decides whether to query additional providers. Each response includes sources and a request-cost audit. Account keys can be disabled at the server level.

## Install and upgrade

Install the pinned release with uv.

```sh
uv tool install 'git+https://github.com/luxuriouspoll/aggregate-search-mcp@v0.5.3'
uv tool update-shell
aggregate-search init
```

Restart your terminal after updating PATH. Configure your MCP client to launch `aggregate-search` with no arguments for stdio transport.

Before upgrading on Windows, disconnect MCP clients such as ZCode to release the executable.

```sh
uv tool install --force --reinstall 'git+https://github.com/luxuriouspoll/aggregate-search-mcp@v0.5.3'
aggregate-search --version
aggregate-search selftest
```

| Feature | Behavior |
|---|---|
| Three tools | `web_search`, `web_fetch`, `web_status` |
| Anonymous-first search | Start without keys, escalate when coverage is insufficient |
| Cost control | Multiple keys, cooldowns, atomic SQLite quota reservations, server-side account-key lock |
| Evidence | URL deduplication, conservative reprint folding, original-source preference, separate channel and page counts |
| Long documents | `web_fetch(url, start_index=20000)` continues reading, with original character offsets |
| Focused reading | `web_fetch(url, focus="proxy configuration")` selects relevant paragraphs using standard-library scoring |
| GitHub | Repository homepages use a bounded anonymous README fetch, with the original citation retained |
| Shared excerpt budget | Primary excerpts across pages first, then supplementary facts |
| Provenance | SearXNG engine names remain metadata under one upstream provider |

Configuration lives in `~/.aggregate-search/`. The example environment file lists supported providers and settings. `FETCH_MAX_CHARS=20000` limits each displayed page. `FETCH_CACHE_MAX_CHARS=100000` limits each cached document and supported upstream requests. `SEARCH_EXCERPT_MAX_CHARS=6000` limits search excerpts across all pages. These settings reload for the next call.

Cache hits avoid additional requests while the entry remains valid and permitted by the current account policy. Expired or evicted entries are fetched again. Cache storage is limited to 64 entries and four million document characters. Character positions refer to the retrieved, redacted text. Upstream truncation can limit available content.

GitHub README retrieval uses HTTPS on a fixed raw-content host, accepts no redirects and limits downloads to 2 MB. Specific issue, pull request, release and file URLs retain their requested targets. Other provider responses are limited to 8 MB decoded data. External text remains wrapped as untrusted content.

The repository includes 25 synthetic offline replay cases with recorded before/after metrics. See [evaluation notes](evals/README.md) for their scope and commands. Full setup instructions are in the [usage guide](docs/usage-guide.md).
