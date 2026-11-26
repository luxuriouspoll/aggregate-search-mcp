r"""集中读取配置。

配置放在全局配置目录里，和代码装在哪个目录无关：

    默认  ~/.aggregate-search/      Windows 即 C:\Users\<用户名>\.aggregate-search\
    覆盖  环境变量 AGGREGATE_SEARCH_HOME（填绝对路径）

目录内的文件：
    .env                  key 与各项开关
    keys.json             可选，集中管理多 key
    key-state.json        key 冷却状态（只存哈希）
    usage.jsonl           本地使用记录
    usage-counters.json   每个 key 当日 / 当月已用次数

优先级：进程环境变量（MCP 客户端注册时传的 env）> .env > keys.json。
每家的 key 支持多个（逗号 / 换行 / 分号分隔），运行期做轮询；
配了 key 的家会被自动启用，无需额外开关。
"""
from __future__ import annotations

import json
import locale
import math
import os
import re
import sys
from pathlib import Path


_HOME_OVERRIDE: str = os.environ.get("AGGREGATE_SEARCH_HOME", "").strip().strip("\"'")


def _resolve_home() -> Path:
    if _HOME_OVERRIDE:
        return Path(os.path.expandvars(_HOME_OVERRIDE)).expanduser().resolve()
    return Path.home() / ".aggregate-search"


CONFIG_DIR: Path = _resolve_home()
ENV_FILE: Path = CONFIG_DIR / ".env"
KEYS_FILE: Path = CONFIG_DIR / "keys.json"

# 读配置时发现的问题。除了打到 stderr，也会显示在 web_status / `aggregate-search status` 里——
# 很多 MCP 客户端不展示 stderr，只打日志的话用户根本看不到。
WARNINGS: list[str] = []


def log(message: str) -> None:
    """写一行日志到 stderr。

    stdout 是 MCP 的协议通道，多写任何字节都会让客户端解析失败。
    有些宿主不给子进程 stderr（sys.stderr 为 None），这时 print(file=None) 会改写到 stdout，
    所以必须先判断。
    """
    stream = sys.stderr
    if stream is None:
        return
    try:
        print(f"[aggregate-search] {message}", file=stream)
    except (OSError, ValueError):
        pass


def _warn(message: str) -> None:
    WARNINGS.append(message)
    log(message)


def file_exists(path: Path) -> bool:
    """目录没有读权限时 is_file() 会抛异常；配置读不了不该让整个服务起不来。"""
    try:
        return path.is_file()
    except OSError:
        return False


def ensure_home() -> None:
    """建好配置目录。里面有密钥和搜索记录，类 Unix 系统上只给自己访问。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            CONFIG_DIR.chmod(0o700)
        except OSError:
            pass


def legacy_file(name: str) -> Path | None:
    """0.3 及更早版本把 .env / keys.json 放在源码目录里。

    只在「从源码目录运行、且没有用 AGGREGATE_SEARCH_HOME 指定配置目录」时才认，
    让老用户 git pull 之后不至于所有 key 突然失效；装成全局命令后这里永远是 None。
    """
    if _HOME_OVERRIDE:
        return None
    candidate = Path(__file__).resolve().parent.parent / name
    return candidate if file_exists(candidate) else None


def _active(path: Path) -> tuple[Path, bool]:
    """配置目录里有就用配置目录的；没有但旧位置还留着，就先兼容读旧的。"""
    if file_exists(path):
        return path, False
    legacy = legacy_file(path.name)
    return (legacy, True) if legacy else (path, False)


# 实际读取的文件；*_IS_LEGACY 为真时启动日志和 web_status 会提示迁移
ACTIVE_ENV_FILE, ENV_IS_LEGACY = _active(ENV_FILE)
ACTIVE_KEYS_FILE, KEYS_IS_LEGACY = _active(KEYS_FILE)


def _read_text(path: Path) -> str | None:
    """读配置文本。Windows 上常见两种非 UTF-8 的情况也要认：

    - PowerShell 5 的 `echo ... > .env` 写出来是 UTF-16
    - 旧版记事本默认按系统编码（简体中文系统是 GBK）保存
    读不出来就明确告警，而不是悄悄当成「没有配置」。
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _warn(f"读不了 {path}：{exc}")
        return None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings = ["utf-16"]
    else:
        encodings = list(dict.fromkeys(
            ["utf-8-sig", locale.getpreferredencoding(False) or "utf-8", "gbk"]
        ))
    for encoding in encodings:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    _warn(f"{path} 的编码无法识别，已忽略其中的全部配置。请用编辑器另存为 UTF-8。")
    return None


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_dotenv(content: str) -> dict[str, str]:
    """把 .env 的文本解析成 {变量名: 值}。写错的行跳过并记一条告警，不会抛异常。"""
    values: dict[str, str] = {}
    for number, raw in enumerate(content.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            _warn(f".env 第 {number} 行已忽略：没有等号（正确写法是 变量名=值，注释请以 # 开头）")
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        name = key.strip()
        # 写错的行跳过并说清是第几行，而不是让整个服务起不来。
        # 告警里只给行号，不引用这一行的任何内容——写错的行里，密钥可能出现在任何位置
        # （比如单独粘贴了一行带 = 结尾的 key，它会被当成「等号左边」）。
        if not _ENV_NAME.fullmatch(name):
            reason = "等号左边不是合法的变量名" if name else "这一行以 = 开头"
            _warn(f".env 第 {number} 行已忽略：{reason}（正确写法是 变量名=值）")
            continue
        values[name] = _strip_inline_comment(value)  # 同一个变量写了多次，以最后一次为准
    return values


def _load_dotenv() -> None:
    if not file_exists(ACTIVE_ENV_FILE):
        return
    content = _read_text(ACTIVE_ENV_FILE)
    if content is None:
        return
    for name, value in parse_dotenv(content).items():
        # 客户端传进来的环境变量优先；但传的是空串（配置模板里常见的占位）时不算数
        if os.environ.get(name):
            continue
        try:
            os.environ[name] = value
        except (ValueError, OSError):
            _warn(f".env 里 {name} 的值含有非法字符，已忽略")


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
    "volcengine": ["VOLCENGINE_API_KEYS", "VOLCENGINE_API_KEY"],
    "parallel": ["PARALLEL_API_KEYS", "PARALLEL_API_KEY"],
    "anysearch": ["ANYSEARCH_API_KEYS", "ANYSEARCH_API_KEY"],
    "keenable": ["KEENABLE_API_KEYS", "KEENABLE_API_KEY"],
    "youcom": ["YDC_API_KEYS", "YDC_API_KEY", "YOUCOM_API_KEYS", "YOUCOM_API_KEY"],
    "baidu": ["BAIDU_API_KEYS", "BAIDU_API_KEY"],
    "yuanbao": ["TENCENTCLOUD_WSA_APIKEYS", "TENCENTCLOUD_WSA_APIKEY"],
    "zhipu": ["ZHIPU_API_KEYS", "ZHIPU_API_KEY"],
    "bocha": ["BOCHA_API_KEYS", "BOCHA_API_KEY"],
}


def _split(raw: str) -> list[str]:
    """按逗号/分号/换行切分，并逐个去掉包裹引号。"""
    return [k for k in (_unquote(part) for part in _SPLIT.split(raw)) if k]


# 想明确表示「一个都不要」时写这些词；单纯留空（`XXX=`）一律按默认值处理，
# 免得 .env 里一行没填完的配置悄悄把功能关掉。
_NONE_WORDS = ("none", "off", "-", "无")


def _env_text(env_name: str, default: str = "") -> str:
    """读文本配置；没设置或留空都用默认值。"""
    return os.environ.get(env_name, "").strip() or default


def _terms(env_name: str, default: str = "") -> list[str]:
    """读「列表」类配置（保留大小写）。留空 = 默认值；写 none = 空列表。"""
    raw = os.environ.get(env_name, "").strip()
    if raw.lower() in _NONE_WORDS:
        return []
    return list(dict.fromkeys(_split(raw or default)))


def _names(env_name: str, default: str = "") -> list[str]:
    """读「厂商名单」类配置：统一小写、去重、保序。写成 Tavily 或重复写两遍都不会出问题。"""
    return list(dict.fromkeys(name.lower() for name in _terms(env_name, default)))


def _load_keys_json() -> dict[str, list[str]]:
    """可选：配置目录下 keys.json 集中管理多 key，格式 {"tavily": ["k1","k2"], ...}。

    环境变量里已经给了该家的 key 时，以环境变量为准（避免两份配置互相干扰）。
    """
    if not file_exists(ACTIVE_KEYS_FILE):
        return {}
    text = _read_text(ACTIVE_KEYS_FILE)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        _warn(f"keys.json 不是合法的 JSON，已忽略：{exc}")
        return {}
    if not isinstance(data, dict):
        _warn("keys.json 最外层必须是 {…} 对象，已忽略")
        return {}
    result: dict[str, list[str]] = {}
    for name, value in data.items():
        if name.startswith("_"):
            continue  # 模板里的 _说明 之类的注释键
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
# 客户端（MCP 注册项）传进来的环境变量要在读 .env 之前拍个快照。
# 热加载时先恢复这份快照再重新读 .env，才能保证「客户端 env > .env > keys.json」的优先级不被打乱。
_CLIENT_ENV: dict[str, str] = {
    name: os.environ[name]
    for names in _KEY_ENV_NAMES.values() for name in names if os.environ.get(name)
}
_CLIENT_ENV.update({
    name: value for name, value in os.environ.items()
    if name in {
        "SEARCH_MODE", "FREE_PROVIDERS", "PAID_PROVIDERS", "DOMESTIC_PROVIDERS", "GLOBAL_PROVIDERS",
        "ESCALATE_EXCLUDE", "PROXY_PROVIDERS", "SEARCH_PROVIDERS", "DAILY_LIMITS", "MONTHLY_LIMITS",
        "WEB_FETCH_PROVIDER", "UNVERIFIED_PROVIDERS", "SEARCH_PROXY", "SEARCH_PROXY_ALL",
        "USE_SYSTEM_PROXY", "SEARCH_TIMEOUT", "TOTAL_TIMEOUT", "ENABLE_KEYLESS", "ADAPTIVE_ROUTING",
        "REGION_ROUTING", "FETCH_CACHE_TTL", "FETCH_KEYED_FALLBACKS", "USAGE_LOG",
    } and value
})

API_KEYS: dict[str, list[str]] = {name: _keys_for(name) for name in _KEY_ENV_NAMES}

# ---- 自托管 / 可改写端点 ----
# SearXNG 没有 key，靠实例地址启用
SEARXNG_BASE_URL: str = os.environ.get("SEARXNG_BASE_URL", "").strip().rstrip("/")
# Firecrawl 支持自托管，其余厂商一般不需要改
FIRECRAWL_BASE_URL: str = _env_text("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev").rstrip("/")

# 可选：SEARCH_PROVIDERS=tavily,querit —— 限定 all 模式查哪几家（不影响第一阶段和补搜）
SEARCH_PROVIDERS_LIMIT: list[str] | None = _names("SEARCH_PROVIDERS") or None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# 可选：HTTP 代理。SEARCH_PROXY 只给 PROXY_PROVIDERS 里的厂商用；
# 设 SEARCH_PROXY_ALL=true 则所有厂商都走它。
SEARCH_PROXY: str = os.environ.get("SEARCH_PROXY", "").strip()
SEARCH_PROXY_ALL: bool = _env_bool("SEARCH_PROXY_ALL", False)
# 这些源在实测中国内直连不稳；可按自己的网络情况用 PROXY_PROVIDERS 覆盖
PROXY_PROVIDERS: set[str] = set(_names("PROXY_PROVIDERS", "jina,duckduckgo,youcom"))
# 其余厂商怎么走：默认跟随系统的网络设置——系统开了代理（或设了 HTTP(S)_PROXY）就会经过它。
# 设为 false 则强制直连，适合「代理开着全局模式、国内厂商被绕到境外节点」的情况。
USE_SYSTEM_PROXY: bool = _env_bool("USE_SYSTEM_PROXY", True)

# 免 key 来源是否默认参与聚合搜索。
# Parallel / Keenable / AnySearch 匿名可用且实测稳定，始终默认启用。
# DuckDuckGo 国内直连超时，Firecrawl 匿名与付费可能重复，因此这几家受开关控制。
# 设置 ENABLE_KEYLESS=true 可启用全部免 key 源。
ENABLE_KEYLESS: bool = _env_bool("ENABLE_KEYLESS", False)


def _env_number(raw_name: str, default, cast, minimum=None):
    """读取数值型配置；写错时回落到默认值并告警，不让 import 期崩溃。"""
    raw = os.environ.get(raw_name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw.strip())
        if not math.isfinite(value):  # nan / inf 能通过大小比较，必须单独挡掉
            raise ValueError(raw)
    except (TypeError, ValueError, OverflowError):
        _warn(f"配置 {raw_name}={raw!r} 不是合法数值，已改用默认值 {default}")
        return default
    if minimum is not None and value < minimum:
        _warn(f"配置 {raw_name}={value} 小于下限 {minimum}，已按 {minimum} 处理")
        return minimum
    return value


# 可选：默认每家返回条数 / 单家超时秒数 / 抓取正文截断长度
DEFAULT_MAX_RESULTS: int = _env_number("MAX_RESULTS", 5, int, minimum=1)
SEARCH_TIMEOUT: float = _env_number("SEARCH_TIMEOUT", 15.0, float, minimum=1.0)
FETCH_MAX_CHARS: int = _env_number("FETCH_MAX_CHARS", 20000, int, minimum=1000)

# 一次搜索 / 抓取的总时限（秒）。到点就带着已经拿到的结果返回，不再等慢的厂商，也不再递补。
# 很多 MCP 客户端的工具超时是 60 秒，一旦超时，已经搜到的结果会全部丢掉，所以默认留了余量。
TOTAL_TIMEOUT: float = _env_number("TOTAL_TIMEOUT", 45.0, float, minimum=5.0)

# 单家结果条数硬上限：各家 API 各有上限，统一卡在这里避免上游 400
MAX_RESULTS_CAP: int = _env_number("MAX_RESULTS_CAP", 20, int, minimum=1)
# 最终交付条数独立于每家抓取条数，避免 max_results=3 时只交付 6 条
TOTAL_RESULTS: int = _env_number("TOTAL_RESULTS", 12, int, minimum=1)
NEAR_DEDUP: bool = _env_bool("NEAR_DEDUP", True)

# ---- 抓取 ----
# 默认免费优先：先用匿名可用的 parallel / keenable，失败再用带 key 的后端。
# WEB_FETCH_PROVIDER=tavily（或 jina,tavily）可把指定后端排到最前。
FETCH_PREFERRED: list[str] = _names("WEB_FETCH_PROVIDER")
# 正文短于这个长度视为“可能没抓全”（反爬页 / 空壳页），会再换一个后端试一次；0 关闭。
FETCH_MIN_CHARS: int = _env_number("FETCH_MIN_CHARS", 200, int, minimum=0)
# 同一个网址在这么多秒内重复抓取，直接返回缓存（0 = 不缓存）。
# 一次搜索里多条结果指向同一个页面、或 Agent 连续追问同一页时，能省掉重复的额度。
FETCH_CACHE_TTL: int = _env_number("FETCH_CACHE_TTL", 300, int, minimum=0)
# 免费后端都失败后，最多再试几个带 key 的后端。地址本身打不开时，没必要把每家的额度都试一遍。
FETCH_KEYED_FALLBACKS: int = _env_number("FETCH_KEYED_FALLBACKS", 2, int, minimum=0)

# 搜索成本控制：auto 先查匿名源，质量不足才有限启用带 key 的源。
# 这些阈值只做第一道确定性门禁；最终语义是否充足由当前 Agent 结合证据判断。
#   auto      匿名优先，本地门禁不足才自动补搜
#   free      只做匿名调用，绝不发送账号 key
#   escalate  跳过匿名阶段，直接补几个不同厂商（供 Agent 判断不够时调用）
#   all       全部已启用厂商
SEARCH_MODES: tuple[str, ...] = ("auto", "free", "escalate", "all")
SEARCH_MODE: str = os.environ.get("SEARCH_MODE", "auto").strip().lower() or "auto"
if SEARCH_MODE not in SEARCH_MODES:
    _warn(f"配置 SEARCH_MODE={SEARCH_MODE!r} 无法识别（可选 {' / '.join(SEARCH_MODES)}），已按 auto 处理")
    SEARCH_MODE = "auto"
# 第一阶段的匿名源：默认不带 key 调用，即使配了它们的 key（例外见下面的 ANON_KEY_FALLBACK）。
FREE_PROVIDERS: list[str] = _names("FREE_PROVIDERS", "parallel,keenable,anysearch")
# 匿名通道被限流 / 拒绝时，如果配了那一家的 key，auto 模式就改用 key 再试一次（free 模式永远不会）。
# 设为 false 则第一阶段严格匿名：被限流的那家本轮直接算失败。
ANON_KEY_FALLBACK: bool = _env_bool("ANON_KEY_FALLBACK", True)
# 通用补搜顺序。名单里没写、但配了 key 的厂商会自动排在最后，不会被漏掉。
# 排序原则：**作者实测过的排前面**，按文档接入但没实测过的排后面——
# 未实测不等于不能用，配了 key 照样会轮到你，只是排在后面，先给稳的那些机会。
# 末尾三家是匿名源的“带 key 版”：同一问题的匿名结果已经有了就不会再选它们。
PAID_PROVIDERS: list[str] = _names(
    "PAID_PROVIDERS",
    # 已实测
    "volcengine,querit,tavily,exa,firecrawl,jina,baidu,"
    # 未实测（按文档接入，没有真实调用验证过）
    "yuanbao,zhipu,bocha,brave,ollama,youcom,"
    # 匿名源的带 key 版
    "parallel,keenable,anysearch",
)
# 没有经过真实调用验证的厂商：只在状态页标注、补搜时排到后面，**不禁用**。
UNVERIFIED_PROVIDERS: set[str] = set(_names(
    "UNVERIFIED_PROVIDERS", "yuanbao,zhipu,bocha,brave,ollama,youcom,perplexity,searxng"
))
# 自适应路由：按近 7 天的实际表现微调补搜顺序（只在本组内调整，绝不跨区、不永久剔除）。
# 关掉它就完全按静态名单排序。
ADAPTIVE_ROUTING: bool = _env_bool("ADAPTIVE_ROUTING", True)
# 某家至少要有这么多次真实调用，才允许参与调权（避免几次偶然失败就把好厂商压下去）。
ADAPTIVE_MIN_CALLS: int = _env_number("ADAPTIVE_MIN_CALLS", 5, int, minimum=1)
# 成功率低于这个值才降权；高于 0.95 且调用够多的略微提权。
ADAPTIVE_BAD_RATE: float = _env_number("ADAPTIVE_BAD_RATE", 0.6, float, minimum=0.0)
ADAPTIVE_GOOD_RATE: float = _env_number("ADAPTIVE_GOOD_RATE", 0.95, float, minimum=0.0)
# 不参与自动补搜的厂商（仍可用 providers 点名，all 模式也照常包含）。
# Perplexity 每次搜索是一次大模型调用，比普通搜索贵得多，默认不自动动用。
ESCALATE_EXCLUDE: set[str] = set(_names("ESCALATE_EXCLUDE", "perplexity"))
# 地区只影响第二阶段补源顺序，不过滤第一阶段或最终结果中的跨地区信息。
# auto：根据问题中的地域实体选择；off：沿用 PAID_PROVIDERS；cn/global：手动指定。
REGION_ROUTING: str = _env_text("REGION_ROUTING", "auto").lower()
DOMESTIC_PROVIDERS: list[str] = _names(
    "DOMESTIC_PROVIDERS", "baidu,volcengine,querit,yuanbao,zhipu,bocha"
)
GLOBAL_PROVIDERS: list[str] = _names("GLOBAL_PROVIDERS", "tavily,exa,firecrawl,jina")
# 只把明确的地域实体作为信号，不能把中文提问等同于国内信息。
DOMESTIC_QUERY_TERMS: list[str] = _terms(
    "DOMESTIC_QUERY_TERMS",
    "中国,国内,大陆,中美,北京,上海,广州,深圳,杭州,成都,重庆,武汉,西安,南京,天津,苏州,国务院,工信部,发改委,国产,人民币,沪深,A股,微信,支付宝,淘宝,天猫,京东,抖音,快手,B站,哔哩哔哩,知乎,小红书,百度,阿里,腾讯,字节跳动,华为,小米,比亚迪,宁德时代",
)
GLOBAL_QUERY_TERMS: list[str] = _terms(
    "GLOBAL_QUERY_TERMS",
    "美国,中美,英国,加拿大,欧洲,欧盟,日本,韩国,新加坡,印度,澳大利亚,俄罗斯,美元,美股,纳斯达克,硅谷,OpenAI,Anthropic,Google,Microsoft,Meta,NVIDIA,Apple,Amazon,Tesla,GitHub,Linux,Kubernetes,Docker,React,Vue,Python,Java,Rust,Go,Node.js,AWS,Azure,GCP,Cloudflare",
)
# 英文短词当地域信号容易误判：query 里写 "how to go back" 不该被当成 Go 语言。
# 这里的词在 routing 里必须区分大小写，并且紧跟版本号 / 语言名 / 常见搭配才算命中。
CASE_SENSITIVE_QUERY_TERMS: set[str] = {"go"}
# auto 模式门禁不足时自动追加的不同厂商数；设 0 关闭自动补搜，完全交给 Agent 用 escalate 决定。
AUTO_ESCALATE_MAX: int = _env_number("AUTO_ESCALATE_MAX", 3, int, minimum=0)
# escalate 模式每次追加的不同厂商数。
ESCALATE_COUNT: int = _env_number("ESCALATE_COUNT", 3, int, minimum=1)
AUTO_MIN_RESULTS: int = _env_number("AUTO_MIN_RESULTS", 4, int, minimum=1)
AUTO_MIN_UNIQUE: int = _env_number("AUTO_MIN_UNIQUE", 3, int, minimum=1)
AUTO_MIN_CONSENSUS: int = _env_number("AUTO_MIN_CONSENSUS", 1, int, minimum=0)
# 官方一手事实源可作为三源共识的替代证据，只用于政策/价格/文档类查询。
# 域名是提示而非事实证明，Agent 仍需审核页面内容和时间。
OFFICIAL_SOURCE_DOMAINS: list[str] = _terms(
    "OFFICIAL_SOURCE_DOMAINS",
    "gov.cn,miit.gov.cn,ndrc.gov.cn,openai.com,developers.openai.com,platform.openai.com,anthropic.com,modelcontextprotocol.io,kubernetes.io,learn.microsoft.com,cloud.google.com,docs.python.org,developer.mozilla.org",
)
# 含比较、最新、价格、政策、多实体等语义时提高门禁，不能仅凭结果数量直接交付。
COMPLEX_QUERY_PATTERNS: list[str] = _terms(
    "COMPLEX_QUERY_PATTERNS",
    "对比,比较,区别,哪个好,最新,今天,近期,价格,政策,法规,风险,评测,深度,分析,vs,versus,compare,latest,pricing",
)


# ---- HTTP 传输（只有用 --transport streamable-http 时才相关）----
# 服务鉴权 Token。绑定非回环地址（对外开放）时**必须**配置，否则拒绝启动——
# 不带鉴权的搜索服务相当于把本机的 key 和额度开放给整个局域网。
MCP_HTTP_TOKEN: str = os.environ.get("MCP_HTTP_TOKEN", "").strip()

# ---- 状态与记录文件：默认都在配置目录里，相对路径也挂到配置目录 ----
def _state_path(env_name: str, default_name: str) -> Path:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return CONFIG_DIR / default_name
    path = Path(os.path.expandvars(raw)).expanduser()
    if path.is_absolute():
        return path
    if path.drive or path.root:
        # Windows 上 `\state.json`（没盘符）和 `D:state.json`（没斜杠）都不算绝对路径，
        # 直接拼接会落到盘根或另一个盘的当前目录，位置难以预料。
        _warn(f"配置 {env_name}={raw!r} 不是完整的绝对路径，已改用默认位置 {CONFIG_DIR / default_name}")
        return CONFIG_DIR / default_name
    return CONFIG_DIR / path


# key 限额状态（只存 key 的哈希和状态，不存 key 本身）
KEY_STATE_FILE: Path = _state_path("KEY_STATE_FILE", "key-state.json")
# 额度耗尽 / 限流时的默认冷却秒数（上游没给 Retry-After 时使用）
QUOTA_COOLDOWN: int = _env_number("QUOTA_COOLDOWN", 3600, int, minimum=1)
RATE_COOLDOWN: int = _env_number("RATE_COOLDOWN", 60, int, minimum=1)

# 本地使用记录：每次搜索 / 抓取一行，只写在本机配置目录
USAGE_LOG: bool = _env_bool("USAGE_LOG", True)
USAGE_LOG_FILE: Path = _state_path("USAGE_LOG_FILE", "usage.jsonl")
USAGE_LOG_MAX_MB: float = _env_number("USAGE_LOG_MAX_MB", 5.0, float, minimum=0.1)
USAGE_COUNTER_FILE: Path = _state_path("USAGE_COUNTER_FILE", "usage-counters.json")


def _parse_limits(raw: str, env_name: str) -> dict[str, int]:
    """`baidu:90,tavily:30` -> {"baidu": 90, "tavily": 30}；0 表示不限。"""
    limits: dict[str, int] = {}
    for part in _split(raw):
        name, sep, value = part.replace("=", ":").partition(":")
        name = name.strip().lower()
        try:
            number = int(value.strip())
        except ValueError:
            number = -1
        if not sep or not name or number < 0:
            _warn(f"配置 {env_name} 里的 {part!r} 无法识别（应为 厂商:次数，如 baidu:90），已忽略")
            continue
        limits[name] = number
    return limits


# 每个 key 每天 / 每月最多发出多少次请求，到量就当它不可用，直到下一天 / 下个月。
# 百度千帆超出每日免费额度后会按量计费，所以内置一个留了余量的默认值；
# 其它家默认不限。写 `厂商:0` 可取消某家的上限。
DAILY_LIMITS: dict[str, int] = {
    "baidu": 90,
    **_parse_limits(os.environ.get("DAILY_LIMITS", ""), "DAILY_LIMITS"),
}
MONTHLY_LIMITS: dict[str, int] = _parse_limits(
    os.environ.get("MONTHLY_LIMITS", ""), "MONTHLY_LIMITS"
)


# ---- 厂商名拼写校验 ----
# 这些配置项里写的都是厂商名，拼错一个字就会静默失效（比如 FREE_PROVIDERS=paralel），
# 表现出来只是「明明配了却不生效」，很难查。这里在启动时统一对一遍，写进 WARNINGS。
_KNOWN_PROVIDERS: frozenset[str] = frozenset(_KEY_ENV_NAMES) | {"duckduckgo", "searxng", "firecrawl"}


def check_provider_names() -> list[str]:
    """检查各配置项里的厂商名是否拼对，返回问题描述（同时写进 WARNINGS）。"""
    problems: list[str] = []
    seen: set[str] = set()

    def check_list(label: str, names, extra: frozenset[str] = frozenset()) -> None:
        unknown = [name for name in names if name and name not in _KNOWN_PROVIDERS | extra]
        if not unknown:
            return
        message = f"配置 {label} 里的厂商标识无法识别：{', '.join(sorted(set(unknown)))}（拼写有误？）"
        problems.append(message)

    for label, names in (
        ("FREE_PROVIDERS", FREE_PROVIDERS),
        ("PAID_PROVIDERS", PAID_PROVIDERS),
        ("DOMESTIC_PROVIDERS", DOMESTIC_PROVIDERS),
        ("GLOBAL_PROVIDERS", GLOBAL_PROVIDERS),
        ("ESCALATE_EXCLUDE", sorted(ESCALATE_EXCLUDE)),
        ("PROXY_PROVIDERS", sorted(PROXY_PROVIDERS)),
        ("SEARCH_PROVIDERS", SEARCH_PROVIDERS_LIMIT or []),
    ):
        check_list(label, names)
    for label, limits in (("DAILY_LIMITS", DAILY_LIMITS), ("MONTHLY_LIMITS", MONTHLY_LIMITS)):
        check_list(label, list(limits))
    # 抓取后端只允许真正支持抓取的那几家
    fetch_names = {"tavily", "keenable", "firecrawl", "ollama", "exa", "jina", "parallel"}
    bad_fetch = [name for name in FETCH_PREFERRED if name and name not in fetch_names]
    if bad_fetch:
        problems.append(
            f"配置 WEB_FETCH_PROVIDER 里的厂商不支持抓取：{', '.join(sorted(set(bad_fetch)))}"
            f"（可选：{', '.join(sorted(fetch_names))}）"
        )
    # keys.json 里写了不存在的厂商，多半也是拼错了
    bad_json = [name for name in _JSON_KEYS if name not in _KNOWN_PROVIDERS]
    if bad_json:
        problems.append(f"keys.json 里的厂商标识无法识别：{', '.join(sorted(set(bad_json)))}（拼写有误？）")

    for message in problems:
        if message not in seen:
            seen.add(message)
            _warn(message)
    return problems


check_provider_names()


# ---------------------------------------------------------------------------
# 热加载：改了 .env / keys.json 之后，不用重启 MCP 服务就生效
#
# 只重载「改完希望马上生效」的东西：各家 key、以及依托 key 的一切派生配置。
# AGGREGATE_SEARCH_HOME 这类目录级路径保持启动期的值——运行期换目录会让状态文件
# 和用量记录散落到两个地方，得不偿失。
# ---------------------------------------------------------------------------

# 热加载会重新读取的环境变量名：各家的 key 变量 + 运行时开关
_RELOADABLE_ENV: frozenset[str] = frozenset(
    [env_name for names in _KEY_ENV_NAMES.values() for env_name in names]
    + [
        "SEARCH_MODE", "FREE_PROVIDERS", "PAID_PROVIDERS", "DOMESTIC_PROVIDERS",
        "GLOBAL_PROVIDERS", "ESCALATE_EXCLUDE", "PROXY_PROVIDERS", "SEARCH_PROVIDERS",
        "DAILY_LIMITS", "MONTHLY_LIMITS", "WEB_FETCH_PROVIDER", "UNVERIFIED_PROVIDERS",
        "SEARCH_PROXY", "SEARCH_PROXY_ALL", "USE_SYSTEM_PROXY", "SEARCH_TIMEOUT",
        "TOTAL_TIMEOUT", "ENABLE_KEYLESS", "ADAPTIVE_ROUTING", "REGION_ROUTING",
        "FETCH_CACHE_TTL", "FETCH_KEYED_FALLBACKS", "USAGE_LOG",
    ]
)

_ENV_SIGNATURE: tuple = ()


def _files_signature() -> tuple:
    """两个配置文件的 (路径, mtime, 大小)。没有文件时记 None。"""
    items = []
    for path in (ACTIVE_ENV_FILE, ACTIVE_KEYS_FILE):
        try:
            stat = path.stat()
            items.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            items.append((str(path), None, None))
    return tuple(items)


def mark_loaded() -> None:
    """记住当前配置文件的指纹（启动时调用一次）。"""
    global _ENV_SIGNATURE
    _ENV_SIGNATURE = _files_signature()


def reload_if_changed() -> bool:
    """配置文件变了就重新读一遍。返回 True 表示发生了重载。

    - 只刷新 key 相关配置与派生名单，不动目录级路径
    - 单行写错照样跳过并告警，不会崩、也不会把密钥打进日志
    - 客户端传入的环境变量始终优先（重载前先恢复基线）
    """
    global _ENV_SIGNATURE, API_KEYS, _JSON_KEYS
    signature = _files_signature()
    if signature == _ENV_SIGNATURE:
        return False
    _ENV_SIGNATURE = signature

    warnings_before = len(WARNINGS)
    # 1) 把上次由 .env 写进 os.environ 的值清掉，恢复到「客户端 env」基线。
    #    注意要比对环境变量名（TAVILY_API_KEYS），不是厂商名（tavily）。
    for name in _RELOADABLE_ENV:
        os.environ.pop(name, None)
    for name, value in _CLIENT_ENV.items():
        os.environ[name] = value

    # 2) 重新读 .env 与 keys.json
    _load_dotenv()
    _JSON_KEYS = _load_keys_json()
    API_KEYS = {name: _keys_for(name) for name in _KEY_ENV_NAMES}

    # 3) 就地刷新各 provider 的 key 池（保留还存在的 key 的轮转位置）
    try:
        from .providers import refresh_pools
        refresh_pools(API_KEYS)
    except Exception as exc:  # 循环导入或 provider 层出问题时，至少 key 字典已更新
        _warn(f"热加载时刷新 key 池失败：{exc}")

    if len(WARNINGS) > warnings_before:
        log("配置文件已重新加载，但有部分内容有问题（见上方告警）")
    else:
        log("配置文件已重新加载（改动即时生效）")
    return True


mark_loaded()
