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
# Local vision model used only as a last-resort fallback (see
# _vision_find_apply_button_text) when a page has no fillable fields AND no
# Apply-style button was found via the normal accessibility-tree search --
# some ATS platforms render their Apply control as something Playwright's
# role-based query can't see (a styled <div>, a custom web component, etc.)
# even though it's plainly visible on screen.
OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "moondream")
VISION_TIMEOUT_SECONDS = int(os.environ.get("AUTOFILL_VISION_TIMEOUT", "60"))
# How long Ollama keeps a model resident in memory after each call before
# unloading it (Ollama's own default is 5 minutes). A run can easily go
# longer than that between mapping calls -- reviewing one listing, picking
# the next -- so the model would otherwise unload and cold-start-reload on
# the next call. "30m" comfortably covers a normal gap between listings
# without keeping it loaded forever after the whole program has exited.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")
# Applies per batch (see MAPPING_BATCH_SIZE), not to the form as a whole --
# fields are mapped and filled one batch at a time, so a slow/stuck batch
# only costs its own fields, not the whole form's progress.
LLM_TIMEOUT_SECONDS = int(os.environ.get("AUTOFILL_LLM_TIMEOUT", "150"))
# Local models lose track of the exact JSON schema and start dropping/misnaming
# keys on very large forms (seen: 87 fields on a Lever form producing valid
# JSON that was missing the "mappings" wrapper entirely). Batching keeps each
# call small enough to be reliable.
MAPPING_BATCH_SIZE = int(os.environ.get("AUTOFILL_MAPPING_BATCH_SIZE", "12"))

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

# "Apply with LinkedIn"/"Apply with GitHub"/etc. matches GENERIC_APPLY_BUTTON_
# PATTERN just as readily as "Apply for Software Engineer" does -- these are
# OAuth-style shortcuts this automation can't and shouldn't complete on your
# behalf (it would mean logging into your LinkedIn/GitHub/etc. account), so
# they're always skipped in favor of the site's own manual application path,
# even when one would otherwise match first in DOM order. "Resume"/"CV" is
# grouped in here too for a different reason: some ATS platforms (e.g.
# Oracle Recruiting Cloud) offer an "Apply with Resume" shortcut that
# auto-parses an uploaded resume into the whole application instead of
# leaving the real form fields in place -- skipping it in favor of the
# manual path means the resume still gets attached (via the real file-
# upload field autofill_form already handles), but every other field goes
# through this module's own field-by-field mapping instead of the ATS's own
# unpredictable resume parser.
THIRD_PARTY_APPLY_KEYWORDS = [
    "linkedin", "github", "indeed", "google", "facebook", "twitter",
    "apple", "seek", "ziprecruiter", "monster", "glassdoor",
    "resume", "cv",
]


def _is_third_party_apply_option(text: str) -> bool:
    lowered = text.strip().lower()
    return any(keyword in lowered for keyword in THIRD_PARTY_APPLY_KEYWORDS)

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
- A profile's "first_name"/"last_name" are the candidate's legal name — use these for any
  field labeled "First Name"/"Last Name"/"Full Name"/"Legal Name". Only use "preferred_name"
  for a field that explicitly asks for a preferred name, nickname, chosen name, or "name you
  go by" — never substitute it for a plain "First Name" field just because it's shorter or
  more casual.
- A profile's "education_start_date" is when the candidate started their most recent degree
  program; "graduation_date" is when they finished (or expect to). A field asking "start
  date", "start term", or "enrollment date" wants "education_start_date"; a field asking
  "end date", "graduation date", or "expected graduation" wants "graduation_date". These are
  two different dates — never fill both with the same value, and never substitute one for
  the other just because they're both dates in the profile.

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
    login_required: bool = False
    # Only ever set by autofill_form_multistep, to the page it actually
    # ended up operating on -- an Apply/Next click can open the real next
    # step in a new tab rather than navigating in place (seen on some
    # Greenhouse listings), and autofill_form_multistep follows that new
    # tab internally. Compare against whatever page you passed in: if it
    # differs, callers that track the page across calls (main.run_
    # automation's company_page) need to switch to it too, or they'll keep
    # looking at the original, now-abandoned tab instead of the real one.
    final_page: Any = None


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


def _call_ollama(user_content: str, num_predict: int = 4096) -> str:
    import ollama
    response = ollama.chat(
        model=OLLAMA_MODEL,
        format="json",
        options={"num_predict": num_predict},
        keep_alive=OLLAMA_KEEP_ALIVE,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return response["message"]["content"]


def warm_up_ollama() -> None:
    """Force OLLAMA_MODEL into memory right away with a throwaway call,
    instead of letting the first real mapping batch be the one that pays
    the cold-start cost. Meant to be kicked off in the background (see
    main.run_automation) right as the browser session starts, so the model
    load overlaps with jobright login/navigation instead of blocking the
    first listing's mapping call. Best-effort -- a failure here just means
    the first real call ends up paying the cold-start cost as before,
    exactly like if this had never been called at all."""
    import ollama
    try:
        ollama.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": "hi"}],
            options={"num_predict": 1},
            keep_alive=OLLAMA_KEEP_ALIVE,
        )
    except Exception:
        pass


APPLY_VISION_PROMPT = (
    "You are looking at a screenshot of a job application webpage. Is there a "
    "button or link visible whose purpose is to start or begin a job "
    "application -- for example labeled something like \"Apply\", \"Apply Now\", "
    "\"Apply for this Job\", \"Start Application\", or similar? "
    "Do NOT pick a button that imports/autofills the application from a "
    "third-party account or a resume upload, such as \"Apply with LinkedIn\", "
    "\"Apply with Indeed\", \"Apply with Google\", or \"Apply with Resume\"/\"CV\" -- "
    "only a plain, direct apply button counts. "
    "If yes, reply with ONLY its exact visible text and nothing else. "
    "If there is no such button or link visible, reply with exactly: NONE"
)


def _call_ollama_vision(image_bytes: bytes, prompt: str) -> str:
    import ollama
    response = ollama.chat(
        model=OLLAMA_VISION_MODEL,
        messages=[{"role": "user", "content": prompt, "images": [image_bytes]}],
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

    if LLM_PROVIDER == "ollama":
        # A fixed 4096-token ceiling measurably slows local CPU inference on
        # small batches -- the model keeps "room" to ramble before settling
        # into the JSON even when the actual answer is short. Scaling the
        # cap to the batch size (~220 tokens/field covers a value + short
        # reasoning sentence each, from observed output) cuts real latency
        # without truncating legitimate output -- worst case a batch that
        # somehow needs more just runs out and fails json.loads() below,
        # which is already handled like any other malformed-response failure.
        num_predict = min(4096, max(800, len(fields) * 220 + 300))
        text = _call_ollama(user_content, num_predict=num_predict)
    else:
        text = _call_anthropic(user_content)
    text = text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    parsed = json.loads(text)
    if isinstance(parsed, list):
        # Some models occasionally drop the {"mappings": [...]} wrapper under
        # instruction-following strain and return the bare array instead.
        return parsed
    return parsed["mappings"]


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
    page: Page, fields: list[FormField], mappings: list[dict[str, Any]], resume_path: str, cover_letter_path: str | None,
    on_frame: Any = None,
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
            if on_frame is not None:
                # Capture right after this field's own visible change, not
                # just before/after the whole batch -- so the live view
                # tracks along as the form fills in field by field instead
                # of jumping straight from empty to fully filled.
                frame_bytes = await take_frame_screenshot(page)
                if frame_bytes is not None:
                    on_frame(frame_bytes)
        except Exception as e:
            errors.append({"label": f.label, "error": str(e)})

    return AutofillResult(filled=filled, needs_review=needs_review, errors=errors)


LOGIN_GATE_MAX_FIELDS = 4
LOGIN_GATE_PHRASES = [
    "welcome back", "already have an account", "forgot your password",
    "forgot password", "sign in to your account", "log in to your account",
]


async def _looks_like_login_gate(page: Page, fields: list[FormField]) -> bool:
    """Best-effort signal that this page is asking you to log into an
    EXISTING account, rather than showing the real application form or a
    normal new-account signup step. There's no reliable site-agnostic way
    to tell these apart with certainty, so this only fires on a strong
    pairing of two signals: a small password-bearing form (a real
    application legitimately has a password field too sometimes, for new
    account creation, but always alongside plenty of other real fields --
    name, resume, etc.) AND clear "returning user" phrasing nearby (plain
    account-creation forms don't say "welcome back" or "forgot your
    password"). Without the phrasing, this falls back to the existing
    account-signup handling (_account_signup_mappings) instead of stopping.
    A false positive here just means pausing for a human a bit early,
    never a wrong auto-fill or auto-submit."""
    if not fields or len(fields) > LOGIN_GATE_MAX_FIELDS:
        return False
    if not any(f.type == "password" for f in fields):
        return False
    for phrase in LOGIN_GATE_PHRASES:
        if await page.get_by_text(phrase, exact=False).count() > 0:
            return True
    return False


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


BOILERPLATE_CONSENT_PATTERNS = [
    "terms and conditions", "terms of service", "terms of use",
    "privacy policy", "privacy notice", "privacy statement",
    "code of conduct", "data protection policy",
]


def _is_boilerplate_consent_checkbox(label: str) -> bool:
    lowered = label.strip().lower()
    return any(pattern in lowered for pattern in BOILERPLATE_CONSENT_PATTERNS)


def _consent_checkbox_mappings(fields: list[FormField]) -> tuple[list[dict[str, Any]], set[str]]:
    """Deterministically check boilerplate agree-to-terms/privacy-policy
    checkboxes -- unlike an EEO/eligibility checkbox, agreeing to the site's
    terms carries no factual claim about the candidate, so there's nothing
    for a human to actually decide here. Handled directly instead of risking
    an LLM refusal or a needs_review pause on every single application."""
    mappings = []
    handled_ids = set()
    for f in fields:
        if f.type == "checkbox" and _is_boilerplate_consent_checkbox(f.label):
            mappings.append({
                "field_id": f.field_id,
                "value": "Yes",
                "needs_review": False,
                "reasoning": "boilerplate terms/privacy-policy consent required to proceed",
            })
            handled_ids.add(f.field_id)
    return mappings, handled_ids


SECOND_ADDRESS_LABEL_PATTERNS = ["address line 2", "address 2", "address line two"]
# \b word-boundary matches, not bare substrings -- "unit" as a plain
# substring also matches inside "opportunity" ("opport-UNIT-y"), which
# wrongly swallowed unrelated fields like "How did you hear about this
# opportunity?" before this was word-boundary-anchored.
SECOND_ADDRESS_LABEL_KEYWORD_PATTERN = re.compile(r"\b(apt|apartment|suite|unit)\b", re.IGNORECASE)


def _is_second_address_field(label: str) -> bool:
    lowered = label.strip().lower()
    if any(pattern in lowered for pattern in SECOND_ADDRESS_LABEL_PATTERNS):
        return True
    # Covers "Apt/Suite/Unit", "Apartment, suite, etc.", "Unit #", and
    # similar -- a job application has no other realistic reason to ask
    # about an apartment/suite/unit, so a bare keyword match is safe here.
    return SECOND_ADDRESS_LABEL_KEYWORD_PATTERN.search(lowered) is not None


# \b word-boundary match (not a bare substring) so this doesn't fire on
# unrelated words that merely contain "ext" -- matches "ext"/"ext."
# equally well as "extension", since the trailing period still counts as a
# word-to-non-word boundary.
PHONE_EXTENSION_LABEL_PATTERN = re.compile(r"\b(extension|ext)\b", re.IGNORECASE)


def _is_phone_extension_field(label: str) -> bool:
    # Per explicit user preference -- no extension to give, and it's always
    # optional, same rationale as the second-address-line field below.
    return PHONE_EXTENSION_LABEL_PATTERN.search(label.strip().lower()) is not None


def _ignored_field_ids(fields: list[FormField]) -> set[str]:
    """Field ids to skip entirely -- never filled, never flagged for manual
    review, never even shown to the LLM. Currently the near-universal
    optional "Address Line 2"/"Apt/Suite/Unit" field, and any "Phone
    Extension" field: per explicit user preference, this automation doesn't
    bother with either at all, and silently skipping them (rather than
    leaving them null+needs_review) avoids cluttering every single
    application's review list with fields that are essentially always
    optional and never actually need a human decision."""
    return {
        f.field_id for f in fields
        if _is_second_address_field(f.label) or _is_phone_extension_field(f.label)
    }


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


# Plain contact/bio fields, matched by label -- straight lookups against
# profile keys with no judgment involved (unlike EEO/eligibility Yes-No
# questions, open-ended essays, or arbitrary dropdown option matching,
# which all still need the LLM's actual language understanding and stay
# out of this list on purpose -- see SYSTEM_PROMPT's own rules on those).
# Order matters: first pattern to match wins, so more specific patterns
# (e.g. "preferred name") are listed before more general ones they'd
# otherwise be swallowed by.
PROFILE_FIELD_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Must come before the bare "preferred name" pattern below -- "preferred
    # full name" wouldn't actually match it (it requires "preferred" directly
    # followed by "name", not with "full" in between), but keeping the more
    # specific one first matches this list's own stated convention.
    (re.compile(r"\bpreferred\s*full\s*name\b", re.I), "preferred_full_name"),
    (re.compile(r"\bpreferred\s*name\b|\bnickname\b|\bname\s+you\s+go\s+by\b|\bchosen\s*name\b", re.I), "preferred_name"),
    (re.compile(r"\bfirst\s*name\b|\bgiven\s*name\b", re.I), "first_name"),
    (re.compile(r"\blast\s*name\b|\bsurname\b|\bfamily\s*name\b", re.I), "last_name"),
    (re.compile(r"\be[- ]?mail\b", re.I), "email"),
    (re.compile(r"\bphone\b|\bmobile\b|\btelephone\b", re.I), "phone"),
    (re.compile(r"\blinkedin\b", re.I), "linkedin_url"),
    (re.compile(r"\bportfolio\b|\bpersonal\s*website\b|\bwebsite\b", re.I), "portfolio_url"),
    (re.compile(r"\bschool\b|\buniversity\b|\bcollege\b", re.I), "school"),
    (re.compile(r"\bdegree\b", re.I), "degree"),
    (re.compile(r"\bfield\s*of\s*study\b|\bmajor\b", re.I), "field_of_study"),
    (re.compile(r"\bcountry\b", re.I), "country"),
    (re.compile(r"\bstate\b|\bprovince\b", re.I), "state"),
]
# "Phone Country Code" / "Dial Code" / "Area Code" ask for a short numeric
# code, not the candidate's full phone number or country name -- must be
# excluded up front, before "phone" or "country" above gets a chance to
# match ("phone" comes first in the list and would otherwise win outright).
_CODE_FIELD_EXCLUSION_PATTERN = re.compile(
    r"\bcode\b.*\b(country|dial|area|phone)\b|\b(country|dial|area|phone)\b.*\bcode\b", re.I
)
# A label mentioning any of these isn't asking about the candidate at all
# (an emergency contact, a professional reference, etc.) -- a bare "First
# Name"/"Phone" match on one of those would wrongly fill someone else's
# info with the candidate's own.
PROFILE_FIELD_DISQUALIFYING_KEYWORDS = [
    "reference", "emergency", "spouse", "supervisor", "manager", "employer",
    "coworker", "colleague",
]


def _match_profile_field_key(label: str) -> str | None:
    lowered = label.strip().lower()
    if any(word in lowered for word in PROFILE_FIELD_DISQUALIFYING_KEYWORDS):
        return None
    if _CODE_FIELD_EXCLUSION_PATTERN.search(lowered):
        return None
    for pattern, key in PROFILE_FIELD_PATTERNS:
        if pattern.search(lowered):
            return key
    return None


def _profile_field_mappings(fields: list[FormField], profile: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
    """Deterministically fill plain single-value contact/bio fields (name,
    email, phone, links, school, degree, field of study, country) straight
    from the profile -- these need only a label match, not any actual
    judgment, so there's no reason to spend an LLM call on them. Restricted
    to simple text-ish input types on purpose: a select/combobox/radio
    still needs real option matching, and a textarea's length usually
    signals it wants more than a bare profile value dropped in verbatim."""
    mappings = []
    handled_ids = set()
    for f in fields:
        if f.type not in ("text", "email", "tel", "url"):
            continue
        key = _match_profile_field_key(f.label)
        if key is None:
            continue
        value = profile.get(key)
        if not value:
            # Nothing confident to fill -- leave it in the batch so the LLM
            # still gets a chance (e.g. it might derive a value this plain
            # lookup can't), rather than silently giving up on it here.
            continue
        mappings.append({
            "field_id": f.field_id,
            "value": value,
            "needs_review": False,
            "reasoning": f"direct match from profile['{key}']",
        })
        handled_ids.add(f.field_id)
    return mappings, handled_ids


async def autofill_form(
    page: Page,
    profile: dict[str, Any],
    resume_path: str,
    cover_letter_path: str | None = None,
    fields: list[FormField] | None = None,
    log: Any = print,
    on_frame: Any = None,
) -> AutofillResult:
    if fields is None:
        fields = await extract_fields(page)
    if not fields:
        return AutofillResult(filled=[], needs_review=[], errors=[])

    ignored_ids = _ignored_field_ids(fields)
    if ignored_ids:
        fields = [f for f in fields if f.field_id not in ignored_ids]
    if not fields:
        return AutofillResult(filled=[], needs_review=[], errors=[])

    account_mappings, account_handled_ids = _account_signup_mappings(fields, profile)
    custom_mappings, custom_handled_ids = _custom_answer_mappings(fields, profile)
    consent_mappings, consent_handled_ids = _consent_checkbox_mappings(fields)
    already_handled_ids = account_handled_ids | custom_handled_ids | consent_handled_ids
    # Only offered fields the earlier layers didn't already claim, so e.g. a
    # remembered custom answer for "Email" still wins over the plain profile
    # lookup instead of the two colliding on the same field.
    profile_mappings, profile_handled_ids = _profile_field_mappings(
        [f for f in fields if f.field_id not in already_handled_ids], profile
    )
    handled_ids = already_handled_ids | profile_handled_ids
    handled_fields = [f for f in fields if f.field_id in handled_ids]
    remaining = [f for f in fields if f.field_id not in handled_ids]

    # Deterministic fields (account signup, remembered custom answers,
    # boilerplate consent checkboxes, plain profile lookups) need no LLM
    # call, so fill those in right away rather than waiting on whatever
    # comes next.
    result = await apply_mapping(
        page, handled_fields,
        account_mappings + custom_mappings + consent_mappings + profile_mappings,
        resume_path, cover_letter_path, on_frame=on_frame,
    )

    if remaining:
        domain = urlparse(page.url).netloc
        batch_result = await _map_and_apply_in_batches(
            page, remaining, profile, domain, resume_path, cover_letter_path, log, on_frame=on_frame
        )
        result = AutofillResult(
            filled=result.filled + batch_result.filled,
            needs_review=result.needs_review + batch_result.needs_review,
            errors=result.errors + batch_result.errors,
        )

    return result


async def _map_and_apply_in_batches(
    page: Page,
    fields: list[FormField],
    profile: dict[str, Any],
    domain: str,
    resume_path: str,
    cover_letter_path: str | None,
    log: Any = print,
    on_frame: Any = None,
) -> AutofillResult:
    """Map and fill one batch of fields at a time, instead of mapping the
    whole form before filling anything. If a later batch's LLM call times out
    or fails, everything already mapped and filled from earlier batches stays
    filled -- only the batch that's actually stuck loses its own fields to
    manual review, not the whole form's progress. Also means the form fills
    in progressively (visible in a live view) rather than all at once at the
    end. Caches (and can fail) per batch too, same as before, so a retry
    doesn't have to redo batches that already succeeded.

    log(), if given, replaces plain print() for progress/failure messages --
    see main.run_automation's docstring."""
    all_filled: list[str] = []
    all_needs_review: list[dict[str, Any]] = []
    all_errors: list[dict[str, Any]] = []

    batches = [fields[i:i + MAPPING_BATCH_SIZE] for i in range(0, len(fields), MAPPING_BATCH_SIZE)]
    num_batches = len(batches)
    if num_batches > 1:
        log(f"  Asking {LLM_PROVIDER} to map {len(fields)} field(s) on {domain} "
            f"({num_batches} batch(es))...")

    for batch_num, batch in enumerate(batches, start=1):
        field_hash = mapping_cache.hash_fields(batch)
        cached = mapping_cache.get(domain, field_hash)
        if cached is not None:
            batch_mappings = cached
        else:
            if num_batches > 1:
                log(f"  Batch {batch_num}/{num_batches} ({len(batch)} field(s))...")
            start = time.time()
            try:
                batch_mappings = await asyncio.wait_for(
                    asyncio.to_thread(get_mappings, batch, profile),
                    timeout=LLM_TIMEOUT_SECONDS,
                )
                log(f"  Batch {batch_num}/{num_batches} mapped in {time.time() - start:.1f}s.")
                mapping_cache.store(domain, field_hash, batch_mappings)
            except asyncio.TimeoutError:
                log(f"  Batch {batch_num}/{num_batches} timed out after {LLM_TIMEOUT_SECONDS}s "
                    f"waiting for {LLM_PROVIDER}; flagging its fields for manual review.")
                batch_mappings = [
                    {"field_id": f.field_id, "value": None, "needs_review": True,
                     "reasoning": "LLM mapping call timed out for this batch"}
                    for f in batch
                ]
            except Exception as e:
                log(f"  Batch {batch_num}/{num_batches} failed ({e}); flagging its fields for manual review.")
                batch_mappings = [
                    {"field_id": f.field_id, "value": None, "needs_review": True,
                     "reasoning": f"LLM mapping call failed or returned malformed JSON for this batch ({e})"}
                    for f in batch
                ]

        batch_result = await apply_mapping(page, batch, batch_mappings, resume_path, cover_letter_path, on_frame=on_frame)
        all_filled.extend(batch_result.filled)
        all_needs_review.extend(batch_result.needs_review)
        all_errors.extend(batch_result.errors)

    return AutofillResult(filled=all_filled, needs_review=all_needs_review, errors=all_errors)


async def find_button_by_names(page: Page, names: list[str]):
    """Return a locator for the first button/link matching any of the given
    names, or None. Skips OAuth-style "Apply with <provider>" shortcuts
    (LinkedIn, GitHub, Indeed, etc.) even if one would otherwise match
    first in DOM order -- see _is_third_party_apply_option."""
    for name in names:
        for role in ("button", "link"):
            locator = page.get_by_role(role, name=name, exact=False)
            count = await locator.count()
            for idx in range(count):
                candidate = locator.nth(idx)
                text = await candidate.inner_text()
                if not _is_third_party_apply_option(text):
                    return candidate
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
    # Also skips "Apply with LinkedIn/GitHub/etc." (see _is_third_party_apply_option) --
    # this pattern would otherwise match those just as readily as a real
    # "Apply for <Job Title>" button.
    for role in ("button", "link"):
        locator = page.get_by_role(role, name=GENERIC_APPLY_BUTTON_PATTERN)
        count = await locator.count()
        for idx in range(count):
            candidate = locator.nth(idx)
            text = await candidate.inner_text()
            if not _is_third_party_apply_option(text):
                return candidate

    if allow_bare_apply:
        for role in ("button", "link"):
            locator = page.get_by_role(role, name="Apply", exact=True)
            if await locator.count() > 0:
                return locator.first
    return None


async def _only_third_party_apply_available(page: Page) -> bool:
    """True if the only apply-style buttons/links on the page are OAuth
    shortcuts (Apply with LinkedIn/GitHub/etc.) with no manual/direct option
    at all -- there's nothing safe for this automation to click in that
    case, as opposed to find_entry_button returning None because the page
    simply has no apply button yet (e.g. the real form is already showing)."""
    found_any = False
    for role in ("button", "link"):
        for name in ENTRY_BUTTON_NAMES:
            locator = page.get_by_role(role, name=name, exact=False)
            count = await locator.count()
            for idx in range(count):
                found_any = True
                text = await locator.nth(idx).inner_text()
                if not _is_third_party_apply_option(text):
                    return False
        locator = page.get_by_role(role, name=GENERIC_APPLY_BUTTON_PATTERN)
        count = await locator.count()
        for idx in range(count):
            found_any = True
            text = await locator.nth(idx).inner_text()
            if not _is_third_party_apply_option(text):
                return False
    return found_any


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


async def _vision_find_apply_button_text(page: Page) -> str | None:
    """Last-resort fallback for when a page has no fillable fields AND
    find_entry_button's accessibility-tree search came up empty -- some ATS
    platforms render their Apply control as something Playwright's
    role-based query can't see (a styled <div>, a custom web component,
    etc.) even though it's plainly visible on screen. Asks a local vision
    model to read a screenshot directly and report the button's visible
    text; returns None (rather than raising) on any failure, since this is
    only ever a fallback on top of the normal DOM-based search, never the
    primary path. Never clicks anything itself or acts on guessed pixel
    coordinates -- the caller still has to find a real Playwright locator
    matching the reported text (see _click_by_visible_text)."""
    try:
        screenshot = await page.screenshot(type="jpeg", quality=70)
    except Exception:
        return None
    if not screenshot:
        return None

    try:
        answer = await asyncio.wait_for(
            asyncio.to_thread(_call_ollama_vision, screenshot, APPLY_VISION_PROMPT),
            timeout=VISION_TIMEOUT_SECONDS,
        )
    except Exception:
        return None

    answer = answer.strip().strip('"').strip("'")
    if not answer or answer.upper().startswith("NONE"):
        return None
    if _is_third_party_apply_option(answer):
        # Defense in depth on top of the prompt's own instruction above --
        # a local vision model isn't perfectly instruction-following, and a
        # large, colorful "Apply with LinkedIn/Indeed/Resume" button is
        # exactly the kind of thing it'll misidentify as *the* apply button,
        # especially when the real form fields are smaller or out of frame.
        return None
    return answer


async def _click_by_visible_text(page: Page, text: str, timeout: int = 5000) -> bool:
    """Click the first visible element matching this text -- broader than
    find_entry_button's strict name-list/pattern matching, since this is
    only ever used for text a vision model already confirmed is a real,
    on-screen Apply-style control (see _vision_find_apply_button_text), not
    for guessing at arbitrary page text."""
    for role in ("button", "link"):
        locator = page.get_by_role(role, name=text, exact=False)
        if await locator.count() > 0:
            try:
                await locator.first.click(timeout=timeout)
                return True
            except Exception:
                pass
    locator = page.get_by_text(text, exact=False)
    if await locator.count() > 0:
        try:
            await locator.first.click(timeout=timeout)
            return True
        except Exception:
            pass
    return False


async def _peek_combobox_options(page: Page, selector: str) -> list[str]:
    """Open a combobox and read whatever options are already rendered,
    without typing or selecting anything -- a starting list to show a human
    reviewer instead of a blank text box. Search-driven widgets (city,
    school, country) render nothing until you type, so an empty result here
    is expected and not itself a problem; short static lists (EEO-style
    Yes/No/Decline questions) render everything immediately and this picks
    those up. Best-effort -- any failure here just means no options shown,
    never a reason to fail the review prompt itself.

    Presses Escape afterward to close the widget back up -- select_combobox_
    option() does its own click-to-open when the answer actually comes in,
    and leaving this one open first could make that second click toggle it
    closed instead, on widgets where the control is a click-to-toggle button
    rather than a plain focus-to-open input."""
    try:
        await page.locator(selector).click(timeout=3000)
        everything = page.get_by_role("option")
        try:
            await everything.first.wait_for(timeout=1500)
        except Exception:
            pass
        texts = await everything.all_inner_texts()
    except Exception:
        return []
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    return texts


async def _resolve_needs_review_interactively(
    page: Page, profile: dict[str, Any], needs_review: list[dict[str, Any]],
    ask_fn: Any = None,
    on_frame: Any = None,
) -> list[dict[str, Any]]:
    """Ask the user for anything Claude couldn't confidently answer, right while
    those fields are still live on the page, fill in whatever they provide, and
    remember it in profile['custom_answers'] so the same question on a future
    application is answered automatically instead of asked again.

    ask_fn, if given, is a synchronous callable(item_dict) -> str used instead
    of the terminal print()/input() below -- e.g. a GUI-backed prompt that
    blocks the calling thread until the user answers. It receives the same
    item dict (label/reasoning/options/type/...) so the caller can render it
    however it likes; an empty/blank return means "skip", same as pressing
    Enter at the terminal prompt.

    on_frame, if given, is a synchronous callable(jpeg_bytes) called with a
    fresh screenshot right before each item is presented -- e.g. to update a
    live view of the page the user is being asked about."""
    still_needs_review = []
    custom_answers = profile.setdefault("custom_answers", {})

    for item in needs_review:
        label = item["label"]
        if item.get("type") == "combobox" and item.get("selector") and not item.get("options"):
            item["options"] = await _peek_combobox_options(page, item["selector"])
        if on_frame is not None:
            frame_bytes = await take_frame_screenshot(page)
            if frame_bytes is not None:
                on_frame(frame_bytes)
        if ask_fn is not None:
            answer = (ask_fn(item) or "").strip()
        else:
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


async def take_frame_screenshot(page: Page) -> bytes | None:
    """Full-page JPEG screenshot for a live view, with a safe fallback.
    full_page=True can fail on very long/lazy-loaded forms (the browser has
    a max capturable canvas height) -- and not always by raising: on an
    extreme-height page it's been observed to "succeed" with a 0-byte
    result instead of throwing, so an empty result is treated as a failure
    too, not just an exception. A capture against certain page states (seen
    on Chromium's own internal "New Tab" page) has also been observed to
    stall outright rather than fail cleanly -- worse than a normal failure,
    since an un-timed-out await can sit there indefinitely and, in that
    specific case, was even observed blocking the tab's own subsequent
    navigation. An explicit timeout turns that into an ordinary handled
    failure. None of this module's on_frame call sites are wrapped in error
    handling upstream, so letting either failure mode propagate would
    silently kill the whole automation run right after a successful fill,
    not just skip one frame update. Fall back to a viewport-only screenshot,
    and give up quietly (None) only if even that comes back empty or fails,
    rather than ever taking the run down over a screenshot."""
    try:
        frame = await page.screenshot(type="jpeg", quality=60, full_page=True, timeout=5000)
        if frame:
            return frame
    except Exception:
        pass
    try:
        frame = await page.screenshot(type="jpeg", quality=60, timeout=5000)
        return frame or None
    except Exception:
        return None


async def _capture_frame(
    page: Page, screenshot_dir: str | None, screenshot_prefix: str, step: int, suffix: str,
    on_frame: Any,
) -> None:
    """Take one screenshot and route it to whichever of disk / on_frame are
    wanted, instead of capturing twice for the same checkpoint."""
    if not screenshot_dir and on_frame is None:
        return
    frame_bytes = await take_frame_screenshot(page)
    if frame_bytes is None:
        return
    if screenshot_dir:
        with open(f"{screenshot_dir}/{screenshot_prefix}_step{step}_{suffix}.jpg", "wb") as f:
            f.write(frame_bytes)
    if on_frame is not None:
        on_frame(frame_bytes)


async def _follow_new_tab_if_opened(page: Page, pages_before: set) -> Page:
    """After a click that might have opened the real next step in a new tab
    instead of navigating page in place (seen on some Greenhouse listings'
    Apply button) -- return that new tab instead, or page itself unchanged
    if nothing new appeared. Closes any other extras that opened alongside
    it too (e.g. an ad/tracking redirect tab), same as main.run_automation
    already does for its own "Original Job Post" click."""
    new_pages = [p for p in page.context.pages if p not in pages_before]
    if not new_pages:
        return page
    target = new_pages[0]
    for extra in new_pages[1:]:
        try:
            await extra.close()
        except Exception:
            pass
    try:
        await target.wait_for_load_state()
    except Exception:
        pass
    return target


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
    ask_fn: Any = None,
    on_frame: Any = None,
    log: Any = print,
) -> AutofillResult:
    """Fill a (possibly multi-step) application form, advancing through
    Next/Continue steps up to max_steps. Also handles landing pages with no
    form yet by clicking an initial Apply/Start Application button. Never
    clicks a final Submit — that's left for a human to do after reviewing
    the filled form. If interactive, prompts for anything Claude couldn't
    confidently answer and remembers the answer in profile['custom_answers'].

    on_needs_review, if given, is called with the list of needs_review items
    right before blocking on the interactive prompts -- e.g. to fire a
    notification the moment the AI hands off to a human, since the prompts
    themselves block until someone is actually there to answer them.

    ask_fn, if given, is passed through to _resolve_needs_review_interactively
    in place of the terminal input() prompt -- see its docstring.

    on_frame, if given, is a synchronous callable(jpeg_bytes) fed a screenshot
    at each point one's already being taken (before/after each step, before
    each per-field prompt) -- e.g. to drive a live view of the page.

    log, if given, replaces plain print() for mapping progress/failure
    messages, so a GUI backed by a log() callback (e.g. webapp.py) actually
    sees why a batch failed instead of it only landing in the terminal.

    If a step's fields look like a login gate for an EXISTING account (see
    _looks_like_login_gate), or the only apply entry point on the page is a
    third-party OAuth shortcut (Apply with LinkedIn/GitHub/etc., see
    _only_third_party_apply_available) with no manual option, stops
    immediately without attempting anything on that step -- there's no
    password in the profile that could possibly be right for an account
    this automation never registered, and it should never complete a
    third-party login on your behalf -- and returns with
    login_required=True so the caller can pause for a human instead of
    guessing."""
    original_page = page
    all_filled: list[str] = []
    all_needs_review: list[dict[str, Any]] = []
    all_errors: list[dict[str, Any]] = []
    login_required = False

    await dismiss_cookie_banner(page)

    for step in range(max_steps):
        # Fields are extracted first (not gated behind the entry-button check):
        # many career sites keep a persistent header (search box, language
        # picker, etc.) that shows up as "fields" even on the pre-application
        # landing page, so we still need to check for an entry button even when
        # fields are already present. But a bare "Apply" match (and the vision
        # fallback below) are only safe to try when there are truly zero REAL
        # fields yet -- seeing any already means clicking a bare "Apply" risks
        # hitting an unrelated shortcut instead of just filling what's already
        # there (see find_entry_button). "Real" excludes fields extract_fields
        # couldn't label at all ("(unlabeled)") -- almost always the same kind
        # of page chrome noise, not anything actually answerable, and letting
        # its mere presence block the entry-button/vision fallback wastes an
        # LLM call mapping nothing useful and surfaces a blank, labelless
        # review prompt instead of ever finding the real Apply button.
        fields = await extract_fields(page)
        real_fields = [f for f in fields if f.label != "(unlabeled)"]

        if await _looks_like_login_gate(page, fields):
            login_required = True
            break

        entry_button = await find_entry_button(page, allow_bare_apply=not real_fields)
        if entry_button is not None:
            pages_before = set(page.context.pages)
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
            new_page = await _follow_new_tab_if_opened(page, pages_before)
            if new_page is not page:
                log("  Apply opened the real application in a new tab -- following it.")
                page = new_page
            await _settle(page)
            continue

        if not real_fields:
            if await _only_third_party_apply_available(page):
                login_required = True
                break
            # Only worth the extra latency of a vision-model screenshot check
            # on the very first step -- by later steps we're already past
            # whatever entry gate this page has, and an empty step there
            # legitimately just means the form is done (e.g. only a Submit
            # button left, which this automation never clicks anyway).
            if step == 0:
                vision_button_text = await _vision_find_apply_button_text(page)
                if vision_button_text is not None:
                    log(f"  No Apply button found via the normal page inspection, but a "
                        f"vision check spotted something labeled \"{vision_button_text}\" -- trying that.")
                    if await _click_by_visible_text(page, vision_button_text):
                        await _settle(page)
                        continue
                    log(f"  Couldn't click \"{vision_button_text}\" after all.")
            break

        await _capture_frame(page, screenshot_dir, screenshot_prefix, step, "before", on_frame)

        result = await autofill_form(page, profile, resume_path, cover_letter_path, fields=fields, log=log, on_frame=on_frame)
        remaining_needs_review = result.needs_review
        if remaining_needs_review and on_needs_review is not None:
            on_needs_review(remaining_needs_review)
        if interactive and remaining_needs_review:
            remaining_needs_review = await _resolve_needs_review_interactively(
                page, profile, remaining_needs_review, ask_fn=ask_fn, on_frame=on_frame
            )

        all_filled.extend(result.filled)
        all_needs_review.extend(remaining_needs_review)
        all_errors.extend(result.errors)

        await _capture_frame(page, screenshot_dir, screenshot_prefix, step, "after", on_frame)

        next_button = await find_next_button(page)
        if next_button is None:
            break
        pages_before = set(page.context.pages)
        try:
            await next_button.click(timeout=5000)
        except Exception:
            break
        new_page = await _follow_new_tab_if_opened(page, pages_before)
        if new_page is not page:
            log("  Continuing opened the next step in a new tab -- following it.")
            page = new_page
        await _settle(page)

    # An all-empty result (nothing filled, nothing flagged, no errors) reads
    # as unqualified success to a caller -- correct when a form was already
    # fully filled by an earlier step and the last step just has a Submit
    # button left, but misleading if this loop actually never managed to
    # fill or even find anything at all (e.g. an Apply button that couldn't
    # be found or clicked, even after the vision fallback above). Only flag
    # it when nothing whatsoever was accomplished across the whole call --
    # a login_required result already explains itself to the caller.
    if not login_required and not (all_filled or all_needs_review or all_errors):
        all_errors.append({
            "label": "(page)",
            "error": "No form fields or Apply-style button could be found on this page -- "
                     "check the browser window.",
        })

    return AutofillResult(
        filled=all_filled, needs_review=all_needs_review, errors=all_errors, login_required=login_required,
        final_page=page if page is not original_page else None,
    )


async def _settle(page: Page) -> None:
    """Wait for a step transition to render without hanging on sites that never
    go fully network-idle (analytics beacons, chat widgets, etc.)."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(1200)
