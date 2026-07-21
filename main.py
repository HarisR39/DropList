import asyncio
import json
import os

from playwright.async_api import async_playwright

import application_tracking
from autofill import autofill_form_multistep

NUM_LISTINGS_TO_REVIEW = 5
MAX_FORM_STEPS = 6
SCREENSHOT_DIR = "screenshots"


def load_profile() -> dict:
    with open("profile.json", "r", encoding="utf-8") as f:
        return json.load(f)


def save_profile(profile: dict) -> None:
    with open("profile.json", "w", encoding="utf-8") as f:
        json.dump(profile, f, indent=2)


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
            await page.click("#apply-now-button-id", force=True)
            await page.wait_for_timeout(800)

            exit_button = page.get_by_text("EXIT", exact=True)
            if await exit_button.count() > 0:
                await exit_button.first.click()

            company_page = None
            try:
                async with page.context.expect_page(timeout=6000) as new_page_info:
                    await page.click("text=Apply without Customizing", timeout=5000)
                company_page = await new_page_info.value
                await company_page.wait_for_load_state()
                print(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] Company page opened: {company_page.url}")
            except Exception:
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
                    )
                except Exception as e:
                    print(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] Autofill failed on this page ({e}); "
                          f"you'll need to fill it manually.")
                    result = None

                # autofill_form_multistep may have added new profile['custom_answers']
                # entries from what you typed in during this listing -- persist those.
                save_profile(profile)

                if result is not None:
                    if result.needs_review or result.errors:
                        application_tracking.record(
                            job_id, "needs_review", company_page.url, result.needs_review, result.errors
                        )
                        print(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] Needs review: "
                              f"{len(result.needs_review)} field(s), {len(result.errors)} error(s).")
                        for item in result.needs_review:
                            print(f"    - {item['label']}: {item['reasoning']}")
                        for item in result.errors:
                            print(f"    ! {item['label']}: {item['error']}")
                    else:
                        application_tracking.record(job_id, "filled_ready_for_submit", company_page.url)
                        print(f"[{i + 1}/{NUM_LISTINGS_TO_REVIEW}] All fields filled confidently.")

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
