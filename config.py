"""集中读取环境变量配置。

优先级：进程环境变量（MCP 客户端注册时传的 env）> 项目目录 .env > keys.json。
每家的 key 支持多个（逗号 / 换行 / 分号分隔），运行期做轮询；
配了 key 的家会被自动启用，无需额外开关。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    env_file = _ROOT / ".env"
    if not env_file.is_file():
        return
    try:
        content = env_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), _strip_inline_comment(value))


def _strip_inline_comment(value: str) -> str:
    """去掉行内注释和包裹引号。

    `SEARCH_TIMEOUT=15   # 单家超时` 这种写法很常见，不能把 `# ...` 算进值里，
    否则 float("15   # ...") 会在 import 期抛 ValueError 导致服务起不来。
    引号内的 # 不算注释。

    引号只剥掉成对包裹的那层；`"k1","k2"` 这种多值写法要保留中间的引号，
    交给 _split 逐个去引号，否则会只剩第一个 key。
    """
    text = value.strip()
    if not text:
        return ""
    # 先按引号状态切掉行内注释
    out: list[str] = []
    quote_char = ""
    for idx, char in enumerate(text):
        if quote_char:
            out.append(char)
            if char == quote_char:
                quote_char = ""
            continue
        if char in ('"', "'"):
            quote_char = char
            out.append(char)
            continue
        if char == "#" and (idx == 0 or text[idx - 1].isspace()):
            break
        out.append(char)
    text = "".join(out).strip()
    # 整串只有一对包裹引号时剥掉（如 "tvly-xxx"）
    if len(text) >= 2 and text[0] in ('"', "'") and text[-1] == text[0]:
        inner = text[1:-1]
        if text[0] not in inner:
            return inner.strip()
    return text


def _unquote(value: str) -> str:
    """逐个值去引号：'\"k1\"' -> 'k1'。"""
    value = value.strip()
    if len(value) >= 2 and value[0] in ('"', "'") and value[-1] == value[0]:
        return value[1:-1].strip()
    return value


_load_dotenv()

_SPLIT = re.compile(r"[,\n;]+")

# 每家 key 的候选环境变量名，靠前的优先（复数形式用于多 key）
_KEY_ENV_NAMES: dict[str, list[str]] = {
    "tavily": ["TAVILY_API_KEYS", "TAVILY_API_KEY"],
    "querit": ["QUERIT_API_KEYS", "QUERIT_API_KEY"],
    "firecrawl": ["FIRECRAWL_API_KEYS", "FIRECRAWL_API_KEY"],
    "ollama": ["OLLAMA_API_KEYS", "OLLAMA_API_KEY"],
    "brave": ["BRAVE_API_KEYS", "BRAVE_API_KEY", "BRAVE_SEARCH_API_KEYS", "BRAVE_SEARCH_API_KEY"],
    "exa": ["EXA_API_KEYS", "EXA_API_KEY"],
    "jina": ["JINA_API_KEYS", "JINA_API_KEY"],
    "perplexity": ["PERPLEXITY_API_KEYS", "PERPLEXITY_API_KEY", "PPLX_API_KEYS", "PPLX_API_KEY"],
    "volcengine": ["VOLCENGINE_API_KEYS", "VOLCENGINE_API_KEY", "WEB_SEARCH_API_KEYS", "WEB_SEARCH_API_KEY"],
    "searxng": ["SEARXNG_API_KEYS", "SEARXNG_API_KEY"],
    "apify": ["APIFY_API_KEYS", "APIFY_API_KEY", "APIFY_TOKEN"],
}


def _split(raw: str) -> list[str]:
    """按逗号/分号/换行切分，并逐个去掉包裹引号。"""
    return [k for k in (_unquote(part) for part in _SPLIT.split(raw)) if k]


def _load_keys_json() -> dict[str, list[str]]:
    """可选：项目目录下 keys.json 集中管理多 key，格式 {"tavily": ["k1","k2"], ...}。

    环境变量里已经给了该家的 key 时，以环境变量为准（避免两份配置互相干扰）。
    """
    path = _ROOT / "keys.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    result: dict[str, list[str]] = {}
    for name, value in (data or {}).items():
        if isinstance(value, str):
            value = _split(value)
        if isinstance(value, list):
            keys = [str(item).strip() for item in value if str(item).strip()]
            if keys:
                result[str(name).strip().lower()] = keys
    return result


_JSON_KEYS = _load_keys_json()


def _dedupe(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _keys_for(name: str) -> list[str]:
    keys: list[str] = []
    for env_name in _KEY_ENV_NAMES[name]:
        keys.extend(_split(os.environ.get(env_name, "")))
    keys = _dedupe(keys)
    return keys or list(_JSON_KEYS.get(name, []))


# ---- 密钥：name -> [key1, key2, ...]，配置了哪家的 key 哪家就被启用 ----
API_KEYS: dict[str, list[str]] = {name: _keys_for(name) for name in _KEY_ENV_NAMES}

# ---- 自托管 / 可改写端点 ----
# SearXNG 没有 key，靠实例地址启用
SEARXNG_BASE_URL: str = os.environ.get("SEARXNG_BASE_URL", "").strip().rstrip("/")
# Firecrawl 支持自托管，其余厂商一般不需要改
FIRECRAWL_BASE_URL: str = (
    os.environ.get("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev").strip().rstrip("/")
)

# 无 key 也能用（但需要上面的实例地址）
KEYLESS_PROVIDERS: set[str] = {"searxng"}

# 可选：SEARCH_PROVIDERS=tavily,querit —— 在已配 key 的基础上再限定默认启用子集
_raw = os.environ.get("SEARCH_PROVIDERS", "")
SEARCH_PROVIDERS_LIMIT: list[str] | None = (
    [p.strip().lower() for p in _raw.split(",") if p.strip()] or None
)

# 可选：抓取后端 tavily | firecrawl | ollama | jina | exa，默认 tavily
DEFAULT_FETCH_PROVIDER: str = os.environ.get("WEB_FETCH_PROVIDER", "tavily").strip().lower()

# 可选：HTTP 代理（DuckDuckGo 等境外免 key 源在国内直连不通，需走代理）
SEARCH_PROXY: str = (
    os.environ.get("SEARCH_PROXY") or os.environ.get("HTTPS_PROXY") or ""
).strip()

# 免 key 来源是否默认参与聚合搜索。
# DuckDuckGo 在国内直连超时（要配 SEARCH_PROXY 才稳），默认关闭避免拖慢每次搜索。
# 设置 ENABLE_KEYLESS=true 可重新打开。
ENABLE_KEYLESS: bool = os.environ.get("ENABLE_KEYLESS", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

def _env_number(raw_name: str, default, cast, minimum=None):
    """读取数值型配置；写错时回落到默认值并告警，不让 import 期崩溃。"""
    raw = os.environ.get(raw_name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw.strip())
    except (TypeError, ValueError):
        print(
            f"[aggregate-search] 配置 {raw_name}={raw!r} 不是合法数值，已回落到默认值 {default}",
            file=sys.stderr,
        )
        return default
    if minimum is not None and value < minimum:
        print(
            f"[aggregate-search] 配置 {raw_name}={value} 小于下限 {minimum}，已按 {minimum} 处理",
            file=sys.stderr,
        )
        return minimum
    return value


# 可选：默认每家返回条数 / 单家超时秒数 / 抓取正文截断长度
DEFAULT_MAX_RESULTS: int = _env_number("MAX_RESULTS", 5, int, minimum=1)
SEARCH_TIMEOUT: float = _env_number("SEARCH_TIMEOUT", 15.0, float, minimum=1.0)
FETCH_MAX_CHARS: int = _env_number("FETCH_MAX_CHARS", 20000, int, minimum=1000)

# 单家结果条数硬上限：各家 API 各有上限，统一卡在这里避免上游 400
MAX_RESULTS_CAP: int = _env_number("MAX_RESULTS_CAP", 20, int, minimum=1)

# key 限额状态落盘位置（只存 key 的哈希和状态，不存 key 本身）
# 相对路径一律挂到项目目录，避免不同启动 CWD 写到意外位置
_state_env = os.environ.get("KEY_STATE_FILE", "").strip()
KEY_STATE_FILE: Path = (
    Path(_state_env) if _state_env and Path(_state_env).is_absolute()
    else _ROOT / (_state_env or ".key-state.json")
)
# 额度耗尽 / 限流时的默认冷却秒数（上游没给 Retry-After 时使用）
QUOTA_COOLDOWN: int = _env_number("QUOTA_COOLDOWN", 3600, int, minimum=1)
RATE_COOLDOWN: int = _env_number("RATE_COOLDOWN", 60, int, minimum=1)
