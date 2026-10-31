# mcp-read-source

Read-only MCP access to explicitly registered local source repositories. The Hub launches this child over stdio and exposes `source__search`, `source__read`, and `source__check_connection`.

## Configure projects

Create `config/private`, then copy `config/projects.example.json` to `config/private/projects.json`. Add project alias-to-root entries locally. Clients pass only the alias; the health tool never returns roots.

```json
{"version":1,"projects":{"app":"<LOCAL_REPOSITORY_ROOT>"}}
```

The reader rejects traversal and symlinks that resolve outside a registered project. It skips VCS, dependency, build, cache, environment, private-config, encrypted-cache, and key/certificate files; it ignores binary files and files larger than 1 MiB. Search examines at most 500 files. A read returns at most 200 lines.

## Run and check

```powershell
uv run --locked mcp-read-source
uv run --locked python -m unittest discover tests
```

The server does not execute repository commands and has no write tools.
