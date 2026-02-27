import asyncio, json
from playwright.async_api import async_playwright

JOB_URL = "https://www.naukri.com/job-listings-node-js-developer-oben-electric-vehicles-bengaluru-2-to-5-years-260226007841"

JS_JOB_PAGE = """() => {
    const tryText = (sels) => {
        for (const s of sels) {
            const el = document.querySelector(s);
            if (el && el.textContent.trim()) {
                return {text: el.textContent.trim().slice(0, 100), cls: (el.className || '').toString().slice(0, 100), tag: el.tagName};
            }
        }
        return null;
    };

    return {
        title: tryText(['h1', '.jd-header-title', 'h1[class*="title"]', '.styles_jd-header-title']),
        company: tryText(['.jd-header-comp-name a', 'a[class*="comp-name"]', '.styles_jd-header-comp-name a']),
        salary: tryText(['.salary span', '.sal-wrap span', 'span[class*="salary"]', '.styles_jhc__salary']),
        experience: tryText(['.exp span', '.exp-wrap span', 'span[class*="experience"]', '.styles_jhc__exp']),
        location: tryText(['.loc span', '.loc-wrap span', 'span[class*="location"]', '.styles_jhc__loc']),
        description: tryText(['.job-desc', 'section.job-desc', '.styles_JDC', '[class*="job-desc"]', '.dang-inner-html']),
        skills: Array.from(document.querySelectorAll('a.chip, span.chip, a[class*="chip"], span[class*="tag-li"], .key-skill a')).slice(0, 10).map(s => s.textContent.trim()),
        already_applied: !!document.querySelector('#already-applied'),
        apply_button: tryText(['button[class*="apply"], #apply-button, .apply-button-container button']),
        url: window.location.href,
    };
}"""

async def main():
    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir='./chrome-profile', headless=False,
        viewport={'width': 1280, 'height': 800},
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    await page.goto(JOB_URL, wait_until='domcontentloaded', timeout=20000)
    await asyncio.sleep(4)

    print(f'URL: {page.url}')
    result = await page.evaluate(JS_JOB_PAGE)
    print(json.dumps(result, indent=2))

    await ctx.close()
    await pw.stop()

asyncio.run(main())
