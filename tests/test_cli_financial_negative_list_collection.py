from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from app import cli
from app.providers.tushare_financial_negative_list_collection import FinancialNegativeListCollectionResult

runner = CliRunner()


def test_collect_cli_unauthorized_fails_before_token_or_client(monkeypatch) -> None:
    token_called = False
    client_called = False

    def _fail_auth(**_: object) -> object:
        raise ValueError("authorization missing")

    def _token() -> str:
        nonlocal token_called
        token_called = True
        return "never-used"

    class _Client:
        def __init__(self, _: str) -> None:
            nonlocal client_called
            client_called = True

    monkeypatch.setattr(
        "app.research.layer_two_financial_negative_list_collection_run_contract.verify_run_contract_file",
        lambda **_: (object(), object()),
    )
    monkeypatch.setattr(
        "app.research.layer_two_financial_negative_list_collection_authorization.verify_collection_authorization_file",
        _fail_auth,
    )
    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", _token)
    monkeypatch.setattr("app.providers.tushare_client.LiveTushareClient", _Client)

    result = runner.invoke(
        cli.app,
        [
            "collect-tushare-financial-negative-list",
            "--authorization-file",
            "missing-auth.json",
        ],
    )
    assert result.exit_code == 1
    assert token_called is False
    assert client_called is False


def test_collect_cli_with_mock_authorization_and_progress(monkeypatch, tmp_path: Path) -> None:
    class _RunContract:
        fixed_staging_dir = "data/raw/a-share-financial-negative-list-20200101-20241231-v3"
        network_authorized = False

    class _Authorization:
        authorization_id = "1" * 64
        run_contract_id = "a" * 64
        run_contract_version = "financial-negative-list-collection-run-contract-v3"
        staging_dir = "data/raw/a-share-financial-negative-list-20200101-20241231-v3"

    class _AuthorizationResult:
        run_contract_id = "a" * 64
        run_contract_version = "financial-negative-list-collection-run-contract-v3"
        response_boundary_policy_id = "b" * 64
        response_boundary_policy_file_sha256 = "c" * 64
        response_boundary_reason_code = "FNLD-013"

    monkeypatch.setattr(
        "app.research.layer_two_financial_negative_list_collection_run_contract.verify_run_contract_file",
        lambda **_: (_RunContract(), object()),
    )
    monkeypatch.setattr(
        "app.research.layer_two_financial_negative_list_collection_authorization.verify_collection_authorization_file",
        lambda **_: (_Authorization(), _AuthorizationResult()),
    )
    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", lambda: "x")
    monkeypatch.setattr("app.providers.tushare_client.LiveTushareClient", lambda _token: object())

    def _fake_collect(**kwargs: object) -> FinancialNegativeListCollectionResult:
        progress = kwargs.get("progress_callback")
        if callable(progress):
            progress("balancesheet", 50, 22176, 50, 5544)
            progress("fina_audit", 22176, 22176, 5544, 5544)
        staging = tmp_path / "staging"
        return FinancialNegativeListCollectionResult(
            staging_dir=staging,
            request_id="a" * 64,
            collection_authorization_id="1" * 64,
            protocol_id="b" * 64,
            requested_symbols=5544,
            partition_count=22176,
            completed_partitions=10,
            reused_partitions=22166,
            source_manifest_path=staging / "source_manifest.json",
            quality_report_path=staging / "quality_report.json",
            collection_manifest_path=staging / "collection_manifest.json",
        )

    monkeypatch.setattr(
        "app.providers.tushare_financial_negative_list_collection.collect_tushare_financial_negative_list",
        _fake_collect,
    )

    result = runner.invoke(
        cli.app,
        [
            "collect-tushare-financial-negative-list",
            "--authorization-file",
            str(tmp_path / "auth.json"),
        ],
    )
    assert result.exit_code == 0
    assert "collection_progress endpoint=balancesheet done=50/22176" in result.stdout
    assert "collection_progress endpoint=fina_audit done=22176/22176" in result.stdout
    assert "collection_authorization_id=" + ("1" * 64) in result.stdout


def test_verify_collection_cli_does_not_read_token(monkeypatch, tmp_path: Path) -> None:
    def _token() -> str:
        raise AssertionError("token must not be read")

    monkeypatch.setattr("app.providers.tushare_client.read_tushare_token", _token)

    def _fake_verify(**_: object) -> FinancialNegativeListCollectionResult:
        staging = tmp_path / "staging"
        return FinancialNegativeListCollectionResult(
            staging_dir=staging,
            request_id="a" * 64,
            collection_authorization_id="1" * 64,
            protocol_id="b" * 64,
            requested_symbols=5544,
            partition_count=22176,
            completed_partitions=0,
            reused_partitions=22176,
            source_manifest_path=staging / "source_manifest.json",
            quality_report_path=staging / "quality_report.json",
            collection_manifest_path=staging / "collection_manifest.json",
        )

    monkeypatch.setattr(
        "app.providers.tushare_financial_negative_list_collection.verify_financial_negative_list_collection",
        _fake_verify,
    )
    result = runner.invoke(
        cli.app,
        [
            "verify-tushare-financial-negative-list-collection",
            "--staging-dir",
            str(tmp_path / "staging"),
        ],
    )
    assert result.exit_code == 0
    assert "collection_authorization_id=" + ("1" * 64) in result.stdout


def test_verify_run_contract_cli_reuses_preverified_contract_for_authorization(monkeypatch) -> None:
    verify_calls = {"run_contract": 0}

    class _RunContract:
        run_contract_id = "a" * 64
        status = "prepared_not_authorized"
        network_authorized = False
        requires_fresh_user_authorization = True

    class _RunContractResult:
        run_contract_id = "a" * 64
        run_contract_version = "financial-negative-list-collection-run-contract-v3"
        status = "prepared_not_authorized"
        network_authorized = False
        requires_fresh_user_authorization = True
        canonical_symbol_count = 5544
        expected_partition_count = 22176
        response_boundary_policy_id = "b" * 64
        response_boundary_policy_path = "config/research/financial-negative-list-response-boundary-policy-v2.json"
        response_boundary_reason_code = "FNLD-013"

    def _verify_run_contract_file(**_: object) -> tuple[object, object]:
        verify_calls["run_contract"] += 1
        return _RunContract(), _RunContractResult()

    def _verify_authorization_file(**kwargs: object) -> tuple[object, object]:
        assert kwargs.get("preverified_run_contract") is not None
        assert kwargs.get("preverified_run_contract_result") is not None

        class _AuthorizationResult:
            authorization_id = "1" * 64
            staging_dir = "data/raw/a-share-financial-negative-list-20200101-20241231-v3"
            network_collection_allowed = True

        return object(), _AuthorizationResult()

    monkeypatch.setattr(
        "app.research.layer_two_financial_negative_list_collection_run_contract.verify_run_contract_file",
        _verify_run_contract_file,
    )
    monkeypatch.setattr(
        "app.research.layer_two_financial_negative_list_collection_authorization.verify_collection_authorization_file",
        _verify_authorization_file,
    )

    result = runner.invoke(
        cli.app,
        [
            "verify-financial-negative-list-collection-run-contract",
            "--run-contract",
            "config/research/financial-negative-list-collection-run-contract-v3.json",
            "--require-authorized",
            "--authorization-file",
            "config/research/mock-auth.json",
        ],
    )
    assert result.exit_code == 0
    assert verify_calls["run_contract"] == 1
