# chatgpt_web.py -- ChatGPT browser-automation responder
# description: OAR responder for chatgpt.com with isolated auth, explicit GPT-5.5/high-thinking selection, verified attachments, send/cancel controls, and response extraction.
# Tags: responder, browser-automation, chatgpt, playwright, auth, attachments
# date: 2026-07-07

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Sequence

PLAYWRIGHT_INSTALL_MESSAGE = "Playwright is required for this browser-automation responder. Install with: pip install playwright && playwright install chromium"

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ImportError as exc:
    _PLAYWRIGHT_IMPORT_ERROR = exc

    class PlaywrightError(Exception):
        """Import-safe placeholder used only before Playwright is installed."""

    class PlaywrightTimeoutError(PlaywrightError):
        """Import-safe placeholder used only before Playwright is installed."""

    def sync_playwright() -> Any:
        raise RuntimeError(PLAYWRIGHT_INSTALL_MESSAGE) from _PLAYWRIGHT_IMPORT_ERROR


# ─── METADATA AND FEATURE MAP ─────────────────────────────────────────────

AGENT = {
    "name": "chatgpt-web",
    "role": "responder",
    "fanout": True,
    "requires_auth": True,
    "model": "gpt 5.5",
    "thinking": "high",
    "description": "Browser automation responder for chatgpt.com using OAR's isolated auth profile.",
    "capabilities": {
        "text_prompt": True,
        "attachments": True,
        "prompt_bar": True,
        "send": True,
        "cancel": True,
        "message_sent": True,
        "message_received": True,
        "model_selection": True,
        "thinking_level": True,
        "deep_research_tab": True,
        "agent_mode_tab": True,
        "persistent_session": True,
        "isolated_browser_profile": True,
        "uses_real_browser_ui": True,
        "reads_user_browser_profile": False,
        "copies_cookies": False,
    },
}

FEATURES = {
    "auth_session": "Authenticated app shell, account controls, and prompt bar are all required.",
    "model": "Select GPT-5.5 before sending.",
    "thinking_level": "Select high thinking before sending.",
    "attachments": "Upload every OAR attachment and wait until upload activity is gone.",
    "prompt_bar": "Find an editable composer and write the exact prompt.",
    "send": "Click a visible enabled send control.",
    "cancel": "Map the stop/cancel control so running generations can be interrupted.",
    "message_sent": "Count assistant messages before send to detect the new turn.",
    "message_received": "Read the latest assistant message after the stream is stable.",
    "deep_research_tab": "Map the research entry point without enabling it by default.",
    "agent_mode_tab": "Map the agent-mode entry point without enabling it by default.",
}

CHATGPT_URL = "https://chatgpt.com/"
TARGET_MODEL_LABELS = ("GPT-5.5", "GPT 5.5", "gpt-5.5", "gpt 5.5")
TARGET_THINKING_LABELS = ("High", "high")
INTELLIGENCE_BUTTON_LABELS = ("High", "Medium", "Instant")
INTELLIGENCE_MENU_SELECTOR = '[data-testid="composer-intelligence-picker-content"]'
DEFAULT_RESPONSE_TIMEOUT_MS = int(os.environ.get("OAR_CHATGPT_RESPONSE_TIMEOUT_MS", str(24 * 60 * 60 * 1000)))
DEFAULT_NAVIGATION_TIMEOUT_MS = int(os.environ.get("OAR_CHATGPT_NAVIGATION_TIMEOUT_MS", "60000"))
DEFAULT_BROWSER_START_TIMEOUT_MS = int(os.environ.get("OAR_CHATGPT_BROWSER_START_TIMEOUT_MS", "20000"))
DEFAULT_BROWSER_EXIT_TIMEOUT_MS = int(os.environ.get("OAR_CHATGPT_BROWSER_EXIT_TIMEOUT_MS", "8000"))
STABLE_RESPONSE_MS = int(os.environ.get("OAR_CHATGPT_STABLE_RESPONSE_MS", "500"))
ATTACHMENT_VERIFY_TIMEOUT_MS = int(os.environ.get("OAR_CHATGPT_ATTACHMENT_VERIFY_TIMEOUT_MS", "30000"))
SEND_CONFIRM_TIMEOUT_MS = int(os.environ.get("OAR_CHATGPT_SEND_CONFIRM_TIMEOUT_MS", "10000"))
CRITICAL_ERROR_PREFIX = "CRITICAL:"
UPLOAD_ERROR_SIGNAL_TERMS = (
    "failed",
    "error",
    "unsupported",
    "too large",
    "couldn't",
    "could not",
    "try again",
    "not allowed",
    "blocked",
)
UPLOAD_ERROR_CONTEXT_TERMS = ("upload", "file", "attachment", "attach")


# ─── SELECTOR MAP ─────────────────────────────────────────────────────────

BROWSER_COMMAND_CANDIDATES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "microsoft-edge-stable",
    "microsoft-edge",
    "brave-browser",
    "brave-browser-beta",
)

COMPOSER_SELECTORS = (
    '[data-testid="composer-input"]',
    "#prompt-textarea",
    'textarea[placeholder*="Message" i]',
    'textarea[aria-label*="Message" i]',
    'div[contenteditable="true"][data-placeholder]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
)

AUTHENTICATED_SELECTORS = (
    '[data-testid="profile-button"]',
    '[data-testid="accounts-menu-button"]',
    'button[aria-label*="account" i]',
    'button[aria-label*="profile" i]',
    'button[aria-label*="settings" i]',
    'a[href="/gpts"]',
    'a[href="/library"]',
)

LOGIN_SELECTORS = (
    'a[href*="/auth/login"]',
    'button:has-text("Log in")',
    'button:has-text("Sign up")',
)

INTELLIGENCE_CONTROL_SELECTORS = (
    'main button.__composer-pill[aria-expanded]:has-text("High")',
    'main button.__composer-pill[aria-expanded]:has-text("Medium")',
    'main button.__composer-pill[aria-expanded]:has-text("Instant")',
    'main button[aria-expanded]:has-text("High")',
    'main button[aria-expanded]:has-text("Medium")',
    'main button[aria-expanded]:has-text("Instant")',
)

INTELLIGENCE_MENU_SELECTORS = (
    INTELLIGENCE_MENU_SELECTOR,
)

MODEL_CONTROL_SELECTORS = (
    *INTELLIGENCE_CONTROL_SELECTORS,
    'main [data-testid="model-switcher-dropdown-button"]',
    'header [data-testid="model-switcher-dropdown-button"]',
    '[data-testid="model-switcher-dropdown-button"]',
    'main button[aria-label*="model selector" i]',
    'main button[aria-label*="switch model" i]',
    'main button[aria-label*="select model" i]',
    'header button[aria-label*="model selector" i]',
    'header button[aria-label*="switch model" i]',
    'header button[aria-label*="select model" i]',
    'main button:has-text("ChatGPT")',
    'main button:has-text("GPT")',
    'header button:has-text("ChatGPT")',
    'header button:has-text("GPT")',
)

THINKING_CONTROL_SELECTORS = (
    *INTELLIGENCE_CONTROL_SELECTORS,
    'main [data-testid*="thinking" i]',
    'main [data-testid*="reason" i]',
    'main button[aria-label*="thinking" i]',
    'main button[aria-label*="reasoning" i]',
    'main button:has-text("Thinking")',
    'main button:has-text("Reasoning")',
    'header [data-testid*="thinking" i]',
    'header [data-testid*="reason" i]',
    'header button[aria-label*="thinking" i]',
    'header button[aria-label*="reasoning" i]',
    'header button:has-text("Thinking")',
    'header button:has-text("Reasoning")',
)

SEND_BUTTON_SELECTORS = (
    '[data-testid="send-button"]',
    '[data-testid="composer-send-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label*="Send" i]',
)

CANCEL_BUTTON_SELECTORS = (
    '[data-testid="stop-button"]',
    '[data-testid*="stop" i]',
    'button[aria-label*="Stop" i]',
    'button[aria-label*="Cancel" i]',
)

ATTACH_INPUT_SELECTORS = (
    'input[type="file"]',
    'input[data-testid*="file" i]',
)

ASSISTANT_SELECTORS = (
    '[data-message-author-role="assistant"]',
    '[data-testid*="conversation-turn"]:has([data-message-author-role="assistant"])',
    'article:has([data-message-author-role="assistant"])',
)

USER_MESSAGE_SELECTORS = (
    '[data-message-author-role="user"]',
    '[data-testid*="conversation-turn"]:has([data-message-author-role="user"])',
    'article:has([data-message-author-role="user"])',
)

DEEP_RESEARCH_SELECTORS = (
    'button:has-text("Deep research")',
    'a:has-text("Deep research")',
    '[data-testid*="deep-research" i]',
    '[aria-label*="deep research" i]',
)

AGENT_MODE_SELECTORS = (
    'button:has-text("Agent mode")',
    'a:has-text("Agent mode")',
    '[data-testid*="agent-mode" i]',
    '[aria-label*="agent mode" i]',
)

TRANSIENT_DIALOG_BUTTONS = (
    "Continue",
    "Got it",
    "OK",
    "Okay",
    "Not now",
    "Maybe later",
)

SELECTORS = {
    "auth": AUTHENTICATED_SELECTORS,
    "login": LOGIN_SELECTORS,
    "intelligence_picker": INTELLIGENCE_CONTROL_SELECTORS + INTELLIGENCE_MENU_SELECTORS,
    "model": MODEL_CONTROL_SELECTORS,
    "thinking": THINKING_CONTROL_SELECTORS,
    "attachments": ATTACH_INPUT_SELECTORS,
    "prompt_bar": COMPOSER_SELECTORS,
    "send": SEND_BUTTON_SELECTORS,
    "cancel": CANCEL_BUTTON_SELECTORS,
    "message_received": ASSISTANT_SELECTORS,
    "deep_research_tab": DEEP_RESEARCH_SELECTORS,
    "agent_mode_tab": AGENT_MODE_SELECTORS,
}

ACTIONS = {
    "select_model": "open model picker, choose GPT-5.5, verify selected state",
    "select_thinking_level": "open thinking/reasoning picker, choose high, verify selected state",
    "add_attachments": "set all attachment files on the upload input and wait for upload idle",
    "send": "fill prompt bar and activate the send button",
    "cancel": "click stop/cancel when a generation is active",
    "read_response": "wait for new assistant output to become non-empty and stable",
}


# ─── OAR AUTH CONTRACT ────────────────────────────────────────────────────

def login_url() -> str:
    return CHATGPT_URL


def auth_check(page: Any) -> bool:
    try:
        if first_visible(page, LOGIN_SELECTORS, timeout_ms=500) is not None:
            return False
        has_composer = first_visible(page, COMPOSER_SELECTORS, timeout_ms=1000) is not None
        has_account = first_visible(page, AUTHENTICATED_SELECTORS, timeout_ms=1000) is not None
        return has_composer and has_account
    except PlaywrightError:
        return False


def browser_launch_options() -> dict[str, Any]:
    options: dict[str, Any] = {
        "mode": "system-cdp",
        "browser": "chromium",
        "force_headed": os.environ.get("OAR_CHATGPT_FORCE_HEADED", "0") == "1",
        "headless_strategy": os.environ.get("OAR_CHATGPT_HEADLESS_STRATEGY", "hidden-window").strip() or "hidden-window",
        "viewport": {"width": 1440, "height": 1000},
    }
    executable = os.environ.get("OAR_CHATGPT_BROWSER_EXECUTABLE", "").strip()
    if executable:
        options["executable_path"] = executable
    return options


# ─── RESPONDER ENTRYPOINT ─────────────────────────────────────────────────

def run(request: dict[str, Any]) -> dict[str, Any]:
    prompt = request_prompt(request)
    profile_dir = auth_profile_dir(request)
    attachment_paths = attachment_files(request)
    launch_options = browser_launch_options()
    headless = os.environ.get("OAR_CHATGPT_HEADLESS", "1") != "0" and not launch_options.get("force_headed", False)

    with sync_playwright() as engine:
        with browser_context(engine, profile_dir, launch_options, headless=headless) as context:
            page = context.pages[0] if context.pages else context.new_page()
            apply_viewport(page, launch_options)
            page.set_default_timeout(DEFAULT_NAVIGATION_TIMEOUT_MS)
            page.goto(CHATGPT_URL, wait_until="domcontentloaded")
            reject_blocking_interstitials(page)
            if not wait_for_authenticated_state(page, DEFAULT_NAVIGATION_TIMEOUT_MS):
                raise RuntimeError("ChatGPT session is missing or expired. Run `oar --init-auth` for this responder.")
            dismiss_transient_dialogs(page)
            selected_state = select_runtime_state(page)
            upload_attachments(page, attachment_paths)
            previous_snapshot = assistant_message_snapshot(page)
            previous_user_snapshot = user_message_snapshot(page)
            fill_composer(page, prompt)
            click_send(page)
            wait_for_message_sent(page, previous_user_snapshot)
            content = wait_for_response(page, previous_snapshot)
            return {
                "content": content,
                "metadata": {
                    "service": "chatgpt.com",
                    "agent": AGENT["name"],
                    "model": "gpt 5.5",
                    "thinking": "high",
                    "selection": selected_state,
                    "response_chars": len(content),
                    "attachments": len(attachment_paths),
                    "headless": headless,
                },
            }


# ─── REQUEST AND ATTACHMENTS ──────────────────────────────────────────────

def request_prompt(request: dict[str, Any]) -> str:
    prompt = str(request.get("prompt", "")).strip()
    if not prompt:
        raise RuntimeError("ChatGPT responder received an empty prompt")
    return prompt


def auth_profile_dir(request: dict[str, Any]) -> Path:
    profile = str((request.get("agent") or {}).get("auth_profile_dir") or "").strip()
    if not profile:
        raise RuntimeError("OAR did not provide an auth profile path. Update OAR before using this responder.")
    path = Path(profile).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def attachment_files(request: dict[str, Any]) -> list[Path]:
    files: list[Path] = []
    for item in request.get("attachments", []):
        if not isinstance(item, dict):
            continue
        value = str(item.get("path", "")).strip()
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise RuntimeError(f"Attachment is no longer available: {path}")
        files.append(path)
    return files


def wait_for_authenticated_state(page: Any, timeout_ms: int) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if auth_check(page):
            return True
        try:
            page.wait_for_timeout(250)
        except PlaywrightError:
            return False
    return auth_check(page)


# ─── BROWSER LAUNCH ───────────────────────────────────────────────────────

@contextlib.contextmanager
def browser_context(engine: Any, profile_dir: Path, options: dict[str, Any], headless: bool) -> Any:
    if options.get("mode") == "system-cdp":
        with system_cdp_context(engine, profile_dir, options, headless=headless) as context:
            yield context
        return

    context = engine.chromium.launch_persistent_context(
        str(profile_dir),
        headless=headless,
        accept_downloads=False,
        viewport=options.get("viewport") or {"width": 1440, "height": 1000},
    )
    try:
        yield context
    finally:
        context.close()


@contextlib.contextmanager
def system_cdp_context(engine: Any, profile_dir: Path, options: dict[str, Any], headless: bool) -> Any:
    executable = resolve_browser_executable(options)
    port = find_open_port()
    strategy = str(options.get("headless_strategy") or "true-headless")
    hidden_window = bool(headless and strategy == "hidden-window")
    command = [
        executable,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless and not hidden_window:
        command.append("--headless=new")
    if hidden_window:
        viewport = options.get("viewport") if isinstance(options.get("viewport"), dict) else {}
        width = int(viewport.get("width", 1440))
        height = int(viewport.get("height", 1000))
        command.extend(["--window-position=-32000,-32000", f"--window-size={width},{height}"])
    command.extend(options.get("args", []))
    command.append("about:blank")

    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    browser = None
    try:
        endpoint = wait_for_cdp_endpoint(port, DEFAULT_BROWSER_START_TIMEOUT_MS / 1000)
        browser = engine.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        yield context
    finally:
        if browser is not None:
            close_cdp_browser(browser)
        if not wait_for_browser_process_exit(process, DEFAULT_BROWSER_EXIT_TIMEOUT_MS / 1000):
            terminate_browser_process(process)


def resolve_browser_executable(options: dict[str, Any]) -> str:
    explicit = str(options.get("executable_path") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        found = shutil.which(explicit)
        if found:
            return found
        raise RuntimeError(f"Browser executable was not found: {explicit}")

    for command in BROWSER_COMMAND_CANDIDATES:
        found = shutil.which(command)
        if found:
            return found
    raise RuntimeError("No Chrome-compatible browser was found. Install Chrome, Chromium, Edge, or Brave, or set OAR_CHATGPT_BROWSER_EXECUTABLE.")


def find_open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_cdp_endpoint(port: int, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    last_error = "not ready"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.25) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("Browser"):
                return f"http://127.0.0.1:{port}"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.05)
    raise RuntimeError(f"Browser CDP endpoint did not become ready: {last_error}")


def terminate_browser_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            process.wait(timeout=5)


def wait_for_browser_process_exit(process: subprocess.Popen[Any], timeout: float) -> bool:
    if process.poll() is not None:
        return True
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return process.poll() is not None


def close_cdp_browser(browser: Any) -> None:
    try:
        session = browser.new_browser_cdp_session()
        try:
            session.send("Browser.close")
        finally:
            with contextlib.suppress(Exception):
                session.detach()
        return
    except Exception:
        with contextlib.suppress(Exception):
            browser.close()


def apply_viewport(page: Any, options: dict[str, Any]) -> None:
    viewport = options.get("viewport") or {"width": 1440, "height": 1000}
    with contextlib.suppress(PlaywrightError):
        page.set_viewport_size(viewport)


# ─── PAGE STATE AND FEATURE DETECTION ─────────────────────────────────────

def first_visible(page: Any, selectors: Iterable[str], timeout_ms: int) -> Any | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.is_visible(timeout=timeout_ms):
                return locator
        except PlaywrightError:
            continue
    return None


def first_visible_near_composer(page: Any, selectors: Iterable[str], timeout_ms: int) -> Any | None:
    deadline = time.monotonic() + (timeout_ms / 1000)
    fallback = None
    selector_list = tuple(selectors)
    while True:
        composer_box = visible_box(first_visible(page, COMPOSER_SELECTORS, timeout_ms=100))
        for selector in selector_list:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 20)
            except PlaywrightError:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if not candidate.is_visible(timeout=100):
                        continue
                except PlaywrightError:
                    continue
                if composer_box is None and fallback is None:
                    fallback = candidate
                candidate_box = visible_box(candidate)
                if composer_box is None or candidate_box is None:
                    continue
                if boxes_share_composer_band(composer_box, candidate_box):
                    return candidate
        if time.monotonic() >= deadline:
            return fallback
        page.wait_for_timeout(100)


def visible_box(locator: Any | None) -> dict[str, float] | None:
    if locator is None:
        return None
    try:
        box = locator.bounding_box(timeout=1000)
    except PlaywrightError:
        return None
    if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        return None
    return box


def boxes_share_composer_band(composer: dict[str, float], candidate: dict[str, float]) -> bool:
    composer_left = composer["x"] - 80
    composer_right = composer["x"] + composer["width"] + 160
    composer_top = composer["y"] - 80
    composer_bottom = composer["y"] + composer["height"] + 100
    candidate_mid_x = candidate["x"] + (candidate["width"] / 2)
    candidate_mid_y = candidate["y"] + (candidate["height"] / 2)
    return composer_left <= candidate_mid_x <= composer_right and composer_top <= candidate_mid_y <= composer_bottom


def all_option_selectors(labels: Sequence[str]) -> tuple[str, ...]:
    selectors: list[str] = []
    for label in labels:
        quoted = css_text(label)
        selectors.extend(
            [
                f'button:has-text({quoted})',
                f'[role="option"]:has-text({quoted})',
                f'[role="menuitem"]:has-text({quoted})',
                f'[data-testid*="model" i]:has-text({quoted})',
                f'[data-testid*="option" i]:has-text({quoted})',
                f'li:has-text({quoted})',
            ]
        )
    return tuple(selectors)


def css_text(value: str) -> str:
    return json.dumps(value)


def normalized_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def text_matches_any(text: str, labels: Sequence[str]) -> bool:
    normalized = normalized_label(text)
    return any(normalized_label(label) in normalized for label in labels)


def locator_text(locator: Any) -> str:
    try:
        return str(locator.inner_text(timeout=1000)).strip()
    except PlaywrightError:
        return ""


def control_text_matches(page: Any, selectors: Sequence[str], labels: Sequence[str]) -> bool:
    control = first_visible(page, selectors, timeout_ms=250)
    return bool(control and text_matches_any(locator_text(control), labels))


def wait_until(page: Any, predicate: Any, timeout_ms: int, failure: str) -> None:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    if predicate():
        return
    raise RuntimeError(failure)


def reject_blocking_interstitials(page: Any) -> None:
    text = ""
    try:
        text = page.locator("body").inner_text(timeout=2000).lower()
    except PlaywrightError:
        return
    blocked_markers = (
        "checking your browser",
        "verify you are human",
        "enable javascript and cookies",
        "unusual activity",
        "temporarily unavailable",
    )
    if any(marker in text for marker in blocked_markers):
        raise RuntimeError("ChatGPT is showing a browser verification or access interstitial. Complete it with `oar --init-auth`.")


def dismiss_transient_dialogs(page: Any) -> None:
    for label in TRANSIENT_DIALOG_BUTTONS:
        button = page.get_by_role("button", name=label, exact=True)
        try:
            if button.first.is_visible(timeout=500):
                button.first.click()
        except PlaywrightError:
            continue


def mapped_feature_state(page: Any) -> dict[str, bool]:
    return {
        "deep_research_tab": first_visible(page, DEEP_RESEARCH_SELECTORS, timeout_ms=250) is not None,
        "agent_mode_tab": first_visible(page, AGENT_MODE_SELECTORS, timeout_ms=250) is not None,
        "cancel_control": first_visible(page, CANCEL_BUTTON_SELECTORS, timeout_ms=250) is not None,
    }


# ─── MODEL, MODES, AND TABS ───────────────────────────────────────────────

def select_runtime_state(page: Any) -> dict[str, Any]:
    state = select_intelligence_state(page)
    state.update(mapped_feature_state(page))
    return state


def select_intelligence_state(page: Any) -> dict[str, str]:
    open_intelligence_picker(page)
    try:
        return {
            "model": verify_intelligence_model(page),
            "thinking": ensure_intelligence_thinking(page),
        }
    finally:
        close_intelligence_picker(page)


def open_intelligence_picker(page: Any) -> Any:
    menu = first_visible(page, INTELLIGENCE_MENU_SELECTORS, timeout_ms=250)
    if menu is not None:
        return menu

    control = first_visible_near_composer(page, INTELLIGENCE_CONTROL_SELECTORS, timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS)
    if control is None:
        raise mode_unavailable_error(page, INTELLIGENCE_BUTTON_LABELS, "ChatGPT intelligence picker", "control was not found")
    control.click()

    menu = first_visible(page, INTELLIGENCE_MENU_SELECTORS, timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS)
    if menu is None:
        raise mode_unavailable_error(page, INTELLIGENCE_BUTTON_LABELS, "ChatGPT intelligence picker", "menu did not open")
    return menu


def verify_intelligence_model(page: Any) -> str:
    option = scoped_intelligence_option(page, TARGET_MODEL_LABELS, ("menuitem", "menuitemradio", "option"))
    if option is None:
        raise mode_unavailable_error(page, TARGET_MODEL_LABELS, "ChatGPT model", "option was not available")
    return "selected"


def ensure_intelligence_thinking(page: Any) -> str:
    option = scoped_intelligence_option(page, TARGET_THINKING_LABELS, ("menuitemradio", "menuitem", "option"))
    if option is None:
        raise mode_unavailable_error(page, TARGET_THINKING_LABELS, "ChatGPT thinking level", "option was not available")
    if locator_attribute(option, "aria-checked") == "true":
        return "already selected"

    option.click()
    wait_until(
        page,
        lambda: intelligence_control_text_matches(page, TARGET_THINKING_LABELS),
        DEFAULT_NAVIGATION_TIMEOUT_MS,
        "ChatGPT thinking level was clicked but selected state could not be verified.",
    )
    return "selected"


def close_intelligence_picker(page: Any) -> None:
    if first_visible(page, INTELLIGENCE_MENU_SELECTORS, timeout_ms=250) is None:
        return
    with contextlib.suppress(PlaywrightError):
        page.keyboard.press("Escape")


def scoped_intelligence_option(page: Any, labels: Sequence[str], roles: Sequence[str]) -> Any | None:
    selectors = scoped_intelligence_option_selectors(labels, roles)
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 20)
        except PlaywrightError:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible(timeout=250) and text_matches_any(locator_text(candidate), labels):
                    return candidate
            except PlaywrightError:
                continue
    return None


def scoped_intelligence_option_selectors(labels: Sequence[str], roles: Sequence[str]) -> tuple[str, ...]:
    selectors: list[str] = []
    for label in labels:
        quoted = css_text(label)
        for role in roles:
            selectors.append(f'{INTELLIGENCE_MENU_SELECTOR} [role="{role}"]:has-text({quoted})')
        selectors.append(f'{INTELLIGENCE_MENU_SELECTOR} button:has-text({quoted})')
    return tuple(selectors)


def locator_attribute(locator: Any, name: str) -> str:
    try:
        return str(locator.get_attribute(name) or "")
    except PlaywrightError:
        return ""


def intelligence_control_text_matches(page: Any, labels: Sequence[str]) -> bool:
    control = first_visible_near_composer(page, INTELLIGENCE_CONTROL_SELECTORS, timeout_ms=250)
    return bool(control and text_matches_any(locator_text(control), labels))


def select_named_mode(page: Any, control_selectors: Sequence[str], labels: Sequence[str], feature_name: str) -> str:
    if control_text_matches(page, control_selectors, labels):
        return "already selected"

    control = first_visible(page, control_selectors, timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS)
    if control is None:
        raise mode_unavailable_error(page, labels, feature_name, "control was not found")
    control.click()

    option = first_visible(page, all_option_selectors(labels), timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS)
    if option is None:
        raise mode_unavailable_error(page, labels, feature_name, "option was not available")
    option.click()

    wait_until(
        page,
        lambda: control_text_matches(page, control_selectors, labels),
        DEFAULT_NAVIGATION_TIMEOUT_MS,
        f"{feature_name} was clicked but selected state could not be verified.",
    )
    return "selected"


def mode_unavailable_error(page: Any, labels: Sequence[str], feature_name: str, reason: str) -> RuntimeError:
    detail = f"{feature_name} {reason}: {', '.join(labels)}."
    if hidden_label_present(page, labels):
        detail += " The target label exists only in hidden page data and is not exposed as a selectable UI control for this session."
    return RuntimeError(detail)


def hidden_label_present(page: Any, labels: Sequence[str]) -> bool:
    try:
        html = str(page.locator("html").evaluate("element => element.innerHTML"))
    except PlaywrightError:
        return False
    normalized = normalized_label(html)
    return any(normalized_label(label) in normalized for label in labels)


def cancel_generation(page: Any) -> bool:
    button = first_visible(page, CANCEL_BUTTON_SELECTORS, timeout_ms=1000)
    if button is None:
        return False
    button.click()
    return True


# ─── PROMPT BAR, ATTACHMENTS, AND SEND ────────────────────────────────────

def upload_attachments(page: Any, paths: list[Path]) -> None:
    if not paths:
        return
    for selector in ATTACH_INPUT_SELECTORS:
        locator = page.locator(selector).first
        try:
            if locator.count() > 0:
                locator.set_input_files([str(path) for path in paths])
                wait_for_uploads_to_settle(page)
                verify_attachments_attached(page, paths)
                return
        except PlaywrightError:
            continue
    raise RuntimeError(f"{CRITICAL_ERROR_PREFIX} ChatGPT file upload input was not available for the current account or page state.")


def wait_for_uploads_to_settle(page: Any) -> None:
    try:
        page.wait_for_function(
            """() => {
                const busy = document.querySelector('[aria-busy="true"], [data-testid*="uploading" i], [data-testid*="progress" i]');
                return !busy;
            }""",
            timeout=ATTACHMENT_VERIFY_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"{CRITICAL_ERROR_PREFIX} Timed out waiting for ChatGPT attachment upload to finish") from exc


def verify_attachments_attached(page: Any, paths: list[Path]) -> None:
    deadline = time.monotonic() + (ATTACHMENT_VERIFY_TIMEOUT_MS / 1000)
    last_error = ""
    missing = [path.name for path in paths]
    while time.monotonic() < deadline:
        text = visible_upload_state_text(page)
        last_error = upload_error_from_text(text)
        if last_error:
            raise RuntimeError(f"{CRITICAL_ERROR_PREFIX} ChatGPT attachment upload failed: {last_error}")
        missing = attachment_names_missing(text, paths)
        if not missing:
            return
        page.wait_for_timeout(100)
    joined = ", ".join(missing)
    raise RuntimeError(f"{CRITICAL_ERROR_PREFIX} ChatGPT attachment upload was not confirmed before send: {joined}")


def visible_upload_state_text(page: Any) -> str:
    try:
        return str(page.evaluate(
            """() => {
                const selectors = [
                    'form',
                    '[data-testid*="composer" i]',
                    '[data-testid*="upload" i]',
                    '[data-testid*="attachment" i]',
                    '[role="alert"]',
                    '[role="status"]',
                    '[aria-live]'
                ];
                const nodes = new Set();
                for (const selector of selectors) {
                    for (const node of document.querySelectorAll(selector)) {
                        nodes.add(node);
                    }
                }
                const composer = document.querySelector('#prompt-textarea, textarea, [contenteditable="true"]');
                let node = composer;
                let depth = 0;
                while (node && depth < 6) {
                    nodes.add(node);
                    node = node.parentElement;
                    depth += 1;
                }
                return Array.from(nodes)
                    .filter((node) => node && node.innerText)
                    .map((node) => node.innerText)
                    .join("\\n");
            }"""
        ))
    except PlaywrightError:
        return ""


def attachment_names_missing(visible_text: str, paths: list[Path]) -> list[str]:
    return [path.name for path in paths if path.name not in visible_text]


def upload_error_from_text(visible_text: str) -> str:
    for raw_line in visible_text.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        lower = line.casefold()
        if not line:
            continue
        if any(term in lower for term in UPLOAD_ERROR_SIGNAL_TERMS) and any(term in lower for term in UPLOAD_ERROR_CONTEXT_TERMS):
            return line[:240]
    return ""


def fill_composer(page: Any, prompt: str) -> None:
    composer = first_visible(page, COMPOSER_SELECTORS, timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS)
    if composer is None:
        raise RuntimeError("ChatGPT composer was not found. The session may be expired or the UI changed.")
    composer.click()
    tag_name = composer.evaluate("element => element.tagName.toLowerCase()")
    editable = composer.evaluate("element => element.isContentEditable")
    if tag_name == "textarea":
        composer.fill(prompt)
    elif editable:
        page.keyboard.insert_text(prompt)
    else:
        raise RuntimeError("ChatGPT composer is neither a textarea nor a contenteditable input.")


def click_send(page: Any) -> None:
    def send_enabled() -> bool:
        button = first_visible(page, SEND_BUTTON_SELECTORS, timeout_ms=250)
        if button is None:
            return False
        try:
            return button.is_enabled(timeout=250)
        except PlaywrightError:
            return False

    wait_until(page, send_enabled, DEFAULT_NAVIGATION_TIMEOUT_MS, "ChatGPT send button did not become enabled.")
    button = first_visible(page, SEND_BUTTON_SELECTORS, timeout_ms=1000)
    if button is None:
        raise RuntimeError("ChatGPT send button disappeared before click.")
    try:
        button.click()
    except PlaywrightError as exc:
        raise RuntimeError("ChatGPT send button was visible but could not be clicked.") from exc


def wait_for_message_sent(page: Any, previous_snapshot: Sequence[str]) -> None:
    selectors = ", ".join(USER_MESSAGE_SELECTORS)
    try:
        page.wait_for_function(
            """({selectors, previousSnapshot}) => {
                const texts = Array.from(document.querySelectorAll(selectors))
                    .map((node) => node.innerText ? node.innerText.trim() : "")
                    .filter((value) => value.length > 0);
                const remaining = new Map();
                for (const text of previousSnapshot) {
                    const normalized = String(text || "").trim();
                    if (normalized) remaining.set(normalized, (remaining.get(normalized) || 0) + 1);
                }
                for (const text of texts) {
                    const count = remaining.get(text) || 0;
                    if (count > 0) {
                        remaining.set(text, count - 1);
                    } else {
                        return true;
                    }
                }
                return false;
            }""",
            arg={"selectors": selectors, "previousSnapshot": list(previous_snapshot)},
            timeout=SEND_CONFIRM_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"{CRITICAL_ERROR_PREFIX} ChatGPT prompt was not confirmed as sent.") from exc


# ─── RESPONSE EXTRACTION ──────────────────────────────────────────────────

def assistant_message_count(page: Any) -> int:
    return len(assistant_message_snapshot(page))


def safe_count(page: Any, selector: str) -> int:
    try:
        return page.locator(selector).count()
    except PlaywrightError:
        return 0


def assistant_message_snapshot(page: Any) -> list[str]:
    return assistant_message_texts(page)


def user_message_snapshot(page: Any) -> list[str]:
    return message_texts(page, USER_MESSAGE_SELECTORS)


def assistant_message_texts(page: Any) -> list[str]:
    return message_texts(page, ASSISTANT_SELECTORS)


def message_texts(page: Any, selectors_source: Sequence[str]) -> list[str]:
    selectors = ", ".join(selectors_source)
    try:
        texts = page.evaluate(
            """(selectors) => Array.from(document.querySelectorAll(selectors))
                .map((node) => node.innerText ? node.innerText.trim() : "")
                .filter((text) => text.length > 0)""",
            selectors,
        )
        if isinstance(texts, list):
            return [str(text).strip() for text in texts if str(text).strip()]
    except PlaywrightError:
        pass

    collected: list[str] = []
    for selector in selectors_source:
        locator = page.locator(selector)
        try:
            count = locator.count()
            for index in range(count):
                text = locator.nth(index).inner_text(timeout=1000).strip()
                if text:
                    collected.append(text)
        except PlaywrightError:
            continue
    return collected


def assistant_texts_after_snapshot(current_texts: Sequence[str], previous_texts: Sequence[str]) -> list[str]:
    remaining: dict[str, int] = {}
    for text in previous_texts:
        normalized = str(text).strip()
        if normalized:
            remaining[normalized] = remaining.get(normalized, 0) + 1
    new_texts: list[str] = []
    for text in current_texts:
        normalized = str(text).strip()
        if not normalized:
            continue
        count = remaining.get(normalized, 0)
        if count > 0:
            remaining[normalized] = count - 1
        else:
            new_texts.append(normalized)
    return new_texts


def wait_for_response(page: Any, previous_snapshot: Sequence[str]) -> str:
    selectors = ", ".join(ASSISTANT_SELECTORS)
    stop_selectors = ", ".join(CANCEL_BUTTON_SELECTORS)
    try:
        page.wait_for_function(
            """({selectors, stopSelectors, previousSnapshot, stableMs}) => {
                const visible = (node) => {
                    const rect = node.getBoundingClientRect();
                    const style = window.getComputedStyle(node);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== "hidden" && style.display !== "none";
                };
                const disabled = (node) => node.matches(":disabled") || node.getAttribute("aria-disabled") === "true";
                const texts = Array.from(document.querySelectorAll(selectors))
                    .map((node) => node.innerText ? node.innerText.trim() : "")
                    .filter((value) => value.length > 0);
                const remaining = new Map();
                for (const text of previousSnapshot) {
                    const normalized = String(text || "").trim();
                    if (normalized) remaining.set(normalized, (remaining.get(normalized) || 0) + 1);
                }
                const newTexts = [];
                for (const text of texts) {
                    const count = remaining.get(text) || 0;
                    if (count > 0) {
                        remaining.set(text, count - 1);
                    } else {
                        newTexts.push(text);
                    }
                }
                const text = newTexts[newTexts.length - 1] || "";
                const visibleStop = Array.from(document.querySelectorAll(stopSelectors)).some((node) => visible(node) && !disabled(node));
                const visibleBusy = Array.from(document.querySelectorAll('[aria-busy="true"]')).some((node) => visible(node));
                const busy = visibleStop || visibleBusy;
                const state = window.__oarChatgptState || {text: "", since: Date.now()};
                if (text !== state.text) {
                    window.__oarChatgptState = {text, since: Date.now()};
                    return false;
                }
                window.__oarChatgptState = state;
                return newTexts.length > 0 && text.length > 0 && !busy && Date.now() - state.since >= stableMs;
            }""",
            arg={
                "selectors": selectors,
                "stopSelectors": stop_selectors,
                "previousSnapshot": list(previous_snapshot),
                "stableMs": STABLE_RESPONSE_MS,
            },
            timeout=DEFAULT_RESPONSE_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        reject_blocking_interstitials(page)
        raise RuntimeError("Timed out waiting for ChatGPT to finish responding.") from exc

    content = latest_new_assistant_text(page, previous_snapshot)
    if not content:
        raise RuntimeError("ChatGPT finished but no assistant response text could be extracted.")
    return content


def latest_new_assistant_text(page: Any, previous_snapshot: Sequence[str]) -> str:
    new_texts = assistant_texts_after_snapshot(assistant_message_texts(page), previous_snapshot)
    return new_texts[-1].strip() if new_texts else ""


def latest_assistant_text(page: Any) -> str:
    texts = assistant_message_texts(page)
    return texts[-1].strip() if texts else ""
