from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


FILES = {
    "src/Api/AcbController.cs": """using Shop.Services;
namespace Shop.Api;

[ApiController]
[Route("api/[controller]")]
public class AcbController : ControllerBase
{
    private readonly IAcbService _acbService;
    private readonly IMediator _mediator;
    public AcbController(IAcbService acbService, IMediator mediator) { _acbService = acbService; _mediator = mediator; }

    [HttpGet("{id}")]
    public async Task<IActionResult> Get(int id)
    {
        var dto = await _acbService.GetAsync(id);
        await _mediator.Send(new RefreshAcbCommand(id));
        return Ok(dto);
    }
}
""",
    "src/Services/IAcbService.cs": """namespace Shop.Services;
public interface IAcbService { Task<AcbDto> GetAsync(int id); }
""",
    "src/Services/AcbService.cs": """namespace Shop.Services;
public class AcbService : IAcbService
{
    private readonly CoreProto.CoreProtoClient _core;
    public AcbService(CoreProto.CoreProtoClient core) { _core = core; }
    public async Task<AcbDto> GetAsync(int id)
    {
        var reply = await _core.GetItemAsync(new ItemRequest { Id = id });
        return Map(reply);
    }
    public AcbDto Map(ItemReply reply) => Map(reply, 0);
    public AcbDto Map(ItemReply reply, int depth) => depth > 3 ? null : Map(reply, depth + 1);
}
""",
    "protos/core.proto": """syntax = "proto3";
package shop.core;
// rpc Ignored (A) returns (B);
service CoreProto {
  rpc GetItem (ItemRequest) returns (ItemReply);
}
message ItemRequest { int32 id = 1; }
message ItemReply { string name = 1; }
""",
    "src/Core/CoreProtoService.cs": """namespace Shop.Core;
public class CoreProtoService : CoreProto.CoreProtoBase
{
    private readonly IItemRepository _items;
    public override async Task<ItemReply> GetItem(ItemRequest request, ServerCallContext context)
    {
        return await _items.FindAsync(request.Id);
    }
}
public interface IItemRepository { Task<ItemReply> FindAsync(int id); }
public class SqlItemRepository : IItemRepository { public Task<ItemReply> FindAsync(int id) => Load(id); private Task<ItemReply> Load(int id) => null; }
public class CachedItemRepository : IItemRepository { public Task<ItemReply> FindAsync(int id) => null; }
""",
    "src/Handlers/RefreshAcbHandler.cs": """namespace Shop.Handlers;
public record RefreshAcbCommand(int Id) : IRequest;
public class RefreshAcbHandler : IRequestHandler<RefreshAcbCommand>
{
    public Task Handle(RefreshAcbCommand request, CancellationToken ct) { return Task.CompletedTask; }
}
""",
    "src/Startup.cs": """namespace Shop;
public static class Startup
{
    public static void Configure(IServiceCollection services, WebApplication app)
    {
        services.AddScoped<IAcbService, AcbService>();
        services.AddGrpcClient<CoreProto.CoreProtoClient>(o => { });
        app.MapGet("/health", () => Results.Ok());
    }
}
""",
    "tests/Shop.Tests/FakeAcbService.cs": """namespace Shop.Tests;
public class FakeAcbService : IAcbService { public Task<AcbDto> GetAsync(int id) => null; }
""",
    "src/Data/Migrations/20240101_Init.Designer.cs": "namespace Shop.Data; public class InitMigration { void Up() {} }\n",
    "src/Api/obj/Debug/Core.cs": "namespace Shop.Generated; public class ObjOnlyType { }\n",
    ".env": "class SecretType { }\n",
}
FIRST_COMMIT = "a" * 40


def write_repo(root: Path) -> None:
    for relative, content in FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git = root / ".git"
    (git / "refs" / "heads" / "feature").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/feature/x\n", encoding="utf-8")
    (git / "refs" / "heads" / "feature" / "x").write_text(FIRST_COMMIT + "\n", encoding="utf-8")


class IndexTest(unittest.TestCase):
    def setUp(self) -> None:
        from mcp_read_source.server import SourceReader

        self.temp = Path(tempfile.mkdtemp())
        self.root = self.temp / "repo"
        self.context_dir = self.temp / "ai-context"
        write_repo(self.root)
        self.reader = SourceReader({"shop": self.root}, context_dir=self.context_dir)

    def tearDown(self) -> None:
        self.reader.store.close()
        shutil.rmtree(self.temp)

    def commit(self, sha: str) -> None:
        (self.root / ".git" / "refs" / "heads" / "feature" / "x").write_text(sha + "\n", encoding="utf-8")

    def assert_no_absolute_root(self, value: object) -> None:
        text = value if isinstance(value, str) else json.dumps(value)
        for form in (str(self.root), self.root.as_posix(), str(self.temp)):
            self.assertNotIn(form, text)
            self.assertNotIn(form.replace("\\", "\\\\"), text)

    def test_trace_follows_di_grpc_mediatr_and_marks_ambiguity_and_recursion(self) -> None:
        tree = self.reader.trace("shop", "GET /api/Acb/{id}")
        self.assertIn("AcbController.Get  src/Api/AcbController.cs:12-18", tree)
        self.assertIn("L15 AcbService.GetAsync  src/Services/AcbService.cs:6-10  [DI IAcbService]", tree)
        self.assertIn("L8 CoreProto.GetItem (proto)  protos/core.proto:5-5  [gRPC client]", tree)
        self.assertIn("CoreProtoService.GetItem  src/Core/CoreProtoService.cs:5-8  [gRPC server]", tree)
        self.assertIn("SqlItemRepository.FindAsync", tree)
        self.assertIn("CachedItemRepository.FindAsync", tree)
        self.assertIn("? ambiguous", tree)
        self.assertIn("SqlItemRepository.Load", tree)
        self.assertIn("L16 RefreshAcbHandler.Handle  src/Handlers/RefreshAcbHandler.cs:5-5  [MediatR RefreshAcbCommand]", tree)
        self.assertIn("L9 AcbService.Map  src/Services/AcbService.cs:11-11", tree)
        self.assertIn("AcbService.Map  src/Services/AcbService.cs:12-12  ⟲ recursive", tree)
        self.assertIn("(+1 external calls)", tree)
        self.assertNotIn("FakeAcbService", tree)
        self.assert_no_absolute_root(tree)

        with_external = self.reader.trace("shop", "AcbController.Get", include_external=True)
        self.assertIn("L17 Ok  ✗ external", with_external)
        shallow = self.reader.trace("shop", "src/Api/AcbController.cs:14", max_depth=1)
        self.assertIn("… depth limit", shallow)
        self.assertNotIn("CoreProtoService.GetItem", shallow)

    def test_find_entry_supports_routes_handlers_rpcs_requests_and_typos(self) -> None:
        def names(query: str, kind: str | None = None) -> list[str]:
            return [match["name"] for match in self.reader.find_entry("shop", query, kind=kind)["matches"]]

        self.assertEqual(names("GET /api/Acb/{id}")[0], "GET /api/Acb/{id}")
        self.assertIn("GET /api/Acb/{id}", names("AcbController.Get"))
        self.assertIn("GET /api/Acb/{id}", names("AcbControler.Get"))
        self.assertEqual(names("GetItem", kind="grpc"), ["CoreProto.GetItem"])
        self.assertEqual(names("RefreshAcbCommand", kind="mediatr"), ["RefreshAcbCommand -> RefreshAcbHandler.Handle"])
        self.assertIn("GET /health", names("health"))
        result = self.reader.find_entry("shop", "acb")
        self.assertEqual(result["matches"][0]["id"], "src/Api/AcbController.cs:12")
        self.assert_no_absolute_root(result)

    def test_gate_skips_scan_when_commit_matches_and_rescans_incrementally(self) -> None:
        first = self.reader.index("shop")
        self.assertEqual(first["ai_context"], "rescanned")
        self.assertEqual(first["branch"], "feature/x")
        self.assertEqual(first["parsed"], 8)
        self.assertTrue((self.context_dir / "shop-feature_x.sqlite.enc").is_file())

        with patch("mcp_read_source.store.iter_index_files", side_effect=AssertionError("walked")), \
                patch("mcp_read_source.indexer.parse_file", side_effect=AssertionError("parsed")):
            cached = self.reader.index("shop")
            self.reader.trace("shop", "AcbController.Get")
            self.reader.search("shop", "AcbService")
        self.assertEqual(cached["ai_context"], "cached (no scan)")

        self.commit("b" * 40)
        unchanged = self.reader.index("shop")
        self.assertEqual((unchanged["ai_context"], unchanged["parsed"]), ("rescanned", 0))

        forced = self.reader.index("shop", force=True)
        self.assertEqual(forced["ai_context"], "rescanned")

        service = self.root / "src/Services/AcbService.cs"
        stat = service.stat()
        os.utime(service, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        self.commit("c" * 40)
        touched = self.reader.index("shop")
        self.assertEqual((touched["parsed"], touched["unchanged_content"]), (0, 1))

    def test_rescan_reflects_shifted_lines_and_deleted_files(self) -> None:
        self.assertEqual(self.reader.read_symbol("shop", "AcbService.GetAsync")["start_line"], 6)
        self.assertEqual(self.reader.find_entry("shop", "RefreshAcbCommand", kind="mediatr")["total"], 1)
        service = self.root / "src/Services/AcbService.cs"
        service.write_text("\n\n\n" + service.read_text(encoding="utf-8"), encoding="utf-8")
        (self.root / "src/Handlers/RefreshAcbHandler.cs").unlink()
        self.commit("d" * 40)

        symbol = self.reader.read_symbol("shop", "AcbService.GetAsync")
        self.assertEqual((symbol["start_line"], symbol["end_line"], symbol["stale"]), (9, 13, False))
        self.assertIn("GetItemAsync", symbol["content"])
        self.assertEqual(self.reader.find_entry("shop", "RefreshAcbCommand", kind="mediatr")["total"], 0)
        tree = self.reader.trace("shop", "AcbController.Get")
        self.assertNotIn("RefreshAcbHandler", tree)
        self.assertIn("src/Services/AcbService.cs:9-13", tree)

    def test_uncommitted_edits_are_flagged_stale_without_rescanning(self) -> None:
        self.reader.index("shop")
        service = self.root / "src/Services/AcbService.cs"
        service.write_text("// edited\n" + service.read_text(encoding="utf-8"), encoding="utf-8")
        with patch("mcp_read_source.store.iter_index_files", side_effect=AssertionError("walked")):
            symbol = self.reader.read_symbol("shop", "AcbService.GetAsync")
            outline = self.reader.outline("shop", "src/Services/AcbService.cs")
        self.assertTrue(symbol["stale"])
        self.assertIn("stale", outline.splitlines()[0])

    def test_new_branch_is_seeded_from_existing_context_and_reads_packed_refs(self) -> None:
        self.reader.index("shop")
        git = self.root / ".git"
        (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (git / "packed-refs").write_text(f"# pack-refs\n{'e' * 40} refs/heads/main\n", encoding="utf-8")

        result = self.reader.index("shop")
        self.assertEqual((result["branch"], result["commit"]), ("main", "e" * 12))
        self.assertTrue(result["seeded_from_other_branch"])
        self.assertEqual(result["parsed"], 0)
        self.assertTrue((self.context_dir / "shop-main.sqlite.enc").is_file())

        (git / "HEAD").write_text("f" * 40 + "\n", encoding="utf-8")
        self.assertEqual(self.reader.index("shop")["branch"], "detached-fffffff")

        (git / "HEAD").write_text("ref: refs/heads/feature_x\n", encoding="utf-8")
        (git / "refs" / "heads" / "feature_x").write_text("9" * 40 + "\n", encoding="utf-8")
        self.assertEqual(self.reader.index("shop")["branch"], "feature_x")
        databases = {path.name for path in self.context_dir.glob("shop-feature_x*.sqlite.enc")}
        self.assertEqual(len(databases), 2)
        self.assertIn("shop-feature_x.sqlite.enc", databases)

    def test_worktree_head_is_read_through_gitdir_and_commondir(self) -> None:
        from mcp_read_source.gitmeta import read_head

        git = self.root / ".git"
        worktree_git = git / "worktrees" / "wt"
        worktree_git.mkdir(parents=True)
        (worktree_git / "HEAD").write_text("ref: refs/heads/release/one\n", encoding="utf-8")
        (worktree_git / "commondir").write_text("../..\n", encoding="utf-8")
        (git / "packed-refs").write_text(f"{'1' * 40} refs/heads/release/one\n", encoding="utf-8")
        worktree = self.temp / "wt"
        (worktree / "src").mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {worktree_git.as_posix()}\n", encoding="utf-8")

        head = read_head(worktree / "src")
        self.assertEqual((head.branch, head.commit, head.label), ("release/one", "1" * 40, "release/one"))
        self.assertEqual(read_head(self.temp).label, "nogit")

    def test_search_uses_symbols_first_and_falls_back_to_disk(self) -> None:
        symbols = self.reader.search("shop", "AcbService")
        self.assertEqual(symbols["source"], "ai-context")
        self.assertEqual(symbols["matches"][0]["symbol"], "AcbService")
        self.assertIn({"path": "src/Services/AcbService.cs", "line": 6, "end_line": 10, "kind": "method",
                       "symbol": "AcbService.GetAsync",
                       "text": "public async Task<AcbDto> GetAsync(int id)"},
                      self.reader.search("shop", "GetAsync", glob="src/Services/*")["matches"])

        text = self.reader.search("shop", "Task.FromResult(new")
        self.assertEqual(text["source"], "disk")
        fallback = self.reader.search("shop", "CompletedTask")
        self.assertEqual((fallback["source"], fallback["total"]), ("disk", 1))
        self.assertEqual(self.reader.search("shop", "CompletedTask", mode="symbol")["total"], 0)

    def test_private_generated_and_build_files_are_never_indexed(self) -> None:
        for query in ("SecretType", "InitMigration", "ObjOnlyType"):
            self.assertEqual(self.reader.search("shop", query, mode="symbol")["total"], 0, query)
        self.assertEqual(self.reader.search("shop", "InitMigration", mode="text")["total"], 0)
        index = self.reader.index("shop")
        self.assertEqual(index["indexed"]["files"], 8)
        self.assert_no_absolute_root(index)

    def test_outline_and_read_symbol_page_long_methods(self) -> None:
        body = "\n".join(f"        Step{i}();" for i in range(250))
        (self.root / "src/Big.cs").write_text(
            f"namespace Shop;\npublic class Big\n{{\n    public void Run()\n    {{\n{body}\n    }}\n}}\n", encoding="utf-8",
        )
        outline = self.reader.outline("shop", "src/Big.cs")
        self.assertEqual(outline.splitlines(), ["src/Big.cs", "class Big  L2-257", "  public void Run()  L4-256"])

        first = self.reader.read_symbol("shop", "Big.Run")
        self.assertEqual((first["read_from"], first["read_to"], first["next_start_line"]), (4, 203, 204))
        second = self.reader.read_symbol("shop", "Big.Run", start_line=204)
        self.assertEqual(second["read_to"], 256)
        self.assertNotIn("next_start_line", second)
        self.assertEqual(self.reader.read_symbol("shop", "Big")["kind"], "class")

    def test_lookup_errors_are_safe(self) -> None:
        from mcp_read_source.graph import GraphError
        from mcp_read_source.server import SourceReadError

        with self.assertRaises(GraphError) as missing:
            self.reader.trace("shop", "NoSuchThing.AtAll")
        self.assert_no_absolute_root(str(missing.exception))
        with self.assertRaises(GraphError):
            self.reader.outline("shop", "src/Data/Migrations/20240101_Init.Designer.cs")
        with self.assertRaises(SourceReadError):
            self.reader.outline("shop", "../outside.cs")
        with self.assertRaises(SourceReadError):
            self.reader.find_entry("shop", "acb", kind="soap")


if __name__ == "__main__":
    unittest.main()
