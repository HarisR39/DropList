import asyncio
import json
import os
import shutil
import urllib.request
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.async_api import async_playwright

import application_tracking
from autofill import AutofillResult, autofill_form_multistep, take_frame_screenshot

MAX_FORM_STEPS = 6
SCREENSHOT_DIR = "screenshots"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
# How often the live view refreshes on its own, independent of the existing
# action-triggered captures (before/after each fill, before each pause) --
# those only fire around specific actions, so anything that changes the page
# in between (a slow page load, a redirect, an animation) previously just
# sat stale in the live view until the next checkpoint.
LIVE_VIEW_INTERVAL_SECONDS = float(os.environ.get("LIVE_VIEW_INTERVAL_SECONDS", "1.5"))

# "Original Job Post" sometimes leads to the listing's original repost on a
# job board rather than the company's own site (see run_automation's
# docstring) -- these all require their own account/login to apply through
# (Indeed's own "Apply now" doesn't mention "Indeed" in its own text once
# you're already on indeed.com, so autofill.py's third-party-button text
# filter can't catch it there). Checked by domain instead, before this
# automation ever tries clicking anything on the page.
JOB_BOARD_DOMAINS = ["indeed.com", "linkedin.com", "glassdoor.com", "ziprecruiter.com", "monster.com"]


def _is_job_board_domain(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return any(netloc == domain or netloc.endswith("." + domain) for domain in JOB_BOARD_DOMAINS)


async def _dismiss_exit_button(page) -> None:
    """Close jobright's own "EXIT" control if it's showing (a modal/tour
    dismiss button, distinct from the reactour overlay's own Escape-key
    handling below it) -- checked at a couple of points during the listing
    hand-off since it can appear more than once in that sequence."""
    exit_button = page.get_by_text("EXIT", exact=True)
    if await exit_button.count() > 0:
        await exit_button.first.click()


class _ResetRequested(Exception):
    """Raised internally (see confirm_or_reset) to unwind out of however
    deeply nested the current listing's retry/did-you-apply loops are and
    land back at the top of the job-picking loop, without tearing down the
    browser/session the way stopping and restarting the whole run would."""


class _StopRequested(Exception):
    """Raised by _pick_target_page when you're done for the session --
    unwinds the same way _ResetRequested does, straight to run_automation's
    own job-picking loop, which ends the run instead of looping again."""


def load_profile() -> dict:
    with open("profile.json", "r", encoding="utf-8") as f:
        return json.load(f)


def save_profile(profile: dict) -> None:
    with open("profile.json", "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)


def _find_login_popup(company_page, known_pages: set):
    """Any page in this browser context that isn't one this automation
    already knows about -- e.g. an OAuth "Continue with Google/LinkedIn"
    flow opening its own window. Best-effort: there's no reliable way to
    positively identify a login popup by URL/title across arbitrary
    providers, so any unexpected extra page is treated as one.

    known_pages must cover jobright's own tab AND every company tab ever
    opened THIS RUN, not just the current listing's -- earlier listings'
    company tabs are deliberately left open (so you can still review/submit
    them later), and checking against only the current pair used to
    misidentify every one of them as a login popup on every later listing."""
    extras = [p for p in company_page.context.pages if p not in known_pages]
    return extras[0] if extras else None


def notify(message: str) -> None:
    """Push a phone notification via ntfy.sh (https://ntfy.sh/<topic>) if
    NTFY_TOPIC is set. Best-effort -- a failed notification shouldn't crash
    the run, just gets logged."""
    if not NTFY_TOPIC:
        print("  (notification skipped: NTFY_TOPIC environment variable is not set)")
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=5)
        print(f"  (notification sent to ntfy.sh/{NTFY_TOPIC}, status {resp.status})")
    except Exception as e:
        print(f"  (notification failed: {e})")


async def _live_view_loop(current_page: dict, on_frame: Callable[[bytes], None]) -> None:
    """Refreshes the live view on its own timer, independent of the
    action-triggered captures sprinkled through the rest of this module and
    autofill.py -- those only fire around specific actions (before/after a
    fill, before a pause), so anything that changes the page in between (a
    slow load, a redirect, an animation) would otherwise just sit stale
    until the next one. current_page is a single-key {"page": ...} dict
    (rather than a plain variable) so the caller can repoint it at whichever
    page is currently relevant -- jobright's own tab or the open company
    tab -- and this loop always picks that up on its next tick. Runs until
    cancelled; a screenshot failure (mid-navigation, page just closed, etc.)
    just skips that one tick instead of ending the loop."""
    while True:
        await asyncio.sleep(LIVE_VIEW_INTERVAL_SECONDS)
        page = current_page.get("page")
        if page is None:
            continue
        try:
            frame_bytes = await take_frame_screenshot(page)
        except Exception:
            continue
        if frame_bytes is not None:
            on_frame(frame_bytes)


def _redirect_new_tabs_away_from_ntp(context) -> None:
    """Immediately navigate any tab opened in this browser context to
    about:blank, racing in before Chrome's own real New Tab Page
    (chrome://newtab/) gets a chance to do anything.

    Only a tab YOU open by hand (Ctrl+T, the + button) ever lands on that
    real NTP in the first place -- Playwright's own new_page() bypasses it
    entirely and starts on about:blank already, confirmed directly: calling
    it produces a page whose .url is "about:blank", never
    "chrome://newtab/". Chrome's real NTP fetches a Discover feed and
    suggested-sites content from Google's own servers, and under this
    automation's fresh, sync-free profile that fetch has been observed to
    hang indefinitely -- which then seems to leave that tab's renderer
    unable to complete ANY navigation you make in it afterward (matches
    exactly what was reported: reusing an already-loaded tab like
    jobright's own works fine; a freshly hand-opened one doesn't). Skipping
    the NTP's own network activity entirely, by never actually letting it
    run, sidesteps the problem rather than trying to debug it -- there's no
    way to script/observe the real NTP the way a human's Ctrl+T actually
    triggers it, so this couldn't be verified more directly than that."""
    async def _on_new_page(new_page) -> None:
        try:
            await new_page.goto("about:blank", timeout=3000)
        except Exception:
            pass

    context.on("page", _on_new_page)


async def _pick_target_page(page, known_pages: set, i: int, confirm_or_reset, log):
    """Show the "browse listings" pause and resolve it into a page to
    autofill. Returns (company_page, job_id, applying_current_page, i) once
    there's something to work with, or None if the pause should just be
    shown again (e.g. you haven't actually clicked into a jobright listing
    yet). Raises _StopRequested/_ResetRequested for those two responses --
    the caller's job-picking loop is expected to end the run on the former
    and handle the latter exactly like it already does everywhere else.

    company_page is page itself (not a separate tab) when you typed 'apply
    current' -- see run_automation's docstring -- unless you opened a new
    tab to browse there instead of navigating page in place, in which case
    it's the most recently opened tab this run doesn't already know about
    (page would otherwise still show jobright's own URL, having never
    moved)."""
    answer = (confirm_or_reset(
        "Logged into jobright.ai. Browse the recommended listings yourself "
        "and click into whichever one you'd like to apply to, through to "
        "its job detail page, then press Enter here (or click Continue) "
        "and this will take over from there -- click Apply, autofill the "
        "form, and pause for your review. Or navigate anywhere yourself -- a "
        "company's application page you found outside jobright -- and type "
        "'apply current' and press Enter (or use Apply to Current Page) to "
        "autofill whatever's open right now, skipping jobright's listing pages "
        "entirely. Type 'stop' and press Enter (or use Stop) to end this "
        "session whenever you're done applying.",
        allow_apply_current=True,
    ) or "").strip().lower()
    if answer == "stop":
        raise _StopRequested()
    if answer == "reset":
        raise _ResetRequested()

    if answer in ("apply current", "apply_current"):
        i += 1
        job_id = f"manual-{i}"
        unknown_pages = [p for p in page.context.pages if p not in known_pages]
        company_page = unknown_pages[-1] if unknown_pages else page
        log(f"[{i}] Applying to whatever's currently open: {company_page.url}")
        return company_page, job_id, True, i

    if "/jobs/info/" not in page.url:
        # jobright's navigation after clicking a listing isn't instant --
        # checking page.url the moment you click Continue can catch it
        # mid-navigation and wrongly report "not on a job page yet" even
        # though you did click one. Give it a couple seconds to actually
        # land before giving up.
        try:
            await page.wait_for_url("**/jobs/info/**", timeout=3000)
        except Exception:
            pass

    if "/jobs/info/" not in page.url:
        log(f"Doesn't look like you're on a job listing page yet (current "
            f"URL: {page.url}) -- click into a listing, then continue again.")
        return None

    i += 1
    job_id = page.url.rsplit("/jobs/info/", 1)[-1].split("?")[0]

    await _dismiss_exit_button(page)

    # A full-screen onboarding-tour overlay (jobright's reactour-based
    # walkthrough) can appear here and intercept every click for 30s
    # until Playwright gives up. Only press Escape if it's actually
    # present -- on this page Escape is also jobright's own shortcut
    # to close the job detail view entirely, which would remove the
    # apply button from the DOM before we ever click it.
    if await page.query_selector("#___reactour") is not None:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)

    # "Original Job Post" links out to the job's original source posting
    # (the company's own careers page) in a new tab -- unlike "Apply with
    # Autofill", it never shows jobright's own "Customize Your Resume"
    # modal.
    pages_before = set(page.context.pages)
    try:
        async with page.context.expect_page(timeout=15000) as new_page_info:
            await page.get_by_text("Original Job Post", exact=True).click()
        company_page = await new_page_info.value
    except Exception:
        company_page = None
    await page.wait_for_timeout(800)

    await _dismiss_exit_button(page)

    if company_page is not None:
        # A single click can occasionally open more than one new tab (e.g.
        # an ad/tracking redirect alongside the real destination) -- close
        # the extras immediately so they don't clutter the browser or get
        # mistaken for a genuine login popup later (see _find_login_popup).
        for extra in page.context.pages:
            if extra not in pages_before and extra is not company_page:
                try:
                    await extra.close()
                except Exception:
                    pass
        await company_page.wait_for_load_state()
        log(f"[{i}] Company page opened: {company_page.url}")
    else:
        log(f"[{i}] No resume-customize popup or new tab appeared; "
            f"check the browser window.")

    return company_page, job_id, False, i


def _handle_reset_or_retry(action: str, company_page, i: int, log) -> bool:
    """Common tail of every confirm_or_reset() answer inside
    _fill_review_retry_loop: raises _ResetRequested for 'reset' (same as
    every other confirm_or_reset call site), or returns True for 'retry'
    when there's a company_page to retry on -- the caller's loop is
    expected to `continue` when this returns True. False means neither:
    just move on to whatever's next."""
    normalized = (action or "").strip().lower()
    if normalized == "reset":
        raise _ResetRequested()
    if company_page is not None and normalized == "retry":
        log(f"[{i}] Retrying autofill on the same page...")
        return True
    return False


async def _fill_review_retry_loop(
    page, company_page, known_pages: set, current_page: dict, job_id: str, i: int,
    profile: dict, max_steps: int, confirm_or_reset, ask_fn, on_frame, log,
):
    """Runs autofill_form_multistep against company_page, then pauses for
    review -- retry (a fresh autofill_form_multistep pass on the same page)
    always available, plus jobright's own "Did you apply?" popup on page
    (the ORIGINAL jobright tab, not company_page) afterward, when that's
    even relevant. Loops on 'retry' from ANY of these pauses, not just the
    main review one -- the point is you can trigger a fresh attempt
    whenever you want, not only right after one's already failed.

    Returns the possibly-updated company_page: an Apply/Next click inside
    autofill_form_multistep can switch to a new tab (see autofill.
    AutofillResult.final_page), and the caller needs that to decide what
    page to navigate back from afterward. Raises _ResetRequested exactly
    like confirm_or_reset callers always have."""
    while True:
        result = None
        login_popup = None

        if company_page is not None:
            # let redirects/dynamic content settle before reading the form.
            # Some career sites never go fully network-idle (analytics
            # beacons, chat widgets, etc.), so don't let that hang/kill
            # the whole run.
            try:
                await company_page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await company_page.wait_for_timeout(1500)

            if _is_job_board_domain(company_page.url):
                # This is a job board's own repost, not the company's
                # site -- its "Apply"/"Continue" buttons lead into
                # that board's own account signup/login (see
                # run_automation's docstring), so don't click
                # anything on it at all; treat it the same as any
                # other login-required page.
                log(f"[{i}] The original job post is hosted on "
                    f"{urlparse(company_page.url).netloc}, not the company's own site -- "
                    f"applying there needs an account with that site, so this automation "
                    f"won't click anything here.")
                result = AutofillResult(filled=[], needs_review=[], errors=[], login_required=True)
            else:
                login_popup = _find_login_popup(company_page, known_pages)

            if login_popup is None and result is None:
                try:
                    result = await autofill_form_multistep(
                        company_page,
                        profile,
                        profile["resume_path"],
                        profile.get("cover_letter_path"),
                        max_steps=max_steps,
                        screenshot_dir=SCREENSHOT_DIR,
                        screenshot_prefix=f"job_{job_id}",
                        # The interactive prompts below block until someone is
                        # actually there to answer them -- notify the moment the
                        # AI hands off, not only after all of them are answered.
                        on_needs_review=lambda items: notify(
                            f"[{i}] {len(items)} field(s) need your input -- "
                            f"come back to the terminal."
                        ),
                        ask_fn=ask_fn,
                        on_frame=on_frame,
                        log=log,
                    )
                except Exception as e:
                    log(f"[{i}] Autofill failed on this page ({e}); "
                        f"you'll need to fill it manually.")
                    result = None

                # An Apply/Next click can open the real next step in a
                # new tab instead of navigating in place (see
                # autofill.AutofillResult.final_page) -- follow it here
                # too, or the old, now-abandoned tab stays what this
                # loop (and _find_login_popup) keeps looking at, while
                # the real one sits unrecognized and gets mistaken for
                # a login popup.
                if result is not None and result.final_page is not None \
                        and result.final_page is not company_page:
                    company_page = result.final_page
                    known_pages.add(company_page)
                    current_page["page"] = company_page

                # autofill_form_multistep may have added new profile['custom_answers']
                # entries from what you typed in during this listing -- persist those.
                save_profile(profile)

                # A login popup can also appear as a side effect of the fill
                # attempt itself (e.g. clicking a "Continue with Google" button
                # partway through a multi-step form) -- check again now.
                login_popup = _find_login_popup(company_page, known_pages)

            # Notify as soon as the AI's autofill attempt is done, one way or
            # another -- success, needs review, failed, or blocked on login --
            # not just when the application ends up fully ready to submit. You
            # might be away from the browser and want to know it's done either way.
            if login_popup is not None:
                log(f"[{i}] A login window opened ({login_popup.url}) -- log in "
                    f"yourself, then close it and retry autofill.")
                notify(f"[{i}] A login window opened -- log in "
                       f"yourself to continue.")
            elif result is not None and result.login_required:
                log(f"[{i}] This page looks like it wants you to log "
                    f"into an existing account, or only offers a third-party option like "
                    f"\"Apply with LinkedIn/GitHub\" -- handle that yourself in the "
                    f"browser, then retry autofill.")
                notify(f"[{i}] Looks like a login or third-party "
                       f"apply option is needed -- handle it yourself to continue.")
            elif result is None:
                notify(f"[{i}] Autofill failed on this page -- "
                       f"you'll need to fill it manually.")
            elif result.needs_review or result.errors:
                application_tracking.record(
                    job_id, "needs_review", company_page.url, result.needs_review, result.errors
                )
                log(f"[{i}] Needs review: "
                    f"{len(result.needs_review)} field(s), {len(result.errors)} error(s).")
                for item in result.needs_review:
                    log(f"    - {item['label']}: {item['reasoning']}")
                for item in result.errors:
                    log(f"    ! {item['label']}: {item['error']}")
                notify(f"[{i}] Filled, but {len(result.needs_review)} "
                       f"field(s) need your review before you submit.")
            else:
                application_tracking.record(job_id, "filled_ready_for_submit", company_page.url)
                log(f"[{i}] All fields filled confidently.")
                notify(f"[{i}] Application filled and ready to review/submit.")

        if on_frame is not None and company_page is not None:
            frame_bytes = await take_frame_screenshot(company_page)
            if frame_bytes is not None:
                on_frame(frame_bytes)

        needs_login = login_popup is not None or (result is not None and result.login_required)
        if needs_login:
            action = confirm_or_reset(
                f"[{i}] It looks like this page wants you to log in, "
                f"or only offers a third-party apply option (LinkedIn/GitHub/etc.) this "
                f"automation won't use. Handle that yourself in the browser, then type "
                f"'retry' and press Enter to have the AI retry autofill, or just press "
                f"Enter to move on without retrying.",
                retryable=True,
            )
        else:
            action = confirm_or_reset(
                f"[{i}] Review the form in the browser "
                f"(check anything flagged above), then submit manually if it looks right. "
                f"If a popup got in the AI's way, dismiss it yourself in the browser, then "
                f"retry autofill on this same page instead of moving on. Press Enter to "
                f"continue, or type 'retry' and press Enter to retry autofill...",
                retryable=company_page is not None,
            )
        if _handle_reset_or_retry(action, company_page, i, log):
            continue

        # jobright shows a "Did you apply?" popup when you switch back to this tab
        # after visiting the company page -- that's your call to answer, not the
        # script's, and leaving it open can block the next listing's clicks. A
        # "retry" response from either of its own fallback pauses below also
        # loops back to the top instead of just re-prompting the same popup.
        did_you_apply = page.get_by_text("Did you apply", exact=False)
        if await did_you_apply.count() == 0:
            break

        if on_frame is not None:
            frame_bytes = await take_frame_screenshot(page)
            if frame_bytes is not None:
                on_frame(frame_bytes)
        apply_options = ["Yes, I applied!", "No, I didn't apply"]
        if ask_fn is not None:
            # GUI mode: answer it right from the review panel instead of
            # needing to click inside the raw (non-embedded) browser window.
            answer = (ask_fn({
                "label": "Did you apply?",
                "reasoning": "jobright is asking whether you actually submitted "
                             "the application on the company page.",
                "type": "radio-group",
                "options": apply_options,
            }) or "").strip()
            match = next((opt for opt in apply_options if opt.strip().lower() == answer.lower()), None)
            if match is not None:
                await page.get_by_text(match, exact=True).first.click()
                await page.wait_for_timeout(500)
            else:
                action = confirm_or_reset(
                    "Couldn't match your answer to a button -- click Yes or No "
                    "yourself in the browser, then press Enter here to continue "
                    "(or type 'retry' to retry autofill instead)...",
                    retryable=company_page is not None,
                )
                if _handle_reset_or_retry(action, company_page, i, log):
                    continue
        else:
            action = confirm_or_reset(
                "A \"Did you apply?\" popup is open in the browser -- click Yes or "
                "No yourself, then press Enter here to continue (or type 'retry' "
                "to retry autofill instead)...",
                retryable=company_page is not None,
            )
            if _handle_reset_or_retry(action, company_page, i, log):
                continue

        break

    return company_page


async def run_automation(
    profile: dict,
    log: Callable[[str], None] = print,
    confirm_fn: Callable[[str], None] | None = None,
    ask_fn: Any = None,
    on_frame: Callable[[bytes], None] | None = None,
    max_steps: int = MAX_FORM_STEPS,
    should_reset: Callable[[], bool] | None = None,
) -> None:
    """Sign into jobright.ai, then pause and let a human pick which listing to
    apply to next -- browse the recommended feed yourself and click into
    whatever job you want (through to its job detail page), then continue
    here and this takes over: click Apply, autofill the real application
    form, and pause again for review. Repeats for as many listings as you
    like; there's no fixed number processed automatically -- end the session
    with Stop (or Ctrl+C in the terminal) whenever you're done. Never submits
    anything -- always pauses for a human to review and submit.

    log() replaces plain print() so a GUI can route status text into its own
    view instead of (or alongside) the terminal.

    confirm_fn(prompt, retryable=False), if given, replaces the terminal
    input() calls that just wait for acknowledgement -- called with a
    message, expected to block until the user responds. retryable=True
    (whenever there's a company page to retry on) at every one of these
    pauses: the review-and-continue pause, and both "did you apply?"
    fallback pauses (the terminal prompt, and the GUI's "couldn't match
    your answer" fallback). A response of "retry" (case-insensitive,
    whitespace-trimmed) at ANY of them loops back to a fresh
    autofill_form_multistep pass on the same company page from scratch --
    e.g. you dismissed a popup that got in the AI's way, or you just want
    another attempt, not necessarily right after a failure or login gate.
    Any other response (including the default terminal input()'s usual
    blank Enter) means continue as before. Defaults to real input().

    ask_fn, if given, is passed through to autofill_form_multistep to replace
    the terminal prompt for fields the AI couldn't confidently answer -- see
    autofill.autofill_form_multistep's docstring. It's also used directly
    here for the "did you apply?" popup: when given, its Yes/No answer is
    clicked on the real popup automatically instead of asking you to click it
    yourself in the browser window.

    on_frame, if given, is a synchronous callable(jpeg_bytes) fed a screenshot
    right before each confirm() pause, in addition to the checkpoints already
    covered inside autofill_form_multistep -- e.g. to drive a live view. Also
    fed a screenshot on its own timer (LIVE_VIEW_INTERVAL_SECONDS, see
    _live_view_loop) of whichever page is currently relevant, independent of
    those action-triggered checkpoints -- so a slow load, redirect, or
    animation between two checkpoints still shows up promptly instead of the
    view sitting stale until the next one.

    should_reset, if given, is checked right before every confirm() pause
    below; when it returns True, that pause is skipped and treated exactly
    like typing 'reset' at it would be (see below) -- e.g. a GUI's Reset
    button that fired while this was mid-autofill, with no pause currently
    showing to answer directly.

    Typing 'reset' (case-insensitive, whitespace-trimmed) at ANY confirm()
    pause -- or should_reset() returning True at the moment one would be
    shown -- abandons whatever listing is currently in progress (closing its
    company page and any login popups) and jumps back to the very first
    "browse the recommended feed" pause, on the SAME browser/login session.
    Unlike stopping and restarting the whole run, this never closes the
    browser or re-authenticates.

    If a company page looks like it wants you to log into an existing
    account -- either autofill_form_multistep detects a login gate on the
    page itself (see its docstring / autofill._looks_like_login_gate), or a
    separate popup window opens (e.g. an OAuth "Continue with Google/
    LinkedIn" flow) -- autofill is skipped for that attempt and the review
    pause explains what's going on instead of presenting it as an ordinary
    review. Log in yourself, then choose Retry to have the AI take another
    pass, or Continue to move on without retrying."""
    confirm = confirm_fn or (lambda prompt, retryable=False, allow_apply_current=False: input(prompt))
    # Wiped at the start of every run rather than left to accumulate --
    # these are only ever useful for reviewing the run that's about to
    # start, and with no cleanup otherwise, they'd just pile up on disk
    # indefinitely across every past run.
    shutil.rmtree(SCREENSHOT_DIR, ignore_errors=True)
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    def confirm_or_reset(prompt: str, retryable: bool = False, allow_apply_current: bool = False) -> str:
        if should_reset is not None and should_reset():
            return "reset"
        return confirm(prompt, retryable=retryable, allow_apply_current=allow_apply_current)

    async with async_playwright() as p:
        # Some real-world career sites sit behind bot detection (Cloudflare,
        # Akamai, etc.) that doesn't show an explicit "blocked" page --
        # instead it just silently hangs the page forever, which matches a
        # "new tab never finishes loading" symptom that's specific to this
        # automation's browser (the exact same URL loads fine in an
        # everyday Chrome profile at the same time). Three things together
        # to look like an ordinary browser instead of an obviously automated
        # one:
        #   - channel="chrome": drives the real, installed Google Chrome
        #     binary (with real branding/API keys) instead of Playwright's
        #     own bundled Chromium build.
        #   - --disable-blink-features=AutomationControlled: removes
        #     several automation signals Chromium otherwise exposes by
        #     default.
        #   - navigator.webdriver override below: belt-and-suspenders on
        #     top of the flag above -- some Chromium versions still leave
        #     it true otherwise, and it's one of the most commonly checked
        #     automation fingerprints.
        # --dns-over-https-mode=off: a brand-new Chromium profile defaults
        # to "Automatic" Secure DNS (tries DNS-over-HTTPS first). If DoH
        # lookups are silently blocked/time out on this network, a fresh
        # profile hangs trying that before ever falling back to plain DNS
        # -- unlike an established profile (this laptop's regular Chrome)
        # that may already be configured differently. Forces plain
        # OS-level DNS resolution instead, matching ordinary browsing.
        browser = await p.chromium.launch(
            channel="chrome",
            headless=False,
            slow_mo=50,
            args=[
                "--dns-over-https-mode=off",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = await browser.new_page()
        # Applies to every page in this context, including tabs opened
        # later (by the automation or by hand) -- see the launch args above
        # for why.
        await page.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        # Not gated behind on_frame (unlike the live-view watchers below) --
        # confirmed to happen in plain terminal use too, with no webapp/live
        # view involved at all. See _redirect_new_tabs_away_from_ntp.
        _redirect_new_tabs_away_from_ntp(page.context)
        # {"page": ...} rather than a plain variable so _live_view_loop
        # (started right below) always picks up whichever page is currently
        # relevant on its next tick -- repointed at company_page/back to
        # page itself at the few places below where that changes.
        current_page = {"page": page}
        # Every page _find_login_popup should never mistake for a login
        # popup -- jobright's own tab plus every company tab opened over
        # the course of this whole run (see _find_login_popup's docstring),
        # not just the current listing's.
        known_pages = {page}
        live_view_task = asyncio.create_task(_live_view_loop(current_page, on_frame)) if on_frame is not None else None
        # current_page is repointed explicitly at every point where the
        # live view actually needs to follow somewhere new (picking a
        # listing, an Apply/Next click opening a new tab mid-fill, a
        # reset) -- deliberately NOT the instant any new tab opens
        # anywhere in the context, including ones you open by hand
        # yourself. That used to auto-follow every new tab immediately,
        # which meant _live_view_loop's timer could land on (and
        # screenshot) a tab you'd just opened and hadn't navigated
        # anywhere yet -- observed to stall that tab's own CDP target
        # entirely, blocking whatever you typed into it right after.
        # Pre-grant geolocation for every origin in this context so ATS sites
        # (Oracle/Taleo, Workday, etc.) that ask for it on page load never
        # trigger Chrome's native "wants to know your location" bubble --
        # that bubble sits outside the page DOM and can physically cover the
        # Apply button, making Playwright's click fail as if nothing were
        # there to click.
        await page.context.grant_permissions(["geolocation"])
        await page.goto("https://jobright.ai")
        await page.get_by_text("Sign In", exact=False).first.click()
        await page.fill("#basic_email", os.environ["JOBRIGHT_EMAIL"])
        await page.fill("#basic_password", os.environ["JOBRIGHT_PASSWORD"])
        # The header's own "Sign In" button (which we just clicked to open this
        # form) is still on the page and also matches role=button name="SIGN IN" --
        # scope to the login form itself (#basic) to hit the real submit button.
        await page.locator("#basic").get_by_role("button", name="SIGN IN", exact=True).click()
        await page.wait_for_url("**/jobs/recommend**", timeout=15000)
        await page.wait_for_selector("h2.index_job-title__Riiip", timeout=20000)

        i = 0
        while True:
            company_page = None
            try:
                picked = await _pick_target_page(page, known_pages, i, confirm_or_reset, log)
                if picked is None:
                    continue
                company_page, job_id, applying_current_page, i = picked

                if company_page is not None:
                    known_pages.add(company_page)
                    current_page["page"] = company_page

                company_page = await _fill_review_retry_loop(
                    page, company_page, known_pages, current_page, job_id, i,
                    profile, max_steps, confirm_or_reset, ask_fn, on_frame, log,
                )

                if applying_current_page:
                    # page itself is what just got filled -- no jobright job
                    # detail page to go back to. Navigate wherever you want
                    # next yourself; the same prompt at the top of this loop
                    # takes either a jobright listing or another "apply
                    # current" from here.
                    log(f"[{i}] Done with this one -- navigate to whatever's next "
                        f"yourself (another manual page, or back to jobright).")
                else:
                    current_page["page"] = page
                    await page.go_back()
                    await page.wait_for_selector("h2.index_job-title__Riiip", timeout=20000)
            except _StopRequested:
                break
            except _ResetRequested:
                log(f"[{i}] Resetting back to the internship listing page...")
                current_page["page"] = page
                # company_page IS page itself when applying to whatever was
                # already open (see "apply current" above) -- never close
                # the one tab this whole run drives.
                if company_page is not None and company_page is not page:
                    try:
                        await company_page.close()
                    except Exception:
                        pass
                for extra in list(page.context.pages):
                    if extra is not page:
                        try:
                            await extra.close()
                        except Exception:
                            pass
                try:
                    await page.goto("https://jobright.ai/jobs/recommend")
                    await page.wait_for_selector("h2.index_job-title__Riiip", timeout=20000)
                except Exception as e:
                    log(f"[{i}] Couldn't navigate back to the listing page cleanly "
                        f"({e}); check the browser window.")

        if live_view_task is not None:
            live_view_task.cancel()
            try:
                await live_view_task
            except asyncio.CancelledError:
                pass
        await browser.close()


async def main():
    profile = load_profile()
    await run_automation(profile)


if __name__ == "__main__":
    asyncio.run(main())
