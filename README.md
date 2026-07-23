# DropList

A personal automation tool that logs into [jobright.ai](https://jobright.ai), walks through its recommended internship/job listings, opens each one's real application page, and uses an LLM to fill out the form from a structured candidate profile. It **never submits anything automatically** — every application is paused for you to review and submit yourself.

## What it actually does

1. Signs into jobright.ai and opens its "Recommended" jobs feed.
2. For a configurable number of listings, opens the job detail page, clicks through jobright's own apply flow, and lands on the real company application page (Greenhouse, Lever, Ashby, SmartRecruiters, Rippling, Gusto, and others) in a new tab. This step alone handles a pile of real-world inconsistency: a full-screen onboarding-tour overlay that can intercept clicks, a "did you apply?" popup that shows up when you switch back to the jobright tab, and an apply flow that sometimes shows a "customize your resume" modal first and sometimes opens the company tab directly with no modal at all.
3. If the company page is just a landing page with no form yet, finds and clicks through the real "Apply"-style button — including ones with dynamic text like "Apply for Software Engineering Intern" that can't be matched by a fixed list, while still avoiding false positives like a "Quick Apply" shortcut or a bare "Apply" pill sitting next to a form that's already loaded.
4. Extracts every field on the real form via the accessibility tree — including messy real-world cases most naive scrapers miss:
   - Native `<select>` dropdowns vs. custom JS-driven comboboxes (react-select-style widgets)
   - Radio buttons and checkboxes grouped by their real shared question, not treated as isolated fields
   - "Yes/No" toggle widgets built from plain buttons with no real form control behind them
   - Hidden/invisible junk (reCAPTCHA fields, shadow validation inputs, submit buttons) filtered out
5. Sends the field set + your profile to an LLM (local via [Ollama](https://ollama.com) by default, or Anthropic's Claude API) to map each field to a value, batching large forms so the model doesn't choke on 50+ fields at once.
6. Fills the form via Playwright, with safety nets at every step: dropdown values are validated against the real options (falling back to a catch-all "Other"/"Not Listed" option when one exists), and anything the model can't confidently answer — or that doesn't match any real option — is flagged instead of guessed.
7. Anything flagged gets asked about interactively in the terminal, filled in live, and remembered (`profile.json`'s `custom_answers`) so the same question on a future application is answered automatically. A push notification (see below) fires the moment the AI hands off, since those prompts block until you're actually there to answer them.
8. Pauses for you to review the filled form and submit it yourself. Nothing is ever auto-submitted.

## Notifications

If `NTFY_TOPIC` is set, [ntfy.sh](https://ntfy.sh) push notifications fire at the natural checkpoints of a run, so you don't have to babysit the terminal:
- The moment a field needs your input (before it blocks on the interactive prompt)
- When a listing finishes: filled and ready to submit, filled but flagged for review, or failed outright

Install the ntfy app, subscribe to a topic of your choosing (pick something random/hard-to-guess — topics are public by default), and set `NTFY_TOPIC` to that value.

## Explicit safety rules baked into the mapping

- Never invents facts, dates, employers, or numbers not present in the profile.
- Refuses to guess on legally-protected EEO questions (gender, race/ethnicity, disability, veteran status) unless the profile explicitly states them — never infers from a name or any other proxy.
- Refuses to guess on eligibility questions (work authorization, sponsorship, relocation) without an explicit profile answer.

## Setup

Requires Python 3.12+ and [Ollama](https://ollama.com) (for local, free LLM inference) or an Anthropic API key.

```bash
pip install -r requirements.txt
playwright install chromium
ollama pull llama3.1:8b   # if using the default local provider
```

Copy `profile.example.json` to `profile.json` and fill in your real information — this file is gitignored and never committed.

Set the following environment variables (e.g. via `setx` on Windows, so they persist across terminal sessions):

| Variable | Required | Purpose |
|---|---|---|
| `JOBRIGHT_EMAIL` / `JOBRIGHT_PASSWORD` | Yes | Your jobright.ai login |
| `ANTHROPIC_API_KEY` | Only if using Claude | Needed if `AUTOFILL_LLM_PROVIDER=anthropic` |
| `NTFY_TOPIC` | No | Enables push notifications (see below) |
| `AUTOFILL_LLM_PROVIDER` | No | `ollama` (default) or `anthropic` |
| `OLLAMA_MODEL` | No | Default `llama3.1:8b` |
| `AUTOFILL_LLM_TIMEOUT` | No | Seconds before a mapping call gives up (default 150) |
| `AUTOFILL_MAPPING_BATCH_SIZE` | No | Fields per LLM call (default 12) |

On Windows, `setx` only takes effect in terminals/processes started *after* it runs — restart your terminal (or fully quit and reopen VS Code, since its integrated terminal inherits the editor's own environment) before running the script.

## Running it

```bash
python main.py
```

Adjust `NUM_LISTINGS_TO_REVIEW` and `MAX_FORM_STEPS` at the top of `main.py` to control how many listings to process per run and how many steps a multi-step form is allowed to take.

## Testing

```bash
pytest
```

52 tests covering field extraction, dropdown/combobox/radio-group/checkbox-group filling, the "Other" fallback, dynamic/bare "Apply" button detection, mapping cache, batching and per-batch failure isolation, and the interactive review flow (including the notification callback).

## Project layout

- `main.py` — the runnable script: login, navigation, review loop.
- `autofill.py` — the actual engine: field extraction, LLM mapping, form filling, multi-step handling.
- `mapping_cache.py` — local JSON cache of LLM field mappings, keyed by domain + field-set hash.
- `application_tracking.py` — local JSON record of what's been applied to and what still needs review.
- `profile.json` (gitignored) / `profile.example.json` (template) — candidate profile data.
- `main_autofill.py` — the pytest suite for `autofill.py`.

## Tech stack

Python (asyncio), Playwright, Ollama, Anthropic API, pytest/pytest-asyncio. No database — local JSON files for caching and tracking state.

## A few honest caveats

- This automates a third-party site's own UI (jobright.ai) and a wide variety of real company application forms. Selectors and quirks are handled defensively, but ATS platforms change their markup over time and new patterns will surface.
- Local LLMs (the default) are meaningfully less reliable than a frontier hosted model at strict JSON output and judgment calls on ambiguous fields — expect to answer more things interactively than you would with Claude.
- This is a personal-use tool, not a polished product. It's meant to save you from re-typing the same information into every application form, not to submit applications without your involvement.
