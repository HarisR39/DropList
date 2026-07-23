"""
Autofill module for DropList.

Given a Playwright `page` already navigated to a job application form,
this module:
  1. Extracts visible form fields via the accessibility tree
  2. Sends them + the candidate profile to Claude for field mapping
     (skipping the call if a cached mapping exists for that domain +
     field-set already)
  3. Fills the form via Playwright
  4. Flags anything low-confidence for manual review instead of guessing
  5. Repeats across "Next"/"Continue" steps for multi-step applications

Usage:
    from autofill import autofill_form_multistep
    result = await autofill_form_multistep(page, profile, resume_path)
    # result.needs_review -> list of fields that were skipped
"""

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Page

import mapping_cache

# "ollama" (local, no API key/billing) or "anthropic" (Claude API).
LLM_PROVIDER = os.environ.get("AUTOFILL_LLM_PROVIDER", "ollama")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
ANTHROPIC_MODEL = "claude-sonnet-5"
LLM_TIMEOUT_SECONDS = int(os.environ.get("AUTOFILL_LLM_TIMEOUT", "150"))
# Local models lose track of the exact JSON schema and start dropping/misnaming
# keys on very large forms (seen: 87 fields on a Lever form producing valid
# JSON that was missing the "mappings" wrapper entirely). Batching keeps each
# call small enough to be reliable.
MAPPING_BATCH_SIZE = int(os.environ.get("AUTOFILL_MAPPING_BATCH_SIZE", "12"))
PER_BATCH_TIMEOUT_SECONDS = 45

_anthropic_client = None


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _anthropic_client

NEXT_BUTTON_NAMES = ["Next", "Continue", "Next Step"]
# Deliberately NOT a bare "Apply" -- that substring-matches unrelated in-page
# controls on real application forms (e.g. a "Quick Apply with MyGreenhouse"
# shortcut, or a small "Apply" pill that just scrolls to the form section),
# which would hijack every step instead of ever reaching the real form.
ENTRY_BUTTON_NAMES = ["Apply Now", "Apply for this Job", "Apply for this position", "Start Application", "Begin Application"]
# Fallback for ATS platforms that embed the job title in the button, e.g.
# "Apply for Software Engineering Intern" -- requires a word after "Apply" so
# it still excludes a bare "Apply" pill and "Quick Apply with MyGreenhouse".
GENERIC_APPLY_BUTTON_PATTERN = re.compile(r"^apply\b.+", re.IGNORECASE)

COOKIE_BANNER_SELECTORS = ["#onetrust-accept-btn-handler"]
COOKIE_BANNER_BUTTON_NAMES = [
    "Accept All Cookies", "Accept All", "Accept Cookies", "I Accept", "Allow All", "Got it",
]

SYSTEM_PROMPT = """You are a form-filling assistant for job applications. You will be given:
1. A list of detected form fields (label, input type, and any placeholder/help text)
2. A candidate's profile data (resume info, contact details, preferences)

Your job is to map each form field to the correct value from the candidate's profile,
or generate an appropriate short answer for open-ended questions.

Rules:
- Only use information present in the candidate profile. Never invent facts, dates, employers, or numbers.
- If a field cannot be confidently answered from the profile (e.g. a company-specific
  essay question requiring info you don't have), set "value" to null and "needs_review" to true.
- For yes/no eligibility questions (work authorization, sponsorship, relocation), only
  answer if the profile explicitly states this. Otherwise null + needs_review.
- For EEO/demographic self-identification questions (gender, race/ethnicity, disability
  status, veteran status), NEVER guess or assume based on a name, or any other proxy.
  Only answer if the profile has an explicit, matching field for it. Otherwise null +
  needs_review — these are the candidate's own information to disclose, not yours to infer.
- For "select" fields, choose from the provided "options" list only — never return a
  value not in that list.
- For "radio-group" fields, "options" lists mutually-exclusive choices for the single
  question in "label" -- pick exactly ONE and return its exact text (from "options") as
  a string in "value".
- For "checkbox-group" fields (select-all-that-apply), return "value" as a JSON array of
  the option strings (from "options") to check -- an empty array [] if none apply, not null
  (null means you can't determine ANY of it; [] means you determined the answer is "none").
- For "combobox" fields, "options" will be empty — these are searchable dropdowns whose
  real choices are rendered by JavaScript and aren't known ahead of time. Give your best
  plain-text guess based on the profile anyway (e.g. a country or city name); the caller
  verifies it against the live widget and asks the candidate for review if nothing matches,
  so an empty options list here is not itself a reason to refuse.
- For file upload fields (resume, cover letter, transcript), return the file type needed
  in "value" (e.g. "resume") so the caller can attach the right file — do not attempt
  to generate file content.
- If the profile value for a yes/no-style question is a JSON boolean (true/false), write
  it out as the word "Yes" or "No" in "value" — never the literal string "true"/"false",
  since real form options are worded that way, not as JSON booleans.
- Keep generated free-text answers under 100 words unless the field specifies a length.

Respond with ONLY valid JSON matching this schema, no preamble or markdown:
{
  "mappings": [
    {
      "field_id": "string, matches the id provided in the input",
      "value": "string or null",
      "needs_review": true | false,
      "reasoning": "one short sentence"
    }
  ]
}"""


@dataclass
class FormField:
    field_id: str
    label: str
    type: str  # "text" | "textarea" | "select" | "combobox" | "file" | "radio-group" | "checkbox-group" | ...
    selector: str  # playwright selector to act on (unused/empty for *-group types)
    options: list[str] = field(default_factory=list)
    option_selectors: list[str] = field(default_factory=list)  # parallel to options, for *-group types


@dataclass
class AutofillResult:
    filled: list[str]
    needs_review: list[dict[str, Any]]
    errors: list[dict[str, Any]]


async def extract_fields(page: Page) -> list[FormField]:
    """Pull labeled, actionable fields from the page using ARIA roles + label association.

    Radio buttons and checkboxes that belong to the same question are grouped
    into a single "radio-group"/"checkbox-group" FormField rather than one
    field per option -- per-option label text alone (e.g. "He/Him", "J1", "0")
    gives an LLM no way to know what's actually being asked. Radios are
    grouped by their shared `name` attribute (standard HTML mutual-exclusion
    behavior); checkboxes are grouped by a shared `<fieldset>` or
    `[data-field-entry-id]` ancestor (seen on Ashby forms) when one exists,
    otherwise treated individually as before."""
    raw = await page.evaluate(
        """
        () => {
            const results = [];
            let counter = 0;
            const nextId = () => 'f' + (counter++);

            function isVisible(el) {
                if (el.getAttribute('aria-hidden') === 'true') return false;
                const rect = el.getBoundingClientRect();
                return !(rect.width === 0 && rect.height === 0);
            }

            function ownLabel(el) {
                if (el.labels && el.labels.length) return el.labels[0].innerText.trim();
                if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
                if (el.placeholder) return el.placeholder;
                const prev = el.closest('div')?.querySelector('label');
                return prev ? prev.innerText.trim() : '';
            }

            function groupContainer(el) {
                return el.closest('fieldset') || el.closest('[data-field-entry-id]');
            }

            function questionLabel(container, name) {
                if (!container) return '';
                if (name) {
                    const byFor = container.querySelector('label[for="' + CSS.escape(name) + '"]');
                    if (byFor) return byFor.innerText.trim();
                }
                const legend = container.querySelector(':scope > legend');
                if (legend) return legend.innerText.trim();
                const questionish = container.querySelector('label[class*=question], label[class*=title]');
                if (questionish) return questionish.innerText.trim();
                return '';
            }

            const groupedInputs = new Set();

            // --- Radio groups: grouped by shared `name` (standard HTML behavior) ---
            const radios = Array.from(document.querySelectorAll('input[type=radio]'))
                .filter(el => !el.disabled && isVisible(el));
            const radioGroups = new Map();
            radios.forEach(el => {
                const key = el.name || el;
                if (!radioGroups.has(key)) radioGroups.set(key, []);
                radioGroups.get(key).push(el);
            });
            radioGroups.forEach((group, name) => {
                group.forEach(el => groupedInputs.add(el));
                const container = groupContainer(group[0]);
                const label = questionLabel(container, typeof name === 'string' ? name : null) || '(unlabeled question)';
                const optionSelectors = group.map(el => {
                    const oid = nextId();
                    el.setAttribute('data-autofill-id', oid);
                    return '[data-autofill-id="' + oid + '"]';
                });
                results.push({
                    field_id: nextId(),
                    label,
                    type: 'radio-group',
                    options: group.map(ownLabel),
                    option_selectors: optionSelectors,
                });
            });

            // --- Checkbox groups: grouped by shared fieldset/field-entry ancestor ---
            const checkboxes = Array.from(document.querySelectorAll('input[type=checkbox]'))
                .filter(el => !el.disabled && isVisible(el));
            const checkboxGroups = new Map();
            const ungroupedCheckboxes = [];
            checkboxes.forEach(el => {
                const container = groupContainer(el);
                if (container) {
                    if (!checkboxGroups.has(container)) checkboxGroups.set(container, []);
                    checkboxGroups.get(container).push(el);
                } else {
                    ungroupedCheckboxes.push(el);
                }
            });
            checkboxGroups.forEach((group, container) => {
                if (group.length < 2) {
                    // Only one checkbox in this container -- a lone yes/no
                    // checkbox, not a multi-select group.
                    ungroupedCheckboxes.push(...group);
                    return;
                }
                group.forEach(el => groupedInputs.add(el));
                const label = questionLabel(container, null) || '(unlabeled question)';
                const optionSelectors = group.map(el => {
                    const oid = nextId();
                    el.setAttribute('data-autofill-id', oid);
                    return '[data-autofill-id="' + oid + '"]';
                });
                results.push({
                    field_id: nextId(),
                    label,
                    type: 'checkbox-group',
                    options: group.map(ownLabel),
                    option_selectors: optionSelectors,
                });
            });

            // --- Yes/No button-toggle widgets: two <button>Yes</button>/<button>No</button>
            // siblings with only a hidden, non-interactive backing checkbox (tabindex="-1")
            // -- no real radio/checkbox to click, so this needs its own detection. Shaped
            // exactly like a 2-option radio-group, so reuse that type.
            const handledToggleContainers = new Set();
            Array.from(document.querySelectorAll('button')).forEach(btn => {
                if (btn.innerText.trim().toLowerCase() !== 'yes') return;
                const parent = btn.parentElement;
                if (!parent || handledToggleContainers.has(parent)) return;
                const noBtn = Array.from(parent.children)
                    .find(c => c.tagName === 'BUTTON' && c.innerText.trim().toLowerCase() === 'no');
                if (!noBtn) return;
                handledToggleContainers.add(parent);
                const container = groupContainer(parent);
                const label = questionLabel(container, null) || '(unlabeled question)';
                const yesId = nextId();
                btn.setAttribute('data-autofill-id', yesId);
                const noId = nextId();
                noBtn.setAttribute('data-autofill-id', noId);
                results.push({
                    field_id: nextId(),
                    label,
                    type: 'radio-group',
                    options: ['Yes', 'No'],
                    option_selectors: ['[data-autofill-id="' + yesId + '"]', '[data-autofill-id="' + noId + '"]'],
                });
                // The hidden backing checkbox (if any) inside this same container
                // isn't a real control -- don't let it leak through as its own field.
                parent.querySelectorAll('input[type=checkbox]').forEach(el => {
                    const idx = ungroupedCheckboxes.indexOf(el);
                    if (idx !== -1) ungroupedCheckboxes.splice(idx, 1);
                });
            });

            // --- Everything else: text/textarea/select/combobox/file, plus lone checkboxes ---
            const simpleInputs = Array.from(document.querySelectorAll('input, textarea, select'))
                .filter(el => el.type !== 'radio' && (el.type !== 'checkbox' || ungroupedCheckboxes.includes(el)));

            simpleInputs.forEach((el) => {
                if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button' || el.disabled) return;
                if (el.name === 'g-recaptcha-response' || el.id.startsWith('g-recaptcha')) return;
                if (!isVisible(el)) return;

                const label = ownLabel(el);
                let options = [];
                if (el.tagName === 'SELECT') {
                    options = Array.from(el.options).map(o => o.text.trim()).filter(Boolean);
                }
                let type = el.type || el.tagName.toLowerCase();
                // A native <select>'s own .type is "select-one"/"select-multiple",
                // never the plain "select" the rest of this module assumes.
                if (el.tagName === 'SELECT') {
                    type = 'select';
                }
                // React-select and similar widgets render a plain text <input>
                // with role="combobox" backing a JS-driven option list -- not a
                // native <select>, so el.type would otherwise say "text" and
                // options would be empty even though this is really a dropdown.
                if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete') === 'list') {
                    type = 'combobox';
                }

                const id = nextId();
                el.setAttribute('data-autofill-id', id);
                results.push({
                    field_id: id,
                    label: label || '(unlabeled)',
                    type,
                    options,
                    option_selectors: [],
                });
            });

            return results;
        }
        """
    )
    return [
        FormField(
            field_id=r["field_id"],
            label=r["label"],
            type=r["type"],
            selector="" if r.get("option_selectors") else f'[data-autofill-id="{r["field_id"]}"]',
            options=r.get("options", []),
            option_selectors=r.get("option_selectors", []),
        )
        for r in raw
    ]


def _call_anthropic(user_content: str) -> str:
    message = _get_anthropic_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return message.content[0].text


def _call_ollama(user_content: str) -> str:
    import ollama
    response = ollama.chat(
        model=OLLAMA_MODEL,
        format="json",
        options={"num_predict": 4096},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return response["message"]["content"]


def get_mappings(fields: list[FormField], profile: dict[str, Any]) -> list[dict[str, Any]]:
    fields_payload = [
        {"field_id": f.field_id, "label": f.label, "type": f.type, "options": f.options}
        for f in fields
    ]
    user_content = (
        f"CANDIDATE PROFILE:\n{json.dumps(profile, indent=2)}\n\n"
        f"FORM FIELDS DETECTED ON THIS PAGE:\n{json.dumps(fields_payload, indent=2)}\n\n"
        "Map each field to a value per the rules above."
    )

    text = _call_ollama(user_content) if LLM_PROVIDER == "ollama" else _call_anthropic(user_content)
    text = text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(text)
    if isinstance(parsed, list):
        # Some models occasionally drop the {"mappings": [...]} wrapper under
        # instruction-following strain and return the bare array instead.
        return parsed
    return parsed["mappings"]


def get_mappings_cached(fields: list[FormField], profile: dict[str, Any], domain: str) -> list[dict[str, Any]]:
    """Same as get_mappings, but (a) splits large field lists into smaller
    batches -- local models start dropping/misnaming JSON keys on very large
    forms (seen: an 87-field Lever form producing valid JSON missing the
    "mappings" key entirely) -- and (b) caches (and can fail) per batch, so
    one bad batch doesn't lose mappings the rest of the form already got
    right, and a retry doesn't have to redo batches that already succeeded."""
    all_mappings: list[dict[str, Any]] = []
    batches = [fields[i:i + MAPPING_BATCH_SIZE] for i in range(0, len(fields), MAPPING_BATCH_SIZE)]

    for batch_num, batch in enumerate(batches, start=1):
        field_hash = mapping_cache.hash_fields(batch)
        cached = mapping_cache.get(domain, field_hash)
        if cached is not None:
            all_mappings.extend(cached)
            continue

        if len(batches) > 1:
            print(f"  Batch {batch_num}/{len(batches)} ({len(batch)} field(s))...")
        try:
            batch_mappings = get_mappings(batch, profile)
        except Exception as e:
            print(f"  Batch {batch_num}/{len(batches)} failed ({e}); flagging its fields for manual review.")
            batch_mappings = [
                {"field_id": f.field_id, "value": None, "needs_review": True,
                 "reasoning": "LLM mapping call failed or returned malformed JSON for this batch"}
                for f in batch
            ]
        else:
            mapping_cache.store(domain, field_hash, batch_mappings)
        all_mappings.extend(batch_mappings)

    return all_mappings


def _best_word_overlap_index(value: str, option_texts: list[str]) -> int | None:
    """Index of the option with the most word overlap with value, or None if
    there's no clear single winner. Used when Playwright's literal substring
    name-match misses an option whose wording paraphrases the value (e.g.
    value "Not a veteran" vs. option "I am not a protected veteran" -- "not a
    veteran" isn't a contiguous substring, but the two clearly agree)."""
    value_words = {w for w in value.lower().split() if len(w) > 1}
    if not value_words:
        return None
    scores = [sum(1 for w in value_words if w in text.lower()) for text in option_texts]
    best = max(scores, default=0)
    if best == 0 or scores.count(best) != 1:
        return None
    return scores.index(best)


OTHER_OPTION_PATTERNS = ["other", "not listed", "none of the above", "not represented here", "not applicable"]


def _find_other_option_index(option_texts: list[str]) -> int | None:
    """Index of a catch-all option ("Other", "Not Listed", etc.), or None if
    there isn't one. Used as a last resort when a returned value genuinely
    doesn't match any option -- picking a real "Other"-style escape hatch is
    more useful than flagging for manual review when one is available."""
    for i, text in enumerate(option_texts):
        text_lower = text.strip().lower()
        if any(pattern in text_lower for pattern in OTHER_OPTION_PATTERNS):
            return i
    return None


async def _try_match_open_options(page: Page, value: str) -> bool:
    """Look at whatever combobox options are currently rendered and click one
    if there's an unambiguous match. Returns False without clicking anything
    if there's no match or more than one equally-good candidate."""
    named = page.get_by_role("option", name=value, exact=False)
    try:
        await named.first.wait_for(timeout=2000)
    except Exception:
        pass
    if await named.count() >= 1:
        await named.first.click()
        return True

    everything = page.get_by_role("option")
    try:
        await everything.first.wait_for(timeout=1000)
    except Exception:
        pass
    count = await everything.count()
    if count == 0:
        return False
    if count == 1:
        await everything.first.click()
        return True

    # Multiple options are open -- e.g. a short static EEO-style list that's
    # always fully shown. A literal substring match already failed above, so
    # try a looser word-overlap match; if nothing stands out as the clear
    # winner (e.g. 10 same-named cities across different states), don't guess.
    texts = await everything.all_inner_texts()
    idx = _best_word_overlap_index(value, texts)
    if idx is None:
        return False
    await everything.nth(idx).click()
    return True


async def select_combobox_option(page: Page, selector: str, value: str) -> bool:
    """Select an option from a react-select-style combobox (role="combobox"
    backed by a JS-rendered option list, not a native <select>).

    These widgets fall into two different behaviors and there's no reliable
    way to tell which one a given field is ahead of time:
      - Short static lists (EEO Yes/No/Decline-style questions) render every
        option as soon as you click to open it, and typing anything that
        isn't a literal substring of an option's text empties the list to
        zero instead of doing anything useful.
      - Search-driven lists (city/school/country) render nothing until you
        type, then filter server-side.

    So: try matching against whatever's already open first (handles the
    static-list case safely), and only type to trigger a search as a
    fallback if that didn't turn up a confident match."""
    locator = page.locator(selector)
    await locator.click()

    if await _try_match_open_options(page, value):
        return True

    await locator.fill(value)
    if await _try_match_open_options(page, value):
        return True

    # Last resort: some search-driven lists (school, field of study, etc.) also
    # provide a catch-all "Other"/"Not Listed" entry for values outside their
    # known list -- worth trying before giving up entirely.
    for fallback in ("Other", "Not Listed"):
        await locator.fill(fallback)
        if await _try_match_open_options(page, fallback):
            return True
    return False


def _needs_review_entry(f: FormField, reasoning: str) -> dict[str, Any]:
    return {
        "label": f.label,
        "reasoning": reasoning,
        "field_id": f.field_id,
        "selector": f.selector,
        "type": f.type,
        "options": f.options,
        "option_selectors": f.option_selectors,
    }


async def apply_mapping(
    page: Page, fields: list[FormField], mappings: list[dict[str, Any]], resume_path: str, cover_letter_path: str | None
) -> AutofillResult:
    field_by_id = {f.field_id: f for f in fields}
    filled, needs_review, errors = [], [], []

    for m in mappings:
        f = field_by_id.get(m["field_id"])
        if f is None:
            continue
        if m.get("needs_review") or m.get("value") is None:
            needs_review.append(_needs_review_entry(f, m.get("reasoning", "")))
            continue

        value = m["value"]
        if f.type == "select":
            # The model doesn't always return the option text verbatim (wrong case,
            # extra whitespace, or a value that isn't actually one of the options --
            # more common with smaller local models). A raw select_option() call would
            # throw and land as a silent "errors" entry the user never gets asked
            # about, so validate first and route a mismatch into needs_review instead,
            # where it flows into the same interactive prompt as anything else unsure.
            match = next((opt for opt in f.options if opt.strip().lower() == str(value).strip().lower()), None)
            if match is None:
                other_idx = _find_other_option_index(f.options)
                if other_idx is not None:
                    match = f.options[other_idx]
            if match is None:
                needs_review.append(_needs_review_entry(
                    f, f"Returned value \"{value}\" doesn't match any of this dropdown's options"))
                continue
            value = match

        elif f.type == "radio-group":
            idx = next((i for i, opt in enumerate(f.options) if opt.strip().lower() == str(value).strip().lower()), None)
            if idx is None:
                idx = _best_word_overlap_index(str(value), f.options)
            if idx is None:
                idx = _find_other_option_index(f.options)
            if idx is None:
                needs_review.append(_needs_review_entry(
                    f, f"Returned value \"{value}\" doesn't clearly match any of this question's options"))
                continue
            value = idx

        elif f.type == "checkbox-group":
            requested = value if isinstance(value, list) else [value]
            requested_norm = {str(v).strip().lower() for v in requested if v not in (None, "")}
            indices = [i for i, opt in enumerate(f.options) if opt.strip().lower() in requested_norm]
            if requested_norm and not indices:
                other_idx = _find_other_option_index(f.options)
                if other_idx is not None:
                    indices = [other_idx]
            if requested_norm and not indices:
                needs_review.append(_needs_review_entry(
                    f, f"None of the returned values {sorted(requested_norm)} matched this question's options"))
                continue
            value = indices

        try:
            if f.type == "file":
                path = resume_path if "resume" in str(value).lower() else cover_letter_path
                if path:
                    await page.locator(f.selector).set_input_files(path)
            elif f.type == "select":
                await page.locator(f.selector).select_option(label=value)
            elif f.type == "combobox":
                matched = await select_combobox_option(page, f.selector, str(value))
                if not matched:
                    needs_review.append(_needs_review_entry(
                        f, f"No dropdown option appeared for \"{value}\" after typing it"))
                    continue
            elif f.type == "radio-group":
                # .click() rather than .check(): option_selectors here may point to a
                # real <input type=radio> OR a plain <button> (e.g. a "Yes"/"No"
                # toggle widget with only a hidden, non-interactive backing checkbox)
                # -- .check() only works on real checkbox/radio inputs, .click() works
                # on both, and clicking a radio input natively selects it same as .check().
                await page.locator(f.option_selectors[value]).click()
            elif f.type == "checkbox-group":
                for i in value:
                    await page.locator(f.option_selectors[i]).check()
            elif f.type in ("checkbox", "radio"):
                if str(value).lower() in ("yes", "true", "1"):
                    await page.locator(f.selector).check()
            else:
                await page.locator(f.selector).fill(str(value))
            filled.append(f.label)
        except Exception as e:
            errors.append({"label": f.label, "error": str(e)})

    return AutofillResult(filled=filled, needs_review=needs_review, errors=errors)


def _account_signup_mappings(fields: list[FormField], profile: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    """Deterministically map password fields (and any email field alongside them) to
    profile['account_password'] / profile['account_email']. A password can't be inferred
    by Claude from a resume profile, so this is handled directly instead of risking a
    needs_review on every single signup form."""
    password_fields = [f for f in fields if f.type == "password"]
    if not password_fields:
        return [], set()

    account_password = profile.get("account_password")
    account_email = profile.get("account_email") or profile.get("email")

    mappings = []
    handled_ids = set()

    for f in password_fields:
        mappings.append({
            "field_id": f.field_id,
            "value": account_password,
            "needs_review": account_password is None,
            "reasoning": "account signup password from profile" if account_password else "no account_password set in profile",
        })
        handled_ids.add(f.field_id)

    for f in fields:
        if f.field_id in handled_ids:
            continue
        if f.type == "email" or "email" in f.label.lower():
            mappings.append({
                "field_id": f.field_id,
                "value": account_email,
                "needs_review": account_email is None,
                "reasoning": "account signup email from profile" if account_email else "no account_email/email set in profile",
            })
            handled_ids.add(f.field_id)

    return mappings, handled_ids


def _custom_answer_mappings(fields: list[FormField], profile: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    """Reuse answers the user was previously asked for (profile['custom_answers'],
    keyed by lowercased field label) instead of asking again or re-flagging them."""
    custom_answers = profile.get("custom_answers", {})
    if not custom_answers:
        return [], set()

    mappings = []
    handled_ids = set()
    for f in fields:
        key = f.label.strip().lower()
        if key in custom_answers:
            mappings.append({
                "field_id": f.field_id,
                "value": custom_answers[key],
                "needs_review": False,
                "reasoning": "reused a previously-provided answer",
            })
            handled_ids.add(f.field_id)
    return mappings, handled_ids


async def autofill_form(
    page: Page,
    profile: dict[str, Any],
    resume_path: str,
    cover_letter_path: str | None = None,
    fields: list[FormField] | None = None,
) -> AutofillResult:
    if fields is None:
        fields = await extract_fields(page)
    if not fields:
        return AutofillResult(filled=[], needs_review=[], errors=[])

    account_mappings, account_handled_ids = _account_signup_mappings(fields, profile)
    custom_mappings, custom_handled_ids = _custom_answer_mappings(fields, profile)
    handled_ids = account_handled_ids | custom_handled_ids
    remaining = [f for f in fields if f.field_id not in handled_ids]

    mappings = account_mappings + custom_mappings
    if remaining:
        domain = urlparse(page.url).netloc
        mappings.extend(await _get_mappings_with_timeout(remaining, profile, domain))

    return await apply_mapping(page, fields, mappings, resume_path, cover_letter_path)


async def _get_mappings_with_timeout(
    fields: list[FormField], profile: dict[str, Any], domain: str
) -> list[dict[str, Any]]:
    """Run the (blocking) LLM mapping call off the event loop, print progress
    so a slow local model doesn't look like a hang, and give up gracefully
    (flagging for manual review) instead of blocking forever."""
    num_batches = max(1, -(-len(fields) // MAPPING_BATCH_SIZE))  # ceil div
    timeout = max(LLM_TIMEOUT_SECONDS, num_batches * PER_BATCH_TIMEOUT_SECONDS)
    print(f"  Asking {LLM_PROVIDER} to map {len(fields)} field(s) on {domain} "
          f"({num_batches} batch(es))...")
    start = time.time()
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(get_mappings_cached, fields, profile, domain),
            timeout=timeout,
        )
        print(f"  Got mappings in {time.time() - start:.1f}s.")
        return result
    except asyncio.TimeoutError:
        print(f"  Timed out after {timeout}s waiting for {LLM_PROVIDER}; "
              f"flagging these fields for manual review.")
    except Exception as e:
        print(f"  Mapping call failed ({e}); flagging these fields for manual review.")

    return [
        {"field_id": f.field_id, "value": None, "needs_review": True, "reasoning": "LLM mapping call failed or timed out"}
        for f in fields
    ]


async def find_button_by_names(page: Page, names: list[str]):
    """Return a locator for the first button/link matching any of the given names, or None."""
    for name in names:
        for role in ("button", "link"):
            locator = page.get_by_role(role, name=name, exact=False)
            if await locator.count() > 0:
                return locator.first
    return None


async def find_next_button(page: Page):
    """Return a locator for a Next/Continue button on the current step, or None."""
    return await find_button_by_names(page, NEXT_BUTTON_NAMES)


async def find_entry_button(page: Page, allow_bare_apply: bool = False):
    """Return a locator for an initial Apply/Start Application button, or None.

    allow_bare_apply enables matching an exact, bare "Apply" button as a last
    resort. That's deliberately gated behind a flag rather than always on: a
    bare "Apply" also matches unrelated in-page controls on pages that already
    have a real form on them (a "Quick Apply with MyGreenhouse" shortcut, or a
    small "Apply" pill that just scrolls to the form section) -- clicking those
    instead of filling the already-present form caused real bugs before. It's
    only safe to try when the page genuinely has no fields yet, which the
    caller is expected to check."""
    exact = await find_button_by_names(page, ENTRY_BUTTON_NAMES)
    if exact is not None:
        return exact

    # Some ATS platforms render a dynamic "Apply for <Job Title>" button whose
    # exact text can't be listed ahead of time. Match anything starting with
    # "Apply" followed by more text -- this still excludes a bare "Apply" pill
    # (e.g. a scroll-to-form shortcut) and unrelated "Quick Apply ..." controls
    # that don't start with the word, both of which caused real problems before.
    for role in ("button", "link"):
        locator = page.get_by_role(role, name=GENERIC_APPLY_BUTTON_PATTERN)
        if await locator.count() > 0:
            return locator.first

    if allow_bare_apply:
        for role in ("button", "link"):
            locator = page.get_by_role(role, name="Apply", exact=True)
            if await locator.count() > 0:
                return locator.first
    return None


async def dismiss_cookie_banner(page: Page) -> bool:
    """Dismiss a known cookie-consent banner if one is covering the page. Scoped to
    well-known cookie-consent patterns only (not a generic popup closer), since
    blindly clicking arbitrary close/X buttons risks dismissing something that
    matters instead of a banner."""
    for selector in COOKIE_BANNER_SELECTORS:
        locator = page.locator(selector)
        try:
            if await locator.count() > 0 and await locator.first.is_visible():
                await locator.first.click()
                return True
        except Exception:
            pass

    for name in COOKIE_BANNER_BUTTON_NAMES:
        locator = page.get_by_role("button", name=name, exact=False)
        try:
            if await locator.count() > 0 and await locator.first.is_visible():
                await locator.first.click()
                return True
        except Exception:
            pass

    return False


async def _resolve_needs_review_interactively(
    page: Page, profile: dict[str, Any], needs_review: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Ask the user for anything Claude couldn't confidently answer, right while
    those fields are still live on the page, fill in whatever they provide, and
    remember it in profile['custom_answers'] so the same question on a future
    application is answered automatically instead of asked again."""
    still_needs_review = []
    custom_answers = profile.setdefault("custom_answers", {})

    for item in needs_review:
        label = item["label"]
        print(f"\nNeeds your input -- \"{label}\"")
        if item.get("reasoning"):
            print(f"  (reason: {item['reasoning']})")
        if item.get("options"):
            print(f"  Options: {', '.join(item['options'])}")

        answer = input("  Enter a value (or press Enter to skip / leave for manual review): ").strip()
        if not answer:
            still_needs_review.append(item)
            continue

        selector = item.get("selector")
        field_type = item.get("type")
        options = item.get("options") or []
        option_selectors = item.get("option_selectors") or []
        try:
            if field_type == "select":
                match = next((opt for opt in options if opt.strip().lower() == answer.strip().lower()), None)
                await page.locator(selector).select_option(label=match or answer)
            elif field_type == "combobox":
                matched = await select_combobox_option(page, selector, answer)
                if not matched:
                    print(f"  No dropdown option appeared for \"{answer}\"; enter it manually in the browser.")
            elif field_type == "radio-group":
                idx = next((i for i, opt in enumerate(options) if opt.strip().lower() == answer.strip().lower()), None)
                if idx is None:
                    idx = _best_word_overlap_index(answer, options)
                if idx is None:
                    print(f"  \"{answer}\" doesn't clearly match any option; select it manually in the browser.")
                else:
                    await page.locator(option_selectors[idx]).click()
            elif field_type == "checkbox-group":
                requested = {part.strip().lower() for part in answer.split(",") if part.strip()}
                matched_any = False
                for i, opt in enumerate(options):
                    if opt.strip().lower() in requested:
                        await page.locator(option_selectors[i]).check()
                        matched_any = True
                if not matched_any:
                    print(f"  \"{answer}\" didn't match any option; select it manually in the browser "
                          f"(comma-separate multiple choices).")
            elif field_type in ("checkbox", "radio"):
                if answer.lower() in ("yes", "true", "1"):
                    await page.locator(selector).check()
            elif selector:
                await page.locator(selector).fill(answer)
            else:
                raise ValueError("no selector recorded for this field")
        except Exception as e:
            print(f"  Couldn't fill it automatically ({e}); enter it manually in the browser.")

        custom_answers[label.strip().lower()] = answer

    return still_needs_review


async def autofill_form_multistep(
    page: Page,
    profile: dict[str, Any],
    resume_path: str,
    cover_letter_path: str | None = None,
    max_steps: int = 6,
    screenshot_dir: str | None = None,
    screenshot_prefix: str = "form",
    interactive: bool = True,
    on_needs_review: Any = None,
) -> AutofillResult:
    """Fill a (possibly multi-step) application form, advancing through
    Next/Continue steps up to max_steps. Also handles landing pages with no
    form yet by clicking an initial Apply/Start Application button. Never
    clicks a final Submit — that's left for a human to do after reviewing
    the filled form. If interactive, prompts for anything Claude couldn't
    confidently answer and remembers the answer in profile['custom_answers'].

    on_needs_review, if given, is called with the list of needs_review items
    right before blocking on the interactive terminal prompts -- e.g. to fire
    a notification the moment the AI hands off to a human, since the prompts
    themselves block until someone is actually there to answer them."""
    all_filled: list[str] = []
    all_needs_review: list[dict[str, Any]] = []
    all_errors: list[dict[str, Any]] = []

    await dismiss_cookie_banner(page)

    for step in range(max_steps):
        # Fields are extracted first (not gated behind the entry-button check):
        # many career sites keep a persistent header (search box, language
        # picker, etc.) that shows up as "fields" even on the pre-application
        # landing page, so we still need to check for an entry button even when
        # fields are already present. But a bare "Apply" match is only safe to
        # try when there are truly zero fields yet -- seeing any fields already
        # means clicking a bare "Apply" risks hitting an unrelated shortcut
        # instead of just filling what's already there (see find_entry_button).
        fields = await extract_fields(page)

        entry_button = await find_entry_button(page, allow_bare_apply=not fields)
        if entry_button is not None:
            try:
                await entry_button.click(timeout=5000)
            except Exception:
                if await dismiss_cookie_banner(page):
                    try:
                        await entry_button.click(timeout=5000)
                    except Exception:
                        break
                else:
                    break
            await _settle(page)
            continue

        if not fields:
            break

        if screenshot_dir:
            await page.screenshot(path=f"{screenshot_dir}/{screenshot_prefix}_step{step}_before.png")

        result = await autofill_form(page, profile, resume_path, cover_letter_path, fields=fields)
        remaining_needs_review = result.needs_review
        if remaining_needs_review and on_needs_review is not None:
            on_needs_review(remaining_needs_review)
        if interactive and remaining_needs_review:
            remaining_needs_review = await _resolve_needs_review_interactively(page, profile, remaining_needs_review)

        all_filled.extend(result.filled)
        all_needs_review.extend(remaining_needs_review)
        all_errors.extend(result.errors)

        if screenshot_dir:
            await page.screenshot(path=f"{screenshot_dir}/{screenshot_prefix}_step{step}_after.png")

        next_button = await find_next_button(page)
        if next_button is None:
            break
        try:
            await next_button.click(timeout=5000)
        except Exception:
            break
        await _settle(page)

    return AutofillResult(filled=all_filled, needs_review=all_needs_review, errors=all_errors)


async def _settle(page: Page) -> None:
    """Wait for a step transition to render without hanging on sites that never
    go fully network-idle (analytics beacons, chat widgets, etc.)."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(1200)
