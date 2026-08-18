from unittest.mock import MagicMock

import main


def _fake_page(context_pages):
    page = MagicMock()
    page.context = MagicMock()
    page.context.pages = context_pages
    return page


def test_find_login_popup_returns_none_when_only_expected_pages_open():
    jobright_page = MagicMock()
    company_page = _fake_page([])
    company_page.context.pages = [jobright_page, company_page]

    assert main._find_login_popup(jobright_page, company_page) is None


def test_find_login_popup_returns_unexpected_extra_page():
    jobright_page = MagicMock()
    company_page = _fake_page([])
    popup_page = MagicMock()
    company_page.context.pages = [jobright_page, company_page, popup_page]

    assert main._find_login_popup(jobright_page, company_page) is popup_page
