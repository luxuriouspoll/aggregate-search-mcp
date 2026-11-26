"""Check this project's wheel and sdist without extracting or importing either one."""

import argparse
import ast
import configparser
from email.parser import BytesHeaderParser
from fnmatch import fnmatchcase
import gzip
from pathlib import Path
import stat
import tarfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PROJECT = "aggregate-search-mcp"
DIST = PROJECT.replace("-", "_")
PACKAGE = "aggregate_search"
INIT = f"{PACKAGE}/__init__.py"
TEMPLATES = {f"{PACKAGE}/templates/{name}" for name in ("env.example", "keys.example.json")}
SDIST_FILES = {"README.md", "CHANGELOG.md", "docs/usage-guide.md", "LICENSE", "pyproject.toml"}
COMMANDS = ("aggregate-search", "aggregate-search-mcp")
# Match path components, not substrings: env.example and keys.example.json are templates.
PRIVATE = (
    ".env", ".env.*", ".env~", "keys.json", "keys.json.*", "keys.json~",
    "*.db", "*.db-*", "*.sqlite", "*.sqlite-*", "*.sqlite3", "*.sqlite3-*",
    "usage.jsonl*", "*.log", "*.log.*", "key-state.json*", ".key-state.json*",
    "usage-counters.json*", "*.migrated", "*.migrated.*",
)


class DistError(ValueError):
    pass


def source_version(data):
    for node in ast.parse(data).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets
        ) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise DistError(f"{INIT}: missing literal __version__")


def one_archive(directory, pattern):
    paths = list(directory.glob(pattern))
    if len(paths) != 1 or not paths[0].is_file():
        raise DistError(f"{directory}: expected one {pattern} file, found {len(paths)} matches")
    return paths[0]


def check_member(name):
    parts = name.rstrip("/").split("/")
    if "\\" in name or any(p in ("", ".", "..") for p in parts):
        raise DistError(f"invalid archive path: {name!r}")
    if any(fnmatchcase(p.casefold(), pattern) for p in parts for pattern in PRIVATE):
        raise DistError(f"private file in archive: {name!r}")


def check_contents(files, read, version, metadata, entries, required):
    missing = required.union({metadata, entries}) - files
    if missing:
        raise DistError(f"missing files: {', '.join(sorted(missing))}")
    if source_version(read(INIT)) != version:
        raise DistError(f"{INIT}: version differs from checkout ({version})")
    headers = BytesHeaderParser().parsebytes(read(metadata))
    if headers.get_all("Name") != [PROJECT] or headers.get_all("Version") != [version]:
        raise DistError(f"{metadata}: expected Name: {PROJECT} and Version: {version}")
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str  # console command names are case-sensitive
    parser.read_string(read(entries).decode("utf-8"))
    for cmd in COMMANDS:
        if parser.get("console_scripts", cmd, fallback=None) != f"{PACKAGE}.cli:main":
            raise DistError(f"{entries}: missing or incorrect console script {cmd}")


def check_dist(directory):
    version = source_version((ROOT / INIT).read_bytes())
    stem = f"{DIST}-{version}"
    wheel = one_archive(directory, "*.whl")
    sdist = one_archive(directory, "*.tar.gz")
    if wheel.name != f"{stem}-py3-none-any.whl" or sdist.name != f"{stem}.tar.gz":
        raise DistError(f"expected {stem}-py3-none-any.whl and {stem}.tar.gz")
    # Include every shipped Python module, not just cli.py: an entry point with
    # missing providers or other runtime modules still makes an unusable wheel.
    common = TEMPLATES | {p.relative_to(ROOT).as_posix() for p in (ROOT / PACKAGE).rglob("*.py")}

    # tarfile stops at the tar end marker, before gzip necessarily checks its trailer.
    with gzip.open(sdist, "rb") as compressed, tarfile.open(fileobj=compressed, mode="r:") as archive:
        files = {}
        for member in archive:
            check_member(member.name)
            root, _, rel = member.name.partition("/")
            if root != stem:
                raise DistError(f"{sdist.name}: unexpected source root {root!r}")
            if member.isdir():
                continue
            if not member.isfile() or not rel or rel in files:
                raise DistError(f"{sdist.name}: invalid or duplicate member {member.name!r}")
            files[rel] = member
        while compressed.read(1024 * 1024):
            pass  # validate CRC and length without buffering the whole sdist

        def read(name):
            with archive.extractfile(files[name]) as stream:
                return stream.read()

        check_contents(files.keys(), read, version, "PKG-INFO", f"{DIST}.egg-info/entry_points.txt",
                       common | SDIST_FILES)

    with zipfile.ZipFile(wheel) as archive:
        files = set()
        for info in archive.infolist():
            check_member(info.filename)
            if not info.is_dir():
                if stat.S_IFMT(info.external_attr >> 16) not in (0, stat.S_IFREG):
                    raise DistError(f"{wheel.name}: not a regular file: {info.filename!r}")
                if info.filename in files:
                    raise DistError(f"{wheel.name}: duplicate member {info.filename!r}")
                files.add(info.filename)
        info_dir = f"{stem}.dist-info"
        check_contents(files, archive.read, version, f"{info_dir}/METADATA", f"{info_dir}/entry_points.txt",
                       common | {f"{info_dir}/WHEEL", f"{info_dir}/RECORD"})
        bad = archive.testzip()
        if bad is not None:
            raise DistError(f"{wheel.name}: bad CRC for {bad!r}")
    return sdist, wheel


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("directory", type=Path, help="directory containing one .tar.gz and one .whl")
    try:
        for path in check_dist(cli.parse_args().directory):
            print(f"OK: {path.name}")
    except (ValueError, SyntaxError, OSError, EOFError, configparser.Error, tarfile.TarError, zipfile.BadZipFile) as exc:
        cli.exit(1, f"dist check failed: {exc}\n")
