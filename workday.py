"""
Workday-specific deterministic autofill layer for DropList.

Every myworkdayjobs.com site runs the same underlying Workday Recruiting
product, just re-themed per company -- unlike Greenhouse/Lever/Ashby/etc.,
where field wording and page structure genuinely vary employer to employer,
Workday's own "My Information" labels, Voluntary Disclosures (EEO) wording,
and Self-Identify disability form are close to identical across every
company's instance (the disability question is in fact the federal OFCCP
CC-305 form verbatim, not Workday's own wording). That stability is what
makes it safe to hardcode here instead of spending an LLM call (and a local
model's occasional bad judgment) on fields whose answer is knowable ahead of
time -- see autofill.py's own docstring on "The mapping pipeline" for the
general principle this follows.

This module only ever CLAIMS fields -- it never invents a value beyond what
autofill.py's existing option-matching/fallback logic in apply_mapping
already does for every other ATS, and never touches anything it isn't
confident belongs to a Workday-standard field. Anything genuinely
employer-custom (an essay question, a company-specific eligibility check)
is deliberately left unclaimed so it still reaches the LLM/needs_review path
like on any other ATS.

Two different kinds of work happen here:
  - workday_field_mappings(): pure field->value mapping for single-value
    controls (My Information, Voluntary Disclosures, Self-Identify), fed
    into autofill.py's existing apply_mapping() exactly like its other
    deterministic layers.
  - fill_experience_sections(): direct page interaction for Workday's
    repeating "Add another" Work Experience / Education panels, which can't
    be expressed as a flat field->value map since it has to click "Add" and
    discover each new panel's fields itself.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from playwright.async_api import Page

if TYPE_CHECKING:
    from autofill import FormField


def is_workday_domain(url: str) -> bool:
    return "myworkdayjobs.com" in urlparse(url).netloc


def _mapping(field_id: str, value: Any, reasoning: str, needs_review: bool = False) -> dict[str, Any]:
    return {
        "field_id": field_id,
        "value": value,
        "needs_review": needs_review or value is None,
        "reasoning": reasoning,
    }


def _best_option_match(options: list[str], candidate: str) -> str | None:
    """Best real option text for candidate, or None. Used for native <select>
    fields specifically -- autofill.apply_mapping's own select-type dispatch
    only does an exact (case-insensitive) match with an "Other" fallback, no
    word-overlap step (unlike its radio-group dispatch), so resolving the
    real option text ourselves here is what lets e.g. profile['country'] ==
    "United States" land on a live option worded "United States of
    America" instead of falling through to needs_review."""
    if not options or not candidate:
        return None
    lowered_candidate = candidate.strip().lower()
    for opt in options:
        if opt.strip().lower() == lowered_candidate:
            return opt
    for opt in options:
        opt_lower = opt.strip().lower()
        if lowered_candidate in opt_lower or opt_lower in lowered_candidate:
            return opt
    import autofill
    idx = autofill._best_word_overlap_index(candidate, options)
    return options[idx] if idx is not None else None


def _profile_disclosure_value(profile: dict[str, Any], key: str) -> str | None:
    value = profile.get(key)
    if value is None:
        return None
    if isinstance(value, bool):
        # Matches autofill.SYSTEM_PROMPT's own instruction to the LLM for
        # the same case: real form options are worded "Yes"/"No", never a
        # JSON boolean.
        return "Yes" if value else "No"
    return str(value)


# ---------------------------------------------------------------------------
# My Information: single-value contact/address/phone fields
# ---------------------------------------------------------------------------

# autofill._profile_field_mappings already handles first/last/preferred
# name, email, phone, linkedin, portfolio, school, degree, field of study,
# country, and state -- but ONLY for text/email/tel/url input types, since
# it has no way to know an arbitrary ATS's exact dropdown wording. Workday's
# wording IS knowable and stable, so this module adds the same fields back
# in for select/combobox types (see _resolve_select_field), plus a few
# fields that are Workday-specific vocabulary (address line 1, city, postal
# code, phone device type) the generic layer has no pattern for at all.
WORKDAY_PLAIN_TEXT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bpreferred\s*first\s*name\b", re.I), "preferred_name"),
    (re.compile(r"\baddress\b", re.I), "address_line_1"),
    (re.compile(r"\bcity\b", re.I), "city"),
    (re.compile(r"\bpostal\s*code\b|\bzip\s*code\b|\bzip\b", re.I), "postal_code"),
]

STATE_LABEL_PATTERN = re.compile(r"\bstate\b|\bprovince\b", re.I)
COUNTRY_LABEL_PATTERN = re.compile(r"\bcountry\b", re.I)
PHONE_DEVICE_LABEL_PATTERN = re.compile(r"phone\s*device\s*type", re.I)

# Abbreviation -> full name, since Workday's State dropdown almost always
# lists full state names ("California") while a profile is more likely to
# hold the two-letter form ("CA") -- tried as-typed first regardless (see
# _resolve_select_field), this is only consulted if that fails.
US_STATE_ABBREVIATIONS: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "PR": "Puerto Rico", "VI": "Virgin Islands", "GU": "Guam",
}


def _match_plain_text_key(label: str) -> str | None:
    for pattern, key in WORKDAY_PLAIN_TEXT_PATTERNS:
        if pattern.search(label):
            return key
    return None


def _resolve_select_field(f: "FormField", profile: dict[str, Any]) -> str | None:
    """Best value to submit for a My Information select/combobox field, or
    None if this isn't one of the fields this module knows how to answer.

    For a native <select> (f.type == "select"), f.options is already known,
    so this pre-resolves the real option text via word-overlap matching --
    apply_mapping's own select dispatch only does an exact match. For a
    combobox (f.type == "combobox"), f.options is always empty (it's a
    JS-rendered widget -- see autofill.extract_fields), so there's nothing
    to pre-resolve against; the plain candidate string is returned as-is and
    apply_mapping's existing select_combobox_option() does the real
    click-and-search against whatever the live widget renders."""
    if PHONE_DEVICE_LABEL_PATTERN.search(f.label):
        candidate = profile.get("phone_device_type") or "Mobile"
    elif STATE_LABEL_PATTERN.search(f.label):
        raw = profile.get("state")
        if not raw:
            return None
        candidate = US_STATE_ABBREVIATIONS.get(raw.strip().upper(), raw)
    elif COUNTRY_LABEL_PATTERN.search(f.label) and "code" not in f.label.lower():
        # The "code" exclusion keeps this off "Country Phone Code" -- that
        # wants a dial code, not the country's name, and this module has no
        # confident answer for it, so it's deliberately left unclaimed.
        candidate = profile.get("country")
        if not candidate:
            return None
    else:
        return None

    if f.type == "select":
        return _best_option_match(f.options, candidate) or candidate
    return candidate


def _my_information_mappings(
    fields: list["FormField"], profile: dict[str, Any]
) -> tuple[list[dict[str, Any]], set[str]]:
    mappings: list[dict[str, Any]] = []
    handled: set[str] = set()
    for f in fields:
        if f.type in ("text", "email", "tel", "url"):
            key = _match_plain_text_key(f.label)
            if key is None:
                continue
            value = profile.get(key)
            if not value:
                continue
            mappings.append(_mapping(f.field_id, value, f"Workday: direct match from profile['{key}']"))
            handled.add(f.field_id)
        elif f.type in ("select", "combobox"):
            value = _resolve_select_field(f, profile)
            if value is None:
                continue
            mappings.append(_mapping(f.field_id, value, "Workday: My Information dropdown matched from profile"))
            handled.add(f.field_id)
    return mappings, handled


# ---------------------------------------------------------------------------
# Voluntary Disclosures (EEO) + Self-Identify disability widget
# ---------------------------------------------------------------------------

DISCLOSURE_FIELD_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"hispanic\s*or\s*latino", re.I), "hispanic_or_latino"),
    (re.compile(r"gender", re.I), "gender"),
    (re.compile(r"race\b|ethnicity", re.I), "race_ethnicity"),
    (re.compile(r"veteran", re.I), "veteran_status"),
]

DISCLOSURE_FIELD_TYPES = ("select", "combobox", "radio-group", "checkbox-group")


def _disclosure_mappings(
    fields: list["FormField"], profile: dict[str, Any]
) -> tuple[list[dict[str, Any]], set[str]]:
    mappings: list[dict[str, Any]] = []
    handled: set[str] = set()
    for f in fields:
        if f.type not in DISCLOSURE_FIELD_TYPES:
            continue
        for pattern, key in DISCLOSURE_FIELD_PATTERNS:
            if not pattern.search(f.label):
                continue
            value = _profile_disclosure_value(profile, key)
            if value is None:
                mappings.append(_mapping(
                    f.field_id, None, f"Workday: no profile['{key}'] set for this voluntary disclosure",
                    needs_review=True,
                ))
            else:
                resolved = _best_option_match(f.options, value) or value if f.type == "select" else value
                mappings.append(_mapping(f.field_id, resolved, f"Workday: profile['{key}'] (voluntary disclosure)"))
            handled.add(f.field_id)
            break
    return mappings, handled


# The federal OFCCP CC-305 self-identification-of-disability form is used
# essentially verbatim by every Workday instance -- its three options always
# mention "disability" in the option text itself (the question's own label
# is usually generic instructional text, not the word "disability"), so
# detecting the widget by option wording is more reliable than by label.
DISABILITY_OPTION_MARKER = "disability"
SIGNATURE_NAME_PATTERN = re.compile(r"^name$|legal\s*name|digital\s*signature|\bsignature\b", re.I)
SIGNATURE_DATE_PATTERN = re.compile(r"today.?s\s*date|signature\s*date|^date$", re.I)


def _self_identify_mappings(
    fields: list["FormField"], profile: dict[str, Any]
) -> tuple[list[dict[str, Any]], set[str]]:
    mappings: list[dict[str, Any]] = []
    handled: set[str] = set()

    disability_field = next(
        (f for f in fields
         if f.type in ("radio-group", "select")
         and any(DISABILITY_OPTION_MARKER in opt.lower() for opt in f.options)),
        None,
    )
    if disability_field is None:
        return mappings, handled

    value = _profile_disclosure_value(profile, "disability_status")
    if value is None:
        mappings.append(_mapping(
            disability_field.field_id, None, "Workday: no profile['disability_status'] set", needs_review=True,
        ))
    else:
        resolved = (_best_option_match(disability_field.options, value) or value) \
            if disability_field.type == "select" else value
        mappings.append(_mapping(disability_field.field_id, resolved, "Workday: profile['disability_status']"))
    handled.add(disability_field.field_id)

    # The signature Name + Today's Date fields sitting alongside the
    # disability question on this same step -- gated on a disability
    # question actually being found on this step (above), so a lone "Name"
    # field elsewhere on the form (e.g. an emergency contact) is never
    # touched by this.
    full_name = " ".join(p for p in [profile.get("first_name"), profile.get("last_name")] if p).strip()
    for f in fields:
        if f.field_id in handled or f.type not in ("text", "email", "tel", "url"):
            continue
        if full_name and SIGNATURE_NAME_PATTERN.search(f.label):
            mappings.append(_mapping(f.field_id, full_name, "Workday: self-identify signature name"))
            handled.add(f.field_id)
        elif SIGNATURE_DATE_PATTERN.search(f.label):
            mappings.append(_mapping(
                f.field_id, date.today().strftime("%m/%d/%Y"), "Workday: self-identify signature date",
            ))
            handled.add(f.field_id)

    return mappings, handled


def workday_field_mappings(
    fields: list["FormField"], profile: dict[str, Any]
) -> tuple[list[dict[str, Any]], set[str]]:
    """Deterministic Workday-specific mappings on top of autofill.py's own
    generic deterministic layers -- My Information fields those don't cover
    (state/country/phone-device selects, address/city/postal code),
    Voluntary Disclosures, and the Self-Identify disability widget. Meant to
    run only on myworkdayjobs.com domains (callers check is_workday_domain
    first) and only on fields the generic layers didn't already claim."""
    mappings: list[dict[str, Any]] = []
    handled: set[str] = set()
    for step in (_my_information_mappings, _disclosure_mappings, _self_identify_mappings):
        remaining = [f for f in fields if f.field_id not in handled]
        if not remaining:
            break
        m, h = step(remaining, profile)
        mappings += m
        handled |= h
    return mappings, handled


# ---------------------------------------------------------------------------
# Work Experience / Education repeating panels
# ---------------------------------------------------------------------------

WORK_EXPERIENCE_HEADING_PATTERN = re.compile(r"work\s*experience", re.I)
EDUCATION_HEADING_PATTERN = re.compile(r"education", re.I)

# (role, accessible-name pattern, profile entry key) -- role is the
# Playwright ARIA role expected for that Workday field. "combobox" entries
# go through _fill_combobox_locator instead of a plain .fill() (see below).
WORK_EXPERIENCE_FIELD_SPEC: list[tuple[str, re.Pattern, str]] = [
    ("textbox", re.compile(r"job\s*title", re.I), "title"),
    ("textbox", re.compile(r"^company$|company\s*name", re.I), "company"),
    ("textbox", re.compile(r"location", re.I), "location"),
    ("checkbox", re.compile(r"currently\s*work\s*here|i\s*currently\s*work", re.I), "current"),
    ("textbox", re.compile(r"^from$|from\s*date|start\s*date", re.I), "start_date"),
    ("textbox", re.compile(r"^to$|to\s*date|end\s*date", re.I), "end_date"),
    ("textbox", re.compile(r"role\s*description|description|responsibilities", re.I), "description"),
]

EDUCATION_FIELD_SPEC: list[tuple[str, re.Pattern, str]] = [
    ("combobox", re.compile(r"school\s*or\s*university|school\b|university", re.I), "school"),
    ("combobox", re.compile(r"degree", re.I), "degree"),
    ("textbox", re.compile(r"field\s*of\s*study|major", re.I), "field_of_study"),
    ("textbox", re.compile(r"graduation\s*date|end\s*date", re.I), "graduation_date"),
]

EDUCATION_MARKER_PATTERN = re.compile(r"school\s*or\s*university|school\b|university", re.I)
WORK_EXPERIENCE_MARKER_PATTERN = re.compile(r"job\s*title", re.I)


def _legacy_single_education_entry(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """profile.json's flat school/degree/field_of_study/graduation_date keys
    describe one (the candidate's most recent) degree -- used when
    profile['education'] isn't set, so existing profiles with only the flat
    keys still get their one entry filled instead of needing to migrate to
    the list form."""
    school = profile.get("school")
    if not school:
        return []
    return [{
        "school": school,
        "degree": profile.get("degree"),
        "field_of_study": profile.get("field_of_study"),
        "graduation_date": profile.get("graduation_date"),
    }]


async def _fill_combobox_locator(page: Page, locator, value: str) -> bool:
    """Same click-then-match-open-options-before-typing strategy as
    autofill.select_combobox_option, adapted to operate on an already-
    resolved Locator (a specific .nth() occurrence inside a repeating panel)
    instead of a fresh CSS selector string."""
    import autofill
    try:
        await locator.click(timeout=3000)
    except Exception:
        return False
    if await autofill._try_match_open_options(page, value):
        return True
    try:
        await locator.fill(value)
    except Exception:
        return False
    return await autofill._try_match_open_options(page, value)


async def _find_add_button(page: Page, heading_pattern: re.Pattern):
    """Locate the 'Add' button for the repeating section whose heading
    matches heading_pattern, by walking up from the heading text itself to
    the closest ancestor that also contains a button whose text includes
    "Add" -- rather than assuming a specific data-automation-id or DOM
    depth. Workday's exact markup per company/version isn't reliably
    documented, so this stays role/label-based like the rest of this
    codebase's field matching (see autofill.extract_fields), and simply
    returns None (caller skips the section) rather than guessing further
    when this doesn't turn up a confident match."""
    heading = page.get_by_text(heading_pattern, exact=False).first
    try:
        if await heading.count() == 0:
            return None
    except Exception:
        return None

    container = heading.locator(
        "xpath=ancestor::*[.//button[contains(normalize-space(.), 'Add')]][1]"
    )
    try:
        if await container.count() == 0:
            return None
    except Exception:
        return None

    add_button = container.get_by_role("button", name=re.compile(r"^add\b", re.I), exact=False)
    try:
        if await add_button.count() == 0:
            return None
    except Exception:
        return None
    return add_button.first


async def _fill_repeating_section(
    page: Page,
    heading_pattern: re.Pattern,
    entries: list[dict[str, Any]],
    field_spec: list[tuple[str, re.Pattern, str]],
    marker_pattern: re.Pattern,
    log: Any,
) -> list[str]:
    """Click 'Add' as many times as needed to reach len(entries) panels for
    this section, then fill each panel's fields by index -- Workday appends
    a new panel after existing ones on each 'Add' click, so the i-th
    occurrence of a given field's accessible name (e.g. the i-th "Job
    Title" textbox) corresponds to the i-th entry in `entries`, in the same
    order.

    Counts already-rendered panels first (via marker_pattern, e.g. "Job
    Title") and only clicks 'Add' for the shortfall -- Workday commonly
    starts a section with one empty panel already visible, and this also
    keeps a retried run from adding duplicate empty panels on top of ones
    already filled by an earlier attempt.

    Fails closed at every step: if the 'Add' button can't be found, or a
    panel doesn't render the field this expects at the expected index, that
    field (or the rest of this section) is just left unfilled rather than
    risking a click on some unrelated part of the page."""
    if not entries:
        return []

    marker_locator = page.get_by_role("textbox", name=marker_pattern, exact=False)
    combobox_marker_locator = page.get_by_role("combobox", name=marker_pattern, exact=False)
    try:
        existing_count = await marker_locator.count()
        if existing_count == 0:
            existing_count = await combobox_marker_locator.count()
    except Exception:
        existing_count = 0

    needed = len(entries) - existing_count
    if needed > 0:
        add_button = await _find_add_button(page, heading_pattern)
        if add_button is None:
            if existing_count == 0:
                log(f"  Couldn't find an 'Add' button for the Workday section matching "
                    f"\"{heading_pattern.pattern}\" -- skipping it, fill it in manually.")
                return []
            needed = 0
        for _ in range(needed):
            try:
                await add_button.click(timeout=5000)
                await page.wait_for_timeout(500)
            except Exception as e:
                log(f"  Couldn't click 'Add' for the Workday section matching "
                    f"\"{heading_pattern.pattern}\" ({e}); filling only what's already there.")
                break

    filled: list[str] = []
    for i, entry in enumerate(entries):
        for kind, label_pattern, entry_key in field_spec:
            value = entry.get(entry_key)
            if not value:
                continue
            locator = page.get_by_role(kind, name=label_pattern, exact=False)
            try:
                count = await locator.count()
            except Exception:
                continue
            if count <= i:
                # This panel didn't render the field expected at this index
                # -- don't guess at some other panel's field instead.
                continue
            target = locator.nth(i)
            try:
                if kind == "checkbox":
                    if str(value).lower() in ("yes", "true", "1"):
                        await target.check()
                elif kind == "combobox":
                    await _fill_combobox_locator(page, target, str(value))
                else:
                    await target.fill(str(value))
                filled.append(f"{entry_key} (entry {i + 1})")
            except Exception as e:
                log(f"  Couldn't fill '{entry_key}' for entry {i + 1} ({e}).")

    return filled


async def fill_experience_sections(page: Page, profile: dict[str, Any], log: Any = print) -> list[str]:
    """Drive Workday's repeating Work Experience / Education panels from
    profile['previous_employers'] / profile['education'] (falling back to
    the flat school/degree/field_of_study/graduation_date keys for a single
    entry when 'education' isn't set -- see _legacy_single_education_entry).

    Returns the list of entries filled, same role as AutofillResult.filled,
    so the caller can fold it into the overall result. A no-op (returns [])
    on any step that doesn't have this section at all -- callers can call
    this unconditionally on every step of a Workday application; each
    section is only ever actually driven once, on whichever step actually
    has it."""
    filled: list[str] = []
    employers = profile.get("previous_employers") or []
    if employers:
        filled += await _fill_repeating_section(
            page, WORK_EXPERIENCE_HEADING_PATTERN, employers, WORK_EXPERIENCE_FIELD_SPEC,
            WORK_EXPERIENCE_MARKER_PATTERN, log,
        )
    education_entries = profile.get("education") or _legacy_single_education_entry(profile)
    if education_entries:
        filled += await _fill_repeating_section(
            page, EDUCATION_HEADING_PATTERN, education_entries, EDUCATION_FIELD_SPEC,
            EDUCATION_MARKER_PATTERN, log,
        )
    return filled
