"""Query-time resolution over ai-context facts: entries, call targets, trace trees and outlines."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

from mcp_read_source.store import Context


MAX_TRACE_LINES = 3000
MAX_TRACE_DEPTH = 400
MAX_BASE_DEPTH = 6
MEDIATOR_CALLS = {"Send", "Publish", "SendAsync", "PublishAsync", "CreateStream"}
CALLABLE_KINDS = ("method", "property", "minimal_api", "rpc")
TEST_PATH_RE = re.compile(r"(^|/)(tests?|[^/]*\.tests?|[^/]*tests)/", re.IGNORECASE)
GENERIC_PARAM_RE = re.compile(r"^T(\w*)$")
LOCATION_RE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+)$")


class GraphError(ValueError):
    """A safe lookup error for tool callers."""


@dataclass(frozen=True)
class Target:
    method: sqlite3.Row
    via: str = ""


class Graph:
    def __init__(self, context: Context) -> None:
        self.ctx = context

    # --- entries ------------------------------------------------------------------------------

    def entries(self) -> list[dict[str, Any]]:
        if "entries" in self.ctx.shared:
            return self.ctx.shared["entries"]
        result = []
        for row in self.ctx.entry_rows():
            path = self.ctx.file_path(row["file_id"])
            handler = f"{row['type_name']}.{row['name']}" if row["type_kind"] != "minimal_api" else row["name"]
            if row["entry_kind"] == "http":
                route = compose_route(row["type_route"], row["route"], row["type_name"], row["name"])
                name = f"{row['http_method']} {route}"
                keys = [name, route, route.lstrip("/"), handler, row["type_name"], row["name"]]
            elif row["entry_kind"] == "grpc":
                name = f"{row['entry_detail']}.{row['name']}"
                keys = [name, handler, row["type_name"], row["name"], row["entry_detail"]]
            else:
                request = (row["entry_detail"] or "").split(",")[0]
                name = f"{request} -> {handler}"
                keys = [request, handler, row["type_name"]]
            result.append({
                "id": f"{path}:{row['start_line']}", "kind": row["entry_kind"], "name": name, "handler": handler,
                "path": path, "line": row["start_line"], "end_line": row["end_line"],
                "_keys": [key.casefold() for key in keys if key], "_method": row["id"],
            })
        result.sort(key=lambda entry: (entry["kind"], entry["name"]))
        self.ctx.shared["entries"] = result
        return result

    def find_entries(self, query: str, kind: str | None = None, limit: int = 20) -> tuple[list[dict[str, Any]], int]:
        needle = query.strip().casefold()
        candidates = [entry for entry in self.entries() if not kind or entry["kind"] == kind]
        scored = []
        for entry in candidates:
            keys = entry["_keys"]
            if needle in keys:
                scored.append((3.0, entry))
            elif any(needle in key for key in keys):
                scored.append((2.0 + 1.0 / (1 + min(len(key) for key in keys if needle in key)), entry))
        if not scored:
            matcher = SequenceMatcher(None, "", needle)
            for entry in candidates:
                best = 0.0
                for key in entry["_keys"]:
                    matcher.set_seq1(key)
                    if matcher.real_quick_ratio() > best and matcher.quick_ratio() > best:
                        best = max(best, matcher.ratio())
                if best >= 0.6:
                    scored.append((best, entry))
        scored.sort(key=lambda item: (-item[0], item[1]["name"]))
        return [_public_entry(entry, score) for score, entry in scored[:limit]], len(scored)

    # --- symbol lookup ------------------------------------------------------------------------

    def locate(self, spec: str) -> dict[str, Any]:
        """Resolve a symbol spec to one method/type, or raise GraphError listing candidates."""
        spec = spec.strip()
        location = LOCATION_RE.match(spec)
        if location:
            return self._locate_line(location.group("path").replace("\\", "/"), int(location.group("line")))
        if re.fullmatch(r"[\w<>`]+(\.[\w<>`]+)?", spec):
            type_name, _, member = spec.partition(".")
            rows = self.ctx.methods_named(type_name, member) if member else []
            if len(rows) == 1:
                return self._method_symbol(rows[0])
            if not member:
                types = self.ctx.types_named(type_name)
                if len(types) == 1:
                    return self._type_symbol(types[0])
                if types:
                    raise GraphError(_candidates("Several types match", [self._type_symbol(t) for t in types]))
            if rows:
                raise GraphError(_candidates("Several methods match", [self._method_symbol(r) for r in rows]))
        entries, _ = self.find_entries(spec, limit=5)
        exact = [entry for entry in entries if entry["score"] >= 3]
        clear_leader = entries and entries[0]["score"] >= 2 and (len(entries) == 1 or entries[1]["score"] < 2)
        chosen = exact[0] if len(exact) == 1 else entries[0] if not exact and (len(entries) == 1 or clear_leader) else None
        if chosen is not None:
            return self._method_symbol(self.ctx.method(self._entry_method(chosen)))
        if entries:
            raise GraphError(_candidates("Several entries match; pass one id", entries))
        raise GraphError("No indexed symbol or entry matches")

    def _entry_method(self, public_entry: dict[str, Any]) -> int:
        return next(e["_method"] for e in self.entries() if e["id"] == public_entry["id"])

    def _locate_line(self, path: str, line: int) -> dict[str, Any]:
        record = self.ctx.file_record(path)
        if record is None:
            raise GraphError("File is not indexed")
        containing = [m for m in self.ctx.methods_in_file(record["id"]) if m["start_line"] <= line <= m["end_line"]]
        if containing:
            return self._method_symbol(min(containing, key=lambda m: m["end_line"] - m["start_line"]))
        types = [t for t in self.ctx.types_in_file(record["id"]) if t["start_line"] <= line <= t["end_line"]]
        if types:
            return self._type_symbol(min(types, key=lambda t: t["end_line"] - t["start_line"]))
        raise GraphError("No indexed method or type at that line")

    def _method_symbol(self, method: sqlite3.Row) -> dict[str, Any]:
        return {"symbol": self.label(method), "kind": method["kind"], "path": self.ctx.file_path(method["file_id"]),
                "start_line": method["start_line"], "end_line": method["end_line"], "signature": method["signature"],
                "_method": method["id"]}

    def _type_symbol(self, type_row: sqlite3.Row) -> dict[str, Any]:
        return {"symbol": type_row["name"], "kind": type_row["kind"], "path": self.ctx.file_path(type_row["file_id"]),
                "start_line": type_row["start_line"], "end_line": type_row["end_line"],
                "signature": f"{type_row['kind']} {type_row['name']}"}

    # --- outline ------------------------------------------------------------------------------

    def outline(self, path: str) -> list[str]:
        record = self.ctx.file_record(path)
        if record is None:
            raise GraphError("File is not indexed")
        lines = []
        methods = self.ctx.methods_in_file(record["id"])
        for type_row in self.ctx.types_in_file(record["id"]):
            route = f"  [Route {type_row['route']}]" if type_row["route"] else ""
            lines.append(f"{type_row['kind']} {type_row['name']}  L{type_row['start_line']}-{type_row['end_line']}{route}")
            for method in methods:
                if method["type_id"] == type_row["id"]:
                    http = f"  [{method['http_method']} {method['route'] or ''}]".rstrip() if method["http_method"] else ""
                    http = http + "]" if http and not http.endswith("]") else http
                    lines.append(f"  {method['signature']}  L{method['start_line']}-{method['end_line']}{http}")
        return lines

    # --- trace --------------------------------------------------------------------------------

    def label(self, method: sqlite3.Row) -> str:
        type_row = self.ctx.type_by_id(method["type_id"])
        if method["kind"] == "minimal_api":
            return method["name"]
        suffix = " (proto)" if method["kind"] == "rpc" else ""
        return f"{type_row['name']}.{method['name']}{suffix}"

    def location(self, method: sqlite3.Row) -> str:
        return f"{self.ctx.file_path(method['file_id'])}:{method['start_line']}-{method['end_line']}"

    def trace(self, method_id: int, *, max_depth: int | None = None,
              include_external: bool = False) -> tuple[list[str], dict[str, Any]]:
        root = self.ctx.method(method_id)
        lines = [f"{self.label(root)}  {self.location(root)}"]
        stats = {"nodes": 1, "external_calls": 0, "ambiguous_calls": 0, "truncated": False}
        expanded: set[int] = set()
        limit = MAX_TRACE_DEPTH if max_depth is None else min(max_depth, MAX_TRACE_DEPTH)
        self._walk(root, "", 0, {root["id"]}, expanded, lines, stats, limit, include_external)
        return lines, stats

    def _walk(self, method: sqlite3.Row, prefix: str, depth: int, stack: set[int], expanded: set[int],
              lines: list[str], stats: dict, limit: int, include_external: bool) -> None:
        expanded.add(method["id"])
        children, externals = self.children(method)
        stats["external_calls"] += len(externals)
        rows: list[tuple[str, Target | None, bool]] = []
        for call_lines, target, ambiguous in children:
            rows.append((call_lines, target, ambiguous))
            stats["ambiguous_calls"] += int(ambiguous)
        if include_external:
            rows.extend((f"L{call['line']}", None, False) for call in externals)
        elif externals:
            rows.append(("", None, False))
        for index, (call_lines, target, ambiguous) in enumerate(rows):
            if len(lines) >= MAX_TRACE_LINES:
                stats["truncated"] = True
                return
            last = index == len(rows) - 1
            branch = prefix + ("└─ " if last else "├─ ")
            head = f"{call_lines} " if call_lines else ""
            if target is None:
                if include_external and call_lines:
                    call = externals[index - len(children)]
                    receiver = f"{call['receiver']}." if call["receiver"] else ""
                    lines.append(f"{branch}{head}{receiver}{call['name']}  ✗ external")
                else:
                    lines.append(f"{branch}(+{len(externals)} external calls)")
                continue
            child = target.method
            text = f"{branch}{head}{self.label(child)}  {self.location(child)}"
            if target.via:
                text += f"  [{target.via}]"
            if ambiguous:
                text += "  ? ambiguous"
            stats["nodes"] += 1
            if child["id"] in stack:
                lines.append(f"{text}  ⟲ recursive")
            elif child["id"] in expanded:
                lines.append(f"{text}  ↺ see above")
            elif depth + 1 >= limit:
                lines.append(f"{text}  … depth limit")
            else:
                lines.append(text)
                self._walk(child, prefix + ("   " if last else "│  "), depth + 1, stack | {child["id"]},
                           expanded, lines, stats, limit, include_external)

    def children(self, method: sqlite3.Row) -> tuple[list[tuple[str, Target, bool]], list[sqlite3.Row]]:
        if method["kind"] == "rpc":
            return [("", target, False) for target in self.rpc_implementations(method)], []
        grouped: dict[int, tuple[list[int], Target, bool]] = {}
        externals = []
        for call in self.ctx.calls(method["id"]):
            targets = self.resolve(method, call)
            if not targets:
                externals.append(call)
                continue
            for target in targets:
                entry = grouped.setdefault(target.method["id"], ([], target, len(targets) > 1))
                if call["line"] not in entry[0]:
                    entry[0].append(call["line"])
        children = [(",".join(f"L{n}" for n in call_lines), target, ambiguous)
                    for call_lines, target, ambiguous in grouped.values()]
        return children, externals

    def rpc_implementations(self, rpc: sqlite3.Row) -> list[Target]:
        service = self.ctx.type_by_id(rpc["type_id"])["name"]
        targets = []
        for derived in self.ctx.derived(f"{service}Base"):
            for method in self.ctx.methods_of(derived["id"], rpc["name"]):
                targets.append(Target(method, "gRPC server"))
        return _prefer_non_test(self.ctx, targets)

    # --- resolution ---------------------------------------------------------------------------

    def resolve(self, caller: sqlite3.Row, call: sqlite3.Row) -> list[Target]:
        name, argc, arg0 = call["name"], call["argc"], call["arg0_type"]
        caller_type = self.ctx.type_by_id(caller["type_id"])
        if name in MEDIATOR_CALLS and arg0:
            handlers = self.ctx.handlers_for(arg0)
            if handlers:
                return _prefer_non_test(self.ctx, [Target(h, f"MediatR {arg0}") for h in handlers])
        receiver = call["receiver"]
        targets: list[Target] = []
        if not call["receiver_type"] and receiver in ("", "this"):
            targets = [Target(m) for m in self._find_in_lineage(self._lineage(caller_type), name, argc)]
        elif not call["receiver_type"] and receiver == "base":
            bases = [t for t in self._lineage(caller_type) if t["id"] not in {f["id"] for f in self._family(caller_type)}]
            targets = [Target(m) for m in self._find_in_lineage(bases, name, argc)]
        else:
            for type_name, type_args in self._receiver_types(caller, caller_type, call):
                targets.extend(self._methods_on(type_name, type_args, name, argc, caller, caller_type))
        if not targets and receiver not in ("", "this", "base"):
            extensions = [m for m in self.ctx.extension_methods(name) if _accepts(m, argc + 1 if argc >= 0 else -1)]
            visible = self._visible(caller["file_id"], caller_type["namespace"])
            extensions = [m for m in extensions if self.ctx.type_by_id(m["type_id"])["namespace"] in visible] or extensions
            if 1 <= len(extensions) <= 3:
                targets = [Target(m, "extension") for m in extensions]
        unique: dict[int, Target] = {}
        for target in targets:
            unique.setdefault(target.method["id"], target)
        return list(unique.values())

    def _receiver_types(self, caller: sqlite3.Row, caller_type: sqlite3.Row,
                        call: sqlite3.Row) -> list[tuple[str, str]]:
        if call["receiver_type"]:
            return [(call["receiver_type"], "")]
        receiver = call["receiver"]
        if receiver == "?":
            return []
        segments = receiver.split(".")
        current = self._member_type(caller_type, segments[0])
        if current is None:
            for index in range(len(segments) - 1, -1, -1):
                if self.ctx.types_named(segments[index]):
                    current, segments = (segments[index], ""), segments[index:]
                    break
            else:
                return []
        for segment in segments[1:]:
            found = None
            for type_row in self._pick(self.ctx.types_named(current[0]), caller):
                found = self._member_type(type_row, segment)
                if found:
                    break
            if found is None:
                if not self.ctx.types_named(segment):
                    return []
                found = (segment, "")
            current = found
        return [current]

    def _methods_on(self, type_name: str, type_args: str, name: str, argc: int,
                    caller: sqlite3.Row, caller_type: sqlite3.Row) -> list[Target]:
        if type_name.endswith("Client") and len(type_name) > len("Client"):
            rpc_name = name[:-5] if name.endswith("Async") else name
            rpcs = self.ctx.proto_rpc(type_name[:-6], rpc_name)
            if rpcs:
                return [Target(rpc, "gRPC client") for rpc in rpcs]
        targets = []
        for type_row in self._pick(self.ctx.types_named(type_name), caller):
            if type_row["kind"] == "interface":
                registered = bool(self.ctx.registered_impls(type_row["name"]))
                via = f"{'DI' if registered else 'impl'} {type_row['name']}"
                for impl in self._implementations(type_row, type_args, caller):
                    targets.extend(Target(m, via) for m in self._find_in_lineage(self._lineage(impl), name, argc))
                continue
            for method in self._find_in_lineage(self._lineage(type_row), name, argc):
                if " abstract " in f" {method['signature']} ":
                    overrides = [Target(m, f"override of {type_row['name']}")
                                 for derived in self.ctx.derived(type_row["name"])
                                 for m in self.ctx.methods_of(derived["id"], name) if m["override"]]
                    targets.extend(overrides or [Target(method)])
                else:
                    targets.append(Target(method))
        return _prefer_non_test(self.ctx, targets)

    def _implementations(self, interface: sqlite3.Row, type_args: str, caller: sqlite3.Row,
                         depth: int = 0) -> list[sqlite3.Row]:
        registered = [t for name in self.ctx.registered_impls(interface["name"])
                      for t in self._pick(self.ctx.types_named(name), caller) if t["kind"] != "interface"]
        if registered:
            return registered
        derived = list(self.ctx.derived(interface["name"]))
        if type_args:
            exact = [d for d in derived if d["base_args"] == type_args]
            generic = [d for d in derived if d["base_args"] and all(
                GENERIC_PARAM_RE.match(arg) for arg in d["base_args"].split(","))]
            derived = exact or generic or derived
        result = [d for d in derived if d["kind"] != "interface"]
        if depth < 2:
            for sub in (d for d in derived if d["kind"] == "interface"):
                result.extend(self._implementations(sub, "", caller, depth + 1))
        return result

    def _member_type(self, type_row: sqlite3.Row, name: str) -> tuple[str, str] | None:
        for owner in self._lineage(type_row):
            rows = self.ctx.members(owner["id"], name)
            if rows and rows[0]["type_name"]:
                return rows[0]["type_name"], rows[0]["type_args"] or ""
        return None

    def _find_in_lineage(self, lineage: list[sqlite3.Row], name: str, argc: int) -> list[sqlite3.Row]:
        fallback: list[sqlite3.Row] = []
        for type_row in lineage:
            found = [m for m in self.ctx.methods_of(type_row["id"], name) if m["kind"] in CALLABLE_KINDS]
            matched = [m for m in found if _accepts(m, argc)]
            if matched:
                return matched
            if found and not fallback:
                fallback = found
        return fallback

    def _family(self, type_row: sqlite3.Row) -> list[sqlite3.Row]:
        return [t for t in self.ctx.types_named(type_row["name"])
                if t["namespace"] == type_row["namespace"] and t["kind"] == type_row["kind"]] or [type_row]

    def _lineage(self, type_row: sqlite3.Row) -> list[sqlite3.Row]:
        key = ("lineage", type_row["id"])
        cache = self.ctx._cache
        if key in cache:
            return cache[key]
        seen: dict[int, sqlite3.Row] = {}
        level = self._family(type_row)
        for _ in range(MAX_BASE_DEPTH):
            next_level = []
            for row in level:
                if row["id"] in seen:
                    continue
                seen[row["id"]] = row
                for base in self.ctx.bases(row["id"]):
                    for base_row in self._pick(self.ctx.types_named(base["base_simple"]), None, row):
                        if base_row["kind"] in ("class", "record", "struct") and base_row["id"] not in seen:
                            next_level.extend(self._family(base_row))
            if not next_level:
                break
            level = next_level
        cache[key] = list(seen.values())
        return cache[key]

    def _pick(self, rows: list[sqlite3.Row], caller: sqlite3.Row | None,
              owner: sqlite3.Row | None = None) -> list[sqlite3.Row]:
        if len(rows) <= 1:
            return rows
        if caller is not None:
            visible = self._visible(caller["file_id"], self.ctx.type_by_id(caller["type_id"])["namespace"])
        elif owner is not None:
            visible = self._visible(owner["file_id"], owner["namespace"])
        else:
            return rows
        preferred = [r for r in rows if r["namespace"] in visible]
        return preferred or rows

    def _visible(self, file_id: int, namespace: str) -> set[str]:
        """Namespaces a file can reference without qualification: its usings plus its enclosing namespaces."""
        key = ("visible", file_id, namespace)
        cache = self.ctx._cache
        if key not in cache:
            parts = namespace.split(".") if namespace else []
            cache[key] = self.ctx.usings(file_id) | {".".join(parts[:i]) for i in range(1, len(parts) + 1)}
        return cache[key]


def compose_route(type_route: str | None, method_route: str | None, type_name: str, method_name: str) -> str:
    controller = type_name[:-len("Controller")] if type_name.endswith("Controller") else type_name
    method_route = method_route or ""
    if method_route.startswith(("/", "~/")):
        route = "/" + method_route.lstrip("~/")
    else:
        parts = [part.strip("/") for part in (type_route or "", method_route) if part and part.strip("/")]
        route = "/" + "/".join(parts)
    route = re.sub(r"\[controller\]", controller, route, flags=re.IGNORECASE)
    return re.sub(r"\[action\]", method_name, route, flags=re.IGNORECASE)


def _accepts(method: sqlite3.Row, argc: int) -> bool:
    if argc < 0:
        return True
    return method["required"] <= argc and (argc <= method["params"] or bool(method["variadic"]))


def _prefer_non_test(context: Context, targets: list[Target]) -> list[Target]:
    production = [t for t in targets if not TEST_PATH_RE.search(context.file_path(t.method["file_id"]))]
    return production or targets


def _public_entry(entry: dict[str, Any], score: float) -> dict[str, Any]:
    public = {key: value for key, value in entry.items() if not key.startswith("_")}
    public["score"] = round(score, 2)
    return public


def _candidates(message: str, items: list[dict[str, Any]]) -> str:
    listed = "; ".join(
        "{} @ {}".format(item.get("name") or item.get("symbol"),
                         item.get("id") or "{}:{}".format(item["path"], item["start_line"]))
        for item in items[:10]
    )
    return f"{message}: {listed}"
