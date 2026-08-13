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


def test_report_run_issues_truncates_long_note_list(config, capsys):
    notes = [f"channel {i} failed" for i in range(12)]
    report_run_issues(config, run_id=23, status="partial", notes=notes)
    err = capsys.readouterr().err
    assert "channel 0 failed" in err
    assert "channel 7 failed" in err
    assert "and 4 more" in err
