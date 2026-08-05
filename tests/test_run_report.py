import io

from ytdigest.run_report import report_run_issues


def test_report_run_issues_prints_to_stderr(config, capsys):
    report_run_issues(
        config,
        run_id=10,
        status="partial",
        notes=["transcript phase aborted: tier2 blocked"],
    )
    err = capsys.readouterr().err
    assert "run #10 status=partial" in err
    assert "transcript phase aborted" in err


def test_report_run_issues_ok_with_no_notes_is_silent(config, capsys):
    report_run_issues(config, run_id=1, status="ok", notes=[])
    assert capsys.readouterr().err == ""
