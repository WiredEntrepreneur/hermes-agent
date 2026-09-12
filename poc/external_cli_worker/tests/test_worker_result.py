import json

import pytest

from poc.external_cli_worker.worker_result import (
    MAX_ENTRY_LENGTH,
    MAX_LIST_ENTRIES,
    MAX_SUMMARY_LENGTH,
    InvalidWorkerResult,
    WorkerOutcome,
    parse_and_validate_worker_result,
)


def _valid(**updates):
    value = {"schema_version": "1.0", "outcome": "COMPLETED", "summary": "done"}
    value.update(updates)
    return json.dumps(value)


def test_valid_result_normalizes_existing_workspace_paths(tmp_path):
    artifact = tmp_path / "nested" / "proof.txt"
    artifact.parent.mkdir()
    artifact.write_text("proof")
    result = parse_and_validate_worker_result(
        _valid(
            artifacts=[str(artifact)], changed_files=["nested/proof.txt"],
            evidence=["inspected source"], findings=["none"], tests=["3 passed"],
            continuation="review the patch",
        ),
        str(tmp_path),
    )
    assert result.outcome is WorkerOutcome.COMPLETED
    assert result.artifacts == ("nested/proof.txt",)
    assert result.changed_files == ("nested/proof.txt",)


@pytest.mark.parametrize(("response", "code"), [
    ("not json", "malformed_json"),
    ("[]", "top_level_not_object"),
    (json.dumps({"outcome": "COMPLETED", "summary": "done"}), "unsupported_schema_version"),
    (json.dumps({"schema_version": "2.0", "outcome": "COMPLETED", "summary": "done"}), "unsupported_schema_version"),
    (json.dumps({"schema_version": "1.0", "summary": "done"}), "outcome_missing_or_wrong_type"),
    (_valid(outcome="WHATEVER"), "unknown_outcome"),
    (json.dumps({"schema_version": "1.0", "outcome": "COMPLETED"}), "summary_wrong_type"),
    (_valid(summary="  "), "summary_empty"),
    (_valid(tests="pytest"), "tests_wrong_type"),
    (_valid(summary="x" * (MAX_SUMMARY_LENGTH + 1)), "summary_too_large"),
    (_valid(tests=["ok"] * (MAX_LIST_ENTRIES + 1)), "tests_too_many_entries"),
    (_valid(tests=["x" * (MAX_ENTRY_LENGTH + 1)]), "tests_entry_too_large"),
    (_valid(task_id="t_other"), "unexpected_fields"),
    (_valid(run_id=99), "unexpected_fields"),
    (_valid(command="rm something"), "unexpected_fields"),
])
def test_invalid_contract_is_rejected(response, code, tmp_path):
    with pytest.raises(InvalidWorkerResult, match=f"^{code}$"):
        parse_and_validate_worker_result(response, str(tmp_path))


@pytest.mark.parametrize(("field", "claim"), [
    ("artifacts", "../../outside.txt"),
    ("changed_files", "/etc/passwd"),
    ("artifacts", "https://example.invalid/proof"),
])
def test_path_claims_cannot_escape_or_be_followed(field, claim, tmp_path):
    with pytest.raises(InvalidWorkerResult, match=f"^{field}_outside_workspace$"):
        parse_and_validate_worker_result(_valid(**{field: [claim]}), str(tmp_path))


def test_missing_local_claim_is_rejected(tmp_path):
    with pytest.raises(InvalidWorkerResult, match="^changed_files_not_found$"):
        parse_and_validate_worker_result(
            _valid(changed_files=["not-created.py"]), str(tmp_path),
        )


def test_raw_result_size_is_bounded_before_persistence(tmp_path):
    response = _valid() + (" " * 32_768)
    with pytest.raises(InvalidWorkerResult, match="^result_too_large$"):
        parse_and_validate_worker_result(response, str(tmp_path))
