import asyncio
import json
import os
import urllib.request
from typing import Any, Callable

from playwright.async_api import async_playwright

import application_tracking
from autofill import autofill_form_multistep, take_frame_screenshot

NUM_LISTINGS_TO_REVIEW = 5
MAX_FORM_STEPS = 6
SCREENSHOT_DIR = "screenshots"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")


def load_profile() -> dict:
    with open("profile.json", "r", encoding="utf-8") as f:
        return json.load(f)


def save_profile(profile: dict) -> None:
    with open("profile.json", "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)


def _find_login_popup(jobright_page, company_page):
    """A same-context page other than the two we expect (jobright's own tab
    and the company application tab) -- e.g. an OAuth "Continue with
    Google/LinkedIn" flow opening its own window. Best-effort: there's no
    reliable way to positively identify a login popup by URL/title across
    arbitrary providers, so any unexpected extra page is treated as one."""
    known = {jobright_page, company_page}
    extras = [p for p in company_page.context.pages if p not in known]
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


async def run_automation(
    profile: dict,
    log: Callable[[str], None] = print,
    confirm_fn: Callable[[str], None] | None = None,
    ask_fn: Any = None,
    on_frame: Callable[[bytes], None] | None = None,
    num_listings: int = NUM_LISTINGS_TO_REVIEW,
    max_steps: int = MAX_FORM_STEPS,
) -> None:
    """Sign into jobright.ai and walk through its recommended listings,
    autofilling each one's real application form. Never submits anything --
    always pauses for a human to review and submit.

    log() replaces plain print() so a GUI can route status text into its own
    view instead of (or alongside) the terminal.

    confirm_fn(prompt, retryable=False), if given, replaces the terminal
    input() calls that just wait for acknowledgement (review-and-continue
    pause; the "did you apply?" popup when ask_fn isn't given) -- called with
    a message, expected to block until the user responds. retryable=True only
    for the review-and-continue pause; a response of "retry" (case-
    insensitive, whitespace-trimmed) there re-runs autofill on the same page
    from scratch instead of moving on -- e.g. after manually dismissing a
    popup that got in the AI's way. Any other response (including the
    default terminal input()'s usual blank Enter) means continue. Defaults
    to real input().

    ask_fn, if given, is passed through to autofill_form_multistep to replace
    the terminal prompt for fields the AI couldn't confidently answer -- see
    autofill.autofill_form_multistep's docstring. It's also used directly
    here for the "did you apply?" popup: when given, its Yes/No answer is
    clicked on the real popup automatically instead of asking you to click it
    yourself in the browser window.

    on_frame, if given, is a synchronous callable(jpeg_bytes) fed a screenshot
    right before each confirm() pause, in addition to the checkpoints already
    covered inside autofill_form_multistep -- e.g. to drive a live view.

    If a company page looks like it wants you to log into an existing
    account -- either autofill_form_multistep detects a login gate on the
    page itself (see its docstring / autofill._looks_like_login_gate), or a
    separate popup window opens (e.g. an OAuth "Continue with Google/
    LinkedIn" flow) -- autofill is skipped for that attempt and the review
    pause explains what's going on instead of presenting it as an ordinary
    review. Log in yourself, then choose Retry to have the AI take another
    pass, or Continue to move on without retrying."""
    confirm = confirm_fn or (lambda prompt, retryable=False: input(prompt))
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=50)
        page = await browser.new_page()
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

        for i in range(num_listings):
            titles = await page.query_selector_all("h2.index_job-title__Riiip")
            if i >= len(titles):
                log(f"Only {len(titles)} listings loaded, stopping early.")
                break

            await titles[i].click()
            await page.wait_for_url("**/jobs/info/**", timeout=15000)
            job_id = page.url.rsplit("/jobs/info/", 1)[-1].split("?")[0]

            exit_button = page.get_by_text("EXIT", exact=True)
            if await exit_button.count() > 0:
                await exit_button.first.click()

            # A full-screen onboarding-tour overlay (jobright's reactour-based
            # walkthrough) can appear here and intercept every click for 30s
            # until Playwright gives up. Only press Escape if it's actually
            # present -- on this page Escape is also jobright's own shortcut
            # to close the job detail view entirely, which would remove the
            # apply button from the DOM before we ever click it.
            if await page.query_selector("#___reactour") is not None:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)

            # The "Customize Your Resume" modal (with the "Apply without
            # Customizing" link) only shows up for listings with a low profile
            # match score -- for others, this click opens the company tab
            # directly with no modal at all. Watch for a new page across the
            # whole apply-now click, not just the modal-click that may not happen.
            pages_before = set(page.context.pages)
            await page.click("#apply-now-button-id", force=True)
            await page.wait_for_timeout(800)

            exit_button = page.get_by_text("EXIT", exact=True)
            if await exit_button.count() > 0:
                await exit_button.first.click()

            company_page = None
            new_pages = [p2 for p2 in page.context.pages if p2 not in pages_before]
            if new_pages:
                company_page = new_pages[0]
                # A single apply-now click can occasionally open more than one
                # new tab (e.g. an ad/tracking redirect alongside the real
                # destination, more common on listings that route through a
                # login/OAuth provider) -- close the extras immediately so they
                # don't clutter the browser or get mistaken for a genuine login
                # popup later (see _find_login_popup).
                for extra in new_pages[1:]:
                    try:
                        await extra.close()
                    except Exception:
                        pass
            else:
                apply_without_customizing = page.get_by_text("Apply without Customizing", exact=True)
                if await apply_without_customizing.count() > 0:
                    try:
                        async with page.context.expect_page(timeout=6000) as new_page_info:
                            await apply_without_customizing.click(timeout=5000)
                        company_page = await new_page_info.value
                    except Exception:
                        company_page = None

                    if company_page is not None:
                        for extra in page.context.pages:
                            if extra not in pages_before and extra is not company_page:
                                try:
                                    await extra.close()
                                except Exception:
                                    pass

            if company_page is not None:
                await company_page.wait_for_load_state()
                log(f"[{i + 1}/{num_listings}] Company page opened: {company_page.url}")
            else:
                log(f"[{i + 1}/{num_listings}] No resume-customize popup or new tab appeared; "
                    f"check the browser window.")

            # Loops back only on an explicit "retry" response from the review
            # pause below -- e.g. you noticed a popup got in the AI's way,
            # dismissed it yourself in the browser, and want a fresh full
            # autofill pass on this same page rather than moving on. Runs
            # exactly once (no retry offered) if there's no company page.
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

                    login_popup = _find_login_popup(page, company_page)

                    if login_popup is None:
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
                                    f"[{i + 1}/{num_listings}] {len(items)} field(s) need your input -- "
                                    f"come back to the terminal."
                                ),
                                ask_fn=ask_fn,
                                on_frame=on_frame,
                                log=log,
                            )
                        except Exception as e:
                            log(f"[{i + 1}/{num_listings}] Autofill failed on this page ({e}); "
                                f"you'll need to fill it manually.")
                            result = None

                        # autofill_form_multistep may have added new profile['custom_answers']
                        # entries from what you typed in during this listing -- persist those.
                        save_profile(profile)

                        # A login popup can also appear as a side effect of the fill
                        # attempt itself (e.g. clicking a "Continue with Google" button
                        # partway through a multi-step form) -- check again now.
                        login_popup = _find_login_popup(page, company_page)

                    # Notify as soon as the AI's autofill attempt is done, one way or
                    # another -- success, needs review, failed, or blocked on login --
                    # not just when the application ends up fully ready to submit. You
                    # might be away from the browser and want to know it's done either way.
                    if login_popup is not None:
                        log(f"[{i + 1}/{num_listings}] A login window opened -- log in "
                            f"yourself, then close it and retry autofill.")
                        notify(f"[{i + 1}/{num_listings}] A login window opened -- log in "
                               f"yourself to continue.")
                    elif result is not None and result.login_required:
                        log(f"[{i + 1}/{num_listings}] This page looks like it wants you to log "
                            f"into an existing account, or only offers a third-party option like "
                            f"\"Apply with LinkedIn/GitHub\" -- handle that yourself in the "
                            f"browser, then retry autofill.")
                        notify(f"[{i + 1}/{num_listings}] Looks like a login or third-party "
                               f"apply option is needed -- handle it yourself to continue.")
                    elif result is None:
                        notify(f"[{i + 1}/{num_listings}] Autofill failed on this page -- "
                               f"you'll need to fill it manually.")
                    elif result.needs_review or result.errors:
                        application_tracking.record(
                            job_id, "needs_review", company_page.url, result.needs_review, result.errors
                        )
                        log(f"[{i + 1}/{num_listings}] Needs review: "
                            f"{len(result.needs_review)} field(s), {len(result.errors)} error(s).")
                        for item in result.needs_review:
                            log(f"    - {item['label']}: {item['reasoning']}")
                        for item in result.errors:
                            log(f"    ! {item['label']}: {item['error']}")
                        notify(f"[{i + 1}/{num_listings}] Filled, but {len(result.needs_review)} "
                               f"field(s) need your review before you submit.")
                    else:
                        application_tracking.record(job_id, "filled_ready_for_submit", company_page.url)
                        log(f"[{i + 1}/{num_listings}] All fields filled confidently.")
                        notify(f"[{i + 1}/{num_listings}] Application filled and ready to review/submit.")

                if on_frame is not None and company_page is not None:
                    frame_bytes = await take_frame_screenshot(company_page)
                    if frame_bytes is not None:
                        on_frame(frame_bytes)

                needs_login = login_popup is not None or (result is not None and result.login_required)
                if needs_login:
                    action = confirm(
                        f"[{i + 1}/{num_listings}] It looks like this page wants you to log in, "
                        f"or only offers a third-party apply option (LinkedIn/GitHub/etc.) this "
                        f"automation won't use. Handle that yourself in the browser, then type "
                        f"'retry' and press Enter to have the AI retry autofill, or just press "
                        f"Enter to move on without retrying.",
                        retryable=True,
                    )
                else:
                    action = confirm(
                        f"[{i + 1}/{num_listings}] Review the form in the browser "
                        f"(check anything flagged above), then submit manually if it looks right. "
                        f"If a popup got in the AI's way, dismiss it yourself in the browser, then "
                        f"retry autofill on this same page instead of moving on. Press Enter to "
                        f"continue, or type 'retry' and press Enter to retry autofill...",
                        retryable=company_page is not None,
                    )
                if company_page is None or (action or "").strip().lower() != "retry":
                    break
                log(f"[{i + 1}/{num_listings}] Retrying autofill on the same page...")

            # jobright shows a "Did you apply?" popup when you switch back to this tab
            # after visiting the company page -- that's your call to answer, not the
            # script's, and leaving it open can block the next listing's clicks.
            did_you_apply = page.get_by_text("Did you apply", exact=False)
            if await did_you_apply.count() > 0:
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
                        confirm("Couldn't match your answer to a button -- click Yes or "
                                "No yourself in the browser, then press Enter here to continue...")
                else:
                    confirm("A \"Did you apply?\" popup is open in the browser -- click Yes or "
                            "No yourself, then press Enter here to continue...")

            await page.go_back()
            await page.wait_for_selector("h2.index_job-title__Riiip", timeout=20000)

        await browser.close()


async def main():
    profile = load_profile()
    await run_automation(profile)


if __name__ == "__main__":
    asyncio.run(main())
