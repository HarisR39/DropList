import asyncio
import json
import os
import urllib.request

from playwright.async_api import async_playwright

import application_tracking
from autofill import autofill_form_multistep

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


async def main():
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    profile = load_profile()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=50)
        page = await browser.new_page()
        await page.goto("https://jobright.ai")
        await page.get_by_text("Sign In", exact=False).first.click()
        await page.fill("#basic_email", os.environ["JOBRIGHT_EMAIL"])
        await page.fill("#basic_password", os.environ["JOBRIGHT_PASSWORD"])
        await page.get_by_role("button", name="SIGN IN", exact=True).click()
        await page.wait_for_url("**/jobs/recommend**", timeout=15000)
        await page.wait_for_selector("h2.index_job-title__Riiip", timeout=20000)

        for i in range(NUM_LISTINGS_TO_REVIEW):
            titles = await page.query_selector_all("h2.index_job-title__Riiip")
            if i >= len(titles):
                print(f"Only {len(titles)} listings loaded, stopping early.")
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
            new_pages = [p for p in page.context.pages if p not in pages_before]
            if new_pages:
                company_page = new_pages[0]
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
                await company_page.wait_for_load_state()
                print(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] Company page opened: {company_page.url}")
            else:
                print(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] No resume-customize popup or new tab appeared; "
                      f"check the browser window.")

            if company_page is not None:
                # let redirects/dynamic content settle before reading the form.
                # Some career sites never go fully network-idle (analytics beacons,
                # chat widgets, etc.), so don't let that hang/kill the whole run.
                try:
                    await company_page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await company_page.wait_for_timeout(1500)

                try:
                    result = await autofill_form_multistep(
                        company_page,
                        profile,
                        profile["resume_path"],
                        profile.get("cover_letter_path"),
                        max_steps=MAX_FORM_STEPS,
                        screenshot_dir=SCREENSHOT_DIR,
                        screenshot_prefix=f"job_{job_id}",
                        # The interactive terminal prompts below block until someone
                        # is actually there to answer them -- notify the moment the
                        # AI hands off, not only after all of them are answered.
                        on_needs_review=lambda items: notify(
                            f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] {len(items)} field(s) need your input -- "
                            f"come back to the terminal."
                        ),
                    )
                except Exception as e:
                    print(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] Autofill failed on this page ({e}); "
                          f"you'll need to fill it manually.")
                    result = None

                # autofill_form_multistep may have added new profile['custom_answers']
                # entries from what you typed in during this listing -- persist those.
                save_profile(profile)

                # Notify as soon as the AI's autofill attempt is done, one way or
                # another -- success, needs review, or failed -- not just when the
                # application ends up fully ready to submit. You might be away from
                # the browser and want to know it's done either way.
                if result is None:
                    notify(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] Autofill failed on this page -- "
                           f"you'll need to fill it manually.")
                elif result.needs_review or result.errors:
                    application_tracking.record(
                        job_id, "needs_review", company_page.url, result.needs_review, result.errors
                    )
                    print(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] Needs review: "
                          f"{len(result.needs_review)} field(s), {len(result.errors)} error(s).")
                    for item in result.needs_review:
                        print(f"    - {item['label']}: {item['reasoning']}")
                    for item in result.errors:
                        print(f"    ! {item['label']}: {item['error']}")
                    notify(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] Filled, but {len(result.needs_review)} "
                           f"field(s) need your review before you submit.")
                else:
                    application_tracking.record(job_id, "filled_ready_for_submit", company_page.url)
                    print(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] All fields filled confidently.")
                    notify(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] Application filled and ready to review/submit.")

            input(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] Review the form in the browser "
                  f"(check anything flagged above), then submit manually if it looks right. "
                  f"Press Enter to continue...")

            # jobright shows a "Did you apply?" popup when you switch back to this tab
            # after visiting the company page -- that's your call to answer, not the
            # script's, and leaving it open can block the next listing's clicks.
            did_you_apply = page.get_by_text("Did you apply", exact=False)
            if await did_you_apply.count() > 0:
                input("A \"Did you apply?\" popup is open in the browser -- click Yes or "
                      "No yourself, then press Enter here to continue...")

            await page.go_back()
            await page.wait_for_selector("h2.index_job-title__Riiip", timeout=20000)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
