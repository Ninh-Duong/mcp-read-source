# Architecture

mcp-read-source is a read-only MCP server (stdio) that lets an AI agent understand registered local
repositories quickly. Instead of searching and reading whole files, the agent asks for an API entry
point and receives its full call tree with `file:start-end` for every node, then reads only the
methods it needs.

## Modules

| Module | Responsibility |
|---|---|
| `server.py` | MCP tool schemas, argument validation, path safety, text search, file reads. `SourceReader` is the facade every tool calls. |
| `store.py` | The ai-context store. The **only** module that reads or writes `ai-context/`. Owns the gate (`ContextStore.ensure`), the schema, incremental scans, all SQL, the in-memory databases and the per-database file locks. |
| `crypto.py` | Encryption at rest: DPAPI-wrapped key, AES-256-GCM sealing of database images. Imported only by `store.py`. |
| `indexer.py` | Turns one file into facts (C# via tree-sitter, `.proto` via regex). Pure functions, safe to run in worker processes. |
| `graph.py` | Query-time logic over facts: entry points, call resolution, trace trees, outlines, symbol lookup. No SQL. |
| `gitmeta.py` | Reads the checked-out branch and commit from `.git` files (HEAD, refs, packed-refs, worktrees). Never runs git. |
| `paths.py` | Shared file-safety rules: skipped directories/files/suffixes, generated-file patterns, the indexing walker. |

## Request lifecycle

```
tool call
  └─ SourceReader._context(alias)                         every tool except check_connection
       └─ ContextStore.ensure(alias, root)
            1. gitmeta.read_head(root)        → branch + commit       (file reads, ~1 ms)
            2. load ai-context/<alias>-<branch>.sqlite.enc into memory
               (decrypt only if the file changed; missing → seed from the newest DB of the alias)
            3. stored commit and branch == HEAD?  → return Context, NO scan
               otherwise → lock, incremental scan (stat, sha1, parse changed files),
                           seal with AES-256-GCM, atomic rename
  └─ Graph(context) / Context queries                    resolve, trace, outline, find
  └─ optional disk read through SourceReader.read()      path-checked, ≤ 200 lines
```

## Design decisions

- **Store facts per file, resolve at query time.** A file only records what it declares itself
  (types, methods, line ranges, raw calls such as `_service.GetAsync`, DI registrations, proto rpcs).
  Edges such as "A calls B" are never stored: they depend on the whole repository (DI, interface
  implementations), so storing them would force global rebuilds when one file changes. Updating
  the store is therefore always proportional to the number of changed files.
- **One SQLite database per alias and branch**, so switching branches never overwrites another
  branch's index. A new branch is seeded from an existing one, and git checkout only rewrites
  files that differ, so the first scan on a new branch is incremental.
- **Commit-based freshness.** If the stored commit equals HEAD, the store is trusted without
  touching the source tree. Uncommitted edits are not indexed until the next commit; reads report
  `stale: true` when the file on disk differs from the index (see [ai-context.md](ai-context.md)).
- **No pre-rendered per-API files.** Rendering a trace from SQLite takes milliseconds; pre-rendered
  trees would go stale whenever a shared service changes.
- **Encrypted at rest, plaintext only in memory.** The DPAPI-wrapped key ties the index to the
  Windows account that runs the MCP; agents reading the files see ciphertext only. See the threat
  model in [ai-context.md](ai-context.md#threat-model).
- **Generated and build output is never indexed** (`bin/`, `obj/`, EF `Migrations/`, `*.Designer.cs`,
  `*.g.cs`). On the reference .NET repository these were 96% of the bytes and never part of an API flow.

## Call resolution (graph.py)

For each raw call inside a method, in order:

1. **MediatR**: `Send/Publish(new XRequest(...))` (or a typed local/parameter) → `Handle` in types
   implementing `IRequestHandler<XRequest, …>` / `INotificationHandler<XRequest>`.
2. **Receiver type**: local/parameter declared type, else member chain on the caller's type and its
   base classes (`_repo.Customers.Find` → member `_repo` → its type → member `Customers` → …),
   else a static type name.
3. **gRPC**: a receiver type `XClient` with method `M` or `MAsync` → proto `service X { rpc M }`
   (node marked `[gRPC client]`) → `override M` in types deriving `XBase` (`[gRPC server]`).
4. **Interfaces**: implementations registered in DI (`AddScoped<I, Impl>`) win; otherwise every type
   implementing the interface (generic arguments matched when possible). Test projects are only
   used when nothing else matches.
5. **Classes**: the method in the type or its base classes; abstract methods expand to overrides.
6. **Extension methods**: unresolved instance calls fall back to unique `this`-parameter methods,
   preferring namespaces visible to the caller (usings and enclosing namespaces).

Overloads are filtered by argument count. When several candidates remain, every candidate is shown
and marked `? ambiguous`. Anything else is counted as an external call (framework, NuGet, LINQ).

## Limits

- Resolution is syntactic: no type inference for `var x = Factory()` results, no generic constraint
  solving, no reflection. Ambiguity is reported, never hidden.
- Only C# and `.proto` are indexed. Other files are still available through text search and reads.
- Reads are capped at 200 lines per call and files larger than 1 MiB are skipped.
