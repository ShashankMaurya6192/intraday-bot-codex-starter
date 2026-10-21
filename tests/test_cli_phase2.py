from trading_bot.__main__ import _parser, main


def test_cli_exposes_data_analysis_and_paper_only_commands() -> None:
    parser = _parser()
    commands = (
        "refresh-instruments",
        "refresh-intraday-universe",
        "refresh-nifty500",
        "record-scanner-market",
        "bootstrap-history",
        "test-historical",
        "data-status",
        "replay",
        "scan",
        "compare-projection-replay",
        "paper-init",
        "paper-status",
        "paper-kill-switch",
        "paper-report",
    )

    for command in commands:
        arguments = [command]
        if command == "replay":
            arguments.extend(["--date", "2026-08-10"])
        if command == "test-historical":
            arguments.extend(["--symbol", "SBIN", "--date", "2026-08-07"])
        if command == "compare-projection-replay":
            arguments.extend(["--date", "2026-08-10"])
        if command == "paper-kill-switch":
            arguments.extend(["--reason", "test"])
        parsed = parser.parse_args(arguments)
        assert parsed.command == command

    help_text = parser.format_help().lower()
    assert "place-order" not in help_text
    assert "paper-fill" not in help_text
    assert "live-order" not in help_text


def test_paper_report_has_no_data_until_a_session_is_initialized(
    tmp_path, monkeypatch, capsys
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'empty-report.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    assert main(
        [
            "paper-report",
            "--from-date",
            "2026-08-10",
            "--to-date",
            "2026-08-11",
            "--json",
        ]
    ) == 2

    output = capsys.readouterr().out
    assert '"status": "NO_DATA"' in output
    assert '"broker_orders_available": false' in output
    assert '"estimate_only": true' in output


def test_paper_cli_initializes_reports_and_kills_without_broker_orders(
    tmp_path, monkeypatch, capsys
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'paper-cli.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)

    assert main(["paper-init", "--date", "2026-08-10"]) == 0
    assert main(["paper-status", "--date", "2026-08-10", "--json"]) == 0
    assert main(
        [
            "paper-kill-switch",
            "--date",
            "2026-08-10",
            "--reason",
            "operator_test",
        ]
    ) == 0
    assert main(["paper-status", "--date", "2026-08-10", "--json"]) == 2

    output = capsys.readouterr().out
    assert '"broker_orders_available": false' in output
    assert '"kill_switch_active": true' in output
    assert "No broker order was created or transmitted." in output
