# ai-context store

The ai-context is the index every tool reads before touching source files. It lives inside this
repository, is encrypted at rest, is gitignored, and never leaves the local machine.

## Location and naming

```
mcp-read-source/
└─ ai-context/                              gitignored, written only by store.py
   ├─ .key                                  256-bit key wrapped with Windows DPAPI (CurrentUser)
   ├─ .key.lock                             serializes key creation across processes
   ├─ <alias>-<branch>.sqlite.enc           e.g. app-release_main.sqlite.enc (AES-256-GCM)
   ├─ <alias>-<branch>.lock                 one writer per database
   ├─ <alias>-detached-<sha7>.sqlite.enc    detached HEAD
   └─ <alias>-nogit.sqlite.enc              root without a .git directory
```

- `/` and any character outside `[A-Za-z0-9._-]` in the branch name become `_`. If a file with that
  name belongs to another branch (its header label hash differs), a `-<sha1(branch)[:8]>` suffix is
  used instead.
- Aliases are restricted to `[A-Za-z0-9_-]`, so file names are always safe. Register a repository
  folder named `wec.be` as, for example, `wec-be`.
- Deleting any database, or the whole directory, is always safe: the index is derived data and the
  next tool call rebuilds it (about 3 s on the reference repository).

## Encryption at rest

Implemented in `crypto.py`; only `store.py` imports it.

- **Key**: 32 random bytes generated on first use, stored in `.key` as `MRSKEY1\0` + a DPAPI blob
  (`CryptProtectData`, CurrentUser scope, application entropy). Only the Windows account that
  created it can unwrap it. The key is never written in plaintext, logged or returned.
- **Databases**: the SQLite image is serialized in memory and sealed with AES-256-GCM:
  `MRSAI01\0 | label hash (16) | generation (8) | nonce (12) | ciphertext+tag`. The whole header is
  authenticated as associated data. The label hash is `sha256(alias \0 branch)[:16]`, so a file
  copied under another branch's name is detected and never reused.
- **No plaintext on disk**: databases are decrypted with `sqlite3.Connection.deserialize` into an
  in-memory connection (`temp_store=MEMORY`, no journal or WAL files). After a scan the image is
  serialized, sealed, written to `<file>.<pid>.tmp` and atomically renamed with `os.replace`.
- **Failure is never fatal**: a missing or foreign key creates a new key and deletes every sealed
  database; a file that fails authentication or belongs to another label is ignored and rebuilt.
  Plaintext files left by older versions (`*.sqlite`, `-wal`, `-shm`) are deleted on start, as are
  temp files older than 10 minutes.
- **Windows only**: on other platforms the gate raises `ai-context encryption requires Windows
  DPAPI`; there is no plaintext fallback.

### Threat model

| Threat | Protected |
|---|---|
| An agent or tool reading `ai-context/` files directly (Read, Grep, `cat`, indexing) | Yes: ciphertext only; `.claude/settings.json` also denies these paths |
| Files copied, uploaded, committed or included in a backup | Yes: useless without the DPAPI key |
| Another Windows account on the same machine | Yes: DPAPI unwraps the key only for the creating account |
| Tampering or swapping database files | Yes: AES-GCM authentication and label binding; the file is rebuilt |
| Code running as the **same** Windows account that deliberately calls this package to decrypt | **No**. Any process that can run as the MCP's account can do what the MCP does. To close this gap, run the MCP under a dedicated Windows account and grant only that account access to `ai-context/` (planned "level B"; no code change needed because DPAPI follows the account). |

## Freshness rule (the gate)

`ContextStore.ensure(alias, root, force=False)` runs at the start of every tool except
`source__check_connection`:

1. Read the branch and commit from `.git` files only: `HEAD` → `refs/heads/<branch>` or
   `packed-refs`. For worktrees and submodules the `.git` file's `gitdir:` and `commondir` are
   followed. Parent directories are searched when the root is a subfolder of a repository.
2. Load the database for `<alias>-<branch>` into memory. It is decrypted only when the file's
   `(mtime_ns, size)` differs from the copy already in memory, so other processes' saves are picked
   up automatically. If the file does not exist, the newest complete database of the same alias is
   decrypted and used as a seed.
3. If `meta.head_commit` equals the current commit **and** `meta.branch` equals the current branch
   (and `force` is false): **return without scanning**. This costs about 1–2 ms.
4. Otherwise take `<alias>-<branch>.lock`, reload if another process saved meanwhile, and run an
   incremental scan:
   - walk indexable files (`.cs`, `.proto`) with `os.scandir`, never following symlinks or
     junctions, skipping the rules in `paths.py` and generated files;
   - compare `(size, mtime_ns)` with `files`; for differing files compute `sha1`. If the content is
     unchanged, only the stat columns are updated. Otherwise the file is parsed again;
   - delete removed and replaced files (child rows cascade), insert new facts, and write
     `head_commit`, `branch`, `alias`, a new `generation` (nanosecond timestamp) and `complete=1`;
   - seal and save the database only if something changed.
5. Repositories without git (`nogit`) have no commit, so they are rescanned incrementally on every
   call; the file is rewritten only when something changed.

Consequences:

- **Uncommitted edits are not indexed** until the next commit. `source__read_symbol` and
  `source__outline` stat only the file they return and add `stale: true` when it differs from the
  index; line numbers may then be off. `source__index(force=true)` rescans on demand.
- Concurrent MCP processes are safe: the per-database lock file (`msvcrt.locking`) lets one process
  scan while others wait (timeout 120 s), and readers reload after the atomic rename.
- Full builds parse in a process pool when 300+ files need parsing; if the pool cannot start, the
  scan falls back to sequential parsing.

## In-process caches

- One decrypted in-memory database per `<alias>-<branch>`, kept for the life of the process.
- `Context._cache`: per request, memoizes lookups during one trace.
- `Context.shared`: per database and `meta.generation`; holds the entry list. Any scan that changes
  data, by any process, writes a new generation and therefore invalidates it.

## Schema (`SCHEMA_VERSION = "1"`)

All child tables cascade on delete from `files` (directly or through `types`/`methods`).

| Table | Columns | Notes |
|---|---|---|
| `meta` | `key`, `value` | `schema_version`, `parser_version`, `alias`, `branch`, `head_commit`, `generation`, `complete`, `built_at` |
| `files` | `id`, `path` (relative posix, unique), `size`, `mtime_ns`, `sha1`, `lang`, `line_count` | `lang` is `cs`, `proto` or `unparsed` |
| `usings` | `file_id`, `namespace` | used to prefer visible namespaces when names collide |
| `types` | `id`, `file_id`, `namespace`, `name`, `kind`, `start_line`, `end_line`, `route` | kinds: `class`, `interface`, `struct`, `record`, `enum`, `proto_service`, `proto_message`, `minimal_api` |
| `type_bases` | `type_id`, `base_simple`, `base_args` | `IRequestHandler` + `GetQuery,Dto`; `CoreProtoBase` + empty |
| `members` | `type_id`, `name`, `type_name`, `type_args` | fields, properties, constructor and primary-constructor parameters |
| `methods` | `id`, `type_id`, `file_id`, `name`, `kind`, `params`, `required`, `variadic`, `extension`, `override`, `signature`, `start_line`, `end_line`, `http_method`, `route` | kinds: `method`, `constructor`, `property` (bodied only), `rpc`, `minimal_api` |
| `calls` | `method_id`, `line`, `receiver`, `receiver_type`, `name`, `argc`, `arg0_type` | raw, unresolved calls; `receiver_type` is filled when the receiver is a typed local or parameter; `arg0_type` is the type of the first argument when known |
| `registrations` | `file_id`, `line`, `kind`, `service_type`, `impl_type`, `route` | `scoped`/`transient`/`singleton`/`grpc_client`/`map_get`… |

Entry points are derived at query time: methods with `http_method`; `override` methods in types
deriving `<Service>Base` where a proto service `<Service>` declares that rpc; `Handle` in MediatR
handler types; minimal API lambdas (`MapGet` etc.).

**Bump `indexer.PARSER_VERSION`** whenever extracted facts change, and `store.SCHEMA_VERSION` when
tables change. Either mismatch resets the database on the next call.

## What is indexed

- Indexed: `*.cs` and `*.proto` up to 1 MiB.
- Never indexed: everything skipped by `paths.py` (`.git`, `bin`, `obj`, `node_modules`, `private`,
  `ai-context`, `.env*`, credentials, keys/certificates, …), symlinks, junctions and generated
  files (`Migrations/`, `*.Designer.cs`, `*.g.cs`, `*.g.i.cs`, `ModelSnapshot.cs`).
- File contents are **not** stored, only facts (names, signatures, routes) and line ranges.
