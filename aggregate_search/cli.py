"""命令行入口。

    aggregate-search                启动 MCP 服务（stdio）—— MCP 客户端里配的就是这条命令
    aggregate-search init           生成全局配置目录和 .env 模板
    aggregate-search path           显示配置目录位置
    aggregate-search status         查看各家 key、冷却与用量
    aggregate-search reset-cooldown 厂商 [--apply]  预览或清除该厂商的本地账号冷却
    aggregate-search search 关键词   直接搜一次（--mode / --region / --providers / --time-range / --domains）
    aggregate-search fetch URL      抓取网页正文
    aggregate-search check          发出真实请求，检查启用的搜索通道
    aggregate-search usage [天数]    查看本地使用记录汇总
    aggregate-search selftest       离线自测（不联网、不读取也不改动你的配置）
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__


def _prepare_streams() -> None:
    """把输出流整理好，必须在导入其它模块之前做（配置告警在导入时就会打印）。

    - 有些宿主不给子进程 stderr（sys.stderr 为 None）。这时 print(file=None) 会改写到 stdout，
      而 stdout 是 MCP 的协议通道——所以先接一个黑洞上去。
    - Windows 下管道 / 重定向默认 GBK，中文会乱码或直接报编码错误，统一成 UTF-8。
    """
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _template(name: str) -> str:
    from importlib import resources

    return resources.files("aggregate_search").joinpath("templates", name).read_text(encoding="utf-8")


def _write_private(path: Path, data: bytes) -> None:
    """新建一个只有自己能读写的文件（里面是密钥）。已存在则报错，绝不覆盖。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def cmd_init(source: str | None) -> int:
    """建好配置目录；没有 .env 就生成一份（有旧文件则原样迁过来）。已有的不动。"""
    from . import config

    home = config.CONFIG_DIR
    config.ensure_home()
    env_file = config.ENV_FILE
    print(f"配置目录: {home}")

    if source:
        origin = Path(source).expanduser()
        if not origin.is_file():
            print(f"[失败] 找不到要导入的文件: {origin}")
            return 1
    else:
        origin = config.legacy_file(".env")

    if env_file.exists():
        print(f"[保留] 已有配置文件，未改动: {env_file}")
        if origin and origin.resolve() != env_file.resolve():
            print(f"       （没有覆盖它；如确实要改用 {origin}，请先自行备份并删除上面的文件）")
    elif origin:
        _write_private(env_file, origin.read_bytes())
        print(f"[迁移] 已把 {origin} 复制为 {env_file}")
        print("       旧文件仍在原处，确认新位置可用后可以自行删除。")
    else:
        _write_private(env_file, _template("env.example").encode("utf-8"))
        print(f"[新建] 已生成配置模板: {env_file}")

    legacy_keys = config.legacy_file("keys.json")
    if legacy_keys and not config.KEYS_FILE.exists() and not source:
        _write_private(config.KEYS_FILE, legacy_keys.read_bytes())
        print(f"[迁移] 已把 {legacy_keys} 复制为 {config.KEYS_FILE}")
    sample = home / "keys.example.json"
    if not sample.exists():
        sample.write_text(_template("keys.example.json"), encoding="utf-8")

    print()
    print("接下来：")
    print(f"  1. 用记事本等编辑器打开 {env_file}，把要用的厂商那一行去掉开头的 # 并填入 key")
    print("     （一个 key 都不填也能用：Parallel / Keenable / AnySearch 匿名免费）")
    print("  2. 运行 aggregate-search status 确认哪些厂商已启用")
    print("  3. 保存后下一次工具调用即使用新的运行配置；监听和目录等启动配置需重启")
    return 0


def cmd_path() -> int:
    from . import config

    print(config.CONFIG_DIR)
    return 0


def cmd_status(days: int) -> int:
    from .server import status_text

    print(status_text(days))
    return 0


def cmd_reset_cooldown(provider: str, *, apply: bool) -> int:
    import sqlite3

    from . import config, store
    from .providers import REGISTRY

    provider = provider.lower()
    # API_KEYS 的键是支持账号的厂商，空池也保留；不能只检查现有池中的指纹。
    if provider not in REGISTRY or provider not in config.get_settings().API_KEYS:
        print("请指定一个支持账号 key 的已知厂商名，不接受 all、通配符或明文 key。", file=sys.stderr)
        return 2
    try:
        count = store.reset_provider_cooldown(provider, apply=apply)
    except store.LegacyCooldownPending as exc:
        print(f"[未清除] {exc}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error) as exc:
        detail = getattr(exc, "sqlite_errorname", type(exc).__name__)
        print(f"[失败] 无法访问状态库或完成事务（{detail}）；请检查占锁、权限或数据库完整性。", file=sys.stderr)
        return 1
    if apply:
        print(f"{provider}：已清除 {count} 条本地账号 key 有效冷却。")
    else:
        print(f"[预览] {provider}：{count} 条本地账号 key 有效冷却。未更改；加 --apply 才清除。")
    return 0


def cmd_usage(days: int) -> int:
    from . import usage

    print(usage.summarize(days))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    from .server import web_search

    providers = [p for p in (args.providers or "").replace("，", ",").split(",") if p.strip()]
    domains = [d for d in (args.domains or "").replace("，", ",").split(",") if d.strip()]
    text = asyncio.run(_run_once(web_search(
        " ".join(args.query),
        max_results=args.max,
        providers=providers or None,
        total_results=args.total,
        search_mode=args.mode,
        region=args.region,
        time_range=args.time_range,
        domains=domains or None,
    )))
    print(text)
    return 0


def cmd_fetch(url: str, backend: str) -> int:
    from .server import web_fetch

    print(asyncio.run(_run_once(web_fetch(url, backend))))
    return 0


async def _check(query: str) -> int:
    from . import config

    with config.use_settings():
        return await _check_channels(query)


async def _check_channels(query: str) -> int:
    from . import config
    from .providers import (
        REGISTRY,
        can_anonymous,
        enabled_search_providers,
        has_keys,
        pool_status,
        run_search,
    )

    enabled = enabled_search_providers()
    free = config.force_anonymous()
    anonymous = [n for n in config.get_settings().FREE_PROVIDERS if n in REGISTRY and can_anonymous(n)]
    anonymous += [n for n in enabled if n not in anonymous and can_anonymous(n) and (free or not has_keys(n))]
    keyed = [] if free else [n for n in enabled if has_keys(n)]
    print(f"测试查询: {query}")
    if free:
        print("服务端已禁止发送账号 key，本次仅检查匿名通道。\n")
    else:
        print("会发出真实请求；账号通道可能轮换多个 key 并消耗额度。\n")

    failed = 0
    for title, names, force_anonymous in (
        ("匿名通道（不带 key）", anonymous, True),
        ("账号 key 通道", keyed, False),
    ):
        print(f"== {title} ==")
        if not names:
            print("（没有可测的厂商）\n")
            continue
        _, _, records = await run_search(names, query, 3, True if force_anonymous else ())
        for record in records:
            seconds = f"{record.ms / 1000:.1f}s"
            if record.ok:
                print(f"[OK]   {record.label}: {record.count} 条，{seconds}")
                for item in record.results[:1]:
                    print(f"         {item.title[:50] or '(无标题)'}  {item.url}")
            elif not record.sent:
                print(f"[跳过] {record.label}: 没有发出请求（{record.error}）")
            else:
                failed += 1
                print(f"[失败] {record.label}: {record.error}（{seconds}）")
        print()
    statuses = pool_status()
    if statuses:
        print("== key 状态 ==")
        for name, line in statuses.items():
            print(f"{name}: {line}")
    return 1 if failed else 0


async def _run_once(call):
    from .providers import close_clients

    try:
        return await call
    finally:
        await close_clients()


def cmd_check(query: str) -> int:
    return asyncio.run(_run_once(_check(query)))


# 自测子进程只继承这些与运行环境有关的变量；其余一律不带，
# 这样你机器上配的 key、代理和各种开关都影响不到自测结果。
_PASSTHROUGH_ENV = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "VIRTUAL_ENV",
)


def cmd_selftest(child: bool) -> int:
    if child:
        from .selftest import run

        return run()
    from .selftest import SANDBOX_ENV

    with tempfile.TemporaryDirectory(prefix="aggregate-search-selftest-") as sandbox:
        env = {name: os.environ[name] for name in _PASSTHROUGH_ENV if name in os.environ}
        env.update(SANDBOX_ENV)
        env["AGGREGATE_SEARCH_HOME"] = sandbox
        env["PYTHONIOENCODING"] = "utf-8"
        # 固定 cwd 和导入根，避免 SDK 读取当前目录的 .env 或从客户端 PYTHONPATH 载入模块。
        package_parent = str(Path(__file__).resolve().parent.parent)
        entry = "import sys; sys.path.insert(0, sys.argv.pop(1)); from aggregate_search.cli import main; main()"
        print("离线自测：在临时沙盒里运行，不联网，不读取也不改动你的配置。\n")
        sys.stdout.flush()
        done = subprocess.run(
            [sys.executable, "-I", "-B", "-c", entry, package_parent, "selftest", "--child"],
            cwd=sandbox, env=env,
        )
    return done.returncode


class _CooldownParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse 默认会复述多余参数；有人把密钥错当参数时不能写进终端日志。
        super().error("请指定一个厂商名；可选参数仅 --apply（查看帮助用 --help）。")


def build_parser(*, private_errors: bool = False) -> argparse.ArgumentParser:
    parser_type = _CooldownParser if private_errors else argparse.ArgumentParser
    parser = parser_type(
        prog="aggregate-search",
        description="聚合联网搜索 MCP。不带任何参数运行即启动 MCP 服务（stdio），供 MCP 客户端调用。",
    )
    parser.add_argument("--version", action="version", version=f"aggregate-search {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="命令")

    p = sub.add_parser("serve", help="启动 MCP 服务（不带参数时的默认行为）")
    p.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio",
                   help="传输方式，默认 stdio（本地子进程）；streamable-http 用于局域网/远程访问")
    p.add_argument("--host", default="127.0.0.1",
                   help="HTTP 监听地址，默认 127.0.0.1（只本机）。绑定其它地址必须配 MCP_HTTP_TOKEN")
    p.add_argument("--port", type=int, default=8000, help="HTTP 端口，默认 8000")

    p = sub.add_parser("init", help="生成全局配置目录和 .env 模板（已有配置不会被覆盖）")
    p.add_argument("--from", dest="source", metavar=".env路径", help="从已有的 .env 文件导入")

    sub.add_parser("path", help="显示配置目录位置")

    p = sub.add_parser("status", help="查看各家 key、冷却、用量和抓取顺序")
    p.add_argument("--usage", dest="days", type=int, default=0, metavar="天数", help="附带近 N 天使用记录")

    p = sub.add_parser("reset-cooldown", help="预览指定厂商的账号 key 冷却（--apply 才清除）", allow_abbrev=False)
    p.add_argument("provider", metavar="厂商", help="一个支持账号 key 的厂商名，例如 tavily；不接受明文 key")
    p.add_argument("--apply", action="store_true", help="清除当前有效的本地账号冷却，不改变日/月计数或匿名冷却")

    p = sub.add_parser("search", help="直接搜索一次")
    p.add_argument("query", nargs="+", help="搜索关键词")
    p.add_argument("--mode", choices=("auto", "free", "escalate", "all"), help="搜索模式，默认 auto")
    p.add_argument("--region", help="补搜偏好: domestic / global / mixed")
    p.add_argument("--providers", help="只查指定厂商，逗号分隔，如 tavily,exa")
    p.add_argument("--max", type=int, help="每家取几条")
    p.add_argument("--total", type=int, help="最终返回多少个页面")
    p.add_argument("--time-range", dest="time_range",
                   help="只要最近一段时间的结果：day / week / month / year（也认 24h、7d、本月）")
    p.add_argument("--domains", help="只在这些站点里搜，逗号分隔，如 docs.python.org,github.com")

    p = sub.add_parser("fetch", help="抓取网页正文")
    p.add_argument("url")
    p.add_argument("backend", nargs="?", default="", help="可选，指定抓取后端")

    p = sub.add_parser("check", help="让每家各发一次真实请求，检查连通性")
    p.add_argument("query", nargs="*", help="测试用的关键词")

    p = sub.add_parser("usage", help="查看本地使用记录汇总")
    p.add_argument("days", nargs="?", type=int, default=7, help="统计最近几天，默认 7")

    p = sub.add_parser("selftest", help="离线自测（不联网、不动你的配置）")
    p.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    _prepare_streams()
    if not argv or argv[0] == "serve":
        # MCP 客户端走这里。stdio 时 stdout 是协议通道，这条路径上不能往 stdout 打印任何东西。
        transport, host, port = "stdio", "127.0.0.1", 8000
        if argv:
            parsed = build_parser().parse_args(argv)
            transport = getattr(parsed, "transport", "stdio") or "stdio"
            host = getattr(parsed, "host", "127.0.0.1") or "127.0.0.1"
            port = getattr(parsed, "port", 8000) or 8000
        from .server import main as serve

        serve(transport=transport, host=host, port=port)
        return

    args = build_parser(private_errors=argv[0] == "reset-cooldown").parse_args(argv)
    command = args.command
    if command == "init":
        code = cmd_init(args.source)
    elif command == "path":
        code = cmd_path()
    elif command == "status":
        code = cmd_status(args.days)
    elif command == "reset-cooldown":
        code = cmd_reset_cooldown(args.provider, apply=args.apply)
    elif command == "usage":
        code = cmd_usage(args.days)
    elif command == "search":
        code = cmd_search(args)
    elif command == "fetch":
        code = cmd_fetch(args.url, args.backend)
    elif command == "check":
        code = cmd_check(" ".join(args.query) or "Python 3.13 新特性")
    elif command == "selftest":
        code = cmd_selftest(args.child)
    else:
        build_parser().print_help()
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    main()
