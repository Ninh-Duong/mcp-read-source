"""Turn one source file into per-file facts for the ai-context store.

Facts only describe what the file itself declares; cross-file resolution happens at query time.
Every function here is pure so batches can run in worker processes.
"""
from __future__ import annotations

import bisect
import re
from typing import Any

import tree_sitter_c_sharp
from tree_sitter import Language, Node, Parser, Query, QueryCursor


PARSER_VERSION = "1"
INDEXED_SUFFIXES = (".cs", ".proto")
HTTP_ATTRIBUTES = {
    "httpget": "GET", "httppost": "POST", "httpput": "PUT", "httpdelete": "DELETE",
    "httppatch": "PATCH", "httphead": "HEAD", "httpoptions": "OPTIONS",
}
REGISTRATION_KINDS = {
    "addscoped": "scoped", "addtransient": "transient", "addsingleton": "singleton",
    "tryaddscoped": "scoped", "tryaddtransient": "transient", "tryaddsingleton": "singleton",
    "addgrpcclient": "grpc_client",
}
MAP_VERBS = {"mapget": "GET", "mappost": "POST", "mapput": "PUT", "mapdelete": "DELETE", "mappatch": "PATCH"}
TYPE_KINDS = {
    "class_declaration": "class", "interface_declaration": "interface", "struct_declaration": "struct",
    "record_declaration": "record", "record_struct_declaration": "record", "enum_declaration": "enum",
}
MAX_RECEIVER = 120
MAX_SIGNATURE = 300

_LANGUAGE = Language(tree_sitter_c_sharp.language())
_CALL_QUERY = Query(_LANGUAGE, "(invocation_expression) @call")
_LOCAL_QUERY = Query(_LANGUAGE, """
(local_declaration_statement (variable_declaration type: (_) @type (variable_declarator name: (identifier) @name)))
(foreach_statement type: (_) @type left: (identifier) @name)
(variable_declarator name: (identifier) @name (object_creation_expression type: (_) @type))
""")
_parser: Parser | None = None


def parse_file(relative: str, data: bytes) -> dict[str, Any]:
    if relative.endswith(".proto"):
        return parse_proto(data.decode("utf-8"))
    return parse_csharp(data)


def parse_batch(items: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
    return [parse_file(relative, data) for relative, data in items]


# --- C# ---------------------------------------------------------------------------------------

def parse_csharp(data: bytes) -> dict[str, Any]:
    global _parser
    if _parser is None:
        _parser = Parser(_LANGUAGE)
    tree = _parser.parse(data)
    facts: dict[str, Any] = {
        "lang": "cs", "line_count": data.count(b"\n") + (0 if data.endswith(b"\n") else 1),
        "usings": [], "types": [], "registrations": [],
    }
    methods: list[dict[str, Any]] = []
    _collect_declarations(tree.root_node, "", None, facts, methods)
    _collect_bodies(tree.root_node, facts, methods)
    return facts


def _collect_declarations(node: Node, namespace: str, owner: dict | None, facts: dict, methods: list) -> None:
    for child in node.named_children:
        kind = child.type
        if kind == "using_directive":
            name = child.named_children[-1] if child.named_children else None
            if name is not None and name.type in ("identifier", "qualified_name"):
                facts["usings"].append(_text(name))
        elif kind == "file_scoped_namespace_declaration":
            namespace = _text(child.child_by_field_name("name"))
        elif kind == "namespace_declaration":
            nested = _text(child.child_by_field_name("name"))
            body = child.child_by_field_name("body")
            if body is not None:
                _collect_declarations(body, f"{namespace}.{nested}" if namespace else nested, None, facts, methods)
        elif kind in TYPE_KINDS:
            _collect_type(child, namespace, facts, methods)
        elif owner is not None and kind in ("method_declaration", "constructor_declaration", "property_declaration"):
            _collect_member(child, owner, methods)
        elif owner is not None and kind == "field_declaration":
            declaration = next((c for c in child.named_children if c.type == "variable_declaration"), None)
            if declaration is not None:
                type_name, type_args = _type_name(declaration.child_by_field_name("type"))
                for declarator in declaration.named_children:
                    if declarator.type == "variable_declarator":
                        owner["members"].append([_text(declarator.child_by_field_name("name")), type_name, type_args])


def _collect_type(node: Node, namespace: str, facts: dict, methods: list) -> None:
    attributes = _attributes(node)
    record = {
        "name": _text(node.child_by_field_name("name")), "namespace": namespace, "kind": TYPE_KINDS[node.type],
        "start": node.start_point[0] + 1, "end": node.end_point[0] + 1,
        "route": attributes.get("route"), "bases": [], "members": [], "methods": [],
    }
    for child in node.named_children:
        if child.type == "base_list":
            for base in child.named_children:
                target = base.named_children[0] if base.type == "primary_constructor_base_type" and base.named_children else base
                record["bases"].append(list(_type_name(target)))
        elif child.type == "parameter_list":
            for name, type_name, type_args, *_ in _parameters(child):
                record["members"].append([name, type_name, type_args])
    facts["types"].append(record)
    body = node.child_by_field_name("body")
    if body is not None and record["kind"] != "enum":
        _collect_declarations(body, namespace, record, facts, methods)


def _collect_member(node: Node, owner: dict, methods: list) -> None:
    kind = node.type
    attributes = _attributes(node)
    name = _text(node.child_by_field_name("name"))
    if kind == "property_declaration":
        type_name, type_args = _type_name(node.child_by_field_name("type"))
        owner["members"].append([name, type_name, type_args])
        accessors = node.child_by_field_name("accessors")
        has_body = node.child_by_field_name("value") is not None and node.child_by_field_name("value").type == "arrow_expression_clause"
        if accessors is not None:
            has_body = has_body or any(
                body.type in ("block", "arrow_expression_clause")
                for accessor in accessors.named_children for body in accessor.named_children
            )
        if not has_body:
            return
        params, method_kind = [], "property"
    else:
        params_node = node.child_by_field_name("parameters")
        params = _parameters(params_node) if params_node is not None else []
        method_kind = "constructor" if kind == "constructor_declaration" else "method"
        if kind == "constructor_declaration":
            for param_name, type_name, type_args, *_ in params:
                owner["members"].append([param_name, type_name, type_args])
    modifiers = {_text(c) for c in node.named_children if c.type == "modifier"}
    method = {
        "name": name, "kind": method_kind,
        "params": len(params), "required": sum(1 for p in params if p[3]),
        "variadic": int(any(p[4] for p in params)), "extension": int(bool(params) and params[0][5]),
        "override": int("override" in modifiers), "signature": _signature(node),
        "start": node.start_point[0] + 1, "end": node.end_point[0] + 1,
        "http": attributes.get("http"), "route": attributes.get("http_route") or attributes.get("route"),
        "locals": {p[0]: p[1] for p in params if p[1]}, "calls": [],
        "_range": (node.start_byte, node.end_byte),
    }
    if method["http"] is None and attributes.get("route") is not None:
        method["http"] = "ANY"
    owner["methods"].append(method)
    methods.append(method)


def _collect_bodies(root: Node, facts: dict, methods: list) -> None:
    methods.sort(key=lambda item: item["_range"][0])
    starts = [item["_range"][0] for item in methods]
    calls = [(node, _call(node)) for node in QueryCursor(_CALL_QUERY).captures(root).get("call", [])]
    for node, call in calls:
        if call is None:
            continue
        lowered = call[1].casefold()
        if lowered in REGISTRATION_KINDS or lowered in MAP_VERBS:
            synthetic = _registration(node, lowered, call[2], facts)
            if synthetic is not None:
                index = bisect.bisect_right(starts, synthetic["_range"][0])
                starts.insert(index, synthetic["_range"][0])
                methods.insert(index, synthetic)
    for _pattern, match in QueryCursor(_LOCAL_QUERY).matches(root):
        type_name, _ = _type_name(match["type"][0])
        for name_node in match["name"]:
            owner = _owner(methods, starts, name_node.start_byte)
            if owner is not None and type_name and type_name != "var":
                owner["locals"].setdefault(_text(name_node), type_name)
    for node, call in calls:
        owner = _owner(methods, starts, node.start_byte)
        if call is None or owner is None:
            continue
        receiver, name, arguments = call
        args = [a for a in arguments.named_children if a.type == "argument"] if arguments is not None else []
        receiver_type = owner["locals"].get(receiver, "") if receiver and "." not in receiver else ""
        owner["calls"].append([
            node.start_point[0] + 1, receiver, receiver_type, name, len(args), _argument_type(args, owner),
        ])
    for method in methods:
        method["calls"].sort(key=lambda call: call[0])
        method.pop("_range", None)
        method.pop("locals", None)


def _owner(methods: list, starts: list, position: int) -> dict | None:
    index = bisect.bisect_right(starts, position) - 1
    steps = 0
    while index >= 0 and steps < 64:
        start, end = methods[index]["_range"]
        if start <= position < end:
            return methods[index]
        index -= 1
        steps += 1
    return None


def _call(node: Node) -> tuple[str, str, Node | None] | None:
    function = node.child_by_field_name("function")
    arguments = node.child_by_field_name("arguments")
    if function is None:
        return None
    if function.type == "identifier":
        return "", _text(function), arguments
    if function.type == "generic_name":
        return "", _text(function.named_children[0]), arguments
    if function.type == "member_access_expression":
        name_node = function.child_by_field_name("name")
        expression = function.child_by_field_name("expression")
        name = _text(name_node.named_children[0]) if name_node.type == "generic_name" else _text(name_node)
        return _receiver(expression), name, arguments
    if function.type == "conditional_access_expression":
        binding = next((c for c in function.named_children if c.type == "member_binding_expression"), None)
        if binding is None:
            return None
        name_node = binding.child_by_field_name("name")
        name = _text(name_node.named_children[0]) if name_node.type == "generic_name" else _text(name_node)
        return _receiver(function.child_by_field_name("condition")), name, arguments
    return None


def _receiver(expression: Node | None) -> str:
    if expression is None:
        return ""
    if expression.type in ("this_expression", "this"):
        return "this"
    if expression.type in ("base_expression", "base"):
        return "base"
    if expression.type == "generic_name":
        return _text(expression.named_children[0])
    if expression.type == "alias_qualified_name":
        return _text(expression.child_by_field_name("name"))
    if expression.type in ("identifier", "member_access_expression", "qualified_name"):
        text = re.sub(r"\s+", "", _text(expression))
        if text.startswith("this."):
            text = text[5:]
        return text[:MAX_RECEIVER] if re.fullmatch(r"[\w.]+", text) else "?"
    return "?"


def _argument_type(args: list[Node], owner: dict) -> str:
    if not args:
        return ""
    value = args[0].named_children[-1] if args[0].named_children else None
    if value is None:
        return ""
    if value.type == "object_creation_expression":
        return _type_name(value.child_by_field_name("type"))[0]
    if value.type == "identifier":
        return owner["locals"].get(_text(value), "")
    return ""


def _registration(node: Node, lowered: str, arguments: Node | None, facts: dict) -> dict | None:
    function = node.child_by_field_name("function")
    name_node = function.child_by_field_name("name") if function.type == "member_access_expression" else function
    line = node.start_point[0] + 1
    if lowered in REGISTRATION_KINDS:
        types: list[str] = []
        if name_node is not None and name_node.type == "generic_name":
            type_args = next((c for c in name_node.named_children if c.type == "type_argument_list"), None)
            types = [_type_name(t)[0] for t in type_args.named_children] if type_args is not None else []
        elif arguments is not None:
            for argument in arguments.named_children:
                value = argument.named_children[-1] if argument.named_children else None
                if value is not None and value.type == "typeof_expression":
                    types.append(_type_name(value.child_by_field_name("type"))[0])
        if types:
            facts["registrations"].append([line, REGISTRATION_KINDS[lowered], types[0], types[-1], None])
        return None
    args = [a for a in arguments.named_children if a.type == "argument"] if arguments is not None else []
    route = _string_value(args[0].named_children[-1]) if args and args[0].named_children else None
    if route is None:
        return None
    verb = MAP_VERBS[lowered]
    facts["registrations"].append([line, f"map_{verb.lower()}", None, None, route])
    handler = args[1].named_children[-1] if len(args) > 1 and args[1].named_children else None
    method = {
        "name": f"{verb} {route}", "kind": "minimal_api", "params": 0, "required": 0, "variadic": 0,
        "extension": 0, "override": 0, "signature": f"{verb} {route}",
        "start": line, "end": node.end_point[0] + 1, "http": verb, "route": route, "locals": {}, "calls": [],
        "_range": (handler.start_byte, handler.end_byte) if handler is not None and handler.type == "lambda_expression"
        else (-2, -1),
    }
    if handler is not None and handler.type == "lambda_expression":
        lambda_params = handler.child_by_field_name("parameters")
        if lambda_params is not None and lambda_params.type == "parameter_list":
            method["locals"] = {p[0]: p[1] for p in _parameters(lambda_params) if p[1]}
    elif handler is not None:
        receiver = _receiver(handler)
        if receiver not in ("", "?"):
            head, _, tail = receiver.rpartition(".")
            method["calls"].append([line, head, "", tail or head, -1, ""])
    container = next((t for t in facts["types"] if t["name"] == "MinimalApi" and t["kind"] == "minimal_api"), None)
    if container is None:
        container = {"name": "MinimalApi", "namespace": "", "kind": "minimal_api", "start": line, "end": line,
                     "route": None, "bases": [], "members": [], "methods": []}
        facts["types"].append(container)
    container["methods"].append(method)
    container["start"], container["end"] = min(container["start"], line), max(container["end"], method["end"])
    return method


def _attributes(node: Node) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for attribute_list in node.named_children:
        if attribute_list.type != "attribute_list":
            continue
        for attribute in attribute_list.named_children:
            if attribute.type != "attribute":
                continue
            name = _type_name(attribute.child_by_field_name("name"))[0].casefold().removesuffix("attribute")
            arguments = next((c for c in attribute.named_children if c.type == "attribute_argument_list"), None)
            value = None
            if arguments is not None:
                for argument in arguments.named_children:
                    if argument.named_children and not any(c.type == "name_equals" for c in argument.named_children):
                        value = _string_value(argument.named_children[-1])
                        break
            if name == "route":
                found["route"] = value if value is not None else ""
            elif name in HTTP_ATTRIBUTES and "http" not in found:
                found["http"] = HTTP_ATTRIBUTES[name]
                found["http_route"] = value
    return found


def _parameters(node: Node) -> list[tuple[str, str, str, bool, bool, bool]]:
    result = []
    for parameter in node.named_children:
        if parameter.type != "parameter":
            continue
        type_name, type_args = _type_name(parameter.child_by_field_name("type"))
        tokens = {c.type for c in parameter.children}
        modifiers = {_text(c) for c in parameter.named_children if c.type == "modifier"}
        result.append((
            _text(parameter.child_by_field_name("name")), type_name, type_args,
            "=" not in tokens and "params" not in tokens and "params" not in modifiers,
            "params" in tokens or "params" in modifiers, "this" in modifiers,
        ))
    return result


def _signature(node: Node) -> str:
    start = next((c.start_byte for c in node.children if c.type != "attribute_list"), node.start_byte)
    end_node = node.child_by_field_name("parameters") or node.child_by_field_name("name")
    end = end_node.end_byte if end_node is not None else node.end_byte
    text = node.text[start - node.start_byte:end - node.start_byte].decode("utf-8", "replace")
    return re.sub(r"\s+", " ", text).strip()[:MAX_SIGNATURE]


def _type_name(node: Node | None) -> tuple[str, str]:
    if node is None:
        return "", ""
    kind = node.type
    if kind == "generic_name":
        args = next((c for c in node.named_children if c.type == "type_argument_list"), None)
        arg_names = ",".join(_type_name(c)[0] for c in args.named_children) if args is not None else ""
        return _text(node.named_children[0]), arg_names
    if kind == "qualified_name" or kind == "alias_qualified_name":
        return _type_name(node.child_by_field_name("name"))
    if kind in ("nullable_type", "array_type", "pointer_type", "ref_type", "scoped_type"):
        inner = node.child_by_field_name("type") or (node.named_children[0] if node.named_children else None)
        return _type_name(inner)
    if kind == "implicit_type":
        return "var", ""
    return _text(node), ""


def _string_value(node: Node | None) -> str | None:
    if node is None:
        return None
    if node.type == "string_literal":
        return "".join(_text(c) for c in node.named_children if c.type == "string_literal_content")
    if node.type == "verbatim_string_literal":
        return _text(node)[2:-1]
    if node.type == "raw_string_literal":
        return _text(node).strip('"')
    return None


def _text(node: Node | None) -> str:
    return node.text.decode("utf-8", "replace") if node is not None else ""


# --- proto ------------------------------------------------------------------------------------

_PROTO_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_PROTO_BLOCK = re.compile(r"\b(service|message)\s+(\w+)\s*\{")
_PROTO_RPC = re.compile(
    r"\brpc\s+(\w+)\s*\(\s*(stream\s+)?([\w.]+)\s*\)\s*returns\s*\(\s*(stream\s+)?([\w.]+)\s*\)"
)


def parse_proto(text: str) -> dict[str, Any]:
    clean = _PROTO_COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), text)
    facts: dict[str, Any] = {
        "lang": "proto", "line_count": text.count("\n") + (0 if text.endswith("\n") else 1),
        "usings": [], "types": [], "registrations": [],
    }
    package = re.search(r"\bpackage\s+([\w.]+)\s*;", clean)
    namespace = package.group(1) if package else ""
    for match in _PROTO_BLOCK.finditer(clean):
        end = _closing_brace(clean, match.end() - 1)
        kind, name = match.group(1), match.group(2)
        record = {
            "name": name, "namespace": namespace, "kind": f"proto_{kind}",
            "start": clean.count("\n", 0, match.start()) + 1, "end": clean.count("\n", 0, end) + 1,
            "route": None, "bases": [], "members": [], "methods": [],
        }
        if kind == "service":
            for rpc in _PROTO_RPC.finditer(clean, match.end(), end):
                line = clean.count("\n", 0, rpc.start()) + 1
                request = ("stream " if rpc.group(2) else "") + rpc.group(3)
                response = ("stream " if rpc.group(4) else "") + rpc.group(5)
                record["methods"].append({
                    "name": rpc.group(1), "kind": "rpc", "params": 1, "required": 1, "variadic": 0,
                    "extension": 0, "override": 0, "signature": f"rpc {rpc.group(1)}({request}) returns ({response})",
                    "start": line, "end": clean.count("\n", 0, rpc.end()) + 1,
                    "http": None, "route": None, "calls": [],
                })
        facts["types"].append(record)
    return facts


def _closing_brace(text: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(text) - 1
