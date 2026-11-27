"""Offline regression tests: only archive small fixtures, never local user files."""

import gzip
import io
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest
import zipfile

from check_dist import DistError, check_dist, source_version


ROOT = Path(__file__).resolve().parents[1]
INIT = "aggregate_search/__init__.py"
ENTRY_POINTS = (b"[console_scripts]\naggregate-search = aggregate_search.cli:main\n"
                b"aggregate-search-mcp = aggregate_search.cli:main\n")


class CheckDistTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        version = source_version((ROOT / INIT).read_bytes())
        self.stem = f"aggregate_search_mcp-{version}"
        self.info = f"{self.stem}.dist-info"
        self.egg = "aggregate_search_mcp.egg-info"
        self.sdist = self.directory / f"{self.stem}.tar.gz"
        self.wheel = self.directory / f"{self.stem}-py3-none-any.whl"
        metadata = f"Metadata-Version: 2.4\nName: aggregate-search-mcp\nVersion: {version}\n".encode()
        common = {p.relative_to(ROOT).as_posix(): b"# fixture\n"
                  for p in (ROOT / "aggregate_search").rglob("*.py")}
        common.update({INIT: f"__version__ = '{version}'\n".encode(),
                       "aggregate_search/templates/env.example": b"# empty template\n",
                       "aggregate_search/templates/keys.example.json": b"{}\n"})
        self.source = {**common, "PKG-INFO": metadata, f"{self.egg}/entry_points.txt": ENTRY_POINTS,
                       "LICENSE": b"MIT", "pyproject.toml": b"[project]\n", "README.md": b"readme", "README_EN.md": b"english",
                       "CHANGELOG.md": b"changes", "docs/usage-guide.md": b"guide"}
        self.binary = {**common, f"{self.info}/METADATA": metadata, f"{self.info}/entry_points.txt": ENTRY_POINTS,
                       f"{self.info}/RECORD": b"", f"{self.info}/WHEEL": b"Wheel-Version: 1.0\n"}
        self.write_source(self.source)
        self.write_wheel(self.binary)

    def write_source(self, files):
        with tarfile.open(self.sdist, "w:gz") as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(f"{self.stem}/{name}")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def write_wheel(self, files):
        with zipfile.ZipFile(self.wheel, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in files.items():
                archive.writestr(name, data)

    def test_clean_pair(self):
        self.assertEqual(check_dist(self.directory), (self.sdist, self.wheel))

    def test_cli_exit_codes(self):
        cmd = [sys.executable, "-I", str(ROOT / "scripts/check_dist.py"), str(self.directory)]
        good = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn(self.wheel.name, good.stdout)
        self.assertIn(self.sdist.name, good.stdout)
        self.write_source({**self.source, ".env.local": b""})
        bad = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("private file", bad.stderr)

    def test_truncated_sdist(self):
        self.sdist.write_bytes(self.sdist.read_bytes()[:-8])  # remove the gzip trailer
        with self.assertRaises(EOFError):
            check_dist(self.directory)

    def test_missing_duplicate_or_unreadable_archive(self):
        for path in (self.sdist, self.wheel):
            with self.subTest(path=path.name):
                data = path.read_bytes()
                path.unlink()
                with self.assertRaisesRegex(DistError, "expected one"):
                    check_dist(self.directory)
                path.write_bytes(data)
                extra = path.with_name("stale-" + path.name)
                extra.write_bytes(data)
                with self.assertRaisesRegex(DistError, "expected one"):
                    check_dist(self.directory)
                extra.unlink()
                path.write_bytes(b"not an archive")
                with self.assertRaises((gzip.BadGzipFile, tarfile.ReadError, zipfile.BadZipFile)):
                    check_dist(self.directory)
                path.write_bytes(data)

    def test_incomplete_and_misplaced_members(self):
        shared = ("aggregate_search/templates/env.example", "aggregate_search/templates/keys.example.json",
                  "aggregate_search/cli.py", "aggregate_search/providers/__init__.py")
        for files, write, required in (
            (self.source, self.write_source, shared + ("README.md", "CHANGELOG.md", "docs/usage-guide.md", "PKG-INFO")),
            (self.binary, self.write_wheel, shared + (f"{self.info}/RECORD", f"{self.info}/METADATA")),
        ):
            for name in required:
                with self.subTest(archive=write.__name__, name=name):
                    broken = files.copy()
                    broken["elsewhere/" + name] = broken.pop(name)
                    write(broken)
                    with self.assertRaisesRegex(DistError, "missing files"):
                        check_dist(self.directory)
            write(files)

    def test_wrong_version_or_entry_point(self):
        for files, write, metadata, entries in (
            (self.source, self.write_source, "PKG-INFO", f"{self.egg}/entry_points.txt"),
            (self.binary, self.write_wheel, f"{self.info}/METADATA", f"{self.info}/entry_points.txt"),
        ):
            for name, value, message in (
                (INIT, b"__version__ = 'wrong'", "version differs"),
                (metadata, b"Name: aggregate-search-mcp\nVersion: wrong\n", "Version:"),
                (entries, ENTRY_POINTS.replace(b"aggregate-search-mcp =", b"other ="), "console script"),
                (entries, ENTRY_POINTS.replace(b"cli:main", b"cli:other", 1), "console script"),
            ):
                with self.subTest(archive=write.__name__, name=name):
                    write({**files, name: value})
                    with self.assertRaisesRegex(DistError, message):
                        check_dist(self.directory)
            write(files)

    def test_private_names_at_root_and_nested(self):
        private = (".env", ".env.local", ".env.production.local", "keys.json", "state.db", "state.db-shm",
                   "state.db-wal", "state.db-journal", "usage.jsonl", "usage.jsonl.1", "debug.log.2",
                   ".key-state.json", "key-state.json", "usage-counters.json", "key-state.json.migrated",
                   "usage-counters.json.migrated", "custom-state.migrated")
        for files, write in ((self.source, self.write_source), (self.binary, self.write_wheel)):
            for prefix in ("", "nested/local/"):
                for name in private:
                    with self.subTest(archive=write.__name__, member=prefix + name):
                        write({**files, prefix + name: b""})
                        with self.assertRaisesRegex(DistError, "private file"):
                            check_dist(self.directory)
            write(files)
