r"""集中读取配置。

配置放在全局配置目录里，和代码装在哪个目录无关：

    默认  ~/.aggregate-search/      Windows 即 C:\Users\<用户名>\.aggregate-search\
    覆盖  环境变量 AGGREGATE_SEARCH_HOME（填绝对路径）

目录内的文件：
    .env                  key 与各项开关
    keys.json             可选，集中管理多 key
    state.db              key 冷却状态与日 / 月用量（只存哈希）
    usage.jsonl           本地使用记录

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
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import cached_property, partial, wraps
from pathlib import Path
from types import MappingProxyType


# 在读任何文件之前截取客户端环境；.env 不写回 os.environ。
_CLIENT_ENV = dict(os.environ)
_HOME_OVERRIDE: str = _CLIENT_ENV.get("AGGREGATE_SEARCH_HOME", "").strip().strip("\"'")


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
    """仅在新位置明确不存在时才回退旧配置；权限出错不能重新启用旧文件。"""
    try:
        path.stat()
        return path, False
    except FileNotFoundError:
        pass
    except OSError:
        return path, False
    legacy = legacy_file(path.name)
    return (legacy, True) if legacy else (path, False)


def active_files() -> tuple[tuple[Path, bool], tuple[Path, bool]]:
    return _active(ENV_FILE), _active(KEYS_FILE)


def _read_text(path: Path, *, strict: bool = False) -> str | None:
    """读配置文本。Windows 上常见两种非 UTF-8 的情况也要认：

    - PowerShell 5 的 `echo ... > .env` 写出来是 UTF-16
    - 旧版记事本默认按系统编码（简体中文系统是 GBK）保存
    读不出来就明确告警，而不是悄悄当成「没有配置」。
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if not strict:
            _warn(f"读不了 {path}：文件不存在")
        return None
    except OSError as exc:
        _warn(f"读不了 {path}：{exc}")
        if strict:
            raise
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
    _warn(f"{path} 的编码无法识别，请用编辑器另存为 UTF-8。")
    if strict:
        raise OSError("配置文件编码无法识别")
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


def _load_dotenv(path: Path, *, strict: bool = False) -> dict[str, str]:
    if not strict and not file_exists(path):
        return {}
    content = _read_text(path, strict=strict)
    values = {}
    for name, value in parse_dotenv(content or "").items():
        if "\0" in value:
            _warn(f".env 里 {name} 的值含有非法字符，已忽略")
        else:
            values[name.upper() if os.name == "nt" else name] = value
    return values


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


def _env_text(env_name: str, default: str = "", *, env=None) -> str:
    """读文本配置；没设置或留空都用默认值。"""
    return (os.environ if env is None else env).get(env_name, "").strip() or default


def _terms(env_name: str, default: str = "", *, env=None) -> list[str]:
    """读「列表」类配置（保留大小写）。留空 = 默认值；写 none = 空列表。"""
    raw = (os.environ if env is None else env).get(env_name, "").strip()
    if raw.lower() in _NONE_WORDS:
        return []
    return list(dict.fromkeys(_split(raw or default)))


def _names(env_name: str, default: str = "", *, env=None) -> list[str]:
    """读「厂商名单」类配置：统一小写、去重、保序。写成 Tavily 或重复写两遍都不会出问题。"""
    return list(dict.fromkeys(name.lower() for name in _terms(env_name, default, env=env)))


def _load_keys_json(path: Path, *, strict: bool = False) -> dict[str, list[str]]:
    """可选：配置目录下 keys.json 集中管理多 key，格式 {"tavily": ["k1","k2"], ...}。

    环境变量里已经给了该家的 key 时，以环境变量为准（避免两份配置互相干扰）。
    """
    if not strict and not file_exists(path):
        return {}
    text = _read_text(path, strict=strict)
    if text is None:
        return {}
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        _warn("keys.json 不是合法的 JSON")
        if strict:
            raise OSError("keys.json 无法解析") from None
        return {}
    if not isinstance(data, dict):
        _warn("keys.json 最外层必须是 {…} 对象")
        if strict:
            raise OSError("keys.json 结构错误")
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



def _dedupe(keys: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _keys_for(name: str, dotenv: Mapping[str, str], json_keys: Mapping[str, list[str]]) -> tuple[str, ...]:
    # 同一家厂商的不同别名也按来源分层，不能把文件里的旧 key 混进客户端的池。
    for env in (_CLIENT_ENV, dotenv):
        keys = _dedupe([key for alias in _KEY_ENV_NAMES[name] for key in _split(env.get(alias, ""))])
        if keys:
            return tuple(keys)
    return tuple(_dedupe(json_keys.get(name, [])))


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_number(env: Mapping[str, str], name: str, default, cast, minimum=None):
    """读取数值型配置；写错时回落到默认值并告警。"""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw.strip())
        if not math.isfinite(value):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        _warn(f"配置 {name} 不是合法数值，已改用默认值 {default}")
        return default
    if minimum is not None and value < minimum:
        _warn(f"配置 {name}={value} 小于下限 {minimum}，已按 {minimum} 处理")
        return minimum
    return value


SEARCH_MODES = ("auto", "free", "escalate", "all")
# 英文短词得区分大小写，并配合上下文，避免把 "how to go back" 当作 Go 语言。
CASE_SENSITIVE_QUERY_TERMS = frozenset({"go"})


def _effective_env(dotenv: dict[str, str]) -> dict[str, str]:
    # 空串是注册模板里的占位，保持以往的回落行为。
    return {**dotenv, **{name: value for name, value in _CLIENT_ENV.items() if value.strip()}}


def _state_path(env: Mapping[str, str], env_name: str, default_name: str) -> Path:
    raw = env.get(env_name, "").strip()
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
        if not sep or name not in _KNOWN_PROVIDERS or number < 0:
            _warn(f"配置 {env_name} 中有条目无法识别（应为 厂商:次数，如 baidu:90），已忽略")
            continue
        limits[name] = number
    return limits


@dataclass(frozen=True)
class RuntimeSettings:
    """一次工具调用看到的配置。可变容器在发布快照前转成只读值。"""

    revision: int
    API_KEYS: Mapping[str, tuple[str, ...]] = field(repr=False)
    SEARXNG_BASE_URL: str
    FIRECRAWL_BASE_URL: str
    SEARCH_PROVIDERS_LIMIT: tuple[str, ...]
    SEARCH_PROXY: str = field(repr=False)
    SEARCH_PROXY_ALL: bool
    PROXY_PROVIDERS: frozenset[str]
    USE_SYSTEM_PROXY: bool
    ENABLE_KEYLESS: bool
    DEFAULT_MAX_RESULTS: int
    SEARCH_TIMEOUT: float
    FETCH_MAX_CHARS: int
    FETCH_CACHE_MAX_CHARS: int
    SEARCH_EXCERPT_MAX_CHARS: int
    TOTAL_TIMEOUT: float
    MAX_RESULTS_CAP: int
    TOTAL_RESULTS: int
    NEAR_DEDUP: bool
    FETCH_PREFERRED: tuple[str, ...]
    FETCH_MIN_CHARS: int
    FETCH_CACHE_TTL: int
    FETCH_KEYED_FALLBACKS: int
    SEARCH_MODE: str
    SEARCH_NO_ACCOUNT_KEYS: bool
    FREE_PROVIDERS: tuple[str, ...]
    ANON_KEY_FALLBACK: bool
    PAID_PROVIDERS: tuple[str, ...]
    UNVERIFIED_PROVIDERS: frozenset[str]
    ADAPTIVE_ROUTING: bool
    ADAPTIVE_MIN_CALLS: int
    ADAPTIVE_BAD_RATE: float
    ADAPTIVE_GOOD_RATE: float
    ESCALATE_EXCLUDE: frozenset[str]
    REGION_ROUTING: str
    DOMESTIC_PROVIDERS: tuple[str, ...]
    GLOBAL_PROVIDERS: tuple[str, ...]
    DOMESTIC_QUERY_TERMS: tuple[str, ...]
    GLOBAL_QUERY_TERMS: tuple[str, ...]
    AUTO_ESCALATE_MAX: int
    ESCALATE_COUNT: int
    AUTO_MIN_RESULTS: int
    AUTO_MIN_UNIQUE: int
    AUTO_MIN_CONSENSUS: int
    OFFICIAL_SOURCE_DOMAINS: tuple[str, ...]
    COMPLEX_QUERY_PATTERNS: tuple[str, ...]
    QUOTA_COOLDOWN: int
    RATE_COOLDOWN: int
    USAGE_LOG: bool
    USAGE_LOG_MAX_MB: float
    DAILY_LIMITS: Mapping[str, int]
    MONTHLY_LIMITS: Mapping[str, int]

    @cached_property
    def secrets(self) -> tuple[str, ...]:
        secrets = {key for keys in self.API_KEYS.values() for key in keys}
        if MCP_HTTP_TOKEN:
            secrets.add(MCP_HTTP_TOKEN)
        return tuple(sorted(secrets, key=len, reverse=True))


def _build_settings(
    dotenv: dict[str, str], json_keys: dict[str, list[str]], revision: int,
    previous: RuntimeSettings | None = None,
) -> RuntimeSettings:
    env = _effective_env(dotenv)
    text = partial(_env_text, env=env)
    terms = partial(_terms, env=env)
    names = partial(_names, env=env)
    flag = partial(_env_bool, env)
    number = partial(_env_number, env)
    mode = text("SEARCH_MODE", "auto").lower()
    if mode not in SEARCH_MODES:
        _warn(f"配置 SEARCH_MODE 无法识别（可选 {' / '.join(SEARCH_MODES)}），已按 auto 处理")
        mode = "auto"
    raw_lock = text("SEARCH_NO_ACCOUNT_KEYS").lower()
    if raw_lock in ("0", "false", "no", "off"):
        locked = False
    elif raw_lock or (previous and previous.SEARCH_NO_ACCOUNT_KEYS):
        if raw_lock not in ("1", "true", "yes", "on"):
            _warn("SEARCH_NO_ACCOUNT_KEYS 继续禁止账号 key；要解除请明确设置 false")
        locked = True
    else:
        locked = False

    def limits(name: str, defaults: dict[str, int]) -> Mapping[str, int]:
        if previous is not None:
            # 保存中途没有读到某厂商的额度配置，不应把有效上限放宽。显式写 厂商:0 取消。
            defaults = {**defaults, **getattr(previous, name)}
        return MappingProxyType({**defaults, **_parse_limits(text(name), name)})

    return RuntimeSettings(
        revision=revision,
        API_KEYS=MappingProxyType({name: _keys_for(name, dotenv, json_keys) for name in _KEY_ENV_NAMES}),
        SEARXNG_BASE_URL=text("SEARXNG_BASE_URL").rstrip("/"),
        FIRECRAWL_BASE_URL=text("FIRECRAWL_BASE_URL", "https://api.firecrawl.dev").rstrip("/"),
        SEARCH_PROVIDERS_LIMIT=tuple(names("SEARCH_PROVIDERS")),
        SEARCH_PROXY=text("SEARCH_PROXY"),
        SEARCH_PROXY_ALL=flag("SEARCH_PROXY_ALL", False),
        PROXY_PROVIDERS=frozenset(names("PROXY_PROVIDERS", "jina,duckduckgo,youcom")),
        USE_SYSTEM_PROXY=flag("USE_SYSTEM_PROXY", True),
        ENABLE_KEYLESS=flag("ENABLE_KEYLESS", False),
        DEFAULT_MAX_RESULTS=number("MAX_RESULTS", 5, int, minimum=1),
        SEARCH_TIMEOUT=number("SEARCH_TIMEOUT", 15.0, float, minimum=1.0),
        FETCH_MAX_CHARS=number("FETCH_MAX_CHARS", 20000, int, minimum=1000),
        FETCH_CACHE_MAX_CHARS=number("FETCH_CACHE_MAX_CHARS", 100000, int, minimum=1000),
        SEARCH_EXCERPT_MAX_CHARS=number("SEARCH_EXCERPT_MAX_CHARS", 6000, int, minimum=100),
        TOTAL_TIMEOUT=number("TOTAL_TIMEOUT", 45.0, float, minimum=5.0),
        MAX_RESULTS_CAP=number("MAX_RESULTS_CAP", 20, int, minimum=1),
        TOTAL_RESULTS=number("TOTAL_RESULTS", 12, int, minimum=1),
        NEAR_DEDUP=flag("NEAR_DEDUP", True),
        FETCH_PREFERRED=tuple(names("WEB_FETCH_PROVIDER")),
        FETCH_MIN_CHARS=number("FETCH_MIN_CHARS", 200, int, minimum=0),
        FETCH_CACHE_TTL=number("FETCH_CACHE_TTL", 300, int, minimum=0),
        FETCH_KEYED_FALLBACKS=number("FETCH_KEYED_FALLBACKS", 2, int, minimum=0),
        SEARCH_MODE=mode,
        SEARCH_NO_ACCOUNT_KEYS=locked,
        FREE_PROVIDERS=tuple(names("FREE_PROVIDERS", "parallel,keenable,anysearch")),
        ANON_KEY_FALLBACK=flag("ANON_KEY_FALLBACK", True),
        PAID_PROVIDERS=tuple(names(
            "PAID_PROVIDERS",
            "volcengine,querit,tavily,exa,firecrawl,jina,baidu,"
            "yuanbao,zhipu,bocha,brave,ollama,youcom,parallel,keenable,anysearch",
        )),
        UNVERIFIED_PROVIDERS=frozenset(names(
            "UNVERIFIED_PROVIDERS", "yuanbao,zhipu,bocha,brave,ollama,youcom,perplexity,searxng",
        )),
        ADAPTIVE_ROUTING=flag("ADAPTIVE_ROUTING", True),
        ADAPTIVE_MIN_CALLS=number("ADAPTIVE_MIN_CALLS", 5, int, minimum=1),
        ADAPTIVE_BAD_RATE=number("ADAPTIVE_BAD_RATE", 0.6, float, minimum=0.0),
        ADAPTIVE_GOOD_RATE=number("ADAPTIVE_GOOD_RATE", 0.95, float, minimum=0.0),
        ESCALATE_EXCLUDE=frozenset(names("ESCALATE_EXCLUDE", "perplexity")),
        REGION_ROUTING=text("REGION_ROUTING", "auto").lower(),
        DOMESTIC_PROVIDERS=tuple(names("DOMESTIC_PROVIDERS", "baidu,volcengine,querit,yuanbao,zhipu,bocha")),
        GLOBAL_PROVIDERS=tuple(names("GLOBAL_PROVIDERS", "tavily,exa,firecrawl,jina")),
        DOMESTIC_QUERY_TERMS=tuple(terms("DOMESTIC_QUERY_TERMS", "中国,国内,大陆,中美,北京,上海,广州,深圳,杭州,成都,重庆,武汉,西安,南京,天津,苏州,国务院,工信部,发改委,国产,人民币,沪深,A股,微信,支付宝,淘宝,天猫,京东,抖音,快手,B站,哔哩哔哩,知乎,小红书,百度,阿里,腾讯,字节跳动,华为,小米,比亚迪,宁德时代")),
        GLOBAL_QUERY_TERMS=tuple(terms("GLOBAL_QUERY_TERMS", "美国,中美,英国,加拿大,欧洲,欧盟,日本,韩国,新加坡,印度,澳大利亚,俄罗斯,美元,美股,纳斯达克,硅谷,OpenAI,Anthropic,Google,Microsoft,Meta,NVIDIA,Apple,Amazon,Tesla,GitHub,Linux,Kubernetes,Docker,React,Vue,Python,Java,Rust,Go,Node.js,AWS,Azure,GCP,Cloudflare")),
        AUTO_ESCALATE_MAX=number("AUTO_ESCALATE_MAX", 3, int, minimum=0),
        ESCALATE_COUNT=number("ESCALATE_COUNT", 3, int, minimum=1),
        AUTO_MIN_RESULTS=number("AUTO_MIN_RESULTS", 4, int, minimum=1),
        AUTO_MIN_UNIQUE=number("AUTO_MIN_UNIQUE", 3, int, minimum=1),
        AUTO_MIN_CONSENSUS=number("AUTO_MIN_CONSENSUS", 1, int, minimum=0),
        OFFICIAL_SOURCE_DOMAINS=tuple(terms("OFFICIAL_SOURCE_DOMAINS", "gov.cn,miit.gov.cn,ndrc.gov.cn,openai.com,developers.openai.com,platform.openai.com,anthropic.com,modelcontextprotocol.io,kubernetes.io,learn.microsoft.com,cloud.google.com,docs.python.org,developer.mozilla.org")),
        COMPLEX_QUERY_PATTERNS=tuple(terms("COMPLEX_QUERY_PATTERNS", "对比,比较,区别,哪个好,最新,今天,近期,价格,政策,法规,风险,评测,深度,分析,vs,versus,compare,latest,pricing")),
        QUOTA_COOLDOWN=number("QUOTA_COOLDOWN", 3600, int, minimum=1),
        RATE_COOLDOWN=number("RATE_COOLDOWN", 60, int, minimum=1),
        USAGE_LOG=flag("USAGE_LOG", True),
        USAGE_LOG_MAX_MB=number("USAGE_LOG_MAX_MB", 5.0, float, minimum=0.1),
        # 百度超出免费额度后按量计费，内置一个留了余量的默认上限。
        DAILY_LIMITS=limits("DAILY_LIMITS", {"baidu": 90}),
        MONTHLY_LIMITS=limits("MONTHLY_LIMITS", {}),
    )


# ---- 厂商名拼写校验 ----
# 这些配置项里写的都是厂商名，拼错一个字就会静默失效（比如 FREE_PROVIDERS=paralel），
# 表现出来只是「明明配了却不生效」，很难查。这里在启动时统一对一遍，写进 WARNINGS。
_KNOWN_PROVIDERS: frozenset[str] = frozenset(_KEY_ENV_NAMES) | {"duckduckgo", "searxng", "firecrawl"}


def check_provider_names(settings: RuntimeSettings | None = None, json_keys=None) -> list[str]:
    """检查各配置项里的厂商名是否拼对，返回问题描述（同时写进 WARNINGS）。"""
    settings = settings if settings is not None else get_settings()
    json_keys = _JSON_KEYS if json_keys is None else json_keys
    problems: list[str] = []

    def check_list(label: str, names, extra: frozenset[str] = frozenset()) -> None:
        unknown = [name for name in names if name and name not in _KNOWN_PROVIDERS | extra]
        if not unknown:
            return
        message = f"配置 {label} 里的厂商标识无法识别：{', '.join(sorted(set(unknown)))}（拼写有误？）"
        problems.append(message)

    for label, names in (
        ("FREE_PROVIDERS", settings.FREE_PROVIDERS),
        ("PAID_PROVIDERS", settings.PAID_PROVIDERS),
        ("DOMESTIC_PROVIDERS", settings.DOMESTIC_PROVIDERS),
        ("GLOBAL_PROVIDERS", settings.GLOBAL_PROVIDERS),
        ("ESCALATE_EXCLUDE", sorted(settings.ESCALATE_EXCLUDE)),
        ("PROXY_PROVIDERS", sorted(settings.PROXY_PROVIDERS)),
        ("SEARCH_PROVIDERS", settings.SEARCH_PROVIDERS_LIMIT or []),
    ):
        check_list(label, names)
    for label, limits in (("DAILY_LIMITS", settings.DAILY_LIMITS), ("MONTHLY_LIMITS", settings.MONTHLY_LIMITS)):
        check_list(label, list(limits))
    # 抓取后端只允许真正支持抓取的那几家
    fetch_names = {"tavily", "keenable", "firecrawl", "ollama", "exa", "jina", "parallel"}
    bad_fetch = [name for name in settings.FETCH_PREFERRED if name and name not in fetch_names]
    if bad_fetch:
        problems.append(
            f"配置 WEB_FETCH_PROVIDER 里的厂商不支持抓取：{', '.join(sorted(set(bad_fetch)))}"
            f"（可选：{', '.join(sorted(fetch_names))}）"
        )
    # keys.json 里写了不存在的厂商，多半也是拼错了
    bad_json = [name for name in json_keys if name not in _KNOWN_PROVIDERS]
    if bad_json:
        problems.append(f"keys.json 里的厂商标识无法识别：{', '.join(sorted(set(bad_json)))}（拼写有误？）")

    for message in dict.fromkeys(problems):
        _warn(message)
    return problems

# ---- 配置快照与热加载 ----

_SETTINGS_LOCK = threading.RLock()
_REQUEST_SETTINGS: ContextVar[RuntimeSettings | None] = ContextVar("aggregate_search_config", default=None)


def _file_signature(path: Path) -> tuple:
    """同时识别原地保存、原子替换、新建或删除。"""
    try:
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino
    except FileNotFoundError:
        return (str(path),)


def _read_source(path: Path, read, previous_signature, previous_values):
    """正在保存或暂不可读时，保留该文件上一份配置；另一个文件仍可更新。"""
    try:
        signature = _file_signature(path)
        if signature == previous_signature:
            return previous_signature, previous_values
        values = read(path, strict=True)
        if signature == _file_signature(path):
            return signature, values
    except OSError:
        pass
    return previous_signature, previous_values


def get_live_settings() -> RuntimeSettings:
    return _SETTINGS


def get_settings() -> RuntimeSettings:
    return _REQUEST_SETTINGS.get() or _SETTINGS


def force_anonymous() -> bool:
    """服务端硬锁。已在途的请求可以结束，但不能在硬锁开启后重试账号 key。"""
    return _ENV_SIGNATURE is None or _SETTINGS.SEARCH_NO_ACCOUNT_KEYS or get_settings().SEARCH_NO_ACCOUNT_KEYS


def redaction_secrets() -> tuple[str, ...]:
    # 在途旧配置的凭据也要遮盖。HTTP Token 随监听进程启动固定。
    current, live = get_settings(), _SETTINGS
    if current is live:
        return live.secrets
    return tuple(sorted(set(current.secrets) | set(live.secrets), key=len, reverse=True))


def reload_if_changed() -> bool:
    """在工具调用前刷新文件。目录、日志路径和入站 HTTP 鉴权保持启动值。"""
    global _ENV_SIGNATURE, _KEYS_SIGNATURE, _DOTENV, _JSON_KEYS, _SETTINGS
    with _SETTINGS_LOCK:
        (env_file, _), (keys_file, _) = active_files()
        try:
            unchanged = _file_signature(env_file) == _ENV_SIGNATURE and _file_signature(keys_file) == _KEYS_SIGNATURE
        except OSError:
            unchanged = False
        if unchanged:
            return False
        WARNINGS.clear()
        env_signature, dotenv = _read_source(env_file, _load_dotenv, _ENV_SIGNATURE, _DOTENV)
        keys_signature, keys = _read_source(keys_file, _load_keys_json, _KEYS_SIGNATURE, _JSON_KEYS)
        if env_signature == _ENV_SIGNATURE and keys_signature == _KEYS_SIGNATURE:
            return False
        settings = _build_settings(dotenv, keys, _SETTINGS.revision + 1, _SETTINGS)
        check_provider_names(settings, keys)
        _ENV_SIGNATURE, _KEYS_SIGNATURE, _DOTENV, _JSON_KEYS, _SETTINGS = env_signature, keys_signature, dotenv, keys, settings
        log("配置文件已重新加载" + ("（有配置被跳过，见上方告警）" if WARNINGS else "（运行配置即时生效）"))
        return True


@contextmanager
def use_settings(**overrides):
    """固定当前请求的配置；自测可按调用覆盖单个字段。"""
    settings = _REQUEST_SETTINGS.get()
    if settings is None:
        reload_if_changed()
        settings = _SETTINGS
    if overrides:
        settings = replace(settings, **overrides)
    token = _REQUEST_SETTINGS.set(settings)
    try:
        yield settings
    finally:
        _REQUEST_SETTINGS.reset(token)


def with_runtime_settings(fn):
    """保持已有 MCP 工具的参数签名，请求内（含子任务）沿用同一快照。"""
    @wraps(fn)
    async def call(*args, **kwargs):
        with use_settings():
            return await fn(*args, **kwargs)
    return call


(_env_file, _), (_keys_file, _) = active_files()
_ENV_SIGNATURE, _DOTENV = _read_source(_env_file, _load_dotenv, None, {})
_KEYS_SIGNATURE, _JSON_KEYS = _read_source(_keys_file, _load_keys_json, None, {})
_initial_env = _effective_env(_DOTENV)
# 目录和入站 HTTP 鉴权属于启动配置，不能在活跃的监听器上偷偷替换。
MCP_HTTP_TOKEN = _env_text("MCP_HTTP_TOKEN", env=_initial_env)
USAGE_LOG_FILE = _state_path(_initial_env, "USAGE_LOG_FILE", "usage.jsonl")
KEY_STATE_FILE = _state_path(_initial_env, "KEY_STATE_FILE", "key-state.json")   # 仅用于旧版迁移
USAGE_COUNTER_FILE = _state_path(_initial_env, "USAGE_COUNTER_FILE", "usage-counters.json")
_SETTINGS = _build_settings(_DOTENV, _JSON_KEYS, 0)
check_provider_names()
del _initial_env, _env_file, _keys_file
