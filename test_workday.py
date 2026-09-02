from unittest.mock import AsyncMock

import pytest

import autofill
import workday
from autofill import FormField


def _key(name):
    """Normalize a get_by_role/get_by_text `name` argument (a plain string
    or a compiled regex) to a dict-lookup key -- workday.py mixes both, and
    two separately-compiled `re.compile()` calls for the identical pattern
    aren't guaranteed to be the same object, so matching on `.pattern`
    (falling back to the raw string) is what makes the fakes below reliable
    regardless of Python's internal regex-cache behavior."""
    return getattr(name, "pattern", name)


class _Loc:
    """Minimal fake Playwright Locator, extended (relative to
    main_autofill.py's FakeLocator) with `.locator()`/`.get_by_role()`
    chaining -- workday._find_add_button walks from a heading locator to a
    container locator to a button locator, which the plain FakeLocator used
    by autofill.py's own test suite doesn't need to support."""

    def __init__(self, count=0, page=None, heading_key=None, is_container=False):
        self._count = count
        self.page = page
        self.heading_key = heading_key
        self.is_container = is_container
        self.fill = AsyncMock()
        self.check = AsyncMock()
        self.click = AsyncMock()

    async def count(self):
        return self._count

    @property
    def first(self):
        return self

    def nth(self, i):
        return self

    def locator(self, selector):
        add_button = self.page._add_buttons.get(self.heading_key)
        return _Loc(count=1 if add_button is not None else 0, page=self.page,
                    heading_key=self.heading_key, is_container=True)

    def get_by_role(self, role, name=None, exact=False):
        if self.is_container and role == "button":
            return self.page._add_buttons.get(self.heading_key) or _Loc(count=0)
        return self.page.get_by_role(role, name=name, exact=exact)


class FakeRepeatingPage:
    """Fake Page for exercising workday._fill_repeating_section /
    fill_experience_sections without a real browser -- see workday.py's own
    docstring for why this logic can't be verified against a live Workday
    form in this environment (myworkdayjobs.com is blocked by the browser
    tool's site permissions), so these tests pin down the documented
    click-Add/fill-by-index contract instead."""

    def __init__(self):
        self.url = "https://acme.wd5.myworkdayjobs.com/apply"
        self.wait_for_timeout = AsyncMock()
        self._heading_found: set = set()
        self._add_buttons: dict = {}
        self._role_locators: dict = {}

    def configure_heading(self, pattern, found: bool = True):
        key = _key(pattern)
        if found:
            self._heading_found.add(key)
        else:
            self._heading_found.discard(key)

    def configure_add_button(self, heading_pattern, locator: "_Loc"):
        self._add_buttons[_key(heading_pattern)] = locator

    def configure_role(self, role, name_pattern, locator: "_Loc"):
        self._role_locators[(role, _key(name_pattern))] = locator

    def get_by_text(self, pattern, exact=False):
        found = _key(pattern) in self._heading_found
        return _Loc(count=1 if found else 0, page=self, heading_key=_key(pattern))

    def get_by_role(self, role, name=None, exact=False):
        return self._role_locators.get((role, _key(name)), _Loc(count=0))


# ---------------------------------------------------------------------------
# is_workday_domain
# ---------------------------------------------------------------------------

def test_is_workday_domain_true_for_myworkdayjobs_subdomain():
    assert workday.is_workday_domain("https://acme.wd5.myworkdayjobs.com/en-US/External")


def test_is_workday_domain_false_for_other_ats():
    assert not workday.is_workday_domain("https://boards.greenhouse.io/acme")


# ---------------------------------------------------------------------------
# My Information
# ---------------------------------------------------------------------------

def test_my_information_maps_workday_specific_text_fields():
    fields = [
        FormField("f0", "Address", "text", "sel0"),
        FormField("f1", "City", "text", "sel1"),
        FormField("f2", "Postal Code", "text", "sel2"),
        FormField("f3", "Preferred First Name", "text", "sel3"),
    ]
    profile = {
        "address_line_1": "123 Main St", "city": "San Francisco", "postal_code": "94105",
        "preferred_name": "Janie",
    }
    mappings, handled = workday._my_information_mappings(fields, profile)
    by_id = {m["field_id"]: m["value"] for m in mappings}
    assert by_id == {"f0": "123 Main St", "f1": "San Francisco", "f2": "94105", "f3": "Janie"}
    assert handled == {"f0", "f1", "f2", "f3"}


def test_my_information_leaves_field_unclaimed_when_profile_value_missing():
    fields = [FormField("f0", "Address", "text", "sel0")]
    mappings, handled = workday._my_information_mappings(fields, {})
    assert mappings == []
    assert handled == set()


def test_resolve_select_field_phone_device_defaults_to_mobile():
    f = FormField("f0", "Phone Device Type", "select", "sel0", options=["Mobile", "Home", "Work"])
    assert workday._resolve_select_field(f, {}) == "Mobile"


def test_resolve_select_field_state_expands_abbreviation_for_native_select():
    f = FormField("f0", "State", "select", "sel0", options=["Alabama", "California", "Nevada"])
    assert workday._resolve_select_field(f, {"state": "ca"}) == "California"


def test_resolve_select_field_state_combobox_returns_expanded_candidate_unmatched():
    # combobox options are always empty (JS-rendered) -- nothing to
    # pre-resolve against, so the expanded candidate is handed back as-is
    # for apply_mapping's live-widget search to resolve.
    f = FormField("f0", "State", "combobox", "sel0", options=[])
    assert workday._resolve_select_field(f, {"state": "CA"}) == "California"


def test_resolve_select_field_country_matches_word_overlap_paraphrase():
    f = FormField("f0", "Country", "select", "sel0", options=["Canada", "United States of America"])
    assert workday._resolve_select_field(f, {"country": "United States"}) == "United States of America"


def test_resolve_select_field_excludes_phone_country_code():
    f = FormField("f0", "Phone Country Code", "combobox", "sel0", options=[])
    assert workday._resolve_select_field(f, {"country": "United States"}) is None


def test_resolve_select_field_returns_none_for_unrelated_dropdown():
    f = FormField("f0", "How Did You Hear About Us", "select", "sel0", options=["LinkedIn", "Referral"])
    assert workday._resolve_select_field(f, {"country": "United States"}) is None


# ---------------------------------------------------------------------------
# Voluntary Disclosures
# ---------------------------------------------------------------------------

def test_disclosure_mappings_claims_all_four_fields():
    fields = [
        FormField("f0", "Gender", "radio-group", "", options=["Male", "Female", "Decline To Self Identify"]),
        FormField("f1", "Ethnicity", "select", "sel1", options=["Asian", "White", "Decline To Self Identify"]),
        FormField("f2", "Veteran Status", "radio-group", "", options=["Veteran", "Not a Veteran", "Decline"]),
        FormField("f3", "Hispanic or Latino", "radio-group", "", options=["Yes", "No"]),
    ]
    profile = {
        "gender": "Decline To Self Identify", "race_ethnicity": "Decline To Self Identify",
        "veteran_status": "Decline To Self Identify", "hispanic_or_latino": False,
    }
    mappings, handled = workday._disclosure_mappings(fields, profile)
    assert handled == {"f0", "f1", "f2", "f3"}
    by_id = {m["field_id"]: m["value"] for m in mappings}
    assert by_id["f3"] == "No"  # boolean -> "Yes"/"No" conversion
    assert by_id["f1"] == "Decline To Self Identify"  # native select: exact match found directly


def test_disclosure_mappings_needs_review_when_profile_value_missing():
    fields = [FormField("f0", "Gender", "radio-group", "", options=["Male", "Female"])]
    mappings, handled = workday._disclosure_mappings(fields, {})
    assert handled == {"f0"}
    assert mappings[0]["needs_review"] is True
    assert mappings[0]["value"] is None


def test_disclosure_mappings_ignores_non_disclosure_field():
    fields = [FormField("f0", "Favorite Color", "select", "sel0", options=["Red", "Blue"])]
    mappings, handled = workday._disclosure_mappings(fields, {"gender": "Male"})
    assert mappings == []
    assert handled == set()


# ---------------------------------------------------------------------------
# Self-Identify disability widget
# ---------------------------------------------------------------------------

DISABILITY_OPTIONS = [
    "Yes, I Have A Disability, Or Have A History/Record Of Having A Disability",
    "No, I Do Not Have A Disability, Or A History/Record Of Having A Disability",
    "I Do Not Want To Answer",
]


def test_self_identify_maps_disability_question_and_signature_fields():
    fields = [
        FormField("f0", "Please select one:", "radio-group", "", options=DISABILITY_OPTIONS),
        FormField("f1", "Name", "text", "sel1"),
        FormField("f2", "Today's Date", "text", "sel2"),
    ]
    profile = {"disability_status": "Decline To Self Identify", "first_name": "Jane", "last_name": "Doe"}
    mappings, handled = workday._self_identify_mappings(fields, profile)
    assert handled == {"f0", "f1", "f2"}
    by_id = {m["field_id"]: m["value"] for m in mappings}
    assert by_id["f1"] == "Jane Doe"
    assert by_id["f2"] is not None  # today's date, formatted -- exact value not asserted


def test_self_identify_does_not_touch_unrelated_name_field_without_disability_question():
    fields = [FormField("f0", "Name", "text", "sel0")]
    mappings, handled = workday._self_identify_mappings(fields, {"first_name": "Jane", "last_name": "Doe"})
    assert mappings == []
    assert handled == set()


def test_self_identify_needs_review_when_disability_status_missing():
    fields = [FormField("f0", "Please select one:", "radio-group", "", options=DISABILITY_OPTIONS)]
    mappings, handled = workday._self_identify_mappings(fields, {})
    assert handled == {"f0"}
    assert mappings[0]["needs_review"] is True


# ---------------------------------------------------------------------------
# workday_field_mappings (full integration of the three layers)
# ---------------------------------------------------------------------------

def test_workday_field_mappings_combines_all_layers_without_double_claiming():
    fields = [
        FormField("f0", "Address", "text", "sel0"),
        FormField("f1", "Gender", "radio-group", "", options=["Male", "Female", "Decline To Self Identify"]),
        FormField("f2", "Please select one:", "radio-group", "", options=DISABILITY_OPTIONS),
        FormField("f3", "Some Employer-Custom Essay Question", "textarea", "sel3"),
    ]
    profile = {
        "address_line_1": "123 Main St", "gender": "Decline To Self Identify",
        "disability_status": "Decline To Self Identify",
    }
    mappings, handled = workday.workday_field_mappings(fields, profile)
    assert handled == {"f0", "f1", "f2"}
    assert "f3" not in handled  # genuinely custom question left for the LLM/needs_review


# ---------------------------------------------------------------------------
# Work Experience / Education repeating panels
# ---------------------------------------------------------------------------

def test_legacy_single_education_entry_built_from_flat_profile_keys():
    profile = {"school": "Example University", "degree": "Bachelor's", "field_of_study": "CS",
               "graduation_date": "2027-05"}
    assert workday._legacy_single_education_entry(profile) == [{
        "school": "Example University", "degree": "Bachelor's", "field_of_study": "CS",
        "graduation_date": "2027-05",
    }]


def test_legacy_single_education_entry_empty_without_school():
    assert workday._legacy_single_education_entry({}) == []


async def test_fill_experience_sections_dispatches_both_sections_with_right_entries(monkeypatch):
    calls = []

    async def fake_fill(page, heading_pattern, entries, field_spec, marker_pattern, log):
        calls.append((heading_pattern, entries))
        return [f"filled:{heading_pattern.pattern}"]

    monkeypatch.setattr(workday, "_fill_repeating_section", fake_fill)
    profile = {
        "previous_employers": [{"title": "Engineer"}],
        "school": "Example University", "degree": "Bachelor's",
        "field_of_study": "CS", "graduation_date": "2027-05",
    }
    result = await workday.fill_experience_sections(object(), profile, log=print)

    assert len(calls) == 2
    assert calls[0][0] is workday.WORK_EXPERIENCE_HEADING_PATTERN
    assert calls[0][1] == [{"title": "Engineer"}]
    assert calls[1][0] is workday.EDUCATION_HEADING_PATTERN
    assert calls[1][1] == [{
        "school": "Example University", "degree": "Bachelor's",
        "field_of_study": "CS", "graduation_date": "2027-05",
    }]
    assert result == [
        f"filled:{workday.WORK_EXPERIENCE_HEADING_PATTERN.pattern}",
        f"filled:{workday.EDUCATION_HEADING_PATTERN.pattern}",
    ]


async def test_fill_experience_sections_skips_education_without_any_school_info():
    result = await workday.fill_experience_sections(object(), {}, log=print)
    assert result == []


async def test_fill_repeating_section_skips_when_add_button_not_found_and_no_panels(monkeypatch):
    page = FakeRepeatingPage()
    # Heading never configured as found -> _find_add_button returns None.
    logs = []
    result = await workday._fill_repeating_section(
        page, workday.WORK_EXPERIENCE_HEADING_PATTERN, [{"title": "Engineer"}],
        workday.WORK_EXPERIENCE_FIELD_SPEC, workday.WORK_EXPERIENCE_MARKER_PATTERN, logs.append,
    )
    assert result == []
    assert any("Add" in msg for msg in logs)


async def test_fill_repeating_section_clicks_add_then_fills_new_panel_fields():
    page = FakeRepeatingPage()
    page.configure_heading(workday.WORK_EXPERIENCE_HEADING_PATTERN, True)

    job_title_loc = _Loc(count=0)

    async def _on_add_click(*args, **kwargs):
        job_title_loc._count = 1

    add_button = _Loc(count=1)
    add_button.click = AsyncMock(side_effect=_on_add_click)
    page.configure_add_button(workday.WORK_EXPERIENCE_HEADING_PATTERN, add_button)
    page.configure_role("textbox", workday.WORK_EXPERIENCE_MARKER_PATTERN, job_title_loc)

    company_loc = _Loc(count=1)
    location_loc = _Loc(count=1)
    for kind, pattern, key in workday.WORK_EXPERIENCE_FIELD_SPEC:
        if key == "title":
            page.configure_role(kind, pattern, job_title_loc)
        elif key == "company":
            page.configure_role(kind, pattern, company_loc)
        elif key == "location":
            page.configure_role(kind, pattern, location_loc)

    entry = {"title": "Software Engineering Intern", "company": "Example Corp", "location": "SF"}
    result = await workday._fill_repeating_section(
        page, workday.WORK_EXPERIENCE_HEADING_PATTERN, [entry],
        workday.WORK_EXPERIENCE_FIELD_SPEC, workday.WORK_EXPERIENCE_MARKER_PATTERN, print,
    )

    add_button.click.assert_awaited_once()
    job_title_loc.fill.assert_awaited_once_with("Software Engineering Intern")
    company_loc.fill.assert_awaited_once_with("Example Corp")
    location_loc.fill.assert_awaited_once_with("SF")
    assert "title (entry 1)" in result


async def test_fill_repeating_section_does_not_click_add_when_panel_already_exists():
    page = FakeRepeatingPage()
    page.configure_heading(workday.WORK_EXPERIENCE_HEADING_PATTERN, True)
    add_button = _Loc(count=1)
    page.configure_add_button(workday.WORK_EXPERIENCE_HEADING_PATTERN, add_button)

    job_title_loc = _Loc(count=1)  # one panel already rendered
    page.configure_role("textbox", workday.WORK_EXPERIENCE_MARKER_PATTERN, job_title_loc)
    for kind, pattern, key in workday.WORK_EXPERIENCE_FIELD_SPEC:
        if key == "title":
            page.configure_role(kind, pattern, job_title_loc)

    entry = {"title": "Software Engineering Intern"}
    await workday._fill_repeating_section(
        page, workday.WORK_EXPERIENCE_HEADING_PATTERN, [entry],
        workday.WORK_EXPERIENCE_FIELD_SPEC, workday.WORK_EXPERIENCE_MARKER_PATTERN, print,
    )

    add_button.click.assert_not_awaited()
    job_title_loc.fill.assert_awaited_once_with("Software Engineering Intern")


async def test_fill_combobox_locator_prefers_already_open_options(monkeypatch):
    match_calls = []

    async def fake_try_match(page, value):
        match_calls.append(value)
        return True

    monkeypatch.setattr(autofill, "_try_match_open_options", fake_try_match)
    loc = _Loc(count=1)
    matched = await workday._fill_combobox_locator(page=object(), locator=loc, value="Bachelor's Degree")

    assert matched is True
    loc.click.assert_awaited_once()
    loc.fill.assert_not_awaited()  # matched from already-open options, no need to type
    assert match_calls == ["Bachelor's Degree"]


# ---------------------------------------------------------------------------
# OTHER_OPTION_PATTERNS decline-wording extension (benefits every ATS, not
# just Workday, but exists specifically because Workday's own EEO/self-
# identify opt-out options are worded this way -- see autofill.py's
# OTHER_OPTION_PATTERNS comment).
# ---------------------------------------------------------------------------

def test_find_other_option_index_matches_decline_wording():
    options = ["Yes, I Have A Disability", "No, I Do Not Have A Disability", "I Do Not Want To Answer"]
    assert autofill._find_other_option_index(options) == 2
