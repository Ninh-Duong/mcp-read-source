"""The ai-context store: the only module that reads or writes `ai-context/`.

One encrypted SQLite image per `<alias>-<branch>` holds per-file facts. It is decrypted into an
in-memory database and never exists on disk in plaintext. `ContextStore.ensure` is the gate every
tool calls first: when the stored commit matches the checked-out commit it returns immediately
without touching the source tree; otherwise it rescans incrementally and re-encrypts the image.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_read_source import crypto, indexer
from mcp_read_source.gitmeta import HeadInfo, read_head
from mcp_read_source.paths import iter_index_files

try:
    import msvcrt
except ImportError:
    msvcrt = None
    import fcntl


SCHEMA_VERSION = "1"
DB_SUFFIX = ".sqlite.enc"
KEY_FILE = ".key"
PLAINTEXT_PATTERNS = ("*.sqlite", "*.sqlite-wal", "*.sqlite-shm", "*.sqlite-journal")
LOCK_TIMEOUT_SECONDS = 120
REPLACE_TIMEOUT_SECONDS = 10
STALE_TEMP_SECONDS = 600
PARALLEL_THRESHOLD = 300
PARALLEL_BATCH = 150
HANDLER_BASES = ("IRequestHandler", "INotificationHandler", "IStreamRequestHandler")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE, size INTEGER, mtime_ns INTEGER,
    sha1 BLOB, lang TEXT, line_count INTEGER);
CREATE TABLE IF NOT EXISTS usings(
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE, namespace TEXT);
CREATE INDEX IF NOT EXISTS usings_file ON usings(file_id);
CREATE TABLE IF NOT EXISTS types(
    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    namespace TEXT, name TEXT, kind TEXT, start_line INTEGER, end_line INTEGER, route TEXT);
CREATE INDEX IF NOT EXISTS types_name ON types(name);
CREATE INDEX IF NOT EXISTS types_file ON types(file_id);
CREATE TABLE IF NOT EXISTS type_bases(
    type_id INTEGER NOT NULL REFERENCES types(id) ON DELETE CASCADE, base_simple TEXT, base_args TEXT);
CREATE INDEX IF NOT EXISTS type_bases_type ON type_bases(type_id);
CREATE INDEX IF NOT EXISTS type_bases_simple ON type_bases(base_simple);
CREATE TABLE IF NOT EXISTS members(
    type_id INTEGER NOT NULL REFERENCES types(id) ON DELETE CASCADE, name TEXT, type_name TEXT, type_args TEXT);
CREATE INDEX IF NOT EXISTS members_type ON members(type_id, name);
CREATE TABLE IF NOT EXISTS methods(
    id INTEGER PRIMARY KEY, type_id INTEGER NOT NULL REFERENCES types(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL, name TEXT, kind TEXT, params INTEGER, required INTEGER, variadic INTEGER,
    extension INTEGER, override INTEGER, signature TEXT, start_line INTEGER, end_line INTEGER,
    http_method TEXT, route TEXT);
CREATE INDEX IF NOT EXISTS methods_name ON methods(name);
CREATE INDEX IF NOT EXISTS methods_type ON methods(type_id, name);
CREATE INDEX IF NOT EXISTS methods_file ON methods(file_id, start_line);
CREATE TABLE IF NOT EXISTS calls(
    method_id INTEGER NOT NULL REFERENCES methods(id) ON DELETE CASCADE, line INTEGER,
    receiver TEXT, receiver_type TEXT, name TEXT, argc INTEGER, arg0_type TEXT);
CREATE INDEX IF NOT EXISTS calls_method ON calls(method_id);
CREATE TABLE IF NOT EXISTS registrations(
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE, line INTEGER, kind TEXT,
    service_type TEXT, impl_type TEXT, route TEXT);
CREATE INDEX IF NOT EXISTS registrations_file ON registrations(file_id);
CREATE INDEX IF NOT EXISTS registrations_service ON registrations(service_type);
"""
TABLES = ("registrations", "calls", "methods", "members", "type_bases", "types", "usings", "files", "meta")


class ContextError(ValueError):
    """A safe ai-context error; never contains absolute paths."""


@dataclass
class ScanStats:
    scanned: bool = False
    seeded: bool = False
    files: int = 0
    parsed: int = 0
    unchanged_content: int = 0
    removed: int = 0
    elapsed_ms: int = 0


@dataclass
class Context:
    """An open, fresh ai-context for one project branch, plus cached query helpers."""
    connection: sqlite3.Connection
    alias: str
    branch: str
    commit: str | None
    stats: ScanStats
    shared: dict = field(default_factory=dict)
    _cache: dict = field(default_factory=dict)

    def _memo(self, key: tuple, query: str, params: tuple) -> list[sqlite3.Row]:
        if key not in self._cache:
            self._cache[key] = self.connection.execute(query, params).fetchall()
        return self._cache[key]

    def counts(self) -> dict[str, int]:
        row = self.connection.execute(
            "SELECT (SELECT count(*) FROM files), (SELECT count(*) FROM types), "
            "(SELECT count(*) FROM methods), (SELECT count(*) FROM calls)"
        ).fetchone()
        return {"files": row[0], "types": row[1], "methods": row[2], "calls": row[3]}

    def file_record(self, path: str) -> sqlite3.Row | None:
        rows = self._memo(("file", path), "SELECT * FROM files WHERE path=?", (path,))
        return rows[0] if rows else None

    def file_path(self, file_id: int) -> str:
        rows = self._memo(("file_id", file_id), "SELECT path FROM files WHERE id=?", (file_id,))
        return rows[0]["path"] if rows else "?"

    def usings(self, file_id: int) -> set[str]:
        rows = self._memo(("usings", file_id), "SELECT namespace FROM usings WHERE file_id=?", (file_id,))
        return {row["namespace"] for row in rows}

    def type_by_id(self, type_id: int) -> sqlite3.Row:
        return self._memo(("type", type_id), "SELECT * FROM types WHERE id=?", (type_id,))[0]

    def types_named(self, name: str) -> list[sqlite3.Row]:
        return self._memo(("types", name), "SELECT * FROM types WHERE name=?", (name,))

    def bases(self, type_id: int) -> list[sqlite3.Row]:
        return self._memo(("bases", type_id), "SELECT * FROM type_bases WHERE type_id=?", (type_id,))

    def derived(self, base_simple: str) -> list[sqlite3.Row]:
        return self._memo(
            ("derived", base_simple),
            "SELECT t.*, b.base_args FROM type_bases b JOIN types t ON t.id=b.type_id WHERE b.base_simple=?",
            (base_simple,),
        )

    def registered_impls(self, service: str) -> list[str]:
        rows = self._memo(
            ("registered", service),
            "SELECT DISTINCT impl_type FROM registrations WHERE service_type=? AND impl_type IS NOT NULL", (service,),
        )
        return [row["impl_type"] for row in rows]

    def members(self, type_id: int, name: str) -> list[sqlite3.Row]:
        return self._memo(
            ("members", type_id, name), "SELECT * FROM members WHERE type_id=? AND name=?", (type_id, name),
        )

    def methods_of(self, type_id: int, name: str | None = None) -> list[sqlite3.Row]:
        if name is None:
            return self._memo(("methods", type_id), "SELECT * FROM methods WHERE type_id=? ORDER BY start_line", (type_id,))
        return self._memo(
            ("methods", type_id, name), "SELECT * FROM methods WHERE type_id=? AND name=?", (type_id, name),
        )

    def method(self, method_id: int) -> sqlite3.Row:
        return self._memo(("method", method_id), "SELECT * FROM methods WHERE id=?", (method_id,))[0]

    def calls(self, method_id: int) -> list[sqlite3.Row]:
        return self._memo(("calls", method_id), "SELECT * FROM calls WHERE method_id=? ORDER BY line", (method_id,))

    def extension_methods(self, name: str) -> list[sqlite3.Row]:
        return self._memo(("ext", name), "SELECT * FROM methods WHERE name=? AND extension=1", (name,))

    def handlers_for(self, request_type: str) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in HANDLER_BASES)
        rows = self._memo(
            ("handlers", request_type),
            f"SELECT m.* FROM type_bases b JOIN methods m ON m.type_id=b.type_id "
            f"WHERE b.base_simple IN ({marks}) AND m.name='Handle' "
            f"AND (b.base_args=? OR b.base_args LIKE ?)",
            (*HANDLER_BASES, request_type, f"{request_type},%"),
        )
        return rows

    def proto_rpc(self, service: str, rpc: str) -> list[sqlite3.Row]:
        return self._memo(
            ("rpc", service, rpc),
            "SELECT m.* FROM types t JOIN methods m ON m.type_id=t.id "
            "WHERE t.kind='proto_service' AND t.name=? AND m.name=?",
            (service, rpc),
        )

    def methods_in_file(self, file_id: int) -> list[sqlite3.Row]:
        return self._memo(
            ("file_methods", file_id), "SELECT * FROM methods WHERE file_id=? ORDER BY start_line", (file_id,),
        )

    def types_in_file(self, file_id: int) -> list[sqlite3.Row]:
        return self._memo(
            ("file_types", file_id), "SELECT * FROM types WHERE file_id=? ORDER BY start_line", (file_id,),
        )

    def methods_named(self, type_name: str, method_name: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT m.* FROM methods m JOIN types t ON t.id=m.type_id WHERE t.name=? AND m.name=? "
            "ORDER BY m.file_id, m.start_line",
            (type_name, method_name),
        ).fetchall()

    def entry_rows(self) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in HANDLER_BASES)
        return self.connection.execute(
            f"""
            SELECT m.*, t.name AS type_name, t.route AS type_route, t.kind AS type_kind, 'http' AS entry_kind,
                   NULL AS entry_detail
              FROM methods m JOIN types t ON t.id=m.type_id WHERE m.http_method IS NOT NULL
            UNION ALL
            SELECT m.*, t.name, t.route, t.kind, 'grpc', substr(b.base_simple, 1, length(b.base_simple)-4)
              FROM methods m JOIN types t ON t.id=m.type_id JOIN type_bases b ON b.type_id=t.id
             WHERE m.override=1 AND b.base_simple LIKE '%Base'
               AND EXISTS (SELECT 1 FROM types p JOIN methods r ON r.type_id=p.id
                            WHERE p.kind='proto_service' AND p.name=substr(b.base_simple, 1, length(b.base_simple)-4)
                              AND r.name=m.name)
            UNION ALL
            SELECT m.*, t.name, t.route, t.kind, 'mediatr', b.base_args
              FROM methods m JOIN types t ON t.id=m.type_id JOIN type_bases b ON b.type_id=t.id
             WHERE m.name='Handle' AND b.base_simple IN ({marks})
            """,
            HANDLER_BASES,
        ).fetchall()

    def symbols_like(self, needle: str, limit: int) -> tuple[list[sqlite3.Row], int]:
        pattern = f"%{needle.replace('!', '!!').replace('%', '!%').replace('_', '!_')}%"
        query = """
            SELECT 'type' AS symbol_kind, t.kind AS kind, t.name AS name, t.name AS qualified, t.file_id,
                   t.start_line, t.end_line, (t.kind || ' ' || t.name) AS text
              FROM types t WHERE t.name LIKE :p ESCAPE '!'
            UNION ALL
            SELECT 'method', m.kind, m.name, (t.name || '.' || m.name), m.file_id, m.start_line, m.end_line,
                   m.signature
              FROM methods m JOIN types t ON t.id=m.type_id
             WHERE m.name LIKE :p ESCAPE '!' OR m.route LIKE :p ESCAPE '!' OR t.route LIKE :p ESCAPE '!'
        """
        rows = self.connection.execute(
            f"SELECT * FROM ({query}) ORDER BY (lower(name)=lower(:n)) DESC, length(name), qualified LIMIT :l",
            {"p": pattern, "n": needle, "l": limit},
        ).fetchall()
        total = self.connection.execute(f"SELECT count(*) FROM ({query})", {"p": pattern}).fetchone()[0]
        return rows, total


class ContextStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._loaded: dict[Path, _Loaded] = {}
        self._shared: dict[Path, tuple[str, dict]] = {}
        self._paths: dict[tuple[str, str], Path] = {}
        self._key: bytes | None = None

    def ensure(self, alias: str, root: Path, *, force: bool = False) -> Context:
        started = time.perf_counter()
        head = read_head(root)
        self._prepare()
        path = self._database_path(alias, head.label)
        label = crypto.label_hash(alias, head.label)
        stats = ScanStats()
        connection, meta = self._load(path, label, alias, stats)
        if not force and _is_current(meta, head):
            stats.elapsed_ms = round((time.perf_counter() - started) * 1000)
            return Context(connection, alias, head.label, head.commit, stats, self._shared_cache(path, meta))
        with _FileLock(path.with_name(path.name.removesuffix(DB_SUFFIX) + ".lock")):
            connection, meta = self._load(path, label, alias, stats)
            if force or not _is_current(meta, head):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    _scan(connection, root, stats)
                    stats.scanned = True
                    changed = bool(stats.parsed or stats.removed or stats.unchanged_content) or any(
                        meta.get(key) != value for key, value in
                        (("head_commit", head.commit or ""), ("branch", head.label), ("complete", "1"))
                    )
                    if changed:
                        meta["generation"] = str(time.time_ns())
                        _write_meta(connection, alias=alias, branch=head.label, head_commit=head.commit or "",
                                    schema_version=SCHEMA_VERSION, parser_version=indexer.PARSER_VERSION,
                                    complete="1", built_at=str(int(time.time())), generation=meta["generation"])
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                if changed:
                    self._save(path, label, connection)
        stats.elapsed_ms = round((time.perf_counter() - started) * 1000)
        return Context(connection, alias, head.label, head.commit, stats, self._shared_cache(path, meta))

    def _shared_cache(self, path: Path, meta: dict[str, str]) -> dict:
        """Per-database cache reused across requests until a scan bumps the generation."""
        generation = meta.get("generation", "0")
        cached = self._shared.get(path)
        if cached is None or cached[0] != generation:
            cached = (generation, {})
            self._shared[path] = cached
        return cached[1]

    def close(self) -> None:
        for loaded in self._loaded.values():
            loaded.connection.close()
        self._loaded.clear()

    def _prepare(self) -> None:
        """Load (or create) the DPAPI-wrapped key once, and remove plaintext left by older versions."""
        if self._key is not None:
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            with _FileLock(self.directory / ".key.lock"):
                key, created = crypto.load_or_create_key(self.directory / KEY_FILE)
                if created:
                    _remove(self.directory.glob(f"*{DB_SUFFIX}"))
        except crypto.CryptoUnavailable as exc:
            raise ContextError(str(exc)) from None
        except OSError:
            raise ContextError("ai-context key is unavailable") from None
        _remove(path for pattern in PLAINTEXT_PATTERNS for path in self.directory.glob(pattern))
        cutoff = time.time() - STALE_TEMP_SECONDS
        _remove(path for path in self.directory.glob("*.tmp") if _mtime(path) < cutoff)
        self._key = key

    def _database_path(self, alias: str, label: str) -> Path:
        cached = self._paths.get((alias, label))
        if cached is not None:
            return cached
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", label)[:100]
        path = self.directory / f"{alias}-{safe}{DB_SUFFIX}"
        stored = crypto.read_label_hash(path) if path.exists() else None
        if stored is not None and stored != crypto.label_hash(alias, label):
            digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:8]
            path = self.directory / f"{alias}-{safe}-{digest}{DB_SUFFIX}"
        self._paths[(alias, label)] = path
        return path

    def _load(self, path: Path, label: bytes, alias: str,
              stats: ScanStats) -> tuple[sqlite3.Connection, dict[str, str]]:
        """Return the in-memory database for `path`, decrypting again only if the file changed."""
        stamp = _stamp(path)
        loaded = self._loaded.get(path)
        if loaded is None or loaded.stamp != stamp:
            data = None
            if stamp is not None:
                data = self._open(path, label)
            elif loaded is None:
                data = self._seed(alias, path)
                stats.seeded = data is not None
            if loaded is None or stamp is not None:
                connection = _memory_database(data)
                if loaded is not None:
                    loaded.connection.close()
                loaded = _Loaded(connection, stamp)
                self._loaded[path] = loaded
        meta = _meta(loaded.connection)
        if meta.get("schema_version") != SCHEMA_VERSION or meta.get("parser_version") != indexer.PARSER_VERSION:
            _reset(loaded.connection)
            meta = {}
        return loaded.connection, meta

    def _open(self, path: Path, label: bytes) -> bytes | None:
        try:
            stored, data = crypto.open_sealed(self._key, path.read_bytes())
        except (OSError, crypto.CryptoError):
            return None
        return data if stored == label else None

    def _save(self, path: Path, label: bytes, connection: sqlite3.Connection) -> None:
        blob = crypto.seal(self._key, connection.serialize(), label, time.time_ns())
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(blob)
        _replace(temporary, path)
        self._loaded[path].stamp = _stamp(path)

    def _seed(self, alias: str, target: Path) -> bytes | None:
        """Decrypted image of the newest complete database of the same alias, if any."""
        candidates = sorted((p for p in self.directory.glob(f"{alias}-*{DB_SUFFIX}") if p != target),
                            key=_mtime, reverse=True)
        for candidate in candidates:
            try:
                _stored, data = crypto.open_sealed(self._key, candidate.read_bytes())
                connection = _memory_database(data)
            except (OSError, crypto.CryptoError, sqlite3.Error):
                continue
            meta = _meta(connection)
            connection.close()
            if meta.get("alias") == alias and meta.get("complete") == "1" \
                    and meta.get("schema_version") == SCHEMA_VERSION \
                    and meta.get("parser_version") == indexer.PARSER_VERSION:
                return data
        return None


@dataclass
class _Loaded:
    connection: sqlite3.Connection
    stamp: tuple[int, int] | None


class _FileLock:
    """Cross-process exclusive lock on a small lock file (one writer per database)."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self.path = path
        self.timeout = timeout
        self._handle = None

    def __enter__(self) -> "_FileLock":
        self._handle = open(self.path, "a+b")
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._handle.seek(0)
                if msvcrt is not None:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() > deadline:
                    self._handle.close()
                    raise ContextError("ai-context is busy; try again") from None
                time.sleep(0.05)

    def __exit__(self, *exc_info: object) -> None:
        try:
            self._handle.seek(0)
            if msvcrt is not None:
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()


def _is_current(meta: dict[str, str], head: HeadInfo) -> bool:
    """The stored facts describe exactly this branch at this commit (a seed from another branch
    at the same commit is not current until it has been rescanned and saved under this branch)."""
    return bool(head.commit) and meta.get("head_commit") == head.commit \
        and meta.get("branch") == head.label and meta.get("complete") == "1"


def _memory_database(data: bytes | None) -> sqlite3.Connection:
    """An in-memory SQLite database (optionally restored from a decrypted image). Nothing is
    written to disk: no journal file, no temp files."""
    connection = sqlite3.connect(":memory:", isolation_level=None)
    if data:
        try:
            connection.deserialize(data)
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except sqlite3.Error:
            connection.close()
            connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.executescript(SCHEMA)
    return connection


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _replace(source: Path, target: Path) -> None:
    """os.replace with retries: on Windows a reader holding the target briefly blocks the rename."""
    deadline = time.monotonic() + REPLACE_TIMEOUT_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() > deadline:
                source.unlink(missing_ok=True)
                raise ContextError("ai-context could not be saved; try again") from None
            time.sleep(0.02)


def _remove(paths) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def _meta(connection: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: row[1] for row in connection.execute("SELECT key, value FROM meta")}


def _write_meta(connection: sqlite3.Connection, **values: str) -> None:
    connection.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", values.items())


def _reset(connection: sqlite3.Connection) -> None:
    for table in TABLES:
        connection.execute(f"DROP TABLE IF EXISTS {table}")
    connection.executescript(SCHEMA)


def _scan(connection: sqlite3.Connection, root: Path, stats: ScanStats) -> None:
    current = {relative: (size, mtime) for relative, size, mtime in iter_index_files(root, indexer.INDEXED_SUFFIXES)}
    known = {row[1]: row for row in connection.execute("SELECT id, path, size, mtime_ns, sha1 FROM files")}
    stats.files = len(current)
    removed_ids = [row[0] for path, row in known.items() if path not in current]
    to_parse: list[tuple[str, bytes, bytes, int, int]] = []
    touched: list[tuple[int, int, int]] = []
    for relative, (size, mtime) in current.items():
        row = known.get(relative)
        if row is not None and row[2] == size and row[3] == mtime:
            continue
        try:
            data = (root / relative).read_bytes()
        except OSError:
            continue
        digest = hashlib.sha1(data).digest()
        if row is not None and row[4] == digest:
            touched.append((size, mtime, row[0]))
            stats.unchanged_content += 1
            continue
        if row is not None:
            removed_ids.append(row[0])
        to_parse.append((relative, data, digest, size, mtime))
    facts = _parse_all([(relative, data) for relative, data, *_ in to_parse])
    stats.removed = len([path for path in known if path not in current])
    stats.parsed = len(to_parse)
    if removed_ids:
        connection.executemany("DELETE FROM files WHERE id=?", [(file_id,) for file_id in removed_ids])
    if touched:
        connection.executemany("UPDATE files SET size=?, mtime_ns=? WHERE id=?", touched)
    _insert(connection, to_parse, facts)


def _parse_all(items: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    if len(items) >= PARALLEL_THRESHOLD:
        batches = [items[i:i + PARALLEL_BATCH] for i in range(0, len(items), PARALLEL_BATCH)]
        try:
            with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
                return [facts for batch in pool.map(_safe_batch, batches) for facts in batch]
        except Exception:
            pass
    return _safe_batch(items)


def _safe_batch(items: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    results = []
    for relative, data in items:
        try:
            results.append(indexer.parse_file(relative, data))
        except Exception:
            results.append({"lang": "unparsed", "line_count": data.count(b"\n") + 1,
                            "usings": [], "types": [], "registrations": []})
    return results


def _insert(connection: sqlite3.Connection, items: list[tuple], facts_list: list[dict[str, Any]]) -> None:
    next_ids = {
        table: (connection.execute(f"SELECT coalesce(max(id), 0) FROM {table}").fetchone()[0] + 1)
        for table in ("files", "types", "methods")
    }
    rows: dict[str, list[tuple]] = {name: [] for name in
                                    ("files", "usings", "types", "type_bases", "members", "methods", "calls",
                                     "registrations")}
    for (relative, _data, digest, size, mtime), facts in zip(items, facts_list):
        file_id = next_ids["files"]
        next_ids["files"] += 1
        rows["files"].append((file_id, relative, size, mtime, digest, facts["lang"], facts["line_count"]))
        rows["usings"].extend((file_id, namespace) for namespace in facts["usings"])
        rows["registrations"].extend((file_id, *registration) for registration in facts["registrations"])
        for record in facts["types"]:
            type_id = next_ids["types"]
            next_ids["types"] += 1
            rows["types"].append((type_id, file_id, record["namespace"], record["name"], record["kind"],
                                  record["start"], record["end"], record["route"]))
            rows["type_bases"].extend((type_id, simple, args) for simple, args in record["bases"])
            rows["members"].extend((type_id, *member) for member in record["members"])
            for method in record["methods"]:
                method_id = next_ids["methods"]
                next_ids["methods"] += 1
                rows["methods"].append((
                    method_id, type_id, file_id, method["name"], method["kind"], method["params"],
                    method["required"], method["variadic"], method["extension"], method["override"],
                    method["signature"], method["start"], method["end"], method["http"], method["route"],
                ))
                rows["calls"].extend((method_id, *call) for call in method["calls"])
    for table, values in rows.items():
        if values:
            marks = ",".join("?" for _ in values[0])
            connection.executemany(f"INSERT INTO {table} VALUES ({marks})", values)
