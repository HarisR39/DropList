# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

DropList signs into jobright.ai, lets you pick a listing at a time, opens the real company application (Greenhouse, Lever, Ashby, SmartRecruiters, Rippling, Gusto, and others), and uses an LLM to fill the form from `profile.json`. It never submits anything automatically. See `README.md` for the full behavior description, env var reference, and setup steps — this file is about how the code is put together, not what it does for the user.

## Commands

```bash
pip install -r requirements.txt
playwright install chromium

pytest                              # full suite (166 tests)
pytest main_autofill.py             # autofill.py's suite only
pytest test_workday.py              # workday.py's suite only
pytest test_webapp.py               # webapp.py's suite only
pytest test_main.py                 # main.py's pure-logic suite only
pytest main_autofill.py -k some_name -v   # a single test

python main.py       # plain terminal run
python webapp.py     # web GUI at http://127.0.0.1:8765/
```

No lint/build step — this is a plain script + a small FastAPI app, no bundler or type checker configured.

`JOBRIGHT_EMAIL`/`JOBRIGHT_PASSWORD` must be set to run `main.py`/`webapp.py` for real (`conftest.py` stubs `ANTHROPIC_API_KEY` for tests, but nothing stubs Playwright/jobright — tests never launch a real browser). `profile.json` (gitignored) must exist too; copy `profile.example.json` first.

## Architecture

### The three-way split: `main.py` vs `autofill.py` vs `webapp.py`

- **`autofill.py`** knows nothing about jobright.ai or the review/retry flow. Given a Playwright `page` already sitting on *some* application form, `autofill_form_multistep` extracts fields, maps them to values, fills them, and advances through Next/Continue steps. It is the reusable engine — it would work identically if handed a page reached by any means.
- **`main.py`** owns the jobright.ai-specific flow: login, letting you pick a listing (or "apply to current page" to skip jobright's listing pages entirely), opening the company tab, and the pause/review/retry loop around each call into `autofill_form_multistep`. `run_automation` is the single entry point both `main()` and `webapp.py` call.
- **`webapp.py`** doesn't reimplement any of this — `AutomationBridge` runs `run_automation` in a background thread with its own asyncio event loop (Playwright needs `ProactorEventLoop` on Windows) and bridges its callback hooks (`confirm_fn`, `ask_fn`, `on_frame`, `should_reset`) to WebSocket clients over a thread-safe `queue.Queue`. The worker thread never touches FastAPI/Starlette state directly; a single long-lived `broadcaster_loop` (started once via the app's `lifespan`) drains the queue and fans events out, keeping a small in-memory snapshot (`state_snapshot()`) so a page refresh mid-run doesn't lose anything.

### The mapping pipeline: deterministic layers before the LLM ever runs

`autofill_form` doesn't just hand every field to the LLM. Fields are peeled off in order by cheap, deterministic matchers before whatever's left goes to `get_mappings`:

1. `_ignored_field_ids` — silently dropped, never filled or flagged (Address Line 2, phone extension). Applied first, before any of the below even see these fields.
2. `_account_signup_mappings`, `_custom_answer_mappings`, `_consent_checkbox_mappings` — password/email on a signup gate, a previously-typed answer reused for the same label (`profile['custom_answers']`, persisted back to `profile.json` after every attempt), and boilerplate terms/privacy checkboxes, respectively. These three run independently (union of whatever each claims, not a precedence chain over each other).
3. `_profile_field_mappings` — runs last, only on whatever the above didn't claim, so e.g. a remembered custom answer for "Email" still wins over the plain profile lookup. Matches plain text-ish fields by label regex (`PROFILE_FIELD_PATTERNS`) straight to a profile key (name, email, phone, school, state, ...). Only fires for `text`/`email`/`tel`/`url` types on purpose — anything needing real judgment (select options, EEO/eligibility yes-no, open-ended essays) is deliberately excluded and left to the LLM.

Only fields none of these claimed go through `_map_and_apply_in_batches` (LLM), batched by `MAPPING_BATCH_SIZE` and cached in `mapping_cache.json` keyed by `(domain, field-set hash)`. When adding a new "always answer this the same way" rule, it almost always belongs in one of these deterministic layers, not as a `SYSTEM_PROMPT` instruction — cheaper, faster, and doesn't depend on the local model's judgment.

### Finding the Apply/Next button: three fallbacks in order

`find_entry_button` tries an exact name list, then a `^apply\b.+` pattern for dynamic titles ("Apply for Software Engineer"), then (only when the caller says the page truly has zero real fields yet) a bare "Apply". `_is_third_party_apply_option` filters out LinkedIn/Indeed/GitHub/resume-import-style shortcuts from the first two steps — the automation only ever drives the site's own manual form, never an OAuth/third-party shortcut. If none of that finds anything and there are still no fields, `_vision_find_apply_button_text` is a last-resort fallback that screenshots the page and asks a local vision model (`OLLAMA_VISION_MODEL`) to read the button's text off the screenshot, then clicks by visible text — used sparingly (step 0 only) since it's the slowest path.

`"(unlabeled)"` fields (chrome that `extract_fields` couldn't put a real label on — nav search boxes, language pickers) are filtered out of the "are there real fields on this page" check specifically so their mere presence doesn't block the entry-button/vision fallback from running.

### Control flow in `run_automation`: exceptions as control signals

`_pick_target_page` (choose a listing) and `_fill_review_retry_loop` (fill → review → retry) are the two pieces `run_automation`'s outer loop calls each iteration. `retry`/`reset`/`stop` answers at any `confirm_or_reset()` pause don't get threaded back up as return values — `_ResetRequested`/`_StopRequested` are raised and caught in the outer loop's own `try`/`except`, which is what lets a "reset" typed three calls deep (inside the "did you apply?" fallback, say) unwind cleanly back to the top without every intermediate frame needing to know about it.

`should_reset()` exists because a GUI's Reset button can fire while the worker thread is mid-`autofill_form_multistep` call with no `confirm_or_reset()` pause currently showing to answer directly — `confirm_or_reset` checks it before every pause and treats a `True` exactly like typing `'reset'` would.

An Apply/Next click can open the real next step in a *new tab* instead of navigating in place (seen on Greenhouse). `autofill_form_multistep` follows it internally and reports it back via `AutofillResult.final_page` — callers that track the page across calls (`company_page`, `known_pages`) must pick this up or they keep operating on the abandoned original tab.

### The live view has three independent update triggers, not a video stream

There's no embedded browser in the page — screenshots get pushed at specific moments: after each field actually gets filled (`apply_mapping`), on a periodic timer (`_live_view_loop`, `LIVE_VIEW_INTERVAL_SECONDS`) as a backstop, and that's it. There is deliberately **no** reactive screenshot on tab-creation or on-navigation — both were tried and both were found to stall a tab's own CDP target hard enough to block whatever you typed into it next (a real, reproduced bug, not a hypothetical one). If you're tempted to add a screenshot trigger tied to a page-lifecycle event, read `_redirect_new_tabs_away_from_ntp`'s docstring first.

### Browser launch specifics that look arbitrary but aren't

`p.chromium.launch(channel="chrome", ...)` drives the real installed Google Chrome, not Playwright's bundled Chromium — some ATS sites behave differently under bot detection for an obviously-automated browser fingerprint. `--disable-blink-features=AutomationControlled` plus a `navigator.webdriver` override init script are the same fight. `_redirect_new_tabs_away_from_ntp` immediately navigates any tab you open by hand to `about:blank`, racing ahead of Chrome's real New Tab Page (which fetches a Discover feed from Google and was observed hanging under this automation's sync-free profile). None of this is cargo-culted — each exists because of a specific, reproduced failure documented in the comment next to it.

### `workday.py`: a per-ATS deterministic layer, because Workday is the one ATS where that's actually safe

Every other ATS this project handles (Greenhouse, Lever, Ashby, ...) has field wording that genuinely varies employer to employer, which is why `autofill.py`'s deterministic layers stick to generic, low-judgment patterns (a plain "First Name" text field) and leave anything else to the LLM. Workday is different: every `myworkdayjobs.com` site is the same underlying product re-themed per company, so its "My Information" labels, Voluntary Disclosures (EEO) wording, and Self-Identify disability form (the federal OFCCP CC-305 form, used verbatim) are stable enough to hardcode with real confidence — see `workday.py`'s own module docstring. `autofill_form` calls `workday.workday_field_mappings` (gated on `workday.is_workday_domain(page.url)`) after its own generic layers, on whatever fields those left unclaimed, so a Workday application only ever reaches the LLM for a genuinely employer-custom question. `autofill_form_multistep` separately calls `workday.fill_experience_sections` every step (a no-op except on whichever step actually has it) to drive Workday's repeating "Add" Work Experience/Education panels from `profile.json`'s `previous_employers`/`education` — that one can't be expressed as a flat field→value map like the rest, since it has to click "Add" and discover each new panel's fields itself, so it operates on the page directly instead of going through `apply_mapping`.

If a future company/ATS turns out to have this same stability (a similarly walled-garden platform, not a bespoke build), the same pattern — a dedicated module gated behind a domain check, feeding into `apply_mapping` for flat fields and calling the page directly for anything structurally repeating — is the template to follow, not a new branch inside `autofill.py` itself.

### Job-board vs. company-site distinction

`JOB_BOARD_DOMAINS`/`_is_job_board_domain` (in `main.py`) is a *domain*-based check done before ever clicking anything on the company page — a listing's "Original Job Post" link sometimes leads to a repost on Indeed/LinkedIn/etc. rather than the company's own site, and those need their own account login that this automation won't attempt. This is separate from `THIRD_PARTY_APPLY_KEYWORDS`/`_is_third_party_apply_option` (in `autofill.py`), which is a *button-text* check for OAuth-style "Apply with LinkedIn/GitHub" shortcuts sitting on an otherwise-legitimate company page. Both exist because they catch different things — collapsing them has been tried implicitly and doesn't work.
