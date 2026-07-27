from __future__ import annotations

import itertools
from collections import defaultdict, deque
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from joinlint.contracts import canonical_json
from joinlint.runtime.domain import (
    AuthorizedRelationship,
    EntityDefinition,
    EntityRef,
    JoinProof,
    NormalizedJoinEdge,
    NormalizedJoinGraph,
    RuntimeFinding,
    SQLValidationOutcome,
    SourceCatalog,
)


MAX_SQL_BYTES = 65_536


class SQLValidationError(ValueError):
    def __init__(self, code: str, *, blocking: bool) -> None:
        super().__init__(code)
        self.code = code
        self.blocking = blocking


@dataclass(frozen=True)
class ResolvedColumn:
    ref: str
    entity_id: str
    column: str

    @property
    def endpoint(self) -> str:
        return f"{self.entity_id}.{self.column}"


@dataclass(frozen=True)
class MatchedEdge:
    normalized: NormalizedJoinEdge
    relationship: AuthorizedRelationship
    child_ref: str
    parent_ref: str


def normalize_sql_graph(
    sql: str,
    catalog: SourceCatalog,
    source_id: str,
) -> NormalizedJoinGraph:
    if not sql or len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise SQLValidationError("REQUEST_TOO_LARGE", blocking=False)
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except ParseError as error:
        raise SQLValidationError("SQL_PARSE_ERROR", blocking=False) from error
    if len(statements) != 1:
        raise SQLValidationError("MULTIPLE_STATEMENTS", blocking=True)
    statement = statements[0]
    if not isinstance(statement, exp.Select):
        raise SQLValidationError("UNSUPPORTED_SQL_STATEMENT", blocking=True)
    _validate_join_shapes(statement)
    schema = {
        entity.physical_name: {column.name: column.physical_type for column in entity.columns}
        for entity in catalog.entities
    }
    try:
        qualified = qualify(
            statement,
            dialect="sqlite",
            schema=schema,
            quote_identifiers=False,
            validate_qualify_columns=True,
        )
    except SqlglotError as error:
        raise SQLValidationError("UNKNOWN_ENTITY", blocking=True) from error
    scopes = list(traverse_scope(qualified))
    scope_ids = {id(scope): index for index, scope in enumerate(scopes)}
    entities_by_table = {entity.physical_name.casefold(): entity for entity in catalog.entities}
    grouped: dict[tuple[str, str, str], list[tuple[str, str]]] = defaultdict(list)
    refs: dict[str, EntityRef] = {}
    for scope in scopes:
        scope_columns = {id(column) for column in scope.columns}
        for equality in scope.expression.find_all(exp.EQ):
            left = equality.left
            right = equality.right
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
                continue
            if id(left) not in scope_columns or id(right) not in scope_columns:
                continue
            left_column = _resolve_column(
                scope, left, scope_ids, entities_by_table, instance_alias=left.table
            )
            right_column = _resolve_column(
                scope, right, scope_ids, entities_by_table, instance_alias=right.table
            )
            if left_column.ref == right_column.ref:
                continue
            join = _containing_join(equality)
            join_kind = "left" if join is not None and join.side.upper() == "LEFT" else "inner"
            refs[left_column.ref] = EntityRef(
                ref=left_column.ref,
                entity=left_column.entity_id,
            )
            refs[right_column.ref] = EntityRef(
                ref=right_column.ref,
                entity=right_column.entity_id,
            )
            left_ref, right_ref = left_column.ref, right_column.ref
            left_endpoint, right_endpoint = left_column.endpoint, right_column.endpoint
            if join_kind == "left":
                nullable_ref = f"s{scope_ids[id(scope)]}:{join.this.alias_or_name}"
                if left_ref == nullable_ref:
                    left_ref, right_ref = right_ref, left_ref
                    left_endpoint, right_endpoint = right_endpoint, left_endpoint
                elif right_ref != nullable_ref:
                    raise SQLValidationError("UNSUPPORTED_JOIN_SHAPE", blocking=True)
            elif right_ref.encode("utf-8") < left_ref.encode("utf-8"):
                left_ref, right_ref = right_ref, left_ref
                left_endpoint, right_endpoint = right_endpoint, left_endpoint
            grouped[(left_ref, right_ref, join_kind)].append((left_endpoint, right_endpoint))
    for scope in scopes:
        if len(scope.sources) <= 1:
            continue
        required_refs = {
            f"s{scope_ids[id(scope)]}:{alias}"
            for alias, source in scope.sources.items()
            if isinstance(source, (exp.Table, Scope))
        }
        if not required_refs <= set(refs):
            raise SQLValidationError("MISSING_JOIN_PREDICATE", blocking=True)
    edges = tuple(
        NormalizedJoinEdge(
            left_ref=left_ref,
            right_ref=right_ref,
            endpoint_pairs=tuple(
                sorted(pairs, key=lambda pair: (pair[0].encode("utf-8"), pair[1].encode("utf-8")))
            ),
            join_kind=join_kind,  # type: ignore[arg-type]
        )
        for (left_ref, right_ref, join_kind), pairs in sorted(
            grouped.items(),
            key=lambda item: tuple(value.encode("utf-8") for value in item[0]),
        )
    )
    return _canonicalize_instances(source_id, tuple(refs.values()), edges)


def validate_sql_graph(
    graph: NormalizedJoinGraph,
    authorized_graph: tuple[AuthorizedRelationship, ...],
    *,
    proof: JoinProof | None = None,
    expected_grain_ref: str | None = None,
    entity_definitions: tuple[EntityDefinition, ...] = (),
) -> SQLValidationOutcome:
    matched: list[MatchedEdge] = []
    findings: list[RuntimeFinding] = []
    for edge in graph.edges:
        relationship = _match_relationship(edge, authorized_graph)
        if relationship is None:
            findings.append(_finding("UNSUPPORTED_JOIN_EDGE"))
            continue
        child_ref, parent_ref = _relationship_refs(edge, relationship)
        matched.append(
            MatchedEdge(
                normalized=edge,
                relationship=relationship,
                child_ref=child_ref,
                parent_ref=parent_ref,
            )
        )
    proof_mapping: dict[str, str] | None = None
    if proof is not None:
        proof_mapping = _proof_mapping(graph, matched, proof) if not findings else None
        if proof_mapping is None:
            findings = [_finding("PROOF_GRAPH_MISMATCH")]
    expected_sql_ref = _expected_sql_ref(graph, proof, proof_mapping, expected_grain_ref)
    if expected_sql_ref is not None and not findings:
        if entity_definitions and not _expected_entity_has_stable_grain(
            graph,
            expected_sql_ref,
            entity_definitions,
        ):
            findings.append(_finding("GRAIN_INCOMPATIBLE"))
        elif not _grain_compatible(expected_sql_ref, matched):
            findings.append(_finding("GRAIN_INCOMPATIBLE"))
    findings = sorted(findings, key=lambda item: item.code.encode("utf-8"))
    return SQLValidationOutcome(
        decision="block" if findings else "pass",
        graph=graph,
        matched_relationship_ids=tuple(
            item.relationship.definition.relationship_id for item in matched
        ),
        matched_evidence_ids=tuple(item.relationship.evidence.evidence_id for item in matched),
        proof_matched=None if proof is None else proof_mapping is not None,
        findings=tuple(findings),
    )


def _validate_join_shapes(statement: exp.Select) -> None:
    for join in statement.find_all(exp.Join):
        if join.method.upper() == "NATURAL":
            raise SQLValidationError("UNSUPPORTED_JOIN_KIND", blocking=True)
        explicit_cross = join.kind.upper() == "CROSS" and "pivots" in join.args
        if explicit_cross or join.side.upper() in {"RIGHT", "FULL"}:
            raise SQLValidationError("UNSUPPORTED_JOIN_KIND", blocking=True)
        if join.side.upper() not in {"", "LEFT"}:
            raise SQLValidationError("UNSUPPORTED_JOIN_KIND", blocking=True)
        condition = join.args.get("on")
        if condition is not None and not _equality_conjunction(condition):
            raise SQLValidationError("NON_EQUALITY_JOIN", blocking=True)


def _equality_conjunction(condition: exp.Expression) -> bool:
    if isinstance(condition, exp.And):
        return _equality_conjunction(condition.left) and _equality_conjunction(condition.right)
    return (
        isinstance(condition, exp.EQ)
        and isinstance(condition.left, exp.Column)
        and isinstance(condition.right, exp.Column)
    )


def _resolve_column(
    scope: Scope,
    column: exp.Column,
    scope_ids: dict[int, int],
    entities_by_table: dict[str, EntityDefinition],
    *,
    instance_alias: str,
) -> ResolvedColumn:
    source = scope.sources.get(column.table)
    ref = f"s{scope_ids[id(scope)]}:{instance_alias}"
    if isinstance(source, exp.Table):
        entity = entities_by_table.get(source.name.casefold())
        if entity is None:
            raise SQLValidationError("UNKNOWN_ENTITY", blocking=True)
        physical_column = next(
            (
                item.name
                for item in entity.columns
                if item.name.casefold() == column.name.casefold()
            ),
            None,
        )
        if physical_column is None:
            raise SQLValidationError("UNKNOWN_ENTITY", blocking=True)
        return ResolvedColumn(
            ref=ref,
            entity_id=entity.entity_id,
            column=physical_column,
        )
    if isinstance(source, Scope):
        origin = _scope_projection(source, column.name, scope_ids, entities_by_table)
        return ResolvedColumn(ref=ref, entity_id=origin.entity_id, column=origin.column)
    raise SQLValidationError("UNKNOWN_ENTITY", blocking=True)


def _scope_projection(
    scope: Scope,
    name: str,
    scope_ids: dict[int, int],
    entities_by_table: dict[str, EntityDefinition],
) -> ResolvedColumn:
    for select in scope.expression.selects:
        if select.alias_or_name != name:
            continue
        expression = select.this if isinstance(select, exp.Alias) else select
        if isinstance(expression, exp.Column):
            return _resolve_column(
                scope,
                expression,
                scope_ids,
                entities_by_table,
                instance_alias=expression.table,
            )
    raise SQLValidationError("UNRESOLVED_DERIVED_COLUMN", blocking=True)


def _containing_join(equality: exp.EQ) -> exp.Join | None:
    current: exp.Expression | None = equality
    while current is not None:
        if isinstance(current, exp.Join):
            return current
        current = current.parent
    return None


def _canonicalize_instances(
    source_id: str,
    entity_refs: tuple[EntityRef, ...],
    edges: tuple[NormalizedJoinEdge, ...],
) -> NormalizedJoinGraph:
    refs_by_entity: dict[str, list[str]] = defaultdict(list)
    for item in entity_refs:
        refs_by_entity[item.entity].append(item.ref)
    choices: list[list[dict[str, str]]] = []
    next_index = 0
    for entity in sorted(refs_by_entity, key=lambda value: value.encode("utf-8")):
        raw_refs = sorted(refs_by_entity[entity], key=lambda value: value.encode("utf-8"))
        canonical_refs = [f"i{index}" for index in range(next_index, next_index + len(raw_refs))]
        next_index += len(raw_refs)
        choices.append(
            [
                dict(zip(permutation, canonical_refs, strict=True))
                for permutation in itertools.permutations(raw_refs)
            ]
        )
    best: NormalizedJoinGraph | None = None
    best_bytes: bytes | None = None
    for combination in itertools.product(*choices):
        mapping = {key: value for partial in combination for key, value in partial.items()}
        candidate_edges = tuple(
            sorted(
                (_remap_edge(edge, mapping) for edge in edges),
                key=lambda edge: (
                    edge.left_ref.encode("utf-8"),
                    edge.right_ref.encode("utf-8"),
                    edge.join_kind,
                    edge.endpoint_pairs,
                ),
            )
        )
        candidate_refs = tuple(
            sorted(
                (
                    EntityRef(ref=mapping[item.ref], entity=item.entity)
                    for item in entity_refs
                ),
                key=lambda item: item.ref.encode("utf-8"),
            )
        )
        candidate = NormalizedJoinGraph(
            source_id=source_id,
            entity_refs=candidate_refs,
            edges=candidate_edges,
        )
        body = canonical_json(candidate.model_dump(mode="json"))
        if best_bytes is None or body < best_bytes:
            best = candidate
            best_bytes = body
    if best is None:
        return NormalizedJoinGraph(source_id=source_id, entity_refs=(), edges=())
    return best


def _remap_edge(
    edge: NormalizedJoinEdge,
    mapping: dict[str, str],
) -> NormalizedJoinEdge:
    left_ref = mapping[edge.left_ref]
    right_ref = mapping[edge.right_ref]
    pairs = edge.endpoint_pairs
    if edge.join_kind == "inner" and right_ref.encode("utf-8") < left_ref.encode("utf-8"):
        left_ref, right_ref = right_ref, left_ref
        pairs = tuple((right, left) for left, right in pairs)
    return NormalizedJoinEdge(
        left_ref=left_ref,
        right_ref=right_ref,
        endpoint_pairs=tuple(
            sorted(
                pairs,
                key=lambda pair: (pair[0].encode("utf-8"), pair[1].encode("utf-8")),
            )
        ),
        join_kind=edge.join_kind,
    )


def _match_relationship(
    edge: NormalizedJoinEdge,
    graph: tuple[AuthorizedRelationship, ...],
) -> AuthorizedRelationship | None:
    observed = {_canonical_pair(left, right) for left, right in edge.endpoint_pairs}
    matches = []
    for relationship in graph:
        expected = {
            _canonical_pair(
                f"{relationship.definition.child.entity_id}.{child}",
                f"{relationship.definition.parent.entity_id}.{parent}",
            )
            for child, parent in zip(
                relationship.definition.child.columns,
                relationship.definition.parent.columns,
                strict=True,
            )
        }
        if observed == expected:
            matches.append(relationship)
    return min(
        matches,
        key=lambda item: item.definition.relationship_id.encode("utf-8"),
        default=None,
    )


def _relationship_refs(
    edge: NormalizedJoinEdge,
    relationship: AuthorizedRelationship,
) -> tuple[str, str]:
    child_endpoints = {
        f"{relationship.definition.child.entity_id}.{column}"
        for column in relationship.definition.child.columns
    }
    left_endpoints = {pair[0] for pair in edge.endpoint_pairs}
    if left_endpoints == child_endpoints:
        return edge.left_ref, edge.right_ref
    return edge.right_ref, edge.left_ref


def _proof_mapping(
    graph: NormalizedJoinGraph,
    matched: list[MatchedEdge],
    proof: JoinProof,
) -> dict[str, str] | None:
    sql_by_entity: dict[str, list[str]] = defaultdict(list)
    proof_by_entity: dict[str, list[str]] = defaultdict(list)
    for item in graph.entity_refs:
        sql_by_entity[item.entity].append(item.ref)
    for item in proof.entity_refs:
        proof_by_entity[item.entity].append(item.ref)
    if set(sql_by_entity) != set(proof_by_entity):
        return None
    choices: list[list[dict[str, str]]] = []
    for entity in sorted(sql_by_entity, key=lambda value: value.encode("utf-8")):
        sql_refs = sorted(sql_by_entity[entity], key=lambda value: value.encode("utf-8"))
        proof_refs = sorted(proof_by_entity[entity], key=lambda value: value.encode("utf-8"))
        if len(sql_refs) != len(proof_refs):
            return None
        choices.append(
            [dict(zip(sql_refs, permutation, strict=True)) for permutation in itertools.permutations(proof_refs)]
        )
    expected = sorted(
        (
            edge.relationship_id,
            edge.from_ref if edge.traversal == "forward" else edge.to_ref,
            edge.to_ref if edge.traversal == "forward" else edge.from_ref,
        )
        for edge in proof.edges
    )
    for combination in itertools.product(*choices):
        mapping = {key: value for partial in combination for key, value in partial.items()}
        actual = sorted(
            (
                item.relationship.definition.relationship_id,
                mapping[item.child_ref],
                mapping[item.parent_ref],
            )
            for item in matched
        )
        if actual == expected:
            return mapping
    return None


def _expected_sql_ref(
    graph: NormalizedJoinGraph,
    proof: JoinProof | None,
    mapping: dict[str, str] | None,
    expected_grain_ref: str | None,
) -> str | None:
    if proof is not None and mapping is not None:
        target = expected_grain_ref or proof.expected_grain_ref
        for sql_ref, proof_ref in mapping.items():
            if proof_ref == target:
                return sql_ref
        return None
    if expected_grain_ref is None:
        return None
    matches = [
        item.ref
        for item in graph.entity_refs
        if item.ref == expected_grain_ref
        or item.entity == expected_grain_ref
        or item.entity.rsplit(".", 1)[-1] == expected_grain_ref
    ]
    return matches[0] if len(matches) == 1 else None


def _grain_compatible(expected_ref: str, matched: list[MatchedEdge]) -> bool:
    adjacency: dict[str, list[tuple[str, int]]] = defaultdict(list)
    nullable_refs = {
        item.normalized.right_ref
        for item in matched
        if item.normalized.join_kind == "left"
    }
    if expected_ref in nullable_refs:
        return False
    for item in matched:
        measurements = item.relationship.evidence.measurements
        adjacency[item.child_ref].append((item.parent_ref, measurements.max_parents_per_child))
        adjacency[item.parent_ref].append((item.child_ref, measurements.max_children_per_parent))
    queue = deque([(expected_ref, None)])
    while queue:
        current, previous = queue.popleft()
        for other, max_matches in adjacency[current]:
            if other == previous:
                continue
            if max_matches > 1:
                return False
            queue.append((other, current))
    return True


def _expected_entity_has_stable_grain(
    graph: NormalizedJoinGraph,
    expected_ref: str,
    definitions: tuple[EntityDefinition, ...],
) -> bool:
    entity_ref = next((item for item in graph.entity_refs if item.ref == expected_ref), None)
    if entity_ref is None:
        return False
    entity = next(
        (item for item in definitions if item.entity_id == entity_ref.entity),
        None,
    )
    if entity is None:
        return False
    if entity.primary_key:
        return True
    nullable = {column.name for column in entity.columns if column.nullable}
    return any(not (set(key) & nullable) for key in entity.unique_keys)


def _canonical_pair(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((left, right), key=lambda value: value.encode("utf-8")))  # type: ignore[return-value]


def _finding(code: str) -> RuntimeFinding:
    return RuntimeFinding(code=code, severity="blocking", message=code)
