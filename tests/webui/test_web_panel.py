"""Browser-level evaluation of the local analysis panel.

Every test drives the shipped frontend through Chrome; the server side is the
real ``trace_agent.web.serve`` started by the ``web_panel`` fixture. Screenshots
land in ``reports/webui/`` so a failure leaves visual evidence behind.

Run just this suite:  ``uv run pytest tests/webui``
Skip it entirely:     ``DITING_WEBUI=0 uv run pytest``
"""

from __future__ import annotations

import pytest

pytest.importorskip("selenium", reason="需要 selenium，执行 uv sync --group dev")

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

pytestmark = pytest.mark.webui

# Structural contract of static/index.html: these ids are what app.js binds to,
# so a rename there would break the panel silently rather than loudly.
PANEL_IDS = (
    "search",
    "analyze-open",
    "memory-open",
    "refresh",
    "stats-line",
    "case-list",
    "server-status",
    "empty-state",
    "detail",
    "tabs",
    "analyze-modal",
    "memory-view",
)
TABS = ("overview", "findings", "evidence", "run", "json")
CARD = "#case-list .case-card"

# The panel ships no favicon, so every page load logs one benign 404. That is
# noise for a console-error gate; anything else still fails the test.
_IGNORED_CONSOLE_TEXT = ("favicon.ico",)


def _wait(browser, timeout: float = 20.0) -> WebDriverWait:
    return WebDriverWait(browser.driver, timeout)


def _is_shown(browser, element_id: str):
    """Build a condition that is truthy once #element_id is visible."""

    def check(driver):
        nodes = driver.find_elements(By.ID, element_id)
        return nodes[0] if nodes and nodes[0].is_displayed() else False

    return check


def _count(browser, selector: str) -> int:
    return len(browser.driver.find_elements(By.CSS_SELECTOR, selector))


def _cards(browser, suffix: str = "") -> list:
    """Case cards currently rendered, optionally narrowed by class."""
    return browser.driver.find_elements(By.CSS_SELECTOR, f"{CARD}{suffix}")


def _retype(element, text: str) -> None:
    """Replace a field's content using real keystrokes.

    ``clear()`` does not reliably dispatch ``input`` in Chrome, and the panel
    only re-renders from that event, so deleting with backspaces is what makes
    the client-side filter observable.
    """
    current = element.get_attribute("value") or ""
    if current:
        element.send_keys(Keys.END, Keys.BACK_SPACE * len(current))
    if text:
        element.send_keys(text)


def test_index_serves_panel_shell_and_assets(browser, web_panel) -> None:
    browser.open("/")

    assert "DitingAgent" in browser.driver.title
    for element_id in PANEL_IDS:
        assert browser.driver.find_elements(By.ID, element_id), f"index.html 缺少 #{element_id}"

    # Static assets come from the same handler; a routing regression here can
    # leave the page unstyled or unscripted while "/" still answers 200.
    assert len(web_panel.get("/app.js")) > 10_000
    assert len(web_panel.get("/styles.css")) > 5_000


def test_health_and_case_listing_api(web_panel) -> None:
    assert web_panel.get_json("/api/health")["status"] == "ok"

    payload = web_panel.get_json("/api/cases")
    assert isinstance(payload, dict) and "cases" in payload
    listed = [case["id"] for case in payload["cases"]]
    assert listed == web_panel.cases


def test_case_list_renders_seeded_cases(browser, web_panel) -> None:
    browser.open("/")
    cards = _wait(browser).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, CARD)))

    assert len(cards) == len(web_panel.cases)
    assert str(len(cards)) in browser.driver.find_element(By.ID, "stats-line").text

    # The connection dot starts as "连接中" and flips to ok once health answers.
    status = browser.driver.find_element(By.ID, "server-status")
    _wait(browser, 10).until(lambda d: "ok" in (status.get_attribute("class") or ""))

    # loadCases() auto-selects the first case, so the detail pane replaces the
    # empty state without any user interaction.
    assert _wait(browser).until(_is_shown(browser, "detail"))
    assert not browser.driver.find_element(By.ID, "empty-state").is_displayed()


def test_selecting_case_renders_detail_and_every_tab(browser) -> None:
    browser.open("/")
    _wait(browser).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, CARD)))
    # The first case is already selected on load, so pick another one to prove
    # the click path really switches cases instead of re-rendering the same one.
    ids = [node.get_attribute("data-id") for node in _cards(browser)]
    case_id = ids[-1]
    browser.driver.find_element(
        By.CSS_SELECTOR, f'#case-list .case-card[data-id="{case_id}"]'
    ).click()

    assert _wait(browser).until(_is_shown(browser, "detail"))
    # .crumbs is uppercased and prefixed by CSS, so match on the rendered text.
    _wait(browser).until(
        lambda d: case_id.lower() in d.find_element(By.ID, "crumbs").text.lower()
    )
    assert [node.get_attribute("data-id") for node in _cards(browser, ".active")] == [case_id]

    title = _wait(browser).until(
        lambda d: d.find_element(By.ID, "case-title").text.strip() or None
    )
    assert "加载中" not in title
    assert browser.driver.find_element(By.ID, "case-subtitle").text.strip()

    for tab in TABS:
        button = browser.driver.find_element(By.CSS_SELECTOR, f'#tabs .tab[data-tab="{tab}"]')
        button.click()
        assert "active" in (button.get_attribute("class") or "")

        panel = browser.driver.find_element(By.ID, f"panel-{tab}")
        _wait(browser, 10).until(EC.visibility_of(panel))
        # A blank panel is the classic silent frontend failure, so require markup.
        assert panel.get_attribute("innerHTML").strip(), f"{tab} 面板渲染为空"

    browser.screenshot("detail-json-tab")


def test_search_filters_case_list(browser) -> None:
    browser.open("/")
    cards = _wait(browser).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, CARD)))
    total = len(cards)
    needle = cards[0].find_element(By.CSS_SELECTOR, ".case-card-title").text.strip()[:4]
    assert needle

    search = browser.driver.find_element(By.ID, "search")
    _retype(search, needle)
    _wait(browser).until(lambda d: _count(browser, CARD) > 0)
    for node in _cards(browser):
        assert needle.lower() in node.text.lower()

    _retype(search, "zz-no-such-case")
    _wait(browser).until(lambda d: _count(browser, CARD) == 0)
    assert "未找到" in browser.driver.find_element(By.ID, "stats-line").text

    _retype(search, "")
    _wait(browser).until(lambda d: _count(browser, CARD) == total)


def test_analyze_modal_blocks_blank_required_fields(browser) -> None:
    browser.open("/")
    _wait(browser).until(EC.element_to_be_clickable((By.ID, "analyze-open"))).click()

    modal = browser.driver.find_element(By.ID, "analyze-modal")
    _wait(browser).until(EC.visibility_of(modal))
    # Preflight tells the operator what is missing before a long run is queued.
    _wait(browser).until(lambda d: d.find_element(By.ID, "modal-preflight").text.strip() or None)

    # Native `required` only rejects empty strings; a space reaches the custom
    # trim validation in validateAnalyzeForm(), which is the path worth testing.
    for name in ("trace_path", "scenario", "symptom"):
        browser.driver.find_element(
            By.CSS_SELECTOR, f'#analyze-form [name="{name}"]'
        ).send_keys(" ")
    browser.driver.find_element(By.ID, "analyze-submit").click()

    invalid = _wait(browser).until(
        lambda d: d.find_elements(By.CSS_SELECTOR, "#analyze-form .invalid") or None
    )
    assert len(invalid) == 3, "三个必填项都应被标记为 invalid"

    hints = [
        node.text
        for node in browser.driver.find_elements(By.CSS_SELECTOR, "#analyze-form .field-hint")
    ]
    assert any("必填" in hint or "Trace" in hint for hint in hints)

    # Rejected client side: no error banner, modal stays open for correction.
    assert not browser.driver.find_element(By.ID, "analyze-error").is_displayed()
    assert modal.is_displayed()
    browser.screenshot("analyze-validation")

    browser.driver.find_element(By.ID, "analyze-cancel").click()
    _wait(browser).until(EC.invisibility_of_element_located((By.ID, "analyze-modal")))


def test_memory_view_opens_and_returns(browser) -> None:
    browser.open("/")
    _wait(browser).until(EC.element_to_be_clickable((By.ID, "memory-open"))).click()

    _wait(browser).until(_is_shown(browser, "memory-view"))
    assert "on" in (
        browser.driver.find_element(By.ID, "memory-open").get_attribute("class") or ""
    )

    body = browser.driver.find_element(By.ID, "memory-body")
    _wait(browser, 30).until(
        lambda d: "记忆条目" in (body.get_attribute("innerHTML") or "")
    )
    subtitle = browser.driver.find_element(By.ID, "memory-subtitle").text
    assert "加载中" not in subtitle
    assert "失败" not in subtitle, f"记忆库加载失败: {subtitle}"
    browser.screenshot("memory-view")

    # Client-side filtering narrows the entry count without a refetch.
    browser.driver.find_element(By.ID, "memory-search").send_keys("zz-no-such-memory")
    _wait(browser, 10).until(
        lambda d: "记忆条目 · 0 /" in (body.get_attribute("innerHTML") or "")
    )

    browser.driver.find_element(By.ID, "memory-back").click()
    _wait(browser).until(EC.invisibility_of_element_located((By.ID, "memory-view")))
    # A case was auto-selected on load, so leaving memory returns to its detail.
    assert _wait(browser).until(_is_shown(browser, "detail"))


def test_memory_recall_probe_reports_latency_and_truncation(browser) -> None:
    """The probe is the dashboard's read on section 9's recall observability.

    It runs the real endpoint a run uses, so the assertion is on the two numbers
    a hit list cannot supply: ``latency_ms`` and ``truncated``. Whether the query
    matches anything depends on the seeded corpus, so the test asserts the report
    rendered rather than a particular hit count.
    """
    browser.open("/")
    _wait(browser).until(EC.element_to_be_clickable((By.ID, "memory-open"))).click()
    _wait(browser).until(_is_shown(browser, "memory-view"))

    # Collapsed by default: a query tool nobody asked for should not push the
    # memory table below the fold.
    assert not browser.driver.find_element(By.ID, "memory-probe").is_displayed()
    _wait(browser).until(EC.element_to_be_clickable((By.ID, "memory-probe-toggle"))).click()
    assert _wait(browser).until(_is_shown(browser, "memory-probe"))

    # An empty query is refused in place rather than sent as a match-everything.
    browser.driver.find_element(By.ID, "memory-probe-run").click()
    result = browser.driver.find_element(By.ID, "memory-probe-result")
    _wait(browser, 10).until(lambda d: "先输入一个查询" in (result.text or ""))

    browser.driver.find_element(By.ID, "memory-probe-query").send_keys("冷启动白屏")
    browser.driver.find_element(By.ID, "memory-probe-run").click()
    _wait(browser, 30).until(lambda d: "耗时" in (result.text or ""))

    text = result.text or ""
    assert "ms" in text, f"probe did not report latency_ms: {text}"
    assert ("已截断" in text) or ("未截断" in text), f"probe did not report truncated: {text}"
    assert "命中" in text
    browser.screenshot("memory-recall-probe")

    # Toggling again hides it without losing the rendered report underneath.
    browser.driver.find_element(By.ID, "memory-probe-toggle").click()
    _wait(browser).until(EC.invisibility_of_element_located((By.ID, "memory-probe")))


def test_primary_flow_emits_no_console_errors(browser) -> None:
    browser.open("/")
    _wait(browser).until(EC.element_to_be_clickable((By.CSS_SELECTOR, CARD))).click()
    _wait(browser).until(_is_shown(browser, "detail"))

    for tab in TABS:
        browser.driver.find_element(By.CSS_SELECTOR, f'#tabs .tab[data-tab="{tab}"]').click()

    browser.driver.find_element(By.ID, "memory-open").click()
    _wait(browser).until(_is_shown(browser, "memory-view"))

    shot = browser.screenshot("full-flow")
    severe = [
        entry
        for entry in browser.console_messages("SEVERE")
        if not any(skip in str(entry.get("message", "")) for skip in _IGNORED_CONSOLE_TEXT)
    ]
    assert not severe, (
        f"浏览器控制台出现 SEVERE 日志（截图 {shot}）: "
        + "; ".join(str(entry.get("message", "")) for entry in severe)
    )


@pytest.mark.parametrize(
    ("width", "height", "label"),
    [(1440, 900, "desktop"), (390, 844, "mobile")],
)
def test_layout_has_no_horizontal_overflow(browser, width, height, label) -> None:
    browser.driver.set_window_size(width, height)
    browser.open("/")
    _wait(browser).until(EC.presence_of_all_elements_located((By.CSS_SELECTOR, CARD)))

    metrics = browser.driver.execute_script(
        "return {sw: document.documentElement.scrollWidth,"
        " cw: document.documentElement.clientWidth};"
    )
    shot = browser.screenshot(f"layout-{label}")
    assert metrics["sw"] <= metrics["cw"] + 1, (
        f"{label} 视口 ({width}x{height}) 出现横向溢出 {metrics}，截图 {shot}"
    )
    assert browser.driver.find_element(By.ID, "search").is_displayed()
