from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from test_index import FIRST_COMMIT, write_repo


@unittest.skipUnless(sys.platform == "win32", "ai-context encryption uses Windows DPAPI")
class EncryptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp())
        self.root = self.temp / "repo"
        self.context_dir = self.temp / "ai-context"
        write_repo(self.root)
        self.readers = []

    def tearDown(self) -> None:
        for reader in self.readers:
            reader.store.close()
        shutil.rmtree(self.temp)

    def reader(self):
        """A fresh SourceReader behaves like a separate MCP process sharing the same ai-context."""
        from mcp_read_source.server import SourceReader

        reader = SourceReader({"shop": self.root}, context_dir=self.context_dir)
        self.readers.append(reader)
        return reader

    def database(self) -> Path:
        return self.context_dir / "shop-feature_x.sqlite.enc"

    def commit(self, sha: str) -> None:
        (self.root / ".git" / "refs" / "heads" / "feature" / "x").write_text(sha + "\n", encoding="utf-8")

    def test_disk_holds_only_ciphertext_and_a_dpapi_wrapped_key(self) -> None:
        from mcp_read_source import crypto

        reader = self.reader()
        reader.trace("shop", "AcbController.Get")
        names = {path.name for path in self.context_dir.iterdir()}
        self.assertEqual(names, {".key", ".key.lock", "shop-feature_x.sqlite.enc", "shop-feature_x.lock"})
        for path in self.context_dir.iterdir():
            data = path.read_bytes()
            for secret in (b"AcbService", b"AcbController", b"src/Services", b"SQLite format 3",
                           FIRST_COMMIT.encode(), b"feature/x", "AcbService".encode("utf-16-le")):
                self.assertNotIn(secret, data, f"{secret!r} leaked into {path.name}")

        key_file = (self.context_dir / ".key").read_bytes()
        key = crypto.dpapi_unprotect(key_file[len(crypto.KEY_MAGIC):])
        self.assertEqual(len(key), 32)
        self.assertNotIn(key, key_file)
        stored, plaintext = crypto.open_sealed(key, self.database().read_bytes())
        self.assertEqual(stored, crypto.label_hash("shop", "feature/x"))
        self.assertTrue(plaintext.startswith(b"SQLite format 3"))

    def test_tampered_database_is_rebuilt_not_trusted(self) -> None:
        self.reader().index("shop")
        data = bytearray(self.database().read_bytes())
        data[len(data) // 2] ^= 0xFF
        self.database().write_bytes(bytes(data))

        result = self.reader().index("shop")
        self.assertEqual((result["ai_context"], result["parsed"]), ("rescanned", 8))
        self.assertIn("CoreProtoService.GetItem", self.reader().trace("shop", "AcbController.Get"))

    def test_lost_or_foreign_key_rebuilds_everything(self) -> None:
        self.reader().index("shop")
        (self.context_dir / ".key").write_bytes(b"MRSKEY1\x00not-a-dpapi-blob")

        result = self.reader().index("shop")
        self.assertEqual((result["ai_context"], result["parsed"]), ("rescanned", 8))
        (self.context_dir / ".key").unlink()
        self.assertEqual(self.reader().index("shop")["parsed"], 8)

    def test_database_copied_to_another_branch_name_is_ignored(self) -> None:
        self.reader().index("shop")
        shutil.copy(self.database(), self.context_dir / "shop-main.sqlite.enc")
        git = self.root / ".git"
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "refs" / "heads" / "main").write_text(FIRST_COMMIT + "\n", encoding="utf-8")

        result = self.reader().index("shop")
        self.assertEqual(result["branch"], "main")
        hashed = [p.name for p in self.context_dir.glob("shop-main-*.sqlite.enc")]
        self.assertEqual(len(hashed), 1, "a foreign file under the branch name must not be reused")

    def test_plaintext_left_by_older_versions_is_removed(self) -> None:
        self.context_dir.mkdir()
        for name in ("shop-feature_x.sqlite", "shop-feature_x.sqlite-wal", "shop-feature_x.sqlite-shm"):
            (self.context_dir / name).write_bytes(b"SQLite format 3\x00 AcbService")
        stale = self.context_dir / "shop-feature_x.sqlite.enc.123.tmp"
        stale.write_bytes(b"old")
        os.utime(stale, (1, 1))

        self.reader().index("shop")
        self.assertEqual(sorted(p.name for p in self.context_dir.glob("*.sqlite*")), ["shop-feature_x.sqlite.enc"])
        self.assertFalse(stale.exists())

    def test_other_process_reloads_after_a_writer_saves(self) -> None:
        writer, other = self.reader(), self.reader()
        writer.index("shop")
        self.assertEqual(other.index("shop")["ai_context"], "cached (no scan)")

        service = self.root / "src/Services/AcbService.cs"
        service.write_text("\n\n" + service.read_text(encoding="utf-8"), encoding="utf-8")
        self.commit("b" * 40)
        self.assertEqual(writer.index("shop")["parsed"], 1)

        result = other.index("shop")
        self.assertEqual(result["ai_context"], "cached (no scan)")
        self.assertEqual(other.read_symbol("shop", "AcbService.GetAsync")["start_line"], 8)

    def test_unchanged_rescan_does_not_rewrite_the_file(self) -> None:
        reader = self.reader()
        reader.index("shop")
        before = self.database().stat().st_mtime_ns
        self.assertEqual(reader.index("shop", force=True)["ai_context"], "rescanned")
        self.assertEqual(self.database().stat().st_mtime_ns, before)


if __name__ == "__main__":
    unittest.main()
