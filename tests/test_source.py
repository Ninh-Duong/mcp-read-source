from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import runpy
from unittest.mock import patch


class SourceReaderTest(unittest.TestCase):
    def test_python_module_entry_point_starts_the_server(self) -> None:
        def capture_run(coro) -> None:
            coro.close()

        module = Path(__file__).resolve().parents[1] / "src" / "mcp_read_source" / "server.py"
        with patch("asyncio.run", side_effect=capture_run) as run:
            runpy.run_path(str(module), run_name="__main__")
        run.assert_called_once()

    def test_empty_project_example_is_safe_to_copy_before_user_adds_roots(self) -> None:
        import shutil
        from mcp_read_source.server import ROOT, load_projects

        temporary = Path(tempfile.mkdtemp())
        try:
            private = temporary / "config" / "private"
            private.mkdir(parents=True)
            path = private / "projects.json"
            shutil.copy2(ROOT / "config" / "projects.example.json", path)
            with patch("mcp_read_source.server.ROOT", temporary):
                self.assertEqual(load_projects(path), {})
        finally:
            import shutil
            shutil.rmtree(temporary)

    def setUp(self) -> None:
        from mcp_read_source.server import SourceReader

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.reader = SourceReader({"sample": self.root}, context_dir=Path(self.temp.name) / "ai-context")

    def tearDown(self) -> None:
        self.reader.store.close()
        self.temp.cleanup()

    def test_search_and_read_are_bounded_and_support_untracked_files(self) -> None:
        source = self.root / "src" / "worker.py"
        source.parent.mkdir()
        source.write_text("first\nretry after timeout\nlast\n", encoding="utf-8")

        result = self.reader.search("sample", "retry", glob="*.py")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["matches"][0]["path"], "src/worker.py")
        self.assertEqual(result["matches"][0]["line"], 2)
        excerpt = self.reader.read("sample", "src/worker.py", start_line=2, max_lines=1)
        self.assertEqual(excerpt["content"], "retry after timeout")
        self.assertEqual(excerpt["start_line"], 2)

    def test_skips_private_binary_large_and_build_files(self) -> None:
        (self.root / ".git").mkdir()
        (self.root / ".git" / "secret.py").write_text("needle", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (self.root / "node_modules" / "hidden.js").write_text("needle", encoding="utf-8")
        (self.root / "config" / "private").mkdir(parents=True)
        (self.root / "config" / "private" / "settings.json").write_text("needle", encoding="utf-8")
        (self.root / ".env").write_text("needle", encoding="utf-8")
        (self.root / "credentials.json").write_text("needle", encoding="utf-8")
        (self.root / ".npmrc").write_text("needle", encoding="utf-8")
        (self.root / "binary.dat").write_bytes(b"needle\x00bytes")
        (self.root / "large.txt").write_bytes(b"x" * (1024 * 1024 + 1))
        (self.root / "safe.py").write_text("needle", encoding="utf-8")

        result = self.reader.search("sample", "needle")
        self.assertEqual([item["path"] for item in result["matches"]], ["safe.py"])

    def test_rejects_traversal_symlink_escape_and_reads_over_200_lines(self) -> None:
        from mcp_read_source.server import SourceReadError

        outside = Path(self.temp.name) / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (self.root / "inside.txt").write_text("inside", encoding="utf-8")
        with self.assertRaises(SourceReadError):
            self.reader.read("sample", "../outside.txt")
        try:
            (self.root / "escape.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")
        with self.assertRaises(SourceReadError):
            self.reader.read("sample", "escape.txt")
        with self.assertRaises(SourceReadError):
            self.reader.read("sample", "inside.txt", max_lines=201)

    def test_search_caps_candidate_files_and_rejects_unknown_project_alias(self) -> None:
        from mcp_read_source.server import SourceReadError

        for index in range(501):
            content = "needle" if index == 500 else "other"
            (self.root / f"{index:03}.txt").write_text(content, encoding="utf-8")
        with patch("mcp_read_source.server.MAX_SEARCH_FILES", 500):
            result = self.reader.search("sample", "needle", mode="text")
        self.assertEqual(result["total"], 0)
        self.assertTrue(result["truncated"])
        self.assertEqual(self.reader.search("sample", "needle", mode="text")["total"], 1)
        with self.assertRaises(SourceReadError):
            self.reader.search("local-path", "needle")

    def test_search_and_read_skip_symlink_aliases_to_hidden_targets(self) -> None:
        from mcp_read_source.server import SourceReadError

        root = self.reader.projects["sample"]
        hidden = root / ".env"
        hidden.write_text("private value", encoding="utf-8")
        alias = root / "safe.py"
        resolve = Path.resolve
        is_file = Path.is_file
        stat = Path.stat

        def resolve_candidate(path, *args, **kwargs):
            return hidden if path == alias else resolve(path, *args, **kwargs)

        def is_candidate_file(path, *args, **kwargs):
            return is_file(hidden, *args, **kwargs) if path == alias else is_file(path, *args, **kwargs)

        def stat_candidate(path, *args, **kwargs):
            return stat(hidden, *args, **kwargs) if path == alias else stat(path, *args, **kwargs)

        is_symlink = Path.is_symlink

        def is_candidate_symlink(path):
            return True if path == alias else is_symlink(path)

        with (
            patch("mcp_read_source.server.os.walk", return_value=[(str(root), [], ["safe.py"])]),
            patch.object(Path, "resolve", resolve_candidate),
            patch.object(Path, "is_file", is_candidate_file),
            patch.object(Path, "stat", stat_candidate),
            patch.object(Path, "is_symlink", is_candidate_symlink),
        ):
            self.assertEqual(self.reader._candidate_files(root)[0], [])
            with self.assertRaises(SourceReadError):
                self.reader.read("sample", "safe.py")


if __name__ == "__main__":
    unittest.main()
