import pytest

import main


def test_is_job_board_domain_matches_known_boards():
    assert main._is_job_board_domain("https://www.indeed.com/viewjob?jk=abc123")
    assert main._is_job_board_domain("https://secure.indeed.com/auth")
    assert main._is_job_board_domain("https://www.linkedin.com/jobs/view/123")
    assert main._is_job_board_domain("https://www.glassdoor.com/job-listing/123")
    assert main._is_job_board_domain("https://www.ziprecruiter.com/jobs/123")
    assert main._is_job_board_domain("https://www.monster.com/job-openings/123")


def test_is_job_board_domain_ignores_company_and_ats_sites():
    assert not main._is_job_board_domain("https://job-boards.greenhouse.io/company/jobs/123")
    assert not main._is_job_board_domain("https://company.wd12.myworkdayjobs.com/en-US/apply")
    assert not main._is_job_board_domain("https://jobright.ai/jobs/recommend")
    # Not a real subdomain match -- "notindeed.com" contains "indeed.com" as
    # a bare substring but isn't actually indeed.com or a subdomain of it.
    assert not main._is_job_board_domain("https://notindeed.com/careers")


def test_handle_reset_or_retry_raises_reset_requested():
    with pytest.raises(main._ResetRequested):
        main._handle_reset_or_retry("reset", company_page=object(), i=1, log=print)
    # Case-insensitive, whitespace-trimmed, same as every other
    # confirm_or_reset answer this checks.
    with pytest.raises(main._ResetRequested):
        main._handle_reset_or_retry("  RESET  ", company_page=object(), i=1, log=print)


def test_handle_reset_or_retry_returns_true_and_logs_for_retry_with_company_page():
    logged = []
    result = main._handle_reset_or_retry("retry", company_page=object(), i=3, log=logged.append)

    assert result is True
    assert logged == ["[3] Retrying autofill on the same page..."]


def test_handle_reset_or_retry_ignores_retry_without_company_page():
    # Nothing to retry on -- 'retry' should be a no-op, not raise or log.
    logged = []
    result = main._handle_reset_or_retry("retry", company_page=None, i=1, log=logged.append)

    assert result is False
    assert logged == []


def test_handle_reset_or_retry_returns_false_for_anything_else():
    assert main._handle_reset_or_retry("continue", company_page=object(), i=1, log=print) is False
    assert main._handle_reset_or_retry("", company_page=object(), i=1, log=print) is False
    assert main._handle_reset_or_retry(None, company_page=object(), i=1, log=print) is False
