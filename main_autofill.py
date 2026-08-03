from unittest.mock import AsyncMock, MagicMock

import pytest

import autofill
import mapping_cache
from autofill import AutofillResult, FormField


class FakeLocator:
    def __init__(self, count: int = 0, visible: bool = True):
        self.fill = AsyncMock()
        self.select_option = AsyncMock()
        self.check = AsyncMock()
        self.set_input_files = AsyncMock()
        self.click = AsyncMock()
        self.count = AsyncMock(return_value=count)
        self.is_visible = AsyncMock(return_value=visible)
        self.all_inner_texts = AsyncMock(return_value=[])

        async def _wait_for(*args, **kwargs):
            if count == 0:
                raise TimeoutError("no matching element (fake)")

        self.wait_for = AsyncMock(side_effect=_wait_for)

    @property
    def first(self):
        return self

    def nth(self, index):
        return self


class FakePage:
    """Minimal stand-in for playwright.async_api.Page for unit tests.

    `.locator(selector)` and `.get_by_role(role, name)` both auto-vivify a
    not-found (count=0) FakeLocator by default -- use `set_role_button` to
    make a specific get_by_role(role, name) resolve as present.
    """

    def __init__(self, url="https://example-ats.com/apply"):
        self.url = url
        self._selector_locators: dict[str, FakeLocator] = {}
        self._role_locators: dict[tuple[str, str], FakeLocator] = {}
        self.screenshot = AsyncMock()
        self.wait_for_load_state = AsyncMock()
        self.wait_for_timeout = AsyncMock()

    def locator(self, selector: str) -> FakeLocator:
        if selector not in self._selector_locators:
            self._selector_locators[selector] = FakeLocator()
        return self._selector_locators[selector]

    def get_by_role(self, role: str, name: str | None = None, exact: bool = False) -> FakeLocator:
        key = (role, name)
        if key not in self._role_locators:
            self._role_locators[key] = FakeLocator()
        return self._role_locators[key]

    def set_role_button(self, role: str, name: str | None = None, count: int = 1) -> FakeLocator:
        locator = FakeLocator(count=count)
        self._role_locators[(role, name)] = locator
        return locator


PROFILE = {"first_name": "Jane", "email": "jane@example.com"}


@pytest.fixture(autouse=True)
def isolate_mapping_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(mapping_cache, "CACHE_PATH", str(tmp_path / "mapping_cache.json"))


async def test_single_step_form_fills_confident_fields(monkeypatch):
    fields = [
        FormField("f0", "First Name", "text", '[data-autofill-id="f0"]'),
        FormField("f1", "Country", "select", '[data-autofill-id="f1"]', options=["USA", "Canada"]),
    ]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill,
        "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": "Jane", "needs_review": False, "reasoning": "from profile"},
            {"field_id": "f1", "value": "USA", "needs_review": False, "reasoning": "from profile"},
        ],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["First Name", "Country"]
    assert result.needs_review == []
    assert result.errors == []
    page.locator('[data-autofill-id="f0"]').fill.assert_awaited_once_with("Jane")
    page.locator('[data-autofill-id="f1"]').select_option.assert_awaited_once_with(label="USA")


async def test_select_value_not_in_options_becomes_needs_review(monkeypatch):
    # A model can return a value that isn't verbatim one of the dropdown's
    # options (more common with smaller local models) -- that used to throw
    # inside select_option() and land silently in `errors`, never reaching the
    # interactive prompt. It should route to needs_review instead.
    fields = [FormField("f0", "Country", "select", '[data-autofill-id="f0"]', options=["United States", "Canada"])]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill,
        "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": "USA", "needs_review": False, "reasoning": "candidate is US based"},
        ],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == []
    assert result.errors == []
    assert len(result.needs_review) == 1
    assert result.needs_review[0]["label"] == "Country"
    assert result.needs_review[0]["options"] == ["United States", "Canada"]
    page.locator('[data-autofill-id="f0"]').select_option.assert_not_awaited()


async def test_select_value_matches_option_case_insensitively(monkeypatch):
    fields = [FormField("f0", "Country", "select", '[data-autofill-id="f0"]', options=["United States", "Canada"])]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill,
        "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": "united states  ", "needs_review": False, "reasoning": ""},
        ],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["Country"]
    assert result.needs_review == []
    page.locator('[data-autofill-id="f0"]').select_option.assert_awaited_once_with(label="United States")


RADIO_GROUP_OPTION_SELECTORS = ['[data-autofill-id="f0-opt0"]', '[data-autofill-id="f0-opt1"]', '[data-autofill-id="f0-opt2"]']


async def test_radio_group_exact_match(monkeypatch):
    fields = [FormField(
        "f0", "What pronouns would you like our team to use?", "radio-group", "",
        options=["He/Him", "She/Her", "They/Them"], option_selectors=RADIO_GROUP_OPTION_SELECTORS,
    )]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill, "get_mappings_cached",
        lambda fields, profile, domain: [{"field_id": "f0", "value": "They/Them", "needs_review": False, "reasoning": ""}],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["What pronouns would you like our team to use?"]
    page.locator('[data-autofill-id="f0-opt2"]').click.assert_awaited_once()
    page.locator('[data-autofill-id="f0-opt0"]').click.assert_not_awaited()


async def test_radio_group_word_overlap_fallback_match(monkeypatch):
    fields = [FormField(
        "f0", "Veteran Status", "radio-group", "",
        options=["I am not a protected veteran", "I identify as a protected veteran"],
        option_selectors=['[data-autofill-id="f0-opt0"]', '[data-autofill-id="f0-opt1"]'],
    )]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill, "get_mappings_cached",
        lambda fields, profile, domain: [{"field_id": "f0", "value": "Not a veteran", "needs_review": False, "reasoning": ""}],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["Veteran Status"]
    page.locator('[data-autofill-id="f0-opt0"]').click.assert_awaited_once()


async def test_radio_group_no_match_becomes_needs_review(monkeypatch):
    fields = [FormField(
        "f0", "Gender", "radio-group", "",
        options=["Male", "Female"], option_selectors=['[data-autofill-id="f0-opt0"]', '[data-autofill-id="f0-opt1"]'],
    )]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill, "get_mappings_cached",
        lambda fields, profile, domain: [{"field_id": "f0", "value": "Unknown", "needs_review": False, "reasoning": ""}],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == []
    assert len(result.needs_review) == 1
    assert result.needs_review[0]["option_selectors"] == ['[data-autofill-id="f0-opt0"]', '[data-autofill-id="f0-opt1"]']


async def test_radio_group_falls_back_to_other_option_when_unmatched(monkeypatch):
    fields = [FormField(
        "f0", "Sponsorship type needed", "radio-group", "",
        options=["OPT", "H1B", "TN", "None", "Other"],
        option_selectors=[f'[data-autofill-id="f0-opt{i}"]' for i in range(5)],
    )]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill, "get_mappings_cached",
        lambda fields, profile, domain: [{"field_id": "f0", "value": "Curricular Practical Training", "needs_review": False, "reasoning": ""}],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["Sponsorship type needed"]
    assert result.needs_review == []
    page.locator('[data-autofill-id="f0-opt4"]').click.assert_awaited_once()  # "Other"


async def test_select_falls_back_to_other_option_when_unmatched(monkeypatch):
    fields = [FormField("f0", "Field of Study", "select", '[data-autofill-id="f0"]',
                         options=["Computer Science", "Mathematics", "Not Listed"])]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill, "get_mappings_cached",
        lambda fields, profile, domain: [{"field_id": "f0", "value": "Philosophy", "needs_review": False, "reasoning": ""}],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["Field of Study"]
    assert result.needs_review == []
    page.locator('[data-autofill-id="f0"]').select_option.assert_awaited_once_with(label="Not Listed")


async def test_checkbox_group_falls_back_to_other_when_unmatched(monkeypatch):
    fields = [FormField(
        "f0", "How did you hear about us?", "checkbox-group", "",
        options=["LinkedIn", "Glassdoor", "Other"],
        option_selectors=['[data-autofill-id="f0-opt0"]', '[data-autofill-id="f0-opt1"]', '[data-autofill-id="f0-opt2"]'],
    )]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill, "get_mappings_cached",
        lambda fields, profile, domain: [{"field_id": "f0", "value": ["A friend told me"], "needs_review": False, "reasoning": ""}],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["How did you hear about us?"]
    assert result.needs_review == []
    page.locator('[data-autofill-id="f0-opt2"]').check.assert_awaited_once()


def test_find_other_option_index_matches_common_catchall_phrasings():
    assert autofill._find_other_option_index(["Male", "Female", "Other"]) == 2
    assert autofill._find_other_option_index(["Yes", "No", "Not Listed"]) == 2
    assert autofill._find_other_option_index(["A", "B", "None of the above"]) == 2
    assert autofill._find_other_option_index(["Male", "Female"]) is None


async def test_checkbox_group_checks_matching_options(monkeypatch):
    fields = [FormField(
        "f0", "How did you hear about this opportunity?", "checkbox-group", "",
        options=["LinkedIn", "Glassdoor", "Notion Blog"],
        option_selectors=['[data-autofill-id="f0-opt0"]', '[data-autofill-id="f0-opt1"]', '[data-autofill-id="f0-opt2"]'],
    )]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill, "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": ["LinkedIn", "Notion Blog"], "needs_review": False, "reasoning": ""},
        ],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["How did you hear about this opportunity?"]
    page.locator('[data-autofill-id="f0-opt0"]').check.assert_awaited_once()
    page.locator('[data-autofill-id="f0-opt1"]').check.assert_not_awaited()
    page.locator('[data-autofill-id="f0-opt2"]').check.assert_awaited_once()


async def test_checkbox_group_empty_list_means_none_apply_not_review(monkeypatch):
    fields = [FormField(
        "f0", "Degree Type", "checkbox-group", "",
        options=["Bachelors", "Masters"], option_selectors=['[data-autofill-id="f0-opt0"]', '[data-autofill-id="f0-opt1"]'],
    )]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill, "get_mappings_cached",
        lambda fields, profile, domain: [{"field_id": "f0", "value": [], "needs_review": False, "reasoning": ""}],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["Degree Type"]
    assert result.needs_review == []
    page.locator('[data-autofill-id="f0-opt0"]').check.assert_not_awaited()


async def test_checkbox_group_unmatched_values_becomes_needs_review(monkeypatch):
    fields = [FormField(
        "f0", "What type of role?", "checkbox-group", "",
        options=["Product", "Platform"], option_selectors=['[data-autofill-id="f0-opt0"]', '[data-autofill-id="f0-opt1"]'],
    )]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill, "get_mappings_cached",
        lambda fields, profile, domain: [{"field_id": "f0", "value": ["Nonexistent"], "needs_review": False, "reasoning": ""}],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == []
    assert len(result.needs_review) == 1


async def test_resolve_needs_review_interactively_handles_radio_group(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "Not a veteran")
    page = FakePage()
    profile = {**PROFILE}
    needs_review = [{
        "label": "Veteran Status",
        "reasoning": "no match",
        "field_id": "f0",
        "selector": "",
        "type": "radio-group",
        "options": ["I am not a protected veteran", "I identify as a protected veteran"],
        "option_selectors": ['[data-autofill-id="f0-opt0"]', '[data-autofill-id="f0-opt1"]'],
    }]

    still_needs_review = await autofill._resolve_needs_review_interactively(page, profile, needs_review)

    assert still_needs_review == []
    page.locator('[data-autofill-id="f0-opt0"]').click.assert_awaited_once()


async def test_resolve_needs_review_interactively_handles_checkbox_group(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "LinkedIn, Notion Blog")
    page = FakePage()
    profile = {**PROFILE}
    needs_review = [{
        "label": "How did you hear about this opportunity?",
        "reasoning": "no match",
        "field_id": "f0",
        "selector": "",
        "type": "checkbox-group",
        "options": ["LinkedIn", "Glassdoor", "Notion Blog"],
        "option_selectors": ['[data-autofill-id="f0-opt0"]', '[data-autofill-id="f0-opt1"]', '[data-autofill-id="f0-opt2"]'],
    }]

    still_needs_review = await autofill._resolve_needs_review_interactively(page, profile, needs_review)

    assert still_needs_review == []
    page.locator('[data-autofill-id="f0-opt0"]').check.assert_awaited_once()
    page.locator('[data-autofill-id="f0-opt1"]').check.assert_not_awaited()
    page.locator('[data-autofill-id="f0-opt2"]').check.assert_awaited_once()


async def test_combobox_field_matched_from_already_open_options(monkeypatch):
    # react-select-style widgets: a plain <input role="combobox"> backed by a
    # JS-rendered option list, not a native <select>. Some of these (EEO
    # Yes/No/Decline-style questions) render every option immediately on
    # click, with no typing needed or wanted -- typing can empty the list to
    # zero, so a match here should be used without ever calling fill().
    fields = [FormField("f0", "Country", "combobox", '[data-autofill-id="f0"]')]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill,
        "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": "United States", "needs_review": False, "reasoning": ""},
        ],
    )

    page = FakePage()
    option = page.set_role_button("option", "United States")

    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["Country"]
    assert result.needs_review == []
    field_locator = page.locator('[data-autofill-id="f0"]')
    field_locator.click.assert_awaited_once()
    field_locator.fill.assert_not_awaited()
    option.click.assert_awaited_once()


async def test_combobox_falls_back_to_typing_when_nothing_open_initially(monkeypatch):
    # Search-driven widgets (city/school/country) render nothing until you
    # type -- this must fall back to typing rather than giving up after the
    # first (empty) look.
    fields = [FormField("f0", "Location", "combobox", '[data-autofill-id="f0"]')]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill,
        "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": "Odessa, FL", "needs_review": False, "reasoning": ""},
        ],
    )

    page = FakePage()
    field_locator = page.locator('[data-autofill-id="f0"]')
    option_locator = page.set_role_button("option", "Odessa, FL", count=0)  # nothing rendered yet

    real_fill = field_locator.fill

    async def fill_then_render(value):
        await real_fill(value)
        option_locator.count = AsyncMock(return_value=1)
        option_locator.wait_for = AsyncMock()

    field_locator.fill = AsyncMock(side_effect=fill_then_render)

    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["Location"]
    field_locator.fill.assert_awaited_once_with("Odessa, FL")
    option_locator.click.assert_awaited_once()


async def test_combobox_no_matching_option_becomes_needs_review(monkeypatch):
    fields = [FormField("f0", "Country", "combobox", '[data-autofill-id="f0"]')]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill,
        "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": "Atlantis", "needs_review": False, "reasoning": ""},
        ],
    )

    page = FakePage()  # no matching "option" role registered -> count() stays 0

    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == []
    assert result.errors == []
    assert len(result.needs_review) == 1
    assert result.needs_review[0]["label"] == "Country"


async def test_combobox_falls_back_to_sole_unnamed_option(monkeypatch):
    # Typed value doesn't literally match the rendered option's display text
    # (e.g. "Odessa, FL" vs rendered "Odessa, Florida, United States"), but
    # since exactly one option rendered, it's safe to use it.
    fields = [FormField("f0", "Location", "combobox", '[data-autofill-id="f0"]')]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill,
        "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": "Odessa, FL", "needs_review": False, "reasoning": ""},
        ],
    )

    page = FakePage()
    sole_option = page.set_role_button("option", None, count=1)  # unnamed fallback query

    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["Location"]
    sole_option.click.assert_awaited_once()


async def test_combobox_does_not_guess_among_ambiguous_options(monkeypatch):
    # A bare city name can render many candidates across different states/
    # countries -- guessing the first would risk silently picking the wrong
    # one, so this must fall back to needs_review instead.
    fields = [FormField("f0", "Location", "combobox", '[data-autofill-id="f0"]')]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill,
        "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": "Odessa", "needs_review": False, "reasoning": ""},
        ],
    )

    page = FakePage()
    many_options = page.set_role_button("option", None, count=10)  # ambiguous unnamed fallback

    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == []
    assert len(result.needs_review) == 1
    many_options.click.assert_not_awaited()


async def test_combobox_fuzzy_matches_paraphrased_option_among_several_open(monkeypatch):
    # EEO-style fields render every option immediately (all always open, no
    # typing). A literal substring match can miss the intended option (e.g.
    # "Not a veteran" isn't a contiguous substring of "I am not a protected
    # veteran"), so this needs a looser word-overlap match to pick correctly
    # among several always-visible options instead of giving up.
    fields = [FormField("f0", "Veteran Status", "combobox", '[data-autofill-id="f0"]')]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill,
        "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": "Not a veteran", "needs_review": False, "reasoning": ""},
        ],
    )

    page = FakePage()
    options = page.set_role_button("option", None, count=3)
    options.all_inner_texts = AsyncMock(return_value=[
        "I am not a protected veteran",
        "I identify as one or more of the classifications of a protected veteran",
        "I don't wish to answer",
    ])

    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == ["Veteran Status"]
    assert result.needs_review == []


async def test_needs_review_field_is_skipped_not_guessed(monkeypatch):
    fields = [
        FormField("f0", "Why do you want to work here?", "textarea", '[data-autofill-id="f0"]'),
    ]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    monkeypatch.setattr(
        autofill,
        "get_mappings_cached",
        lambda fields, profile, domain: [
            {"field_id": "f0", "value": None, "needs_review": True, "reasoning": "no info in profile"},
        ],
    )

    page = FakePage()
    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf")

    assert result.filled == []
    assert result.errors == []
    assert len(result.needs_review) == 1
    assert result.needs_review[0]["label"] == "Why do you want to work here?"
    page.locator('[data-autofill-id="f0"]').fill.assert_not_awaited()


SOME_FIELDS = [FormField("f0", "Some Field", "text", '[data-autofill-id="f0"]')]


async def test_multistep_form_advances_and_stops_when_no_next_button(monkeypatch):
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=SOME_FIELDS))
    step_results = [
        AutofillResult(filled=["Name"], needs_review=[], errors=[]),
        AutofillResult(filled=["Email"], needs_review=[{"label": "Essay", "reasoning": "unknown"}], errors=[]),
    ]
    autofill_form_mock = AsyncMock(side_effect=step_results)
    monkeypatch.setattr(autofill, "autofill_form", autofill_form_mock)

    next_button = MagicMock()
    next_button.click = AsyncMock()
    find_next_button_mock = AsyncMock(side_effect=[next_button, None])
    monkeypatch.setattr(autofill, "find_next_button", find_next_button_mock)

    page = FakePage()
    result = await autofill.autofill_form_multistep(
        page, PROFILE, resume_path="resume.pdf", max_steps=6, interactive=False
    )

    assert autofill_form_mock.await_count == 2
    assert next_button.click.await_count == 1
    assert result.filled == ["Name", "Email"]
    assert result.needs_review == [{"label": "Essay", "reasoning": "unknown"}]


async def test_multistep_calls_on_needs_review_before_blocking_on_input(monkeypatch):
    # The interactive terminal prompts block until a human answers them --
    # on_needs_review must fire before that, not after, so a caller (e.g. a
    # notification) isn't stuck waiting on the same block it's meant to warn about.
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=SOME_FIELDS))
    needs_review_items = [{"label": "Essay", "reasoning": "unknown"}]
    monkeypatch.setattr(
        autofill, "autofill_form",
        AsyncMock(return_value=AutofillResult(filled=[], needs_review=needs_review_items, errors=[])),
    )
    monkeypatch.setattr(autofill, "find_next_button", AsyncMock(return_value=None))

    interactive_resolver_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(autofill, "_resolve_needs_review_interactively", interactive_resolver_mock)

    seen = []
    page = FakePage()
    await autofill.autofill_form_multistep(
        page, PROFILE, resume_path="resume.pdf", max_steps=6,
        on_needs_review=seen.append,
    )

    assert seen == [needs_review_items]
    interactive_resolver_mock.assert_awaited_once()


async def test_multistep_on_needs_review_not_called_when_nothing_needs_review(monkeypatch):
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=SOME_FIELDS))
    monkeypatch.setattr(
        autofill, "autofill_form",
        AsyncMock(return_value=AutofillResult(filled=["Name"], needs_review=[], errors=[])),
    )
    monkeypatch.setattr(autofill, "find_next_button", AsyncMock(return_value=None))

    callback = MagicMock()
    page = FakePage()
    await autofill.autofill_form_multistep(
        page, PROFILE, resume_path="resume.pdf", max_steps=6, on_needs_review=callback
    )

    callback.assert_not_called()


async def test_multistep_form_caps_at_max_steps(monkeypatch):
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=SOME_FIELDS))
    monkeypatch.setattr(
        autofill, "autofill_form", AsyncMock(return_value=AutofillResult(filled=[], needs_review=[], errors=[]))
    )
    next_button = MagicMock()
    next_button.click = AsyncMock()
    monkeypatch.setattr(autofill, "find_next_button", AsyncMock(return_value=next_button))

    page = FakePage()
    await autofill.autofill_form_multistep(page, PROFILE, resume_path="resume.pdf", max_steps=3)

    assert autofill.autofill_form.await_count == 3


async def test_multistep_clicks_entry_button_then_fills_form(monkeypatch):
    # Landing page: an entry ("Apply Now") button is present -- even though the
    # page may already have incidental fields (nav search box, etc.), it should
    # still be clicked before any fill is attempted. It's gone on the next step.
    entry_button = MagicMock()
    entry_button.click = AsyncMock()
    monkeypatch.setattr(autofill, "find_entry_button", AsyncMock(side_effect=[entry_button, None]))
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=SOME_FIELDS))

    autofill_form_mock = AsyncMock(return_value=AutofillResult(filled=["Name"], needs_review=[], errors=[]))
    monkeypatch.setattr(autofill, "autofill_form", autofill_form_mock)
    monkeypatch.setattr(autofill, "find_next_button", AsyncMock(return_value=None))

    page = FakePage()
    result = await autofill.autofill_form_multistep(page, PROFILE, resume_path="resume.pdf", max_steps=6)

    entry_button.click.assert_awaited_once()
    autofill_form_mock.assert_awaited_once()
    assert result.filled == ["Name"]


async def test_multistep_gives_up_when_no_fields_and_no_entry_button(monkeypatch):
    monkeypatch.setattr(autofill, "find_entry_button", AsyncMock(return_value=None))
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=[]))
    autofill_form_mock = AsyncMock()
    monkeypatch.setattr(autofill, "autofill_form", autofill_form_mock)

    page = FakePage()
    result = await autofill.autofill_form_multistep(page, PROFILE, resume_path="resume.pdf", max_steps=6)

    autofill_form_mock.assert_not_awaited()
    assert result == AutofillResult(filled=[], needs_review=[], errors=[])


async def test_multistep_survives_entry_button_click_exception(monkeypatch):
    # A cookie banner or other overlay can intercept the click; that shouldn't
    # crash the whole run -- it should try dismissing a cookie banner, retry
    # once, and give up gracefully if it still fails.
    entry_button = MagicMock()
    entry_button.click = AsyncMock(side_effect=Exception("intercepted"))
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=[]))
    monkeypatch.setattr(autofill, "find_entry_button", AsyncMock(return_value=entry_button))
    monkeypatch.setattr(autofill, "dismiss_cookie_banner", AsyncMock(return_value=False))
    autofill_form_mock = AsyncMock()
    monkeypatch.setattr(autofill, "autofill_form", autofill_form_mock)

    page = FakePage()
    result = await autofill.autofill_form_multistep(page, PROFILE, resume_path="resume.pdf", max_steps=6)

    autofill_form_mock.assert_not_awaited()
    assert result == AutofillResult(filled=[], needs_review=[], errors=[])


async def test_find_entry_button_matches_apply_now_link():
    page = FakePage()
    page.set_role_button("link", "Apply Now")
    assert await autofill.find_entry_button(page) is not None


async def test_find_entry_button_returns_none_when_absent():
    page = FakePage()
    assert await autofill.find_entry_button(page) is None


async def test_find_entry_button_matches_dynamic_job_title_button():
    # Some ATS platforms embed the job title in the button text (e.g. "Apply
    # for Software Engineering Intern"), which isn't in the fixed name list.
    page = FakePage()
    page.set_role_button("button", autofill.GENERIC_APPLY_BUTTON_PATTERN)
    assert await autofill.find_entry_button(page) is not None


async def test_find_entry_button_generic_pattern_does_not_match_bare_apply():
    assert not autofill.GENERIC_APPLY_BUTTON_PATTERN.match("Apply")


async def test_find_entry_button_generic_pattern_does_not_match_quick_apply():
    assert not autofill.GENERIC_APPLY_BUTTON_PATTERN.match("Quick Apply with MyGreenhouse")


async def test_find_entry_button_generic_pattern_matches_dynamic_titles():
    assert autofill.GENERIC_APPLY_BUTTON_PATTERN.match("Apply for Software Engineering Intern")
    assert autofill.GENERIC_APPLY_BUTTON_PATTERN.match("Apply now")
    assert autofill.GENERIC_APPLY_BUTTON_PATTERN.match("Apply for this Job")


async def test_find_entry_button_ignores_bare_apply_by_default():
    # A bare "Apply" also matches unrelated in-page controls on pages that
    # already have a real form (a "Quick Apply" shortcut, a scroll-to-form
    # pill) -- must not match unless the caller explicitly opts in.
    page = FakePage()
    page.set_role_button("button", "Apply")
    assert await autofill.find_entry_button(page) is None
    assert await autofill.find_entry_button(page, allow_bare_apply=False) is None


async def test_find_entry_button_matches_bare_apply_when_allowed():
    # Some small/simple career pages have literally nothing but a bare
    # "Apply" button -- correct to click when the page truly has no fields.
    page = FakePage()
    page.set_role_button("button", "Apply")
    assert await autofill.find_entry_button(page, allow_bare_apply=True) is not None


async def test_multistep_does_not_click_bare_apply_when_fields_already_present(monkeypatch):
    # The exact SAP-style false positive this is guarding against: a bare
    # "Apply" pill coexisting with a real, already-fillable form -- clicking
    # it would derail filling the form that's already there.
    fields = [FormField("f0", "Some Field", "text", '[data-autofill-id="f0"]')]
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=fields))
    find_entry_button_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(autofill, "find_entry_button", find_entry_button_mock)
    monkeypatch.setattr(
        autofill, "autofill_form", AsyncMock(return_value=AutofillResult(filled=["Some Field"], needs_review=[], errors=[]))
    )
    monkeypatch.setattr(autofill, "find_next_button", AsyncMock(return_value=None))

    page = FakePage()
    result = await autofill.autofill_form_multistep(page, PROFILE, resume_path="resume.pdf", max_steps=6)

    find_entry_button_mock.assert_awaited_once_with(page, allow_bare_apply=False)
    assert result.filled == ["Some Field"]


async def test_dismiss_cookie_banner_clicks_known_accept_button():
    page = FakePage()
    accept = page.set_role_button("button", "Accept All Cookies")
    assert await autofill.dismiss_cookie_banner(page) is True
    accept.click.assert_awaited_once()


async def test_dismiss_cookie_banner_returns_false_when_nothing_present():
    page = FakePage()
    assert await autofill.dismiss_cookie_banner(page) is False


async def test_find_entry_button_does_not_match_quick_apply_shortcut():
    # Regression: a bare "Apply" substring-matches unrelated in-page controls
    # like a "Quick Apply with MyGreenhouse" button on a real Greenhouse form,
    # hijacking every step before the real form is ever reached.
    page = FakePage()
    page.set_role_button("button", "Quick Apply with MyGreenhouse")
    assert await autofill.find_entry_button(page) is None


async def test_resolve_needs_review_interactively_fills_and_remembers_answer(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "Relocation is fine")
    page = FakePage()
    profile = {**PROFILE}
    needs_review = [{
        "label": "Are you willing to relocate to Austin specifically?",
        "reasoning": "not in profile",
        "field_id": "f0",
        "selector": '[data-autofill-id="f0"]',
        "type": "textarea",
        "options": [],
    }]

    still_needs_review = await autofill._resolve_needs_review_interactively(page, profile, needs_review)

    assert still_needs_review == []
    page.locator('[data-autofill-id="f0"]').fill.assert_awaited_once_with("Relocation is fine")
    assert profile["custom_answers"]["are you willing to relocate to austin specifically?"] == "Relocation is fine"


async def test_resolve_needs_review_interactively_handles_combobox(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "United States")
    page = FakePage()
    option = page.set_role_button("option", "United States")
    profile = {**PROFILE}
    needs_review = [{
        "label": "Country",
        "reasoning": "no dropdown option matched",
        "field_id": "f0",
        "selector": '[data-autofill-id="f0"]',
        "type": "combobox",
        "options": [],
    }]

    still_needs_review = await autofill._resolve_needs_review_interactively(page, profile, needs_review)

    assert still_needs_review == []
    page.locator('[data-autofill-id="f0"]').fill.assert_not_awaited()
    option.click.assert_awaited_once()
    assert profile["custom_answers"]["country"] == "United States"


async def test_resolve_needs_review_interactively_uses_ask_fn_instead_of_input(monkeypatch):
    # A GUI (or any other caller) supplies ask_fn to replace the terminal
    # input() prompt entirely -- input() must not be touched when it's given.
    def boom(prompt=""):
        raise AssertionError("input() should not be called when ask_fn is provided")

    monkeypatch.setattr("builtins.input", boom)
    seen_items = []

    def fake_ask_fn(item):
        seen_items.append(item)
        return "Relocation is fine"

    page = FakePage()
    profile = {**PROFILE}
    needs_review = [{
        "label": "Are you willing to relocate to Austin specifically?",
        "reasoning": "not in profile",
        "field_id": "f0",
        "selector": '[data-autofill-id="f0"]',
        "type": "textarea",
        "options": [],
    }]

    still_needs_review = await autofill._resolve_needs_review_interactively(
        page, profile, needs_review, ask_fn=fake_ask_fn
    )

    assert still_needs_review == []
    page.locator('[data-autofill-id="f0"]').fill.assert_awaited_once_with("Relocation is fine")
    assert profile["custom_answers"]["are you willing to relocate to austin specifically?"] == "Relocation is fine"
    # ask_fn must receive the full structured item (label/reasoning/type/options/...),
    # not just a formatted prompt string, so a GUI can render it appropriately.
    assert seen_items == [needs_review[0]]


async def test_resolve_needs_review_interactively_ask_fn_blank_answer_skips():
    page = FakePage()
    profile = {**PROFILE}
    needs_review = [{
        "label": "Essay question",
        "reasoning": "not in profile",
        "field_id": "f0",
        "selector": '[data-autofill-id="f0"]',
        "type": "textarea",
        "options": [],
    }]

    still_needs_review = await autofill._resolve_needs_review_interactively(
        page, profile, needs_review, ask_fn=lambda item: ""
    )

    assert still_needs_review == needs_review
    page.locator('[data-autofill-id="f0"]').fill.assert_not_awaited()
    assert profile.get("custom_answers", {}) == {}


async def test_multistep_passes_ask_fn_through_to_interactive_resolver(monkeypatch):
    monkeypatch.setattr(autofill, "extract_fields", AsyncMock(return_value=SOME_FIELDS))
    monkeypatch.setattr(
        autofill, "autofill_form",
        AsyncMock(return_value=AutofillResult(filled=[], needs_review=[{"label": "Essay", "reasoning": ""}], errors=[])),
    )
    monkeypatch.setattr(autofill, "find_next_button", AsyncMock(return_value=None))

    interactive_resolver_mock = AsyncMock(return_value=[])
    monkeypatch.setattr(autofill, "_resolve_needs_review_interactively", interactive_resolver_mock)

    sentinel_ask_fn = lambda item: "answer"  # noqa: E731
    page = FakePage()
    await autofill.autofill_form_multistep(
        page, PROFILE, resume_path="resume.pdf", max_steps=6, ask_fn=sentinel_ask_fn
    )

    interactive_resolver_mock.assert_awaited_once_with(
        page, PROFILE, [{"label": "Essay", "reasoning": ""}], ask_fn=sentinel_ask_fn
    )


async def test_resolve_needs_review_interactively_skips_on_blank_answer(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    page = FakePage()
    profile = {**PROFILE}
    needs_review = [{
        "label": "Essay question",
        "reasoning": "not in profile",
        "field_id": "f0",
        "selector": '[data-autofill-id="f0"]',
        "type": "textarea",
        "options": [],
    }]

    still_needs_review = await autofill._resolve_needs_review_interactively(page, profile, needs_review)

    assert still_needs_review == needs_review
    page.locator('[data-autofill-id="f0"]').fill.assert_not_awaited()
    assert profile.get("custom_answers", {}) == {}


async def test_custom_answer_reused_without_calling_claude(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("Claude should not be called when a custom answer is cached")

    monkeypatch.setattr(autofill, "get_mappings_cached", _boom)

    fields = [FormField("f0", "Essay Question", "textarea", '[data-autofill-id="f0"]')]
    profile = {**PROFILE, "custom_answers": {"essay question": "My answer from before"}}
    page = FakePage()

    result = await autofill.autofill_form(page, profile, resume_path="resume.pdf", fields=fields)

    assert result.filled == ["Essay Question"]
    assert result.needs_review == []
    page.locator('[data-autofill-id="f0"]').fill.assert_awaited_once_with("My answer from before")


async def test_account_signup_fields_filled_deterministically_without_claude(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("Claude should not be called for password/account-email fields")

    monkeypatch.setattr(autofill, "get_mappings_cached", _boom)

    fields = [
        FormField("f0", "Password", "password", '[data-autofill-id="f0"]'),
        FormField("f1", "Confirm Password", "password", '[data-autofill-id="f1"]'),
        FormField("f2", "Email", "email", '[data-autofill-id="f2"]'),
    ]
    profile = {**PROFILE, "account_password": "s3cret-throwaway", "account_email": "signup@example.com"}
    page = FakePage()

    result = await autofill.autofill_form(page, profile, resume_path="resume.pdf", fields=fields)

    assert result.needs_review == []
    assert result.errors == []
    assert set(result.filled) == {"Password", "Confirm Password", "Email"}
    page.locator('[data-autofill-id="f0"]').fill.assert_awaited_once_with("s3cret-throwaway")
    page.locator('[data-autofill-id="f1"]').fill.assert_awaited_once_with("s3cret-throwaway")
    page.locator('[data-autofill-id="f2"]').fill.assert_awaited_once_with("signup@example.com")


async def test_account_signup_needs_review_when_no_password_configured(monkeypatch):
    fields = [FormField("f0", "Password", "password", '[data-autofill-id="f0"]')]
    page = FakePage()

    result = await autofill.autofill_form(page, PROFILE, resume_path="resume.pdf", fields=fields)

    assert result.filled == []
    assert len(result.needs_review) == 1
    assert result.needs_review[0]["label"] == "Password"


def test_hash_fields_is_stable_and_order_independent_between_runs():
    fields_a = [
        FormField("f0", "Email", "text", "sel0"),
        FormField("f1", "Country", "select", "sel1", options=["USA", "Canada"]),
    ]
    fields_b = [
        FormField("x0", "Email", "text", "other-selector"),
        FormField("x1", "Country", "select", "other-selector-2", options=["USA", "Canada"]),
    ]
    # Different field_ids/selectors shouldn't matter -- only label/type/options do.
    assert mapping_cache.hash_fields(fields_a) == mapping_cache.hash_fields(fields_b)


def test_mapping_cache_round_trip():
    mappings = [{"field_id": "f0", "value": "Jane", "needs_review": False, "reasoning": ""}]
    assert mapping_cache.get("example.com", "somehash") is None
    mapping_cache.store("example.com", "somehash", mappings)
    assert mapping_cache.get("example.com", "somehash") == mappings


def test_get_mappings_cached_splits_large_field_lists_into_batches(monkeypatch):
    # Large forms overwhelm local models' instruction-following (seen live: an
    # 87-field Lever form producing valid JSON missing the "mappings" key
    # entirely) -- each call should only ever see MAPPING_BATCH_SIZE fields.
    monkeypatch.setattr(autofill, "MAPPING_BATCH_SIZE", 3)
    fields = [FormField(f"f{i}", f"Field {i}", "text", f"sel{i}") for i in range(7)]
    seen_batch_sizes = []

    def fake_get_mappings(batch, profile):
        seen_batch_sizes.append(len(batch))
        return [{"field_id": f.field_id, "value": f.label, "needs_review": False, "reasoning": ""} for f in batch]

    monkeypatch.setattr(autofill, "get_mappings", fake_get_mappings)

    result = autofill.get_mappings_cached(fields, PROFILE, "example.com")

    assert seen_batch_sizes == [3, 3, 1]
    assert [m["field_id"] for m in result] == [f"f{i}" for i in range(7)]


def test_get_mappings_cached_isolates_a_failing_batch(monkeypatch):
    # One batch erroring (malformed JSON, timeout, etc.) shouldn't lose the
    # mappings another batch already got right.
    monkeypatch.setattr(autofill, "MAPPING_BATCH_SIZE", 2)
    fields = [FormField(f"f{i}", f"Field {i}", "text", f"sel{i}") for i in range(4)]

    def fake_get_mappings(batch, profile):
        if batch[0].field_id == "f2":
            raise ValueError("malformed JSON from model")
        return [{"field_id": f.field_id, "value": "ok", "needs_review": False, "reasoning": ""} for f in batch]

    monkeypatch.setattr(autofill, "get_mappings", fake_get_mappings)

    result = autofill.get_mappings_cached(fields, PROFILE, "example.com")
    by_id = {m["field_id"]: m for m in result}

    assert by_id["f0"]["value"] == "ok"
    assert by_id["f1"]["value"] == "ok"
    assert by_id["f2"]["needs_review"] is True
    assert by_id["f3"]["needs_review"] is True


def test_get_mappings_handles_bare_list_response(monkeypatch):
    # Some models occasionally drop the {"mappings": [...]} wrapper under
    # instruction-following strain and return the bare array instead.
    monkeypatch.setattr(
        autofill, "_call_ollama",
        lambda user_content: '[{"field_id": "f0", "value": "Jane", "needs_review": false, "reasoning": ""}]',
    )
    fields = [FormField("f0", "First Name", "text", "sel0")]

    result = autofill.get_mappings(fields, PROFILE)

    assert result == [{"field_id": "f0", "value": "Jane", "needs_review": False, "reasoning": ""}]
