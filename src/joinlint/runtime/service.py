from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from joinlint.config import load_config
from joinlint.errors import JoinLintError
from joinlint.mcp_contracts import (
    GetJoinPlanRequest,
    GetJoinPlanResponse,
    JoinPlanData,
    SQLValidationData,
    ValidateSQLRequest,
    ValidateSQLResponse,
    not_validated_scope,
    validated_scope,
)
from joinlint.model import ModelV1, load_model
from joinlint.paths import SafeProject
from joinlint.runtime.cache import RuntimeCache
from joinlint.runtime.domain import (
    POLICY_VERSION,
    VERIFIER_VERSION,
    AuthorizedRelationship,
    EntityDefinition,
    EntityRef,
    JoinProof,
    SourceCatalog,
    SourceIdentity,
)
from joinlint.runtime.evidence import (
    build_authorized_graph,
    relationship_definitions,
    verify_relationship,
)
from joinlint.runtime.planner import plan_join, proof_lifecycle
from joinlint.runtime.sources import (
    SQLiteSnapshot,
    extract_sqlite_catalog,
    locate_sqlite_sources,
    snapshot_sqlite,
)
from joinlint.runtime.sql import SQLValidationError, normalize_sql_graph, validate_sql_graph


@dataclass(frozen=True)
class RuntimeContext:
    identity: SourceIdentity
    snapshot: SQLiteSnapshot
    catalog: SourceCatalog
    graph: tuple[AuthorizedRelationship, ...]


class RuntimeService:
    def __init__(
        self,
        root: Path,
        explicit_sources: tuple[str, ...] = (),
        *,
        auto: bool = True,
        cache: RuntimeCache | None = None,
    ) -> None:
        with SafeProject(root) as boundary:
            self.root = boundary.root
        self.explicit_sources = explicit_sources
        self.auto = auto and not explicit_sources
        self.cache = cache or RuntimeCache()

    def get_join_plan(self, request: GetJoinPlanRequest) -> GetJoinPlanResponse:
        with self._contexts() as contexts:
            context, resolved_refs = self._resolve_plan_context(contexts, request.entity_refs)
            proof = plan_join(
                resolved_refs,
                request.start_ref,
                request.expected_grain_ref,
                request.max_depth,
                request.include_alternatives,
                context.graph,
                entity_definitions=context.catalog.entities,
            )
            self.cache.store_proof(proof)
            for alternative in proof.alternatives:
                self.cache.store_proof(alternative)
            lifecycle = proof_lifecycle(
                proof,
                current_snapshot_id=context.snapshot.document.snapshot_id,
                current_policy_version=POLICY_VERSION,
                available_evidence_ids=(edge.evidence.evidence_id for edge in context.graph),
            )
            return GetJoinPlanResponse(
                status="ok",
                data=JoinPlanData(
                    proof=proof,
                    lifecycle=lifecycle,
                    validated_scope=validated_scope(proof_bound=True),
                    not_validated_scope=not_validated_scope(proof_bound=True),
                ),
            )

    def validate_sql(self, request: ValidateSQLRequest) -> ValidateSQLResponse:
        if request.dialect != "sqlite":
            raise JoinLintError("UNSUPPORTED_DIALECT", "only SQLite is supported", 2)
        proof = self._proof(request.plan_id)
        with self._contexts() as contexts:
            if proof is not None:
                context = self._context_for_source(contexts, proof.source_id)
                if request.source_id is not None and request.source_id != proof.source_id:
                    raise JoinLintError("INVALID_ARGUMENT", "source_id does not match proof", 2)
                lifecycle = proof_lifecycle(
                    proof,
                    current_snapshot_id=context.snapshot.document.snapshot_id,
                    current_policy_version=POLICY_VERSION,
                    available_evidence_ids=(edge.evidence.evidence_id for edge in context.graph),
                )
                if lifecycle.status == "stale":
                    raise JoinLintError("PROOF_STALE", "proof must be replanned", 3)
                if lifecycle.status == "unverifiable":
                    raise JoinLintError("PROOF_UNVERIFIABLE", "proof evidence is unavailable", 3)
                if (
                    request.expected_grain_ref is not None
                    and request.expected_grain_ref not in {item.ref for item in proof.entity_refs}
                ):
                    raise JoinLintError(
                        "INVALID_ARGUMENT",
                        "expected_grain_ref is not present in proof",
                        2,
                    )
                graph = normalize_sql_graph(request.sql, context.catalog, context.identity.source_id)
                return self._validation_response(
                    graph,
                    context.graph,
                    context.catalog.entities,
                    proof=proof,
                    expected_grain_ref=request.expected_grain_ref,
                )
            context, graph = self._normalize_without_proof(contexts, request)
            return self._validation_response(
                graph,
                context.graph,
                context.catalog.entities,
                proof=None,
                expected_grain_ref=request.expected_grain_ref,
            )

    def _validation_response(
        self,
        graph: object,
        authorized_graph: tuple[AuthorizedRelationship, ...],
        entity_definitions: tuple[EntityDefinition, ...],
        *,
        proof: JoinProof | None,
        expected_grain_ref: str | None,
    ) -> ValidateSQLResponse:
        from joinlint.runtime.domain import NormalizedJoinGraph

        if not isinstance(graph, NormalizedJoinGraph):
            raise TypeError("normalized graph is required")
        outcome = validate_sql_graph(
            graph,
            authorized_graph,
            proof=proof,
            expected_grain_ref=expected_grain_ref,
            entity_definitions=entity_definitions,
        )
        proof_bound = proof is not None
        return ValidateSQLResponse(
            status="findings" if outcome.findings else "ok",
            data=SQLValidationData(
                normalized_join_graph=outcome.graph,
                matched_relationship_ids=tuple(sorted(outcome.matched_relationship_ids)),
                matched_evidence_ids=tuple(sorted(outcome.matched_evidence_ids)),
                proof_matched=outcome.proof_matched,
                validated_scope=validated_scope(proof_bound=proof_bound),
                not_validated_scope=not_validated_scope(proof_bound=proof_bound),
                execution_count=0,
            ),
            findings=outcome.findings,
        )

    def _proof(self, plan_id: str | None) -> JoinProof | None:
        if plan_id is None:
            return None
        try:
            proof = self.cache.load_proof(plan_id)
        except ValueError as error:
            raise JoinLintError("INVALID_ARGUMENT", "plan_id is invalid", 2) from error
        if proof is None:
            raise JoinLintError("PROOF_NOT_AVAILABLE", "proof is not available", 3)
        return proof

    @contextmanager
    def _contexts(self) -> Iterator[tuple[RuntimeContext, ...]]:
        identities = locate_sqlite_sources(
            self.root,
            self.explicit_sources,
            auto=self.auto,
        )
        legacy_model, curated_by_locator = self._legacy_inputs()
        with ExitStack() as stack:
            contexts: list[RuntimeContext] = []
            for identity in identities:
                snapshot = stack.enter_context(snapshot_sqlite(self.root, identity))
                catalog = extract_sqlite_catalog(snapshot)
                seeds = relationship_definitions(
                    catalog,
                    legacy_model,
                    curated_source_ids=curated_by_locator.get(identity.relative_locator, ()),
                )
                evidence = tuple(self._evidence(snapshot, catalog, seed) for seed in seeds)
                graph = build_authorized_graph(
                    seeds,
                    evidence,
                    snapshot.document.snapshot_id,
                )
                contexts.append(
                    RuntimeContext(
                        identity=identity,
                        snapshot=snapshot,
                        catalog=catalog,
                        graph=graph,
                    )
                )
            yield tuple(contexts)

    def _evidence(self, snapshot: SQLiteSnapshot, catalog: SourceCatalog, seed):  # type: ignore[no-untyped-def]
        selector = self.cache.evidence_selector(
            seed.definition.relationship_id,
            snapshot.document.snapshot_id,
            seed.provenance,
            VERIFIER_VERSION,
        )
        cached = self.cache.load_evidence_for(
            seed.definition.relationship_id,
            snapshot.document.snapshot_id,
            seed.provenance,
            VERIFIER_VERSION,
        )
        if cached is not None:
            return cached
        with self.cache.locked(selector):
            cached = self.cache.load_evidence_for(
                seed.definition.relationship_id,
                snapshot.document.snapshot_id,
                seed.provenance,
                VERIFIER_VERSION,
            )
            if cached is not None:
                return cached
            record = verify_relationship(snapshot, catalog, seed)
            self.cache.store_evidence(record)
            return record

    def _resolve_plan_context(
        self,
        contexts: tuple[RuntimeContext, ...],
        requested: tuple[EntityRef, ...],
    ) -> tuple[RuntimeContext, tuple[EntityRef, ...]]:
        matches: list[tuple[RuntimeContext, tuple[EntityRef, ...]]] = []
        for context in contexts:
            resolved: list[EntityRef] = []
            for item in requested:
                entity = self._resolve_entity(context.catalog, item.entity)
                if entity is None:
                    break
                resolved.append(EntityRef(ref=item.ref, entity=entity))
            else:
                matches.append((context, tuple(resolved)))
        if not matches:
            raise JoinLintError("ENTITY_NOT_FOUND", "requested entities were not found", 3)
        if len(matches) > 1:
            raise JoinLintError("SOURCE_AMBIGUOUS", "requested entities match multiple sources", 3)
        return matches[0]

    @staticmethod
    def _resolve_entity(catalog: SourceCatalog, requested: str) -> str | None:
        exact = [entity.entity_id for entity in catalog.entities if entity.entity_id == requested]
        if exact:
            return exact[0]
        physical = requested.rsplit(".", 1)[-1]
        matches = [
            entity.entity_id for entity in catalog.entities if entity.physical_name == physical
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _context_for_source(
        contexts: tuple[RuntimeContext, ...],
        source_id: str,
    ) -> RuntimeContext:
        matches = [context for context in contexts if context.identity.source_id == source_id]
        if not matches:
            raise JoinLintError("PROOF_UNVERIFIABLE", "proof source is unavailable", 3)
        return matches[0]

    def _normalize_without_proof(
        self,
        contexts: tuple[RuntimeContext, ...],
        request: ValidateSQLRequest,
    ):
        if request.source_id is not None:
            context = self._context_for_source(contexts, request.source_id)
            return context, normalize_sql_graph(
                request.sql,
                context.catalog,
                context.identity.source_id,
            )
        matches = []
        unknown_error: SQLValidationError | None = None
        for context in contexts:
            try:
                graph = normalize_sql_graph(
                    request.sql,
                    context.catalog,
                    context.identity.source_id,
                )
            except SQLValidationError as error:
                if error.code != "UNKNOWN_ENTITY":
                    raise
                unknown_error = error
                continue
            matches.append((context, graph))
        if not matches:
            if unknown_error is not None:
                raise unknown_error
            raise JoinLintError("SOURCE_NOT_FOUND", "no source can validate SQL", 3)
        if len(matches) > 1:
            raise JoinLintError("SOURCE_AMBIGUOUS", "SQL matches multiple sources", 3)
        return matches[0]

    def _legacy_inputs(self) -> tuple[ModelV1 | None, dict[str, tuple[str, ...]]]:
        with SafeProject(self.root) as project:
            if not project.exists_relative(PurePosixPath(".joinlint")):
                return None, {}
            has_config = project.exists_relative(PurePosixPath(".joinlint/config.yaml"))
            has_model = project.exists_relative(PurePosixPath(".joinlint/model.yaml"))
        if not has_config or not has_model:
            return None, {}
        config = load_config(self.root)
        model = load_model(self.root)
        by_locator: dict[str, list[str]] = {}
        for source_id, source in config.sources.items():
            if source.kind == "sqlite":
                by_locator.setdefault(source.path.as_posix(), []).append(source_id)
        return model, {
            locator: tuple(sorted(source_ids, key=lambda value: value.encode("utf-8")))
            for locator, source_ids in by_locator.items()
        }
