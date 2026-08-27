from unittest.mock import MagicMock

import main


def _fake_page(context_pages):
    page = MagicMock()
    page.context = MagicMock()
    page.context.pages = context_pages
    return page


def test_find_login_popup_returns_none_when_only_known_pages_open():
    jobright_page = MagicMock()
    company_page = _fake_page([])
    company_page.context.pages = [jobright_page, company_page]

    assert main._find_login_popup(company_page, {jobright_page, company_page}) is None


def test_find_login_popup_returns_unexpected_extra_page():
    jobright_page = MagicMock()
    company_page = _fake_page([])
    popup_page = MagicMock()
    company_page.context.pages = [jobright_page, company_page, popup_page]

    assert main._find_login_popup(company_page, {jobright_page, company_page}) is popup_page


def test_find_login_popup_ignores_earlier_listings_company_pages():
    # Regression: earlier listings' company tabs are deliberately left open
    # (so you can still review/submit them later) -- known_pages must cover
    # ALL of them, not just the current listing's, or every one of them
    # gets permanently misidentified as a login popup on every later
    # listing (this was worse in "apply to current page" mode, where
    # jobright_page and company_page collapse to the same object, but the
    # underlying bug affected normal jobright-listing mode too once a
    # couple of listings had been processed in the same run).
    jobright_page = MagicMock()
    older_company_page_1 = MagicMock()
    older_company_page_2 = MagicMock()
    current_company_page = _fake_page([])
    current_company_page.context.pages = [
        jobright_page, older_company_page_1, older_company_page_2, current_company_page,
    ]
    known_pages = {jobright_page, older_company_page_1, older_company_page_2, current_company_page}

    assert main._find_login_popup(current_company_page, known_pages) is None


def test_find_login_popup_apply_current_mode_does_not_flag_older_tabs():
    # "Apply to Current Page" aliases company_page to jobright_page itself
    # (see run_automation's confirm_or_reset docs), collapsing known_pages
    # to fewer distinct entries -- must still correctly ignore older,
    # legitimately-opened tabs instead of treating them as a login popup.
    page = MagicMock()
    older_company_page = MagicMock()
    page.context = MagicMock()
    page.context.pages = [page, older_company_page]
    known_pages = {page, older_company_page}

    assert main._find_login_popup(page, known_pages) is None
