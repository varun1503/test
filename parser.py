"""
parser.py — Deterministic layer.

Uses tree-sitter-java to build the ground-truth skeleton the LLM is NOT allowed to
invent: the inventory of data-element variables ("PD variables"), where each is read
and written, the method-call graph, and Vert.x EventBus wiring (consumer/send by
address string). Everything here is grounded in exact file:line:col spans.

The LLM layer (agents.py) only *names* and *classifies* transformations over these
facts, and bridges framework indirection it cannot resolve statically.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from typing import Optional

from tree_sitter import Language, Parser, Query, QueryCursor
import tree_sitter_java as tsjava

_LANG = Language(tsjava.language())
_PARSER = Parser(_LANG)


# ---------- grounded fact types -------------------------------------------------

@dataclass(frozen=True)
class CodeRef:
    """A verbatim, checkable pointer into source. Every emitted lineage hop must
    carry at least one of these (see validate.py)."""
    file: str
    start_line: int   # 1-indexed
    end_line: int
    symbol: str
    snippet: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class DataElement:
    """A discovered data-carrying variable/field/param, or a string-keyed payload
    field like requestBody.getString("applicationIdentifier")."""
    name: str
    kind: str                    # field | local | param | payload_key
    java_type: Optional[str]
    file: str
    decl: Optional[CodeRef]
    reads: list[CodeRef] = field(default_factory=list)
    writes: list[CodeRef] = field(default_factory=list)
    enclosing_method: Optional[str] = None
    enclosing_class: Optional[str] = None

    @property
    def qualified(self) -> str:
        c = self.enclosing_class or os.path.basename(self.file).removesuffix(".java")
        return f"{c}.{self.name}"


@dataclass
class CallEdge:
    caller: str          # ClassName.methodName
    callee: str          # best-effort resolved method name / expression
    ref: CodeRef


@dataclass
class BusEdge:
    """Vert.x EventBus wiring. kind in {consumer, send, request, publish}.
    Static analysis cannot link a send('addr') to a consumer('addr'); we surface the
    address string so the LLM/graph layer can join producers to consumers."""
    kind: str
    address: Optional[str]       # literal address if resolvable, else None
    enclosing_class: str
    enclosing_method: str
    ref: CodeRef


@dataclass
class ParsedFile:
    path: str
    package: Optional[str]
    classes: list[str]
    data_elements: list[DataElement]
    calls: list[CallEdge]
    bus_edges: list[BusEdge]
    constants: dict[str, str] = field(default_factory=dict)


# ---------- tree-sitter helpers -------------------------------------------------

def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf8", "replace")

def _ref(node, src: bytes, path: str, symbol: str) -> CodeRef:
    return CodeRef(
        file=path,
        start_line=node.start_point[0] + 1,
        end_line=node.end_point[0] + 1,
        symbol=symbol,
        snippet=_text(node, src)[:400],
    )

def _query(pattern: str):
    q = Query(_LANG, pattern)
    return QueryCursor(q)

def _first_child_of_type(node, type_name):
    for c in node.children:
        if c.type == type_name:
            return c
    return None

def _enclosing(node, src: bytes):
    """Return (class_name, method_name) enclosing a node."""
    cls = meth = None
    cur = node.parent
    while cur is not None:
        if meth is None and cur.type in ("method_declaration", "constructor_declaration"):
            n = cur.child_by_field_name("name")
            if n is not None:
                meth = _text(n, src)
        if cur.type == "class_declaration":
            n = cur.child_by_field_name("name")
            if n is not None:
                cls = _text(n, src)
            break
        cur = cur.parent
    return cls, meth


# ---------- main parse ----------------------------------------------------------

_Q_PACKAGE = _query("(package_declaration (scoped_identifier) @p) (package_declaration (identifier) @p)")
_Q_CLASS   = _query("(class_declaration name: (identifier) @name)")
_Q_FIELD   = _query("(field_declaration) @decl")
_Q_LOCAL   = _query("(local_variable_declaration) @decl")
_Q_PARAM   = _query("(formal_parameter) @decl")
_Q_IDENT   = _query("(identifier) @id")
_Q_INVOKE  = _query("(method_invocation) @call")


def parse_file(path: str) -> ParsedFile:
    with open(path, "rb") as fh:
        src = fh.read()
    tree = _PARSER.parse(src)
    root = tree.root_node

    package = None
    for _n, nodes in _Q_PACKAGE.captures(root).items():
        if nodes:
            package = _text(nodes[0], src)

    classes = [_text(n, src) for n in _Q_CLASS.captures(root).get("name", [])]

    elements: dict[tuple, DataElement] = {}

    def _add_one(decl_node, name_node, jtype, kind):
        name = _text(name_node, src)
        cls, meth = _enclosing(decl_node, src)
        key = (name, meth or "", cls or "", kind)
        de = DataElement(
            name=name, kind=kind, java_type=jtype, file=path,
            decl=_ref(name_node, src, path, name),
            enclosing_method=meth, enclosing_class=cls,
        )
        elements[key] = de
        return de

    def _add_decls(cursor, kind):
        # A declaration node owns its own type + declarator(s); we read the name(s)
        # from inside each node so name and type can never be mis-zipped.
        for decl in cursor.captures(root).get("decl", []):
            if kind == "param":
                name_n = decl.child_by_field_name("name")
                jtype_n = decl.child_by_field_name("type")
                if name_n is not None:
                    _add_one(decl, name_n, _text(jtype_n, src) if jtype_n else None, kind)
                continue
            jtype_n = decl.child_by_field_name("type")
            jtype = _text(jtype_n, src) if jtype_n else None
            for child in decl.children:
                if child.type == "variable_declarator":
                    name_n = child.child_by_field_name("name")
                    if name_n is not None:
                        _add_one(decl, name_n, jtype, kind)

    _add_decls(_Q_FIELD, "field")
    _add_decls(_Q_LOCAL, "local")
    _add_decls(_Q_PARAM, "param")

    # read/write facts: every identifier occurrence matching a known element name
    by_name: dict[str, list[DataElement]] = {}
    decl_positions: set[tuple] = set()
    for de in elements.values():
        by_name.setdefault(de.name, []).append(de)
        if de.decl:
            decl_positions.add((de.decl.file, de.decl.start_line, de.decl.symbol))

    src_lines = src.decode("utf8", "replace").splitlines()
    for idn in _Q_IDENT.captures(root).get("id", []):
        nm = _text(idn, src)
        if nm not in by_name:
            continue
        pos = (path, idn.start_point[0] + 1, nm)
        if pos in decl_positions:
            continue  # this is the declaration itself, not a use
        cls, meth = _enclosing(idn, src)
        de = _resolve_scope(by_name[nm], cls, meth)
        if de is None:
            continue
        ref = _ref(idn, src, path, nm)
        ln = idn.start_point[0]
        full_line = src_lines[ln].strip() if 0 <= ln < len(src_lines) else ref.snippet
        ref = CodeRef(file=ref.file, start_line=ref.start_line, end_line=ref.end_line,
                      symbol=ref.symbol, snippet=full_line)
        if _is_write_position(idn):
            de.writes.append(ref)
        else:
            de.reads.append(ref)

    # payload keys: X.getString("key") / getJsonObject("key") / getInteger("key") ...
    calls: list[CallEdge] = []
    bus_edges: list[BusEdge] = []
    for call in _Q_INVOKE.captures(root).get("call", []):
        _handle_invocation(call, src, path, elements, calls, bus_edges)

    # resolve $CONSTANT bus addresses against string-literal field initializers in this file
    constants = _string_constants(root, src)
    for be in bus_edges:
        if be.address and be.address.startswith("$"):
            resolved = constants.get(be.address[1:])
            if resolved is not None:
                be.address = resolved

    return ParsedFile(
        path=path, package=package, classes=classes,
        data_elements=list(elements.values()), calls=calls, bus_edges=bus_edges,
        constants=constants,
    )


_Q_CONST = _query(
    "(field_declaration declarator: (variable_declarator "
    "name: (identifier) @n value: (string_literal) @v))"
)

def _string_constants(root, src: bytes) -> dict[str, str]:
    caps = _Q_CONST.captures(root)
    names = caps.get("n", [])
    vals = caps.get("v", [])
    out = {}
    # match by shared parent variable_declarator to avoid order coupling
    for name_node in names:
        vd = name_node.parent
        val_node = vd.child_by_field_name("value") if vd else None
        if val_node is not None and val_node.type == "string_literal":
            frag = _first_child_of_type(val_node, "string_fragment")
            lit = _text(frag, src) if frag is not None else _text(val_node, src).strip('"')
            out[_text(name_node, src)] = lit
    return out


def _pairs(cursor: QueryCursor, root, a: str, b: str):
    caps = cursor.captures(root)
    return list(zip(caps.get(a, []), caps.get(b, [])))

def _decl_col(de: DataElement) -> int:
    return -1  # column unused for skip heuristic; name+line match is enough here

def _resolve_scope(candidates, cls, meth):
    """JavaSymbolSolver-style walk: prefer local/param in the same method, then field
    of the same class, then anything."""
    same_method = [d for d in candidates if d.enclosing_method == meth and d.kind in ("local", "param")]
    if same_method:
        return same_method[0]
    same_class_field = [d for d in candidates if d.enclosing_class == cls and d.kind == "field"]
    if same_class_field:
        return same_class_field[0]
    return candidates[0] if candidates else None

def _is_write_position(idn) -> bool:
    p = idn.parent
    if p is None:
        return False
    if p.type == "assignment_expression":
        left = p.child_by_field_name("left")
        return left is not None and left.id == idn.id
    if p.type == "variable_declarator":
        name = p.child_by_field_name("name")
        return name is not None and name.id == idn.id
    return False


_PAYLOAD_GETTERS = {
    "getString", "getInteger", "getLong", "getBoolean", "getDouble",
    "getFloat", "getJsonObject", "getJsonArray", "getValue", "getBinary",
}
_BUS_METHODS = {"consumer", "send", "request", "publish"}


def _handle_invocation(call, src, path, elements, calls, bus_edges):
    name_node = call.child_by_field_name("name")
    if name_node is None:
        return
    method = _text(name_node, src)
    cls, meth = _enclosing(call, src)
    args = call.child_by_field_name("arguments")

    # caller->callee call edge
    calls.append(CallEdge(
        caller=f"{cls or '?'}.{meth or '?'}",
        callee=method,
        ref=_ref(call, src, path, method),
    ))

    # payload key access -> synthesize a payload_key data element
    if method in _PAYLOAD_GETTERS and args is not None:
        lit = _first_string_arg(args, src)
        if lit is not None:
            obj = call.child_by_field_name("object")
            obj_name = _text(obj, src) if obj is not None else "?"
            key = (f"{obj_name}.{lit}", meth or "", cls or "")
            de = elements.get(key)
            if de is None:
                de = DataElement(
                    name=f"{obj_name}.{lit}", kind="payload_key",
                    java_type=method.replace("get", "").lower(),
                    file=path, decl=_ref(call, src, path, f'{obj_name}.getString("{lit}")'),
                    enclosing_method=meth, enclosing_class=cls,
                )
                elements[key] = de
            de.reads.append(_ref(call, src, path, f'{obj_name}.{method}("{lit}")'))

    # Vert.x eventBus wiring
    if method in _BUS_METHODS:
        addr = _first_string_arg(args, src) if args is not None else None
        bus_edges.append(BusEdge(
            kind=method, address=addr,
            enclosing_class=cls or "?", enclosing_method=meth or "?",
            ref=_ref(call, src, path, f'eventBus.{method}(...)'),
        ))

    # string-key mutations: obj.remove("KEY") / obj.set("KEY", v) / obj.put("KEY", v)
    # register obj.KEY as a payload_key element so masking/mutation hops can ground.
    if method in {"remove", "set", "put"} and args is not None:
        lit = _first_string_arg(args, src)
        obj = call.child_by_field_name("object")
        if lit and not lit.startswith("$") and obj is not None:
            obj_name = _text(obj, src).split(".")[0].split("(")[0]
            key = (f"{obj_name}.{lit}", meth or "", cls or "", "payload_key")
            if key not in elements:
                elements[key] = DataElement(
                    name=f"{obj_name}.{lit}", kind="payload_key", java_type="header/field",
                    file=path, decl=_ref(call, src, path, f'{obj_name}.{method}("{lit}")'),
                    enclosing_method=meth, enclosing_class=cls,
                )


def _first_string_arg(args_node, src: bytes) -> Optional[str]:
    for c in args_node.children:
        if c.type == "string_literal":
            frag = _first_child_of_type(c, "string_fragment")
            return _text(frag, src) if frag is not None else _text(c, src).strip('"')
        # constant reference like ADDRESS -> return the identifier name for later resolution
    for c in args_node.children:
        if c.type == "identifier":
            return "$" + _text(c, src)  # unresolved constant, marked with $
    return None


def parse_folder(folder: str) -> list[ParsedFile]:
    out = []
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            if fn.endswith(".java"):
                out.append(parse_file(os.path.join(root, fn)))
    return out


# ---------- candidate dataflow hops (intra-procedural def-use) -------------------

@dataclass
class Hop:
    """A grounded candidate edge: target := f(sources). The LLM names/classifies the
    transformation; it does NOT invent the endpoints."""
    target: str
    sources: list[str]
    via_method: Optional[str]   # e.g. body, getString, mapFrom, remove
    enclosing_class: str
    enclosing_method: str
    ref: CodeRef


_Q_LOCAL2 = _query("(local_variable_declaration) @d")
_Q_ASSIGN = _query("(assignment_expression) @a")
_Q_INVOKE2 = _query("(method_invocation) @c")

_MUTATORS = {"remove", "clear", "redact", "mask", "hash", "encrypt",
             "set", "put", "add", "setHeaders"}


def dataflow_hops(path: str) -> list[Hop]:
    with open(path, "rb") as fh:
        src = fh.read()
    root = _PARSER.parse(src).root_node
    hops: list[Hop] = []

    def _rhs_sources(rhs_node):
        srcs, via = [], None
        # method call on RHS: capture object + string-key -> payload element name
        stack = [rhs_node]
        while stack:
            n = stack.pop()
            if n.type == "method_invocation":
                mn = n.child_by_field_name("name")
                m = _text(mn, src) if mn else None
                if via is None:
                    via = m
                obj = n.child_by_field_name("object")
                if obj is not None:
                    obj_txt = _text(obj, src)
                    args = n.child_by_field_name("arguments")
                    lit = _first_string_arg(args, src) if args is not None else None
                    if m in _PAYLOAD_GETTERS and lit and not lit.startswith("$"):
                        srcs.append(f"{obj_txt.split('(')[0]}.{lit}")
                    else:
                        base = obj_txt.split(".")[0].split("(")[0]
                        if base and base[0].islower():
                            srcs.append(base)
            elif n.type == "identifier":
                # skip identifiers that are the *method name* of an invocation
                par = n.parent
                if par is not None and par.type == "method_invocation" \
                   and par.child_by_field_name("name") is not None \
                   and par.child_by_field_name("name").id == n.id:
                    pass
                else:
                    t = _text(n, src)
                    if t and t[0].islower():
                        srcs.append(t)
            for c in n.children:
                stack.append(c)
        # de-dup preserving order
        seen, uniq = set(), []
        for s in srcs:
            if s not in seen:
                seen.add(s); uniq.append(s)
        return uniq, via

    for d in _Q_LOCAL2.captures(root).get("d", []):
        jtype = d.child_by_field_name("type")
        for child in d.children:
            if child.type != "variable_declarator":
                continue
            name_n = child.child_by_field_name("name")
            val_n = child.child_by_field_name("value")
            if name_n is None or val_n is None:
                continue
            cls, meth = _enclosing(d, src)
            srcs, via = _rhs_sources(val_n)
            tgt = _text(name_n, src)
            srcs = [s for s in srcs if s != tgt]
            if srcs:
                hops.append(Hop(
                    target=tgt, sources=srcs, via_method=via,
                    enclosing_class=cls or "?", enclosing_method=meth or "?",
                    ref=_ref(d, src, path, tgt),
                ))

    for a in _Q_ASSIGN.captures(root).get("a", []):
        left = a.child_by_field_name("left")
        right = a.child_by_field_name("right")
        if left is None or right is None:
            continue
        cls, meth = _enclosing(a, src)
        srcs, via = _rhs_sources(right)
        tgt = _text(left, src)
        srcs = [s for s in srcs if s != tgt]
        if srcs:
            hops.append(Hop(
                target=tgt, sources=srcs, via_method=via,
                enclosing_class=cls or "?", enclosing_method=meth or "?",
                ref=_ref(a, src, path, tgt),
            ))

    # mutation hops: obj.remove(...) / obj.set(...) / obj.put(...) mutate `obj` in place
    for c in _Q_INVOKE2.captures(root).get("c", []):
        name_n = c.child_by_field_name("name")
        obj_n = c.child_by_field_name("object")
        if name_n is None or obj_n is None:
            continue
        method = _text(name_n, src)
        if method not in _MUTATORS:
            continue
        if obj_n.type != "identifier":
            continue
        tgt = _text(obj_n, src)
        if not tgt or not tgt[0].islower():
            continue
        cls, meth = _enclosing(c, src)
        args = c.child_by_field_name("arguments")
        srcs, _ = ([], None)
        if args is not None:
            lit = _first_string_arg(args, src)
            if lit and not lit.startswith("$"):
                srcs = [f"{tgt}.{lit}"]
        hops.append(Hop(
            target=tgt, sources=srcs or [tgt], via_method=method,
            enclosing_class=cls or "?", enclosing_method=meth or "?",
            ref=_ref(c, src, path, f"{tgt}.{method}"),
        ))
    return hops
