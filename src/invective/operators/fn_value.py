r"""The `fn_value` genre: a function's body becomes a return of one value of its type.

The type is the function's return annotation, after cargo-mutants' `FnValue`. The
type table is pytest-gremlins' reading of cargo-mutants' for Python,
copied as is from `src/pytest_gremlins/operators/fn_value.py` of
https://github.com/gsfernandes81/pytest-gremlins, which is MIT licensed:

    MIT License

    Copyright (c) 2024 Mike Lane

    Permission is hereby granted, free of charge, to any person obtaining a
    copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to
    permit persons to whom the Software is furnished to do so, subject to
    the following conditions:

    The above copyright notice and this permission notice shall be included
    in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
    OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
    IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
    CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
    TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
    SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

The table:

    annotation                          replacements
    none, None, or a type not below     None
    bool                                True, False
    int                                 0, 1, -1
    float                               0.0, 1.0, -1.0
    str                                 '', 'xyzzy'
    bytes                               b'', b'\x00', b'\x01'
    T | None, Optional[T]               None, then every value of T
    list[T], Sequence[T]                [], then [v] for every value of T
    set[T], frozenset[T]                empty, then one element of every T
    dict[K, V], Mapping[K, V]           {}, then {k: v} for every pair
    tuple[A, B]                         the product of every value of A, B
    tuple[T, ...]                       (), then (v,) for every value of T
    Iterator[T], Iterable[T]            iter([]), then iter([v]) for every T
    NoReturn, Never                     no mutant

A generator (or async generator) instead becomes empty, then yields one
value of its element type. As in cargo-mutants, a function with an empty
body (only a docstring, `pass` or `...`) has no mutant, and a replacement identical
to the body is no mutant; constructors (`__init__`, `__new__`) are skipped as
cargo-mutants skips `new`, and so are `@abstractmethod` and `@overload` functions,
which have no body to mutate, as cargo-mutants skips trait methods without one.
"""

from __future__ import annotations

import ast
import itertools
from typing import NamedTuple

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef

_SKIPPED_NAMES = frozenset({"__init__", "__new__"})
_SKIPPED_DECORATORS = frozenset({"abstractmethod", "overload"})
_SEQUENCES = frozenset({"list", "List", "Sequence", "MutableSequence", "Collection"})
_SETS = frozenset({"set", "Set", "AbstractSet", "MutableSet"})
_FROZENSETS = frozenset({"frozenset", "FrozenSet"})
_MAPPINGS = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})
_TUPLES = frozenset({"tuple", "Tuple"})
_ITERATORS = frozenset(
    {
        "Iterator",
        "Iterable",
        "Generator",
        "AsyncIterator",
        "AsyncIterable",
        "AsyncGenerator",
        "Reversible",
    }
)
_NEVER = frozenset({"NoReturn", "Never"})
_SCALARS: dict[str, list[str]] = {
    "None": ["None"],
    "bool": ["True", "False"],
    "int": ["0", "1", "-1"],
    "float": ["0.0", "1.0", "-1.0"],
    "str": ["''", "'xyzzy'"],
    "bytes": ["b''", "b'\\x00'", "b'\\x01'"],
}

#: A type name given without arguments to its values; a name not here gives None.
_BARE: dict[str | None, list[str]] = {
    **{name: [] for name in _NEVER},
    **_SCALARS,
    **{name: ["[]", "[None]"] for name in _SEQUENCES},
    **{name: ["set()", "{None}"] for name in _SETS},
    **{name: ["frozenset()", "frozenset({None})"] for name in _FROZENSETS},
    **{name: ["{}", "{None: None}"] for name in _MAPPINGS},
    **{name: ["()", "(None,)"] for name in _TUPLES},
}

#: The generic types of one argument: their names, the empty value, and the format
#: that holds one value of the argument's type.
_ONE_ARGUMENT = (
    (_SEQUENCES, "[]", "[{}]"),
    (_SETS, "set()", "{{{}}}"),
    (_FROZENSETS, "frozenset()", "frozenset({{{}}})"),
    (_ITERATORS, "iter([])", "iter([{}])"),
)


class Replacement(NamedTuple):
    """One mutant of a function, as the body that replaces its own."""

    body: list[ast.stmt]  #: what stands in for the statements after the docstring
    value: str  #: the value's source, as the report names it


def _type_name(node: ast.expr) -> str | None:
    """Return the name a type expression ends in, `None` for None, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    return None


def _type_args(node: ast.Subscript) -> list[ast.expr]:
    """Return the type arguments of a subscripted type, in order."""
    return list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]


def _optional_inner(node: ast.expr) -> ast.expr | None:
    """Return `T` from `T | None`, `None | T`, `Optional[T]` or `Union[T, None]`.

    Any other type gives None.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        if _type_name(node.right) == "None":
            return node.left
        if _type_name(node.left) == "None":
            return node.right
        return None
    if isinstance(node, ast.Subscript):
        match _type_name(node.value), _type_args(node):
            case "Optional", [inner]:
                return inner
            case "Union", [_, _] as args:
                others = [arg for arg in args if _type_name(arg) != "None"]
                if len(others) == 1:
                    return others[0]
    return None


def iterator_item(annotation: ast.expr | None) -> ast.expr | None:
    """Return the element type of an iterator or generator annotation, else None."""
    if (
        isinstance(annotation, ast.Subscript)
        and _type_name(annotation.value) in _ITERATORS
    ):
        return _type_args(annotation)[0]
    return None


def type_values(annotation: ast.expr | None) -> list[str]:
    """Return the source of every replacement value for a return annotation.

    The values follow the table in the module docstring, in its order.
    """
    if annotation is None:
        return ["None"]
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            return type_values(ast.parse(annotation.value, mode="eval").body)
        except SyntaxError:
            return ["None"]
    inner = _optional_inner(annotation)
    if inner is not None:
        return ["None", *(value for value in type_values(inner) if value != "None")]
    if isinstance(annotation, ast.Subscript):
        return _generic_values(annotation)
    return list(_BARE.get(_type_name(annotation), ["None"]))


def _generic_values(annotation: ast.Subscript) -> list[str]:
    """Return the values for a subscripted annotation that is not optional."""
    name = _type_name(annotation.value)
    args = _type_args(annotation)
    if name in _TUPLES:
        return _tuple_values(args)
    if name in _MAPPINGS:
        return _mapping_values(args)
    for names, empty, holding in _ONE_ARGUMENT:
        if name in names:
            return [empty, *(holding.format(value) for value in type_values(args[0]))]
    return ["None"]


def _mapping_values(args: list[ast.expr]) -> list[str]:
    """Return the values for a mapping with the type arguments *args*.

    A mapping without exactly a key type and a value type gives None.
    """
    match args:
        case [key, value]:
            pairs = itertools.product(type_values(key), type_values(value))
            return ["{}", *(f"{{{k}: {v}}}" for k, v in pairs)]
        case _:
            return ["None"]


def _tuple_values(args: list[ast.expr]) -> list[str]:
    """Return the values for a tuple with the type arguments *args*."""
    match args:
        case [item, ast.Constant(value=last)] if last is Ellipsis:
            return ["()", *(f"({value},)" for value in type_values(item))]
        case [ast.Tuple(elts=[])]:
            return ["()"]
    products = itertools.product(*(type_values(arg) for arg in args))
    return [
        f"({', '.join(values)}{',' if len(args) == 1 else ''})" for values in products
    ]


def _has_docstring(body: list[ast.stmt]) -> bool:
    """Return whether the non-empty *body* opens with a docstring."""
    first = body[0]
    return (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )


def split_docstring(body: list[ast.stmt]) -> tuple[list[ast.stmt], list[ast.stmt]]:
    """Return a function body as its docstring, as a list of none or one, and the rest."""
    count = 1 if body and _has_docstring(body) else 0
    return body[:count], body[count:]


def _is_empty(statements: list[ast.stmt]) -> bool:
    """Return whether *statements* are only `pass` and constant expressions."""
    return all(
        isinstance(statement, ast.Pass)
        or (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant))
        for statement in statements
    )


def is_generator(node: FunctionNode) -> bool:
    """Return whether *node* yields, outside any function, lambda or class in it."""
    pending: list[ast.AST] = list(node.body)
    while pending:
        current = pending.pop()
        if isinstance(current, (ast.Yield, ast.YieldFrom)):
            return True
        if isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
        ):
            continue
        pending.extend(ast.iter_child_nodes(current))
    return False


def _decorator_name(decorator: ast.expr) -> str | None:
    """Return the name a decorator ends in, called or not."""
    return _type_name(decorator.func if isinstance(decorator, ast.Call) else decorator)


def skipped(node: FunctionNode) -> bool:
    """Return whether the genre leaves *node* without a mutant.

    It does for a constructor, an overload, an abstract method, and an empty body.
    """
    if node.name in _SKIPPED_NAMES:
        return True
    if any(
        _decorator_name(decorator) in _SKIPPED_DECORATORS
        for decorator in node.decorator_list
    ):
        return True
    return _is_empty(split_docstring(node.body)[1])


def _same(statements: list[ast.stmt], replacement: list[ast.stmt]) -> bool:
    """Return whether *replacement* is the same code as *statements*."""
    if len(statements) != len(replacement):
        return False
    return ast.dump(ast.Module(body=statements, type_ignores=[])) == ast.dump(
        ast.Module(body=replacement, type_ignores=[])
    )


def _expr(source: str) -> ast.expr:
    """Return the expression *source* parses as."""
    return ast.parse(source, mode="eval").body


def replacements(node: FunctionNode) -> list[Replacement]:
    """Return every replacement body for *node*; a function `skipped` has none."""
    if skipped(node):
        return []
    _docstring, statements = split_docstring(node.body)
    candidates: list[tuple[list[ast.stmt], str]]
    if is_generator(node):
        item = iterator_item(node.returns)
        values = type_values(item) if item is not None else ["None"]
        candidates = [([ast.Return()], "iter([])")]
        candidates += [
            ([ast.Expr(ast.Yield(_expr(value))), ast.Return()], f"iter([{value}])")
            for value in values
        ]
    else:
        candidates = [
            ([ast.Return(_expr(value))], value) for value in type_values(node.returns)
        ]
    return [
        Replacement(body, value)
        for body, value in candidates
        if not _same(statements, body)
    ]


def describe(node: FunctionNode, value: str) -> str:
    """Return `replace <name> [-> <annotation>] with <value>`, as the report names it."""
    signature = (
        node.name
        if node.returns is None
        else f"{node.name} -> {ast.unparse(node.returns)}"
    )
    return f"replace {signature} with {value}"
