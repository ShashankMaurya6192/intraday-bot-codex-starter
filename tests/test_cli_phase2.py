from trading_bot.__main__ import _parser


def test_phase2_cli_exposes_only_data_and_analysis_commands() -> None:
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
    )

    for command in commands:
        arguments = [command]
        if command == "replay":
            arguments.extend(["--date", "2026-08-10"])
        if command == "test-historical":
            arguments.extend(["--symbol", "SBIN", "--date", "2026-08-07"])
        parsed = parser.parse_args(arguments)
        assert parsed.command == command

    help_text = parser.format_help().lower()
    assert "place-order" not in help_text
    assert "paper-fill" not in help_text
