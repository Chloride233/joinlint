from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from benchmarks.agent_join.contracts import Edge
from benchmarks.agent_join.sql_edges import canonical_edge, extract_join_edges
from benchmarks.formal_eval.contracts import StrictModel
from joinlint.contracts import canonical_json
from joinlint.mcp_contracts import recovery_guidance


class OracleDocument(StrictModel):
    schema_map: dict[str, dict[str, str]] = Field(alias="schema")
    allowed_graphs: tuple[tuple[Edge, ...], ...]


def load_oracle(path: Path) -> OracleDocument:
    if path.is_symlink() or not path.is_file():
        raise ValueError("oracle document must be one regular file")
    return OracleDocument.model_validate_json(path.read_text(encoding="utf-8"))


def plan_oracle(
    document: OracleDocument,
    entity_refs: list[dict[str, str]],
    start_ref: str,
    expected_grain_ref: str | None = None,
    max_depth: int = 4,
    include_alternatives: bool = False,
) -> dict[str, Any]:
    del include_alternatives
    refs = {item.get("ref"): item.get("entity") for item in entity_refs}
    if (
        len(refs) != len(entity_refs)
        or not 2 <= len(refs) <= 8
        or start_ref not in refs
        or (expected_grain_ref is not None and expected_grain_ref not in refs)
        or not 1 <= max_depth <= 4
        or any(not isinstance(ref, str) or not isinstance(entity, str) for ref, entity in refs.items())
    ):
        return _error("get_join_plan", "INVALID_ARGUMENT", inconclusive=False)
    requested = set(refs.values())
    for graph in document.allowed_graphs:
        normalized = {canonical_edge(*edge) for edge in graph}
        graph_entities = {endpoint.rsplit(".", 1)[0] for edge in normalized for endpoint in edge}
        if requested <= graph_entities:
            selected = [
                edge
                for edge in sorted(normalized)
                if {endpoint.rsplit(".", 1)[0] for endpoint in edge} <= requested
            ]
            if len(selected) < len(requested) - 1:
                continue
            edges = [_proof_edge(edge, refs) for edge in selected]
            plan_id = hashlib.sha256(
                canonical_json(
                    {
                        "entity_refs": sorted(entity_refs, key=lambda item: str(item["ref"])),
                        "start_ref": start_ref,
                        "expected_grain_ref": expected_grain_ref or start_ref,
                        "max_depth": max_depth,
                        "edges": edges,
                    }
                )
            ).hexdigest()
            proof = {
                "schema_version": 2,
                "plan_id": plan_id,
                "claim_scope": "physical_join_only",
                "source_id": "oracle",
                "snapshot_id": "0" * 64,
                "policy_version": "oracle-v1",
                "entity_refs": entity_refs,
                "start_ref": start_ref,
                "expected_grain_ref": expected_grain_ref or start_ref,
                "max_depth": max_depth,
                "edges": edges,
                "alternatives": [],
                "verified_at": "1970-01-01T00:00:00Z",
                "freshness_checked_at": "1970-01-01T00:00:00Z",
            }
            return {
                "schema_version": 3,
                "command": "get_join_plan",
                "status": "ok",
                "data": {
                    "proof": proof,
                    "lifecycle": {
                        "status": "current",
                        "reason": None,
                        "freshness_checked_at": "1970-01-01T00:00:00Z",
                    },
                    "validated_scope": ["physical_join_endpoints", "proof_binding"],
                    "not_validated_scope": ["answer_correctness"],
                    "execution_count": 0,
                },
                "findings": [],
                "error": None,
            }
    return _error("get_join_plan", "NO_VERIFIED_PATH", inconclusive=True)


def validate_oracle_sql(
    document: OracleDocument,
    sql: str,
    *,
    plan_id: str | None = None,
) -> dict[str, Any]:
    try:
        predicted = extract_join_edges(sql, document.schema_map)
    except ValueError:
        return _error("validate_sql", "SQL_PARSE_ERROR", inconclusive=False)
    allowed = [{canonical_edge(*edge) for edge in graph} for graph in document.allowed_graphs]
    safe = any(predicted == graph for graph in allowed)
    graph = {
        "source_id": "oracle",
        "entity_refs": [],
        "edges": [
            {
                "left_ref": f"i{index * 2}",
                "right_ref": f"i{index * 2 + 1}",
                "endpoint_pairs": [list(edge)],
                "join_kind": "inner",
            }
            for index, edge in enumerate(sorted(predicted))
        ],
    }
    return {
        "schema_version": 3,
        "command": "validate_sql",
        "status": "ok" if safe else "findings",
        "data": {
            "normalized_join_graph": graph,
            "matched_relationship_ids": [],
            "matched_evidence_ids": [],
            "proof_matched": plan_id is not None,
            "repair_proof": None,
            "validated_scope": [
                "physical_join_endpoints",
                *(["proof_binding"] if plan_id is not None else []),
            ],
            "not_validated_scope": [
                "answer_correctness",
                *([] if plan_id is not None else ["proof_binding"]),
            ],
            "execution_count": 0,
        },
        "findings": (
            []
            if safe
            else [
                {
                    "code": "UNSUPPORTED_JOIN_EDGE",
                    "severity": "blocking",
                    "message": "UNSUPPORTED_JOIN_EDGE",
                    "guidance": recovery_guidance(
                        "UNSUPPORTED_JOIN_EDGE"
                    ).model_dump(mode="json"),
                }
            ]
        ),
        "error": None,
    }


def create_oracle_server(document: OracleDocument) -> FastMCP:
    mcp = FastMCP("JoinLintOracle")

    @mcp.tool(name="get_join_plan")
    def get_join_plan(
        entity_refs: list[dict[str, str]],
        start_ref: str,
        expected_grain_ref: str | None = None,
        max_depth: int = 4,
        include_alternatives: bool = False,
    ) -> dict[str, Any]:
        return plan_oracle(
            document,
            entity_refs,
            start_ref,
            expected_grain_ref,
            max_depth,
            include_alternatives,
        )

    @mcp.tool(name="validate_sql")
    def validate_sql(
        sql: str,
        dialect: str = "sqlite",
        source_id: str | None = None,
        plan_id: str | None = None,
        expected_grain_ref: str | None = None,
    ) -> dict[str, Any]:
        del source_id, expected_grain_ref
        if dialect != "sqlite":
            return _error("validate_sql", "UNSUPPORTED_DIALECT", inconclusive=False)
        return validate_oracle_sql(document, sql, plan_id=plan_id)

    return mcp


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oracle", type=Path, required=True)
    arguments = parser.parse_args(argv)
    create_oracle_server(load_oracle(arguments.oracle)).run(transport="stdio")
    return 0


def _proof_edge(edge: Edge, refs: dict[str, str]) -> dict[str, Any]:
    child, parent = edge
    child_entity, child_column = child.rsplit(".", 1)
    parent_entity, parent_column = parent.rsplit(".", 1)
    ref_by_entity = {entity: ref for ref, entity in refs.items()}
    relationship_id = hashlib.sha256(f"{child}\0{parent}".encode("utf-8")).hexdigest()
    return {
        "from_ref": ref_by_entity[child_entity],
        "to_ref": ref_by_entity[parent_entity],
        "relationship_id": relationship_id,
        "evidence_id": hashlib.sha256(f"evidence\0{relationship_id}".encode("utf-8")).hexdigest(),
        "child": {"entity_id": child_entity, "columns": [child_column]},
        "parent": {"entity_id": parent_entity, "columns": [parent_column]},
        "predicates": [f"{child} = {parent}"],
        "traversal": "forward",
        "cardinality": "one_to_one",
        "provenance": "curated",
    }


def _error(command: str, code: str, *, inconclusive: bool) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "command": command,
        "status": "inconclusive" if inconclusive else "error",
        "data": None,
        "findings": [],
        "error": {
            "code": code,
            "message": code,
            "guidance": recovery_guidance(code).model_dump(mode="json"),
        },
    }
