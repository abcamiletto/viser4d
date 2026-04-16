from __future__ import annotations

from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser, Page, Playwright, sync_playwright


@pytest.fixture(scope="session")
def playwright_instance() -> Iterator[Playwright]:
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance: Playwright) -> Iterator[Browser]:
    browser = playwright_instance.chromium.launch(
        headless=True,
        args=["--disable-dev-shm-usage", "--use-gl=swiftshader"],
    )
    yield browser
    browser.close()


@pytest.fixture()
def page(browser: Browser) -> Iterator[Page]:
    context = browser.new_context(viewport={"width": 1400, "height": 1000})
    page = context.new_page()
    page.set_default_timeout(10_000)
    yield page
    context.close()
