from __future__ import annotations

import pytest
from pydantic import ValidationError

from benchmarks.formal_eval.contracts import (
    AgentResultRow,
    DeterministicEvidenceBundle,
    PreregistrationV2,
    QueryContract,
    QueryOutputField,
    SealedAgentTask,
)
from benchmarks.formal_eval.fake import (
    fake_agent_rows,
    fake_deterministic_evidence,
    fake_preregistration,
)
from benchmarks.formal_eval.query_contract import render_task_input


def test_preregistration_requires_two_model_tiers() -> None:
    preregistration = fake_preregistration()
    payload = preregistration.model_dump(mode="python")
    payload["models"] = (preregistration.models[0], preregistration.models[0])

    with pytest.raises(ValidationError, match="two distinct model IDs"):
        PreregistrationV2.model_validate(payload)


def test_query_contract_must_match_the_scored_entity_set() -> None:
    contract = QueryContract(
        required_entities=("customers", "orders"),
        output_fields=(
            QueryOutputField(entity="orders", column="id", alias="order_id"),
            QueryOutputField(entity="customers", column="name", alias="customer_name"),
        ),
        row_grain_entity="orders",
    )

    with pytest.raises(ValidationError, match="query contract entity set"):
        SealedAgentTask(
            task_id="task-1",
            database_id="database-1",
            question="Return every order and customer name.",
            schema_text="TABLE customers; TABLE orders",
            schema={
                "customers": {"id": "integer", "name": "text"},
                "orders": {"id": "integer", "customer_id": "integer"},
            },
            sql_shape="customers.id=orders.customer_id",
            gold_sql=(
                "SELECT orders.id, customers.name FROM orders JOIN customers "
                "ON orders.customer_id = customers.id"
            ),
            database_path="databases/database-1.sqlite",
            expected_entities=("customers", "orders", "payments"),
            allowed_graphs=((('customers.id', 'orders.customer_id'),),),
            oracle_has_safe_path=True,
            query_contract=contract,
        )


def test_query_contract_rejects_an_output_field_absent_from_the_schema() -> None:
    with pytest.raises(ValidationError, match="query contract output field"):
        SealedAgentTask(
            task_id="task-1",
            database_id="database-1",
            question="Return every order and customer email.",
            schema_text="TABLE customers; TABLE orders",
            schema={
                "customers": {"id": "integer", "name": "text"},
                "orders": {"id": "integer", "customer_id": "integer"},
            },
            sql_shape="customers.id=orders.customer_id",
            gold_sql=(
                "SELECT orders.id, customers.name FROM orders JOIN customers "
                "ON orders.customer_id = customers.id"
            ),
            database_path="databases/database-1.sqlite",
            expected_entities=("customers", "orders"),
            allowed_graphs=((('customers.id', 'orders.customer_id'),),),
            oracle_has_safe_path=True,
            query_contract=QueryContract(
                required_entities=("customers", "orders"),
                output_fields=(
                    QueryOutputField(
                        entity="customers", column="email", alias="customer_email"
                    ),
                ),
                row_grain_entity="orders",
            ),
        )


def test_query_contract_is_rendered_without_join_predicates() -> None:
    task = SealedAgentTask(
        task_id="task-1",
        database_id="database-1",
        question="Return every order and customer name.",
        schema_text="TABLE customers; TABLE orders",
        schema={
            "customers": {"id": "integer", "name": "text"},
            "orders": {"id": "integer", "customer_id": "integer"},
        },
        sql_shape="customers.id=orders.customer_id",
        gold_sql=(
            "SELECT orders.id, customers.name FROM orders JOIN customers "
            "ON orders.customer_id = customers.id"
        ),
        database_path="databases/database-1.sqlite",
        expected_entities=("customers", "orders"),
        allowed_graphs=((('customers.id', 'orders.customer_id'),),),
        oracle_has_safe_path=True,
        query_contract=QueryContract(
            required_entities=("customers", "orders"),
            output_fields=(
                QueryOutputField(entity="orders", column="id", alias="order_id"),
                QueryOutputField(
                    entity="customers", column="name", alias="customer_name"
                ),
            ),
            row_grain_entity="orders",
        ),
    )

    rendered = render_task_input(task)

    assert "Trusted query contract (defines intent, not join predicates):" in rendered
    assert '"required_entities":["customers","orders"]' in rendered
    assert "orders.customer_id = customers.id" not in rendered


def test_preregistration_allows_two_tiers_from_one_declared_family() -> None:
    preregistration = fake_preregistration()

    assert {model.family for model in preregistration.models} == {"fake-series"}
    assert {model.tier for model in preregistration.models} == {
        "high_capability",
        "cost_efficient",
    }


def test_preregistration_rejects_cross_family_substitution() -> None:
    preregistration = fake_preregistration()
    payload = preregistration.model_dump(mode="python")
    payload["models"] = (
        preregistration.models[0],
        preregistration.models[1].model_copy(update={"family": "another-family"}),
    )

    with pytest.raises(ValidationError, match="one provider family with two tiers"):
        PreregistrationV2.model_validate(payload)


def test_preregistration_requires_digest_pinned_remote_image() -> None:
    preregistration = fake_preregistration()
    payload = preregistration.model_dump(mode="python")
    payload["image_reference"] = "ghcr.io/example/joinlint-formal:latest"

    with pytest.raises(ValidationError, match="frozen image digest"):
        PreregistrationV2.model_validate(payload)


def test_safe_abstention_is_a_success_only_without_oracle_path() -> None:
    abstention = next(row for row in fake_agent_rows() if row.safe_abstention)
    payload = abstention.model_dump(mode="python")
    payload["oracle_has_safe_path"] = True

    with pytest.raises(ValidationError, match="safe abstention"):
        AgentResultRow.model_validate(payload)


def test_failed_agent_outcome_requires_stable_failure_code() -> None:
    failed = next(
        row
        for row in fake_agent_rows()
        if row.condition == "control" and not row.join_correct_task_completion
    )
    payload = failed.model_dump(mode="python")
    payload["failure_code"] = None

    with pytest.raises(ValidationError, match="successful completion or failure code"):
        AgentResultRow.model_validate(payload)


def test_control_row_rejects_mcp_behavior() -> None:
    control = next(row for row in fake_agent_rows() if row.condition == "control")
    payload = control.model_dump(mode="python")
    payload["plan_called"] = True

    with pytest.raises(ValidationError, match="control rows"):
        AgentResultRow.model_validate(payload)


def test_deterministic_evidence_rejects_duplicate_case_ids() -> None:
    evidence = fake_deterministic_evidence()
    payload = evidence.model_dump(mode="python")
    payload["relationship_rows"] = (
        evidence.relationship_rows[0],
        evidence.relationship_rows[0],
    )

    with pytest.raises(ValidationError, match="duplicate cases"):
        DeterministicEvidenceBundle.model_validate(payload)
