"""Find the App Service plans an accelerator's Bicep will create, with the SKU and instance count it will ask for.

Accelerators pick the plan SKU inside the template, often behind a flavor switch and a ternary, for example
``main.bicep`` -> ``module ... = if (isAvm)`` -> ``skuName: enableScalability || enableRedundancy ? 'P1v4' : 'B3'`` ->
a wrapper module -> ``Microsoft.Web/serverfarms``. This module follows that chain from the entry template:

* a small scanner masks strings and comments so blocks can be matched with braces;
* params, vars, modules (with their ``if`` condition and ``params``) and ``Microsoft.Web/serverfarms`` resources are
  parsed per file; ``br/public:avm/res/web/serverfarm`` modules count as plans too;
* a tiny evaluator resolves literals, params, vars, ``== != ! && ||``, parentheses and ``? :`` from the root values the
  caller resolved the azd way. Anything else (functions, property access, interpolation) is *unresolved*, never guessed.

Location parameters are passed in as symbols, so each plan reports which root parameter decides its region (for
example the primary ``location`` rather than the AI services region).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

MAX_DEPTH = 6
SERVERFARM_BR = re.compile(r"(?:^|/)web/serverfarm(?::|$)", re.I)


class Unresolved(Exception):
    pass


@dataclass(frozen=True)
class Sym:
    """A root parameter whose value is deliberately kept symbolic (used for location parameters)."""

    name: str


# --------------------------------------------------------------------------- scanning


def _mask(text: str) -> str:
    """Same-length copy where string contents (incl. interpolations) and comments are spaces; quotes are kept."""
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out[i:j] = " " * (j - i)
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out[i:j] = [ch if ch == "\n" else " " for ch in text[i:j]]
            i = j
        elif text.startswith("'''", i):
            j = text.find("'''", i + 3)
            j = n if j < 0 else j + 3
            out[i + 3 : j - 3] = [ch if ch == "\n" else " " for ch in text[i + 3 : j - 3]]
            i = j
        elif c == "'":
            j = _end_of_string(text, i)
            out[i + 1 : j - 1] = " " * (j - 1 - (i + 1))
            i = j
        else:
            i += 1
    return "".join(out)


def _end_of_string(text: str, i: int) -> int:
    """Index just past the string starting at text[i] == "'", honouring escapes and ${...} interpolation."""
    j, n = i + 1, len(text)
    while j < n:
        if text[j] == "\\":
            j += 2
            continue
        if text.startswith("${", j):
            depth, j = 1, j + 2
            while j < n and depth:
                if text[j] == "'":
                    j = _end_of_string(text, j)
                    continue
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                j += 1
            continue
        if text[j] == "'":
            return j + 1
        j += 1
    return n


def _close(masked: str, i: int) -> int:
    """Index of the bracket that closes the one at masked[i]."""
    pairs = {"{": "}", "[": "]", "(": ")"}
    stack = [pairs[masked[i]]]
    j = i + 1
    while j < len(masked) and stack:
        ch = masked[j]
        if ch in pairs:
            stack.append(pairs[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        j += 1
    return j - 1


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _entries(text: str, masked: str, start: int, end: int) -> Dict[str, Tuple[str, int]]:
    """Top-level ``key: value`` entries of the object body text[start:end] -> {key: (value text, line)}."""
    out: Dict[str, Tuple[str, int]] = {}
    i = start
    rx = re.compile(r"[ \t]*([A-Za-z_]\w*|'[^']*')[ \t]*:[ \t]*", re.M)
    while i < end:
        m = rx.match(masked, i)
        if not m:
            nl = masked.find("\n", i, end)
            i = end if nl < 0 else nl + 1
            continue
        key = text[m.start(1) : m.end(1)].strip("'")
        v = m.end()
        j = v
        while j < end and masked[j] != "\n":
            if masked[j] in "{[(":
                j = _close(masked, j) + 1
                continue
            j += 1
        out[key] = (text[v:j].strip(), _line(text, m.start(1)))
        i = j + 1
    return out


# --------------------------------------------------------------------------- parsing


@dataclass
class Module:
    name: str
    path: str
    cond: Optional[str]
    params: Dict[str, Tuple[str, int]]
    line: int
    loop: bool = False


@dataclass
class Farm:
    name: str
    cond: Optional[str]
    sku: Optional[Tuple[str, int]]
    capacity: Optional[Tuple[str, int]]
    zone: Optional[Tuple[str, int]]
    location: Optional[Tuple[str, int]]
    line: int


@dataclass
class BicepFile:
    path: Path
    rel: str
    params: Dict[str, Tuple[Optional[str], int]] = field(default_factory=dict)  # name -> (default expr, line)
    vars: Dict[str, Tuple[str, int]] = field(default_factory=dict)
    modules: List[Module] = field(default_factory=list)
    farms: List[Farm] = field(default_factory=list)


def _decl_value(text: str, masked: str, start: int) -> Tuple[str, int]:
    """Value text from start to end of line, extending over brackets."""
    j = start
    while j < len(masked) and masked[j] != "\n":
        if masked[j] in "{[(":
            j = _close(masked, j) + 1
            continue
        j += 1
    return text[start:j].strip(), j


_HEAD = re.compile(r"^[ \t]*(module|resource)[ \t]+(\w+)[ \t]+'([^']*)'[ \t]*(existing[ \t]*)?=[ \t]*", re.M)


def parse_bicep(path: Path, rel: str) -> BicepFile:
    text = path.read_text(encoding="utf-8", errors="ignore")
    masked = _mask(text)
    bf = BicepFile(path, rel)
    for m in re.finditer(r"^[ \t]*param[ \t]+(\w+)[ \t]+[\w?.]+(?:[ \t]*=[ \t]*)?", masked, re.M):
        has_default = masked[m.start() : m.end()].rstrip().endswith("=")
        value = _decl_value(text, masked, m.end())[0] if has_default else None
        bf.params[m.group(1)] = (value, _line(text, m.start(1)))
    for m in re.finditer(r"^[ \t]*var[ \t]+(\w+)[ \t]*=[ \t]*", masked, re.M):
        bf.vars[m.group(1)] = (_decl_value(text, masked, m.end())[0], _line(text, m.start(1)))
    for m in _HEAD.finditer(masked):
        kind, name, target, existing = m.group(1), m.group(2), text[m.start(3) : m.end(3)], m.group(4)
        if existing:
            continue
        i = m.end()
        cond, loop = None, False
        if masked.startswith("if", i):
            p = masked.find("(", i)
            q = _close(masked, p)
            cond = text[p + 1 : q].strip()
            i = q + 1
            while i < len(masked) and masked[i] in " \t":
                i += 1
        if i < len(masked) and masked[i] == "[":
            loop = True
            b = masked.find("{", i)
        else:
            b = i if i < len(masked) and masked[i] == "{" else -1
        if b < 0:
            continue
        e = _close(masked, b)
        body = _entries(text, masked, b + 1, e)
        line = _line(text, m.start(1))
        if kind == "module":
            prm: Dict[str, Tuple[str, int]] = {}
            pm = re.search(r"^[ \t]*params[ \t]*:[ \t]*\{", masked[b + 1 : e], re.M)
            if pm:
                ps = b + 1 + pm.end() - 1
                prm = _entries(text, masked, ps + 1, _close(masked, ps))
            bf.modules.append(Module(name, target, cond, prm, line, loop))
            if SERVERFARM_BR.search(target) and not loop:
                bf.farms.append(
                    Farm(
                        name,
                        cond,
                        prm.get("skuName"),
                        prm.get("skuCapacity"),
                        prm.get("zoneRedundant"),
                        prm.get("location"),
                        line,
                    )
                )
        elif target.lower().startswith("microsoft.web/serverfarms@") and not loop:
            sku = cap = zone = None
            sm = re.search(r"^[ \t]*sku[ \t]*:[ \t]*\{", masked[b + 1 : e], re.M)
            if sm:
                ss = b + 1 + sm.end() - 1
                sk = _entries(text, masked, ss + 1, _close(masked, ss))
                sku, cap = sk.get("name"), sk.get("capacity")
            pr = re.search(r"^[ \t]*properties[ \t]*:[ \t]*\{", masked[b + 1 : e], re.M)
            if pr:
                ps = b + 1 + pr.end() - 1
                zone = _entries(text, masked, ps + 1, _close(masked, ps)).get("zoneRedundant")
            bf.farms.append(Farm(name, cond, sku, cap, zone, body.get("location"), line))
    return bf


# --------------------------------------------------------------------------- evaluation

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^'\\]|\\.)*')|(?P<num>-?\d+)|(?P<op>==|!=|=~|!~|&&|\|\||<=|>=|[!?:()<>])"
    r"|(?P<id>[A-Za-z_][\w]*(?:\.[A-Za-z_]\w*)*)|(?P<bad>\S))"
)


def _tokens(expr: str) -> List[Tuple[str, str]]:
    out = []
    for m in _TOKEN.finditer(expr):
        kind = m.lastgroup
        if kind is None:
            continue
        val = m.group(kind)
        if kind == "bad":
            raise Unresolved(f"unsupported syntax '{val}'")
        out.append((kind, val))
    return out


class Scope:
    """Lazily evaluated names of one template instance. A thunk is (expr, scope that evaluates it, origin)."""

    def __init__(self, rel: str):
        self.rel = rel
        self.thunks: Dict[str, Tuple[Any, Optional["Scope"], str]] = {}
        self._memo: Dict[str, Any] = {}
        self._busy: set = set()

    def set_value(self, name: str, value: Any, origin: str) -> None:
        self.thunks[name] = (("value", value), None, origin)

    def set_expr(self, name: str, expr: Optional[str], scope: Optional["Scope"], origin: str) -> None:
        self.thunks[name] = (("expr", expr), scope, origin)

    def get(self, name: str) -> Any:
        if name in self._memo:
            return self._memo[name]
        if name not in self.thunks:
            raise Unresolved(f"'{name}' is not a param or var of {self.rel}")
        if name in self._busy:
            raise Unresolved(f"'{name}' refers to itself")
        (kind, payload), scope, _ = self.thunks[name]
        self._busy.add(name)
        try:
            if kind == "value":
                value = payload
            elif payload is None:
                raise Unresolved(f"'{name}' has no value")
            else:
                value = evaluate(payload, scope or self)
        finally:
            self._busy.discard(name)
        self._memo[name] = value
        return value

    def origin(self, expr: Optional[str], fallback: str, depth: int = 0) -> str:
        """Where the value of expr really comes from: follows bare names through module params and vars."""
        e = (expr or "").strip()
        if depth > 20 or not re.fullmatch(r"[A-Za-z_]\w*", e) or e not in self.thunks:
            return fallback
        (kind, payload), scope, where = self.thunks[e]
        if kind == "expr" and payload is not None:
            return (scope or self).origin(payload, where, depth + 1)
        return where


def evaluate(expr: str, scope: Scope) -> Any:
    toks = _tokens(expr)
    pos = 0

    def peek(v: Optional[str] = None) -> bool:
        return pos < len(toks) and (v is None or toks[pos][1] == v)

    def take() -> Tuple[str, str]:
        nonlocal pos
        pos += 1
        return toks[pos - 1]

    def ternary() -> Any:
        cond = orr()
        if peek("?"):
            take()
            a = _lazy(ternary)
            if not peek(":"):
                raise Unresolved("malformed ternary")
            take()
            b = _lazy(ternary)
            if isinstance(cond, Unresolved) or isinstance(cond, Sym):
                if not isinstance(a, Unresolved) and a == b:
                    return a
                raise Unresolved(f"condition of '{expr.strip()[:80]}' cannot be decided")
            chosen = a if cond else b
            if isinstance(chosen, Unresolved):
                raise chosen
            return chosen
        if isinstance(cond, Unresolved):
            raise cond
        return cond

    def _lazy(fn):
        try:
            return fn()
        except Unresolved as e:
            return e

    def orr() -> Any:
        left = _lazy(andd)
        while peek("||"):
            take()
            right = _lazy(andd)
            if left is True or right is True:
                left = True
            elif isinstance(left, Unresolved) or isinstance(right, Unresolved):
                left = left if isinstance(left, Unresolved) else right
            else:
                left = bool(left) or bool(right)
        return left

    def andd() -> Any:
        left = _lazy(eq)
        while peek("&&"):
            take()
            right = _lazy(eq)
            if left is False or right is False:
                left = False
            elif isinstance(left, Unresolved) or isinstance(right, Unresolved):
                left = left if isinstance(left, Unresolved) else right
            else:
                left = bool(left) and bool(right)
        if isinstance(left, Unresolved):
            raise left
        return left

    def eq() -> Any:
        left = unary()
        while peek("==") or peek("!=") or peek("=~") or peek("!~"):
            op = take()[1]
            right = unary()
            if isinstance(left, Sym) or isinstance(right, Sym):
                raise Unresolved("compares a location that is decided later")
            if op in ("=~", "!~"):
                same = str(left).lower() == str(right).lower()
            else:
                same = left == right
            left = same if op in ("==", "=~") else not same
        return left

    def unary() -> Any:
        if peek("!"):
            take()
            v = unary()
            if isinstance(v, Sym):
                raise Unresolved("negates a location")
            return not v
        return primary()

    def primary() -> Any:
        if not peek():
            raise Unresolved("unexpected end of expression")
        kind, val = take()
        if val == "(":
            v = ternary()
            if not peek(")"):
                raise Unresolved("missing )")
            take()
            return v
        if kind == "str":
            body = val[1:-1]
            if "${" in body:
                raise Unresolved("interpolated string")
            return body.replace("\\'", "'").replace("\\\\", "\\")
        if kind == "num":
            return int(val)
        if kind == "id":
            if val in ("true", "false"):
                return val == "true"
            if val == "null":
                return None
            if "." in val or peek("("):
                raise Unresolved(f"'{val}' (functions and properties are not evaluated)")
            return scope.get(val)
        raise Unresolved(f"unexpected '{val}'")

    value = ternary()
    if pos != len(toks):
        raise Unresolved(f"could not read '{expr.strip()[:80]}'")
    return value


# --------------------------------------------------------------------------- walking the template


def _coerce(value: Any) -> Any:
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "false"):
            return low == "true"
        if re.fullmatch(r"-?\d+", low):
            return int(low)
    return value


def find_app_service_plans(
    repo: Path,
    main_rel: str,
    root_values: Dict[str, Tuple[Any, str]],
    symbolic: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """{plans: [...], notes: [...]} for every App Service plan the template creates.

    root_values: {param: (value, source)} resolved by the caller (azd env, parameters file, Bicep default ...); a value
    that is the Bicep default expression is evaluated here. Params in ``symbolic`` stay symbols (locations).
    """
    cache: Dict[Path, BicepFile] = {}
    plans: List[Dict[str, Any]] = []
    notes: List[str] = []

    def load(path: Path) -> Optional[BicepFile]:
        path = path.resolve()
        if path not in cache:
            if not path.is_file():
                return None
            try:
                rel = path.relative_to(repo.resolve()).as_posix()
            except ValueError:
                rel = path.as_posix()
            cache[path] = parse_bicep(path, rel)
        return cache[path]

    root = load(repo / main_rel)
    if root is None:
        return {"plans": [], "notes": [f"{main_rel} not found"]}
    top = Scope(root.rel)
    for name, (default, line) in root.params.items():
        if name in symbolic:
            top.set_value(name, Sym(name), f"{root.rel}:{line}")
        elif name in root_values and root_values[name][0] is not None:
            value, source = root_values[name]
            if source.endswith(" default") and isinstance(value, str) and default is not None:
                top.set_expr(name, default, top, source)  # the Bicep default is an expression: evaluate it here
            else:
                top.set_value(name, _coerce(value), source)
        else:
            top.set_expr(name, default, top, f"{root.rel}:{line}")
    for name, (expr, line) in root.vars.items():
        top.set_expr(name, expr, top, f"{root.rel}:{line}")

    def show(v: Any) -> Any:
        return f"<{v.name}>" if isinstance(v, Sym) else v

    def value_of(entry: Optional[Tuple[str, int]], scope: Scope, rel: str, default: Any = None):
        if entry is None:
            return default, None, None
        expr, line = entry
        where = scope.origin(expr, f"{rel}:{line}")
        try:
            return evaluate(expr, scope), where, None
        except Unresolved as e:
            return None, where, str(e)

    def walk(bf: BicepFile, scope: Scope, depth: int, conditional: bool, chain: List[str]) -> None:
        for farm in bf.farms:
            active, why = True, None
            if farm.cond:
                try:
                    active = bool(evaluate(farm.cond, scope))
                except Unresolved as e:
                    why = str(e)
            if not active:
                continue
            sku, sku_where, sku_err = value_of(farm.sku, scope, bf.rel)
            cap, cap_where, cap_err = value_of(farm.capacity, scope, bf.rel, 1)
            zone, _, _ = value_of(farm.zone, scope, bf.rel, False)
            loc, _, _ = value_of(farm.location, scope, bf.rel)
            plans.append(
                {
                    "name": farm.name,
                    "sku": sku if isinstance(sku, str) else None,
                    "capacity": cap if isinstance(cap, int) else (1 if cap is None and not cap_err else None),
                    "zone_redundant": bool(zone) if isinstance(zone, bool) else False,
                    "location_param": loc.name if isinstance(loc, Sym) else None,
                    "location_value": None if isinstance(loc, Sym) else show(loc),
                    "where": sku_where or f"{bf.rel}:{farm.line}",
                    "capacity_where": cap_where,
                    "declared_at": f"{bf.rel}:{farm.line}",
                    "conditional": conditional or bool(why),
                    "unresolved": "; ".join(x for x in (sku_err, cap_err if farm.capacity else None) if x) or None,
                    "via": " -> ".join(chain + [bf.rel]),
                }
            )
        if depth >= MAX_DEPTH:
            return
        for mod in bf.modules:
            if mod.path.startswith(("br:", "br/", "ts:", "ts/")) or not mod.path.endswith(".bicep"):
                continue
            if mod.loop:
                notes.append(f"{bf.rel}:{mod.line} module '{mod.name}' is in a loop and was not followed")
                continue
            cond_unknown = False
            if mod.cond:
                try:
                    if not evaluate(mod.cond, scope):
                        continue
                except Unresolved:
                    cond_unknown = True
            child = load(bf.path.parent / mod.path)
            if child is None:
                notes.append(f"{bf.rel}:{mod.line} module file {mod.path} not found")
                continue
            cs = Scope(child.rel)
            for name, (default, line) in child.params.items():
                if name in mod.params:
                    expr, pline = mod.params[name]
                    cs.set_expr(name, expr, scope, f"{bf.rel}:{pline}")
                else:
                    cs.set_expr(name, default, cs, f"{child.rel}:{line}")
            for name, (expr, line) in child.vars.items():
                cs.set_expr(name, expr, cs, f"{child.rel}:{line}")
            walk(child, cs, depth + 1, conditional or cond_unknown, chain + [bf.rel])

    walk(root, top, 0, False, [])
    # The same plan reached through a wrapper and its inner resource is one plan: keep the outermost report.
    seen, unique = set(), []
    for p in plans:
        key = (p["where"], p["sku"], p["capacity"])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return {"plans": unique, "notes": notes}
