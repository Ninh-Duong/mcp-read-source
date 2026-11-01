# mcp-read-source

Read-only MCP access to explicitly registered local source repositories, with a per-branch
**ai-context** index so agents can follow an API flow without reading whole files. The Hub launches
this server over stdio.

```
source__find_entry("GET /customers")  →  source__trace(id)  →  source__read_symbol("path:line")
```

`source__trace` returns the full call tree (controller → DI service → gRPC client → proto → gRPC
server → MediatR handler …) with `file:start-end` for every node. See [docs/tools.md](docs/tools.md)
for all tools.

## Configure projects

Create `config/private`, then copy `config/projects.example.json` to `config/private/projects.json`.
Add alias-to-root entries locally (aliases use `[A-Za-z0-9_-]`). Clients pass only the alias; no tool
returns roots.

```json
{"version":1,"projects":{"app":"<LOCAL_REPOSITORY_ROOT>"}}
```

## How the index stays fresh

The index lives in `ai-context/<alias>-<branch>.sqlite.enc` (gitignored, local only), encrypted
with AES-256-GCM under a key wrapped by Windows DPAPI, so only the Windows account running the MCP
can decrypt it and agents only get its data through the tools. Every tool checks it first:
when the stored commit matches the checked-out commit, nothing is scanned; otherwise only changed
files are parsed again. Uncommitted edits are reported as `stale` instead of being indexed. Details:
[docs/ai-context.md](docs/ai-context.md).

## Safety

The server never writes to source repositories, never executes repository code or git, rejects
traversal and symlinks that escape a project, and skips VCS, dependency, build, cache, environment,
private-config, encrypted-cache, key/certificate and generated files. It ignores binary files and
files larger than 1 MiB. A read returns at most 200 lines.

Requires Windows and Python 3.11+.

## Run and check

```powershell
uv run --locked mcp-read-source
uv run --locked python -m unittest discover tests
```

Contributor and agent rules: [AGENTS.md](AGENTS.md). Architecture: [docs/architecture.md](docs/architecture.md).
Development, benchmark and conventions: [docs/development.md](docs/development.md).
