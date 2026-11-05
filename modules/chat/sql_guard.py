"""Parser-based allowlist guard for model-written SQL in the ``query_collected_data`` chat tool.

The guard parses the statement with PostgreSQL's own parser (``pglast`` / libpg_query) and applies a
*default-deny* policy to the resulting syntax tree: only an explicit set of node types, relations,
functions, operators and cast targets is accepted. It is the first of three layers (parser allowlist,
least-privilege ``umwelt_chat_sql_login`` role (migration 0017) whose views are fenced by the reader-inaccessible ``chat_sql.query_fence`` table (0018), read-only transaction). A regex denylist is
deliberately not used as a layer: it missed real bypasses (``chat_messages``, ``query_to_xml``) and
rejected legitimate literals such as ``ILIKE '%set%'``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.visitors import Skip, Visitor

ALLOWED_RELATIONS = frozenset({"scoped_observations", "scoped_documents"})

#: Syntax-tree node types a chat query may contain. Anything else (RangeFunction, XmlExpr, IntoClause,
#: LockingClause, ParamRef, INSERT/UPDATE/DELETE, ...) is rejected by default.
_ALLOWED_NODES = frozenset(
    {
        "SelectStmt",
        "WithClause",
        "CommonTableExpr",
        "RangeVar",
        "RangeSubselect",
        "JoinExpr",
        "Alias",
        "ResTarget",
        "ColumnRef",
        "A_Const",
        "A_Expr",
        "A_Star",
        "A_Indirection",
        "A_ArrayExpr",
        "A_Indices",
        "BoolExpr",
        "NullTest",
        "BooleanTest",
        "CaseExpr",
        "CaseWhen",
        "CoalesceExpr",
        "MinMaxExpr",
        "SubLink",
        "TypeCast",
        "TypeName",
        "FuncCall",
        "SortBy",
        "WindowDef",
        "String",
        "Integer",
        "Float",
        "Boolean",
        "SQLValueFunction",
        "GroupingSet",
        "RowExpr",
    }
)

_ALLOWED_FUNCTIONS = frozenset(
    {
        # aggregates and statistics
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "stddev",
        "stddev_pop",
        "stddev_samp",
        "variance",
        "var_pop",
        "var_samp",
        "corr",
        "percentile_cont",
        "percentile_disc",
        "string_agg",
        "array_agg",
        "bool_and",
        "bool_or",
        "every",
        # window functions
        "row_number",
        "rank",
        "dense_rank",
        "percent_rank",
        "cume_dist",
        "ntile",
        "lag",
        "lead",
        "first_value",
        "last_value",
        "nth_value",
        # strings
        "lower",
        "upper",
        "initcap",
        "length",
        "char_length",
        "character_length",
        "btrim",
        "ltrim",
        "rtrim",
        "substring",
        "substr",
        "replace",
        "left",
        "right",
        "concat",
        "concat_ws",
        "position",
        "strpos",
        "split_part",
        "lpad",
        "rpad",
        "reverse",
        "to_char",
        "to_number",
        "overlay",
        # SIMILAR TO is parsed into ``~ similar_to_escape(pattern)``: a pure pattern translation, no more
        # powerful than the allowed ``~`` regex operators
        "similar_to_escape",
        # dates (now() is STABLE and side-effect free, so it is allowed)
        "date_trunc",
        "date_part",
        "extract",
        "age",
        "now",
        "timezone",
        "to_date",
        "to_timestamp",
        # math
        "round",
        "trunc",
        "ceil",
        "ceiling",
        "floor",
        "abs",
        "sign",
        "sqrt",
        "power",
        "exp",
        "ln",
        "log",
        "log10",
        "mod",
        "width_bucket",
        # json read helpers (the -> / ->> / #> operators are allowed separately)
        "jsonb_typeof",
        "jsonb_array_length",
        "jsonb_extract_path_text",
        "json_typeof",
    }
)

_ALLOWED_OPERATORS = frozenset(
    {
        "+",
        "-",
        "*",
        "/",
        "%",
        "^",
        "=",
        "<>",
        "!=",
        "<",
        ">",
        "<=",
        ">=",
        "||",
        "~~",
        "!~~",
        "~~*",
        "!~~*",
        "~",
        "!~",
        "~*",
        "!~*",
        "->",
        "->>",
        "#>",
        "#>>",
        "@>",
        "<@",
        "?",
        "?|",
        "?&",
        "BETWEEN",
        "NOT BETWEEN",
        "BETWEEN SYMMETRIC",
        "NOT BETWEEN SYMMETRIC",
        "IN",
        "NOT IN",
        "LIKE",
        "NOT LIKE",
        "ILIKE",
        "NOT ILIKE",
    }
)
# SIMILAR TO needs no entry above: the parser rewrites ``a SIMILAR TO b`` to the already-allowed regex
# operator ``~`` applied to ``similar_to_escape(b)``, so it is permitted via that function in ``_ALLOWED_FUNCTIONS`` above.

_ALLOWED_CAST_TYPES = frozenset(
    {
        "text",
        "varchar",
        "bpchar",
        "char",
        "int2",
        "int4",
        "int8",
        "int",
        "integer",
        "bigint",
        "smallint",
        "numeric",
        "decimal",
        "float4",
        "float8",
        "real",
        "double precision",
        "date",
        "time",
        "timestamp",
        "timestamptz",
        "interval",
        "bool",
        "boolean",
        "jsonb",
        "json",
        "uuid",
    }
)

#: ``SQLValueFunctionOp`` member-name prefixes that are pure date/time reads.
_TIME_VALUE_FUNCTION_PREFIXES = (
    "SVFOP_CURRENT_DATE",
    "SVFOP_CURRENT_TIME",
    "SVFOP_CURRENT_TIMESTAMP",
    "SVFOP_LOCALTIME",
)


def _name_parts(names: Any) -> list[str]:
    """Return the lower-cased string components of a qualified-name node tuple."""
    return [str(part.sval).lower() for part in (names or ()) if isinstance(part, ast.String)]


class _ShapeVisitor(Visitor):
    """Walk one subtree and record the first policy violation."""

    def __init__(self, allowed_relations: frozenset[str], top_with: Any | None) -> None:
        """Remember the relation names visible in this subtree and the (separately checked) top WITH."""
        super().__init__()
        self.allowed_relations = allowed_relations
        self.top_with = top_with
        self.scoped_refs = 0
        self.error: str | None = None

    def fail(self, message: str) -> None:
        """Record only the first violation so the caller reports the root cause."""
        if self.error is None:
            self.error = message

    def visit(self, ancestors: Any, node: Any) -> Any:
        """Apply the default-deny node policy; returning ``Skip`` stops descent after a violation."""
        kind = type(node).__name__
        if self.error is not None:
            return Skip
        if kind not in _ALLOWED_NODES:
            self.fail(f"SQL construct {kind} is not allowed")
            return Skip
        if isinstance(node, ast.WithClause):
            if node is self.top_with:
                return Skip  # checked per-CTE by the caller, in definition order
            self.fail("WITH is only allowed at the top level of the query")
            return Skip
        rule = _NODE_RULES.get(kind)
        if rule is not None:
            rule(self, node)
        return None


def _rule_select(visitor: _ShapeVisitor, node: ast.SelectStmt) -> None:
    """Reject ``SELECT INTO`` and locking clauses (``FOR UPDATE`` and friends)."""
    if node.intoClause is not None:
        visitor.fail("SELECT INTO is not allowed")
    if node.lockingClause:
        visitor.fail("SQL locking clauses are not allowed")


def _rule_relation(visitor: _ShapeVisitor, node: ast.RangeVar) -> None:
    """Allow only unqualified scoped views or CTE names defined earlier in this query."""
    if node.schemaname or node.catalogname:
        visitor.fail("schema-qualified relations are not allowed")
        return
    name = str(node.relname).lower()
    if name not in visitor.allowed_relations:
        visitor.fail(f"relation {name} is not allowed; use scoped_observations or scoped_documents")
    elif name in ALLOWED_RELATIONS:
        visitor.scoped_refs += 1


def _rule_function(visitor: _ShapeVisitor, node: ast.FuncCall) -> None:
    """Allow only allowlisted, optionally ``pg_catalog``-qualified functions."""
    parts = _name_parts(node.funcname)
    if len(parts) == 2 and parts[0] == "pg_catalog":
        parts = parts[1:]
    if len(parts) != 1 or parts[0] not in _ALLOWED_FUNCTIONS:
        visitor.fail(f"function {'.'.join(parts) or '?'} is not allowed")


def _rule_operator(visitor: _ShapeVisitor, node: ast.A_Expr) -> None:
    """Allow only a single unqualified operator from the allowlist (no ``OPERATOR(schema.op)``)."""
    parts = [str(p.sval) for p in (node.name or ()) if isinstance(p, ast.String)]
    if len(parts) != 1 or parts[0].upper() not in _ALLOWED_OPERATORS:
        visitor.fail(f"operator {' '.join(parts) or '?'} is not allowed")


def _rule_cast(visitor: _ShapeVisitor, node: ast.TypeCast) -> None:
    """Restrict casts to plain data types (rejects ``regclass``, ``regproc``, ``xml`` and friends)."""
    parts = _name_parts(node.typeName.names if node.typeName else None)
    if parts and parts[0] == "pg_catalog":
        parts = parts[1:]
    if len(parts) != 1 or parts[0] not in _ALLOWED_CAST_TYPES:
        visitor.fail(f"cast to {'.'.join(parts) or '?'} is not allowed")


def _rule_value_function(visitor: _ShapeVisitor, node: ast.SQLValueFunction) -> None:
    """Allow only the date/time keywords (``CURRENT_DATE`` ...); reject ``CURRENT_USER``/``SESSION_USER`` etc."""
    if not str(getattr(node.op, "name", "")).startswith(_TIME_VALUE_FUNCTION_PREFIXES):
        visitor.fail("SQL value function is not allowed")


def _rule_sort(visitor: _ShapeVisitor, node: ast.SortBy) -> None:
    """Reject ``ORDER BY ... USING <operator>`` which names an arbitrary operator."""
    if node.useOp:
        visitor.fail("ORDER BY USING is not allowed")


_NODE_RULES: dict[str, Callable[[Any, Any], None]] = {
    "SelectStmt": _rule_select,
    "RangeVar": _rule_relation,
    "FuncCall": _rule_function,
    "A_Expr": _rule_operator,
    "TypeCast": _rule_cast,
    "SortBy": _rule_sort,
    "SQLValueFunction": _rule_value_function,
}


def _check_subtree(node: Any, allowed: frozenset[str], top_with: Any | None) -> _ShapeVisitor:
    """Run the shape visitor over ``node`` and return it with ``error``/``scoped_refs`` populated."""
    visitor = _ShapeVisitor(allowed, top_with)
    visitor(node)
    return visitor


def check_select_policy(sql: str) -> None:
    """Parse ``sql`` and raise ``ValueError`` unless it satisfies the read-only SELECT allowlist.

    Policy: exactly one ``SELECT``; every relation is ``scoped_observations`` / ``scoped_documents`` or a
    CTE defined *earlier* in the same top-level ``WITH`` (``WITH RECURSIVE`` is rejected: unbounded CPU); no
    schema-qualified names; only allowlisted node types, functions, operators and cast targets; no
    ``INTO``, locking, set-returning functions or data-modifying CTEs; at least one scoped view must be
    read. CTE visibility is checked in definition order so a forward reference cannot resolve to a
    real table of the same name.

    Raises:
        ValueError: With a model-readable reason on the first violation or on a syntax error.
    """
    try:
        statements = parse_sql(sql)
    except ParseError as exc:
        raise ValueError(f"SQL syntax error: {exc}") from exc
    if len(statements) != 1:
        raise ValueError("SQL query must be a single statement without comments or semicolons")
    stmt = statements[0].stmt
    if not isinstance(stmt, ast.SelectStmt):
        raise ValueError("SQL query must begin with SELECT or WITH ... SELECT")
    with_clause = stmt.withClause
    visible = set(ALLOWED_RELATIONS)
    scoped_refs = 0
    if with_clause is not None and with_clause.recursive:
        raise ValueError("WITH RECURSIVE is not allowed")
    for cte in (with_clause.ctes or ()) if with_clause is not None else ():
        if not isinstance(cte.ctequery, ast.SelectStmt):
            raise ValueError("data-modifying WITH queries are not allowed")
        name = str(cte.ctename).lower()
        result = _check_subtree(cte.ctequery, frozenset(visible), with_clause)
        if result.error:
            raise ValueError(result.error)
        scoped_refs += result.scoped_refs
        visible.add(name)
    result = _check_subtree(stmt, frozenset(visible), with_clause)
    if result.error:
        raise ValueError(result.error)
    if scoped_refs + result.scoped_refs == 0:
        raise ValueError("SQL query must reference scoped_observations or scoped_documents")
