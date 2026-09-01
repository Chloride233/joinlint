from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from benchmarks.agent_join.sql_edges import canonical_edge
from benchmarks.formal_eval.contracts import QueryContract, QueryOutputField
from benchmarks.formal_eval.semantic_failure import (
    DatabaseSpec,
    TaskSpec,
    build_semantic_failure_bundle,
)


DATASET_RELEASE = "semantic-join-contract-ambiguity-v1"
MINIMUM_UNOPPOSED_TREATMENT_WINS = 6


@dataclass(frozen=True)
class DomainVocabulary:
    database_id: str
    domain: str
    primary: str
    primary_role: str
    secondary: str
    secondary_role: str
    tertiary: str
    tertiary_role: str
    records: str
    record_name: str
    details: str
    detail_name: str
    parent_ids: tuple[int, int, int]
    record_ids: tuple[int, int, int]


def build_semantic_contract_ambiguity_v1(
    sealed_root: Path,
    output: Path,
) -> dict[str, object]:
    return build_semantic_failure_bundle(
        sealed_root,
        output,
        dataset_release=DATASET_RELEASE,
        database_specs=_database_specs(),
        claim_boundary="trusted_query_contract_opaque_relationship_stress_only",
        require_opaque_alternative_graphs=True,
        source_metadata={
            "construction_rule": "all_preconstructed_opaque_relationship_tasks_v1",
            "outcome_based_task_selection": False,
            "relationship_ambiguity_gate": {
                "candidate_graphs_per_task_minimum": 2,
                "candidate_graphs_preserve_entity_scope": True,
                "candidate_graphs_preserve_join_depth": True,
                "candidate_edge_types_match": True,
                "visible_relationship_columns": "link_letter_only",
                "system_catalog_access": "denied_in_evaluation_database_tool",
            },
            "exact_test_design": {
                "statistical_test": "exact_two_sided_mcnemar",
                "alpha": 0.05,
                "paired_unit_count": 20,
                "minimum_unopposed_treatment_wins_for_significance": (
                    MINIMUM_UNOPPOSED_TREATMENT_WINS
                ),
                "p_value_at_boundary": 0.03125,
                "p_value_one_fewer": 0.0625,
            },
        },
    )


def _database_specs() -> tuple[DatabaseSpec, ...]:
    vocabularies = (
        DomainVocabulary(
            "logistics_opaque",
            "logistics",
            "consignees",
            "consignee",
            "hubs",
            "routing hub",
            "vehicles",
            "assigned vehicle",
            "loads",
            "load",
            "load_lines",
            "load line",
            (101, 202, 303),
            (1001, 1002, 1003),
        ),
        DomainVocabulary(
            "newsroom_opaque",
            "media",
            "authors",
            "commissioning author",
            "desks",
            "publishing desk",
            "assets",
            "attached asset",
            "stories",
            "story",
            "story_assets",
            "story-asset row",
            (11, 22, 33),
            (1101, 1102, 1103),
        ),
        DomainVocabulary(
            "maintenance_opaque",
            "maintenance",
            "customers",
            "requesting customer",
            "branches",
            "servicing branch",
            "technicians",
            "assigned technician",
            "tickets",
            "ticket",
            "visits",
            "service visit",
            (7, 14, 28),
            (1201, 1202, 1203),
        ),
        DomainVocabulary(
            "grants_opaque",
            "grants",
            "recipients",
            "award recipient",
            "programs",
            "funding program",
            "reviewers",
            "assigned reviewer",
            "awards",
            "award",
            "reviews",
            "award review",
            (5, 15, 25),
            (1301, 1302, 1303),
        ),
    )
    return tuple(
        DatabaseSpec(
            vocabulary.database_id,
            vocabulary.domain,
            _schema_sql(vocabulary),
            _tasks(vocabulary),
        )
        for vocabulary in vocabularies
    )


def _schema_sql(vocabulary: DomainVocabulary) -> str:
    first, second, third = vocabulary.parent_ids
    record_first, record_second, record_third = vocabulary.record_ids
    return f"""
CREATE TABLE {vocabulary.primary} (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE {vocabulary.secondary} (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE {vocabulary.tertiary} (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE {vocabulary.records} (
    id INTEGER PRIMARY KEY,
    link_a INTEGER NOT NULL REFERENCES {vocabulary.primary}(id),
    link_b INTEGER NOT NULL,
    link_c INTEGER NOT NULL REFERENCES {vocabulary.secondary}(id),
    link_d INTEGER NOT NULL,
    reference TEXT NOT NULL
);
CREATE TABLE {vocabulary.details} (
    id INTEGER PRIMARY KEY,
    link_a INTEGER NOT NULL REFERENCES {vocabulary.records}(id),
    link_b INTEGER NOT NULL,
    link_c INTEGER NOT NULL REFERENCES {vocabulary.tertiary}(id),
    link_d INTEGER NOT NULL,
    quantity INTEGER NOT NULL
);
INSERT INTO {vocabulary.primary} VALUES ({first},'Aster'),({second},'Beryl'),({third},'Coda');
INSERT INTO {vocabulary.secondary} VALUES ({first},'North'),({second},'Central'),({third},'South');
INSERT INTO {vocabulary.tertiary} VALUES ({first},'Ibis'),({second},'Juniper'),({third},'Kestrel');
INSERT INTO {vocabulary.records} VALUES
    ({record_first},{second},{first},{third},{second},'R-1'),
    ({record_second},{third},{second},{first},{third},'R-2'),
    ({record_third},{first},{third},{second},{first},'R-3');
INSERT INTO {vocabulary.details} VALUES
    (9001,{record_second},{record_first},{third},{second},1),
    (9002,{record_third},{record_second},{first},{third},2),
    (9003,{record_first},{record_third},{second},{first},1),
    (9004,{record_first},{record_second},{third},{first},3);
"""


def _tasks(vocabulary: DomainVocabulary) -> tuple[TaskSpec, ...]:
    p = vocabulary.primary
    s = vocabulary.secondary
    t = vocabulary.tertiary
    r = vocabulary.records
    d = vocabulary.details
    return (
        TaskSpec(
            "record_primary",
            f"Return each {vocabulary.record_name} reference and its {vocabulary.primary_role} label.",
            f"SELECT r.reference,p.label FROM {r} r JOIN {p} p ON r.link_a=p.id ORDER BY r.id",
            f"SELECT r.reference,p.label FROM {r} r JOIN {p} p ON r.link_b=p.id ORDER BY r.id",
            _graph((f"{r}.link_a", f"{p}.id")),
            (p, r),
            "opaque_parallel_integer_links",
            "one_to_many",
            _contract(
                (p, r), r, (r, "reference", "record_reference"), (p, "label", "primary_label")
            ),
        ),
        TaskSpec(
            "record_secondary",
            f"Return each {vocabulary.record_name} reference and its {vocabulary.secondary_role} label.",
            f"SELECT r.reference,s.label FROM {r} r JOIN {s} s ON r.link_c=s.id ORDER BY r.id",
            f"SELECT r.reference,s.label FROM {r} r JOIN {s} s ON r.link_d=s.id ORDER BY r.id",
            _graph((f"{r}.link_c", f"{s}.id")),
            (r, s),
            "opaque_parallel_integer_links",
            "one_to_many",
            _contract(
                (r, s), r, (r, "reference", "record_reference"), (s, "label", "secondary_label")
            ),
        ),
        TaskSpec(
            "detail_record",
            f"Return each {vocabulary.detail_name} identifier and its {vocabulary.record_name} reference.",
            f"SELECT d.id,r.reference FROM {d} d JOIN {r} r ON d.link_a=r.id ORDER BY d.id",
            f"SELECT d.id,r.reference FROM {d} d JOIN {r} r ON d.link_b=r.id ORDER BY d.id",
            _graph((f"{d}.link_a", f"{r}.id")),
            (d, r),
            "opaque_parallel_integer_links",
            "one_to_many",
            _contract((d, r), d, (d, "id", "detail_id"), (r, "reference", "record_reference")),
        ),
        TaskSpec(
            "detail_tertiary",
            f"Return each {vocabulary.detail_name} identifier and its {vocabulary.tertiary_role} label.",
            f"SELECT d.id,t.label FROM {d} d JOIN {t} t ON d.link_c=t.id ORDER BY d.id",
            f"SELECT d.id,t.label FROM {d} d JOIN {t} t ON d.link_d=t.id ORDER BY d.id",
            _graph((f"{d}.link_c", f"{t}.id")),
            (d, t),
            "opaque_parallel_integer_links",
            "one_to_many",
            _contract((d, t), d, (d, "id", "detail_id"), (t, "label", "tertiary_label")),
        ),
        TaskSpec(
            "primary_tertiary",
            f"Return the {vocabulary.primary_role} label and {vocabulary.tertiary_role} label for every {vocabulary.detail_name}.",
            f"SELECT p.label,t.label FROM {d} d JOIN {r} r ON d.link_a=r.id JOIN {p} p ON r.link_a=p.id JOIN {t} t ON d.link_c=t.id ORDER BY d.id",
            f"SELECT p.label,t.label FROM {d} d JOIN {r} r ON d.link_b=r.id JOIN {p} p ON r.link_b=p.id JOIN {t} t ON d.link_d=t.id ORDER BY d.id",
            _graph(
                (f"{d}.link_a", f"{r}.id"),
                (f"{r}.link_a", f"{p}.id"),
                (f"{d}.link_c", f"{t}.id"),
            ),
            (d, p, r, t),
            "opaque_parallel_integer_links",
            "compound",
            _contract(
                (d, p, r, t), d, (p, "label", "primary_label"), (t, "label", "tertiary_label")
            ),
        ),
    )


def _graph(*edges: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((canonical_edge(*edge) for edge in edges)))


def _contract(
    required_entities: tuple[str, ...],
    row_grain_entity: str,
    *output_fields: tuple[str, str, str],
) -> QueryContract:
    return QueryContract(
        required_entities=required_entities,
        output_fields=tuple(
            QueryOutputField(entity=entity, column=column, alias=alias)
            for entity, column, alias in output_fields
        ),
        row_grain_entity=row_grain_entity,
    )
