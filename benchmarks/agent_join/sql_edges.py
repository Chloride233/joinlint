from __future__ import annotations

import json
from collections.abc import Iterable

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from benchmarks.agent_join.contracts import Edge, JoinScore, Submission, ValidatedSQL


FORBIDDEN_QUERY_NODES = (
    exp.Alter,
    exp.Attach,
    exp.Command,
    exp.Copy,
    exp.Create,
    exp.Delete,
    exp.Drop,
    exp.Insert,
    exp.Into,
    exp.Merge,
    exp.Pragma,
    exp.Transaction,
    exp.Update,
)


def canonical_edge(left: str, right: str) -> Edge:
    first, second = sorted((left, right), key=lambda value: value.encode("utf-8"))
    return first, second


def extract_submission(answer: str) -> Submission:
    document = json.loads(answer)
    if (
        not isinstance(document, dict)
        or set(document) != {"sql", "warning"}
        or not isinstance(document["sql"], str)
        or not isinstance(document["warning"], str)
    ):
        raise ValueError("submit_sql output must contain only string sql and warning")
    return Submission(sql=document["sql"].strip(), warning=document["warning"])


def extract_submitted_sql(answer: str) -> str:
    return extract_submission(answer).sql


def validate_readonly_select(sql: str) -> ValidatedSQL:
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError:
        return ValidatedSQL(error_code="SQL_PARSE_ERROR")
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return ValidatedSQL(error_code="UNSAFE_SQL")
    statement = statements[0]
    if any(statement.find(node_type) is not None for node_type in FORBIDDEN_QUERY_NODES):
        return ValidatedSQL(error_code="UNSAFE_SQL")
    if any(
        function.name.lower() == "load_extension"
        for function in statement.find_all(exp.Anonymous)
    ):
        return ValidatedSQL(error_code="UNSAFE_SQL")
    return ValidatedSQL(sql=statement.sql(dialect="sqlite"))


def extract_join_edges(
    sql: str,
    schema: dict[str, dict[str, str]],
) -> frozenset[Edge]:
    validated = validate_readonly_select(sql)
    if validated.sql is None:
        raise ValueError(validated.error_code or "INVALID_SQL")
    try:
        expression = sqlglot.parse_one(validated.sql, read="sqlite")
        expression = qualify(
            expression,
            dialect="sqlite",
            schema=schema,
            quote_identifiers=False,
            validate_qualify_columns=True,
        )
    except sqlglot.errors.SqlglotError as error:
        raise ValueError("SQL_QUALIFICATION_ERROR") from error

    edges: set[Edge] = set()
    for scope in traverse_scope(expression):
        scope_column_ids = {id(column) for column in scope.columns}
        for equality in scope.expression.find_all(exp.EQ):
            left = equality.left
            right = equality.right
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                continue
            if id(left) not in scope_column_ids or id(right) not in scope_column_ids:
                continue
            left_table = _physical_table(scope, left)
            right_table = _physical_table(scope, right)
            if left_table is None or right_table is None or left_table == right_table:
                continue
            edges.add(
                canonical_edge(
                    f"{left_table}.{left.name}",
                    f"{right_table}.{right.name}",
                )
            )
    return frozenset(edges)


def _physical_table(scope: Scope, column: exp.Column) -> str | None:
    source = scope.sources.get(column.table)
    if not isinstance(source, exp.Table):
        return None
    return source.name


def score_join_graph(
    predicted_edges: Iterable[Edge],
    allowed_graphs: Iterable[Iterable[Edge]],
) -> JoinScore:
    predicted = frozenset(canonical_edge(*edge) for edge in predicted_edges)
    allowed = [frozenset(canonical_edge(*edge) for edge in graph) for graph in allowed_graphs]
    if not allowed:
        raise ValueError("at least one allowed graph is required")

    candidates = [(_metrics(predicted, graph), graph) for graph in allowed]
    metrics, matched = min(
        candidates,
        key=lambda candidate: (
            -candidate[0][2],
            tuple(sorted(candidate[1], key=_edge_sort_key)),
        ),
    )
    precision, recall, f1 = metrics
    return JoinScore(
        wrong_join=predicted != matched,
        precision=precision,
        recall=recall,
        f1=f1,
        predicted_edges=sorted(predicted, key=_edge_sort_key),
        matched_graph=sorted(matched, key=_edge_sort_key),
    )


def _metrics(predicted: frozenset[Edge], expected: frozenset[Edge]) -> tuple[float, float, float]:
    if not predicted and not expected:
        return 1.0, 1.0, 1.0
    true_positive = len(predicted & expected)
    precision = true_positive / len(predicted) if predicted else 0.0
    recall = true_positive / len(expected) if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def _edge_sort_key(edge: Edge) -> tuple[bytes, bytes]:
    return edge[0].encode("utf-8"), edge[1].encode("utf-8")
