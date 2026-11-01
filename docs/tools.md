# Tools

Every tool is read-only (`readOnlyHint`), rejects unknown arguments, and identifies projects by
alias. Every tool except `source__check_connection` runs the ai-context gate first (see
[ai-context.md](ai-context.md)); "scans" below means "may run the incremental scan when the commit
changed".

## Recommended agent workflow

1. `source__find_entry(project, "customer search")` → pick an entry `id` (`path:line`).
2. `source__trace(project, id)` → the whole flow with `file:start-end` per node.
3. `source__read_symbol(project, "path:line")` on the nodes you actually need.
4. `source__outline(project, path)` instead of reading a whole file to see its structure.
5. `source__search` for anything that is not a symbol; `source__read` for arbitrary line ranges.

## Reference

| Tool | Arguments | Returns |
|---|---|---|
| `source__check_connection` | none | `configured_projects` (aliases only). No gate. |
| `source__index` | `project`, `force?` | `branch`, `commit` (12 chars), `ai_context` (`cached (no scan)` or `rescanned`), `files_seen`, `parsed`, `unchanged_content`, `removed`, `seeded_from_other_branch`, `elapsed_ms`, `indexed` counts, `entries` per kind. `force=true` rescans even when the commit matches. |
| `source__find_entry` | `project`, `query`, `kind?` (`http`, `grpc`, `mediatr`), `limit?` (1–100, default 20) | `matches[]` with `id` (`path:line`), `kind`, `name` (`GET /api/x/{id}`, `Service.Rpc`, `Request -> Handler.Handle`), `handler`, `path`, `line`, `end_line`, `score`; `total`. Exact and substring matches rank first; fuzzy matching (typos) only runs when nothing matches directly. |
| `source__trace` | `project`, `entry`, `max_depth?` (default unlimited, hard cap 400), `include_external?` | Plain text: a header (branch, commit, cache state, node and call counts, legend) and the tree. `entry` accepts an entry id, `path:line`, a route, `Controller.Action`, `Service.Rpc` or `Type.Method`. Output is capped at 3000 lines (`TRUNCATED` in the header). |
| `source__outline` | `project`, `path` | Plain text: the path (plus `stale` when the file changed since the indexed commit), then each type and its methods with signatures and `Lstart-end`. |
| `source__read_symbol` | `project`, `symbol`, `start_line?` | `symbol`, `kind`, `path`, `start_line`, `end_line`, `signature`, `content`, `read_from`, `read_to`, `stale`, and `next_start_line` when the symbol is longer than 200 lines. `symbol` accepts `path:line`, `Type.Method` or `Type`. |
| `source__search` | `project`, `query`, `glob?`, `limit?` (1–100, default 50), `mode?` | `mode=auto` (default): identifier-like queries are looked up in ai-context symbols (types, methods, routes); if nothing matches, falls back to a text scan. `mode=symbol`: ai-context only. `mode=text`: case-insensitive substring scan of up to 20000 files. Result has `source` (`ai-context` or `disk`), `matches`, `total`, `truncated`. |
| `source__read` | `project`, `path`, `start_line?`, `max_lines?` (1–200) | `path`, `start_line`, `end_line`, `total_lines`, `content`. |

## Trace legend

```
AcbController.Get  src/Api/AcbController.cs:12-18
├─ L15 AcbService.GetAsync  src/Services/AcbService.cs:6-10  [DI IAcbService]
│  ├─ L8 CoreProto.GetItem (proto)  protos/core.proto:5-5  [gRPC client]
│  │  └─ CoreProtoService.GetItem  src/Core/CoreProtoService.cs:5-8  [gRPC server]
│  │     ├─ L7 SqlItemRepository.FindAsync  src/Core/CoreProtoService.cs:11-11  [impl IItemRepository]  ? ambiguous
│  │     │  └─ L11 SqlItemRepository.Load  src/Core/CoreProtoService.cs:11-11
│  │     └─ L7 CachedItemRepository.FindAsync  src/Core/CoreProtoService.cs:12-12  [impl IItemRepository]  ? ambiguous
│  └─ L9 AcbService.Map  src/Services/AcbService.cs:11-11
│     └─ L11 AcbService.Map  src/Services/AcbService.cs:12-12
│        └─ L12 AcbService.Map  src/Services/AcbService.cs:12-12  ⟲ recursive
├─ L16 RefreshAcbHandler.Handle  src/Handlers/RefreshAcbHandler.cs:5-5  [MediatR RefreshAcbCommand]
└─ (+1 external calls)
```

- `Lnn`: line of the call inside the parent method (several lines are joined: `L39,L41`).
- `[via]`: how the call was resolved (`DI I…`, `impl I…`, `gRPC client`, `gRPC server`,
  `MediatR X`, `extension`, `override of X`).
- `↺ see above`: already expanded earlier in this tree (each method expands once).
- `⟲ recursive`: the method is already on the current path.
- `? ambiguous`: several candidates matched; all are shown.
- `(+N external calls)`: calls into framework or library code; pass `include_external=true` to list them.

## Errors

Errors are returned with `isError` and a short message that never contains absolute paths, e.g.
`Unknown project alias`, `File is not indexed`, `Several methods match: A.B @ src/a.cs:10; …`,
`path must remain inside the registered project`. Unexpected failures return
`Source read failed (<ExceptionType>)`.
