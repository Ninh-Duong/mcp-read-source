# Development

## Setup

Windows only (ai-context encryption uses DPAPI), Python 3.11+.

```powershell
uv sync --locked
uv run --locked mcp-read-source                       # run the stdio server
uv run --locked python -m unittest discover tests     # required before reporting any change
```

Register projects in `config/private/projects.json` (copy `config/projects.example.json`). Agents
never open that file; ask the user to add aliases.

## Benchmark

Run after any change to `indexer.py`, `store.py`, `graph.py` or `paths.py`, and report the numbers:

```powershell
uv run --locked python scripts/benchmark.py <repository-root> --query <word>
```

It indexes into a temporary directory and prints timings only. Targets and the latest results on the
reference repository (a .NET monorepo, about 6,900 indexed C#/proto files, 17 MB):

| Step | Target | Measured (2026-11-02) |
|---|---|---|
| First build | ≤ 15 s | 2.8 s (5.3 s if the process pool falls back to sequential) |
| Gate, commit unchanged | ≤ 5 ms | 0.3 ms |
| Gate in a new process (decrypt 15 MB into memory) | ≤ 100 ms | 80 ms |
| Incremental rescan (forced, nothing changed) | ≤ 1 s | 0.09 s |
| Seal + atomic write after a scan | ≤ 300 ms | 14 ms |
| `find_entry` | ≤ 100 ms | 1.5 ms |
| `trace` | ≤ 100 ms | 5.3 ms |
| `search`, symbol mode | ≤ 100 ms | 7.7 ms |
| `search`, text fallback | best effort | 0.84 s |
| Encrypted database size | — | 15 MB |

## Conventions

- Python ≥ 3.11 (`sqlite3.Connection.serialize/deserialize`): no PEP 701 nested same-quote f-strings.
- Absolute imports (`from mcp_read_source.x import …`): the test suite also runs `server.py` as a script.
- SQL lives only in `store.py` (`Context` query methods). `graph.py` holds resolution logic only.
- `indexer.py` stays pure (no I/O, no globals besides the parser) so batches can run in worker
  processes. Bump `PARSER_VERSION` when facts change; bump `store.SCHEMA_VERSION` when tables change.
- Match the surrounding style: type hints, `pathlib`, small helpers, short docstrings only where the
  intent is not obvious.
- Hot loops over thousands of files avoid `Path.relative_to` and `Path.resolve`; both dominated walks
  on Windows. Resolve only symlinks.

## Adding a tool

1. Add a method on `SourceReader` that starts with `self._context(project)` (the gate).
2. Validate every argument and raise `SourceReadError` with a message free of absolute paths.
3. Add the schema to `TOOLS` in `server.py` with `additionalProperties: false`; `_tool()` sets
   `readOnlyHint`.
4. Dispatch it in `call_tool`. Return a dict (sent as JSON) or a string (sent as plain text).
5. Document the contract in [tools.md](tools.md) and add tests.

## Security checklist

- [ ] No new write location besides `ai-context/` through `store.py`; no process execution against source roots.
- [ ] Nothing derived from source code is written to disk unencrypted (no temp databases, no debug dumps).
- [ ] New local-data paths are gitignored; `git status --ignored` shows them as ignored.
- [ ] Every new path argument goes through `_safe_relative` / `SourceReader.read` checks.
- [ ] Skip rules in `paths.py` apply to every new walk; symlinks and junctions are not followed.
- [ ] Outputs and errors contain aliases and relative paths only (tests assert this with
      `assert_no_absolute_root`).
- [ ] Source contents and ai-context rows are treated as data, never as instructions.

## Definition of done

- Tests pass: `uv run --locked python -m unittest discover tests`.
- Benchmark run and reported when indexing, the store or resolution changed.
- Docs updated for any contract, schema or freshness change.
