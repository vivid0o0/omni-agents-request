# glm_web.py -- GLM browser-automation responder
# description: OAR responder for GLM web chat with isolated auth, GLM-5.2 selection, deep-think/web-search toggles, verified attachments, and response extraction.
# Tags: responder, browser-automation, glm, playwright, auth, web-search, deep-think
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
    "name": "glm-web",
    "role": "responder",
    "fanout": True,
    "requires_auth": True,
    "model": "glm 5.2",
    "thinking": "deep think",
    "description": "Browser automation responder for GLM web chat using OAR's isolated auth profile.",
    "capabilities": {
        "text_prompt": True,
        "attachments": True,
        "prompt_bar": True,
        "send": True,
        "cancel": True,
        "message_sent": True,
        "message_received": True,
        "model_selection": True,
        "deep_think": True,
        "web_search": True,
        "persistent_session": True,
        "isolated_browser_profile": True,
        "uses_real_browser_ui": True,
        "reads_user_browser_profile": False,
        "copies_cookies": False,
    },
}

FEATURES = {
    "auth_session": "Authenticated app shell, account controls, and prompt bar are all required.",
    "model": "Select GLM-5.2 before sending.",
    "deep_think": "Enable deep-think mode before sending.",
    "web_search": "Enable web-search mode before sending.",
    "attachments": "Upload every OAR attachment and wait until upload activity is gone.",
    "prompt_bar": "Find an editable composer and write the exact prompt.",
    "send": "Click a visible enabled send control.",
    "cancel": "Map the stop/cancel control so running generations can be interrupted.",
    "message_sent": "Count assistant messages before send to detect the new turn.",
    "message_received": "Read the latest assistant message after the stream is stable.",
}

GLM_URL = "https://chat.z.ai/"
GLM_CONVERSATION_PATH_RE = re.compile(r"/c/[^/?#]+")
TARGET_MODEL_LABELS = ("GLM-5.2", "GLM 5.2", "glm-5.2", "glm 5.2")
TARGET_DEEP_THINK_VARIANTS = ("Max",)
WEB_SEARCH_SIGNAL_TERMS = ("web", "search", "browse", "internet")
DEFAULT_RESPONSE_TIMEOUT_MS = int(os.environ.get("OAR_GLM_RESPONSE_TIMEOUT_MS", str(24 * 60 * 60 * 1000)))
DEFAULT_NAVIGATION_TIMEOUT_MS = int(os.environ.get("OAR_GLM_NAVIGATION_TIMEOUT_MS", "60000"))
DEFAULT_BROWSER_START_TIMEOUT_MS = int(os.environ.get("OAR_GLM_BROWSER_START_TIMEOUT_MS", "20000"))
DEFAULT_BROWSER_EXIT_TIMEOUT_MS = int(os.environ.get("OAR_GLM_BROWSER_EXIT_TIMEOUT_MS", "8000"))
STABLE_RESPONSE_MS = int(os.environ.get("OAR_GLM_STABLE_RESPONSE_MS", "500"))
MAX_GENERATION_ATTEMPTS = max(1, int(os.environ.get("OAR_GLM_MAX_ATTEMPTS", "1")))
SERVICE_RETRY_BACKOFF_MS = max(0, int(os.environ.get("OAR_GLM_SERVICE_RETRY_BACKOFF_MS", "0")))
ATTACHMENT_VERIFY_TIMEOUT_MS = int(os.environ.get("OAR_GLM_ATTACHMENT_VERIFY_TIMEOUT_MS", "30000"))
SEND_CONFIRM_TIMEOUT_MS = int(os.environ.get("OAR_GLM_SEND_CONFIRM_TIMEOUT_MS", "10000"))
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
SERVICE_ERROR_PATTERNS = (
    re.compile(r"\bmodel\s+is\s+currently\s+at\s+capacity\b", re.IGNORECASE),
    re.compile(r"\btry\s+again\s+later\b", re.IGNORECASE),
    re.compile(r"\btoo\s+many\s+requests\b", re.IGNORECASE),
    re.compile(r"\brate[-\s]*limit(?:ed)?\b", re.IGNORECASE),
    re.compile(r"\btemporarily\s+unavailable\b", re.IGNORECASE),
    re.compile(r"\boverloaded\b", re.IGNORECASE),
    re.compile(r"\bservice\s+busy\b", re.IGNORECASE),
)
TRANSIENT_ASSISTANT_LINE_PATTERN = re.compile(
    r"^(?:thinking(?:[.．。…]+)?|skip|searching(?:[.．。…]+)?|generating(?:[.．。…]+)?|analyzing(?:[.．。…]+)?|reading(?:[.．。…]+)?|loading(?:[.．。…]+)?|please\s+wait(?:[.．。…]+)?)$",
    re.IGNORECASE,
)


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
    "#chat-input",
    '[data-testid*="chat-input" i]',
    'textarea[placeholder*="Message" i]',
    'textarea[placeholder*="Ask" i]',
    'textarea[aria-label*="Message" i]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
)

AUTHENTICATED_SELECTORS = (
    '[data-testid*="avatar" i]',
    '[data-testid*="profile" i]',
    'button[aria-label*="account" i]',
    'button[aria-label*="profile" i]',
    'button[aria-label*="user" i]',
    'a[href*="/user"]',
    'a[href*="/profile"]',
)

LOGIN_SELECTORS = (
    'button:has-text("Sign in")',
    'button:has-text("Log in")',
    'a:has-text("Sign in")',
    'a:has-text("Log in")',
)

NEW_CHAT_SELECTORS = (
    'button#sidebar-new-chat-button:has-text("New Chat")',
    '#sidebar-new-chat-button:has-text("New Chat")',
)

MODEL_CONTROL_SELECTORS = (
    '[data-testid*="model" i]',
    'button[aria-label*="model" i]',
    'button:has-text("GLM")',
    'button:has-text("Model")',
)

DEEP_THINK_SELECTORS = (
    'form [type="button"]:has-text("Deep Think")',
    'form [type="button"]:has-text("DeepThink")',
    'form div:has-text("Deep Think Max")',
    'form div:has-text("DeepThink Max")',
    '[data-testid*="deep" i]',
    '[data-testid*="think" i]',
    'button[aria-label*="deep" i]',
    'button[aria-label*="think" i]',
    'button:has-text("Deep Think")',
    'button:has-text("DeepThink")',
    'button:has-text("Think")',
)

WEB_SEARCH_SELECTORS = (
    'form button[type="button"][data-active]',
    '[data-testid*="web" i]',
    '[data-testid*="search" i]',
    'button[aria-label*="web" i]',
    'button[aria-label*="search" i]',
    'button:has-text("Web Search")',
    'button:has-text("Search")',
)

SEND_BUTTON_SELECTORS = (
    "#send-message-button",
    '[data-testid*="send" i]',
    'button[aria-label*="Send" i]',
)

CANCEL_BUTTON_SELECTORS = (
    '[data-testid*="stop" i]',
    '[data-testid*="cancel" i]',
    'button[aria-label*="Stop" i]',
    'button[aria-label*="Cancel" i]',
)

ATTACH_INPUT_SELECTORS = (
    'input[type="file"]',
    'input[data-testid*="file" i]',
    'input[accept]',
)

ASSISTANT_SELECTORS = (
    ".chat-assistant",
    '[class*="chat-assistant" i]',
    '[id^="message-"]:has(.chat-assistant)',
    '[data-role="assistant"]',
    '[data-message-author-role="assistant"]',
)

USER_MESSAGE_SELECTORS = (
    ".chat-user",
    ".user-message",
    '[id^="message-"]:has(.chat-user)',
)

TRANSIENT_DIALOG_BUTTONS = (
    "Continue",
    "Got it",
    "OK",
    "Okay",
    "Not now",
    "Maybe later",
    "I agree",
    "Accept",
    "Next",
    "Confirm",
    "Done",
    "Start",
)

BLOCKING_OVERLAY_SELECTORS = (
    '[data-dialog-overlay][data-state="open"]',
    '[class*="modal-overlay" i][data-state="open"]',
    '[class*="overlay" i][data-state="open"]',
)
BLOCKING_OVERLAY_VIEWPORT_COVERAGE = 0.60

TRANSIENT_SURFACE_SELECTORS = (
    "[data-dialog-content]",
    '[role="dialog"]',
    '._modal-content',
    '[class*="modal-content" i]',
    '[class*="dialog" i]',
)

SELECTORS = {
    "auth": AUTHENTICATED_SELECTORS,
    "login": LOGIN_SELECTORS,
    "model": MODEL_CONTROL_SELECTORS,
    "deep_think": DEEP_THINK_SELECTORS,
    "web_search": WEB_SEARCH_SELECTORS,
    "attachments": ATTACH_INPUT_SELECTORS,
    "prompt_bar": COMPOSER_SELECTORS,
    "send": SEND_BUTTON_SELECTORS,
    "cancel": CANCEL_BUTTON_SELECTORS,
    "message_received": ASSISTANT_SELECTORS,
}

ACTIONS = {
    "select_model": "open model picker, choose GLM-5.2, verify selected state",
    "enable_deep_think": "activate the deep-think toggle and verify enabled state",
    "enable_web_search": "activate the web-search toggle and verify enabled state",
    "add_attachments": "set all attachment files on the upload input and wait for upload idle",
    "send": "fill prompt bar and activate the send button",
    "cancel": "click stop/cancel when a generation is active",
    "read_response": "wait for new assistant output to become non-empty and stable",
}


# ─── OAR AUTH CONTRACT ────────────────────────────────────────────────────

def login_url() -> str:
    return GLM_URL


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
        "force_headed": os.environ.get("OAR_GLM_FORCE_HEADED", "0") == "1",
        "headless_strategy": os.environ.get("OAR_GLM_HEADLESS_STRATEGY", "true-headless").strip() or "true-headless",
        "viewport": {"width": 1440, "height": 1000},
    }
    executable = os.environ.get("OAR_GLM_BROWSER_EXECUTABLE", "").strip()
    if executable:
        options["executable_path"] = executable
    return options


# ─── RESPONDER ENTRYPOINT ─────────────────────────────────────────────────

def run(request: dict[str, Any]) -> dict[str, Any]:
    prompt = request_prompt(request)
    profile_dir = auth_profile_dir(request)
    attachment_paths = attachment_files(request)
    launch_options = browser_launch_options()
    headless = os.environ.get("OAR_GLM_HEADLESS", "1") != "0" and not launch_options.get("force_headed", False)

    with sync_playwright() as engine:
        with browser_context(engine, profile_dir, launch_options, headless=headless) as context:
            page = context.pages[0] if context.pages else context.new_page()
            apply_viewport(page, launch_options)
            page.set_default_timeout(DEFAULT_NAVIGATION_TIMEOUT_MS)
            page.goto(GLM_URL, wait_until="domcontentloaded")
            reject_blocking_interstitials(page)
            if not wait_for_authenticated_state(page, DEFAULT_NAVIGATION_TIMEOUT_MS):
                raise RuntimeError("GLM web session is missing or expired. Run `oar --init-auth` for this responder.")
            dismiss_transient_dialogs(page)
            selected_state: dict[str, Any] = {}
            content = ""
            last_service_error = ""
            for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
                dismiss_transient_dialogs(page)
                start_new_chat(page)
                dismiss_transient_dialogs(page)
                selected_state = select_runtime_state(page)
                upload_attachments(page, attachment_paths)
                previous_snapshot = assistant_message_snapshot(page)
                previous_user_snapshot = user_message_snapshot(page)
                fill_composer(page, prompt)
                click_send(page)
                wait_for_message_sent(page, previous_user_snapshot)
                content = wait_for_response(page, previous_snapshot)
                last_service_error = service_error_response(content)
                if not last_service_error:
                    break
                if attempt == MAX_GENERATION_ATTEMPTS:
                    raise RuntimeError(last_service_error)
                wait_after_service_error(page, attempt)
            return {
                "content": content,
                "metadata": {
                    "service": "chat.z.ai",
                    "agent": AGENT["name"],
                    "model": "glm 5.2",
                    "deep_think": True,
                    "web_search": True,
                    "selection": selected_state,
                    "attempts": attempt,
                    "response_chars": len(content),
                    "attachments": len(attachment_paths),
                    "headless": headless,
                },
            }


# ─── REQUEST AND ATTACHMENTS ──────────────────────────────────────────────

def request_prompt(request: dict[str, Any]) -> str:
    prompt = str(request.get("prompt", "")).strip()
    if not prompt:
        raise RuntimeError("GLM responder received an empty prompt")
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
    raise RuntimeError("No Chrome-compatible browser was found. Install Chrome, Chromium, Edge, or Brave, or set OAR_GLM_BROWSER_EXECUTABLE.")


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
    if locator is None:
        return ""
    try:
        return str(locator.inner_text(timeout=1000)).strip()
    except PlaywrightError:
        return ""


def control_text_matches(page: Any, selectors: Sequence[str], labels: Sequence[str]) -> bool:
    control = first_visible(page, selectors, timeout_ms=250)
    return bool(control and text_matches_any(locator_signal_text(control), labels))


def locator_signal_text(locator: Any) -> str:
    if locator is None:
        return ""
    values = [locator_text(locator)]
    for name in ("aria-label", "title", "id", "data-testid"):
        values.append(locator_attribute(locator, name))
    return " ".join(value for value in values if value)


def locator_attribute(locator: Any, name: str) -> str:
    if locator is None:
        return ""
    try:
        return str(locator.get_attribute(name) or "")
    except PlaywrightError:
        return ""


def semantic_toggle_on(locator: Any) -> bool:
    try:
        text = locator_text(locator).lower()
        state_values = [
            locator.get_attribute("aria-pressed", timeout=250),
            locator.get_attribute("aria-checked", timeout=250),
            locator.get_attribute("data-state", timeout=250),
            locator.get_attribute("data-active", timeout=250),
            locator.get_attribute("class", timeout=250),
        ]
    except PlaywrightError:
        return False

    joined = " ".join(str(value or "").lower() for value in state_values)
    if any(marker in joined for marker in ("true", "checked", "selected", "active", "on", "enabled")):
        return True
    if any(marker in joined for marker in ("false", "unchecked", "inactive", "off", "disabled")):
        return False
    if any(marker in text for marker in ("enabled", "on", "active", "selected")):
        return True
    if any(marker in text for marker in ("disabled", "off", "inactive")):
        return False
    return False


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
        raise RuntimeError("GLM web chat is showing a browser verification or access interstitial. Complete it with `oar --init-auth`.")


def service_error_response(content: str) -> str:
    text = str(content or "").strip()
    if not text:
        return ""
    for pattern in SERVICE_ERROR_PATTERNS:
        if pattern.search(text):
            return f"GLM returned a transient service error instead of an answer: {short_error_text(text)}"
    return ""


def short_error_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    return compact[:220]


def wait_after_service_error(page: Any, attempt: int) -> None:
    if SERVICE_RETRY_BACKOFF_MS <= 0:
        return
    page.wait_for_timeout(SERVICE_RETRY_BACKOFF_MS * attempt)


def dismiss_transient_dialogs(page: Any) -> None:
    deadline = time.monotonic() + 6
    quiet_since: float | None = None
    while time.monotonic() < deadline:
        clicked = False
        for label in TRANSIENT_DIALOG_BUTTONS:
            button = first_exact_transient_button(page, label, timeout_ms=250)
            if button is None:
                continue
            try:
                button.click(timeout=1000)
                page.wait_for_timeout(300)
                clicked = True
                break
            except PlaywrightError:
                continue
        if clicked:
            quiet_since = None
            continue
        if blocking_overlay_visible(page):
            closer = first_transient_close_button(page)
            if closer is not None:
                try:
                    closer.click(timeout=1000)
                    page.wait_for_timeout(300)
                    quiet_since = None
                    continue
                except PlaywrightError:
                    pass
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                if not blocking_overlay_visible(page):
                    quiet_since = None
                    continue
            except PlaywrightError:
                pass
        if transient_dialog_visible(page):
            quiet_since = None
            page.wait_for_timeout(150)
            continue
        if quiet_since is None:
            quiet_since = time.monotonic()
        if time.monotonic() - quiet_since >= 0.75:
            return
        page.wait_for_timeout(150)
    if blocking_overlay_visible(page):
        raise RuntimeError("GLM transient dialog could not be dismissed; a blocking overlay remains.")


def transient_dialog_button_selectors(label: str) -> tuple[str, ...]:
    quoted = css_text(label)
    return (
        f'button:has-text({quoted})',
        f'[role="button"]:has-text({quoted})',
    )


def transient_dialog_visible(page: Any) -> bool:
    if blocking_overlay_visible(page):
        return True
    for label in TRANSIENT_DIALOG_BUTTONS:
        if first_exact_transient_button(page, label, timeout_ms=50) is not None:
            return True
    return False


def blocking_overlay_visible(page: Any) -> bool:
    for selector in BLOCKING_OVERLAY_SELECTORS:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 10)
        except PlaywrightError:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if candidate.is_visible(timeout=100) and locator_covers_viewport(page, candidate, BLOCKING_OVERLAY_VIEWPORT_COVERAGE):
                    return True
            except PlaywrightError:
                continue
    return False


def locator_covers_viewport(page: Any, locator: Any, minimum_ratio: float) -> bool:
    box = visible_box(locator)
    if box is None:
        return False
    try:
        viewport = page.evaluate("() => ({width: window.innerWidth, height: window.innerHeight})")
    except PlaywrightError:
        return False
    width = float(viewport.get("width") or 0)
    height = float(viewport.get("height") or 0)
    if width <= 0 or height <= 0:
        return False
    return box["width"] >= width * minimum_ratio and box["height"] >= height * minimum_ratio


def first_exact_transient_button(page: Any, label: str, timeout_ms: int) -> Any | None:
    overlay_open = blocking_overlay_visible(page)
    for selector in transient_dialog_button_selectors(label):
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 30)
        except PlaywrightError:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible(timeout=timeout_ms):
                    continue
            except PlaywrightError:
                continue
            if not exact_transient_label(locator_text(candidate), label):
                continue
            if overlay_open or inside_transient_surface(candidate):
                return candidate
    return None


def exact_transient_label(text: str, label: str) -> bool:
    return normalized_label(text) == normalized_label(label)


def inside_transient_surface(locator: Any) -> bool:
    selector_list = ", ".join(TRANSIENT_SURFACE_SELECTORS)
    try:
        return bool(locator.evaluate("(element, selectors) => Boolean(element.closest(selectors))", selector_list))
    except PlaywrightError:
        return False


def first_transient_close_button(page: Any) -> Any | None:
    selector_list = ", ".join(f"{surface} button" for surface in TRANSIENT_SURFACE_SELECTORS)
    locator = page.locator(selector_list)
    try:
        count = min(locator.count(), 30)
    except PlaywrightError:
        return None
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible(timeout=100) and transient_close_button_candidate(candidate):
                return candidate
        except PlaywrightError:
            continue
    return None


def transient_close_button_candidate(locator: Any) -> bool:
    signal = normalized_label(locator_signal_text(locator))
    if signal in {"close", "dismiss"} or "close" in signal:
        return True
    try:
        data = locator.evaluate(
            """(element, selectors) => {
                const surface = element.closest(selectors);
                if (!surface) return null;
                const rect = element.getBoundingClientRect();
                const surfaceRect = surface.getBoundingClientRect();
                return {
                    text: (element.innerText || element.textContent || "").trim(),
                    svgCount: element.querySelectorAll("svg").length,
                    width: rect.width,
                    height: rect.height,
                    centerX: rect.x + rect.width / 2,
                    centerY: rect.y + rect.height / 2,
                    surfaceRight: surfaceRect.x + surfaceRect.width,
                    surfaceTop: surfaceRect.y,
                };
            }""",
            ", ".join(TRANSIENT_SURFACE_SELECTORS),
        )
    except PlaywrightError:
        return False
    if not data:
        return False
    text = normalized_label(str(data.get("text") or ""))
    width = float(data.get("width") or 0)
    height = float(data.get("height") or 0)
    center_x = float(data.get("centerX") or 0)
    center_y = float(data.get("centerY") or 0)
    surface_right = float(data.get("surfaceRight") or 0)
    surface_top = float(data.get("surfaceTop") or 0)
    svg_count = int(data.get("svgCount") or 0)
    return (
        not text
        and svg_count > 0
        and 0 < width <= 48
        and 0 < height <= 48
        and center_x >= surface_right - 72
        and center_y <= surface_top + 72
    )


def start_new_chat(page: Any) -> None:
    if empty_chat_ready(page):
        return
    button = first_visible(page, NEW_CHAT_SELECTORS, timeout_ms=5000)
    if button is None:
        raise RuntimeError("GLM new-chat control was not found; refusing to send into an existing conversation.")
    previous_url = current_url(page)
    previous_route_active = conversation_route_active(page)
    button.click()
    wait_until(
        page,
        lambda: empty_chat_ready(page) and (not previous_route_active or current_url(page) != previous_url),
        DEFAULT_NAVIGATION_TIMEOUT_MS,
        "GLM new-chat control was clicked but a fresh empty chat could not be verified.",
    )


def empty_chat_ready(page: Any) -> bool:
    return (
        first_visible(page, COMPOSER_SELECTORS, timeout_ms=250) is not None
        and conversation_is_empty(page)
        and not conversation_route_active(page)
    )


def conversation_is_empty(page: Any) -> bool:
    return assistant_message_count(page) == 0 and user_message_count(page) == 0


def conversation_route_active(page: Any) -> bool:
    return bool(GLM_CONVERSATION_PATH_RE.search(current_url(page)))


def current_url(page: Any) -> str:
    try:
        return str(page.url)
    except PlaywrightError:
        return ""


# ─── MODEL AND MODES ──────────────────────────────────────────────────────

def select_runtime_state(page: Any) -> dict[str, Any]:
    return {
        "model": select_named_mode(page, MODEL_CONTROL_SELECTORS, TARGET_MODEL_LABELS, "GLM model"),
        "deep_think": ensure_deep_think_enabled(page),
        "web_search": ensure_web_search_enabled(page),
    }


def select_named_mode(page: Any, control_selectors: Sequence[str], labels: Sequence[str], feature_name: str) -> str:
    if control_text_matches(page, control_selectors, labels):
        return "already selected"

    control = first_visible(page, control_selectors, timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS)
    if control is None:
        raise RuntimeError(f"{feature_name} control was not found.")
    control.click()

    option = first_visible(page, all_option_selectors(labels), timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS)
    if option is None:
        raise RuntimeError(f"{feature_name} option was not available: {', '.join(labels)}")
    option.click()

    wait_until(
        page,
        lambda: control_text_matches(page, control_selectors, labels),
        DEFAULT_NAVIGATION_TIMEOUT_MS,
        f"{feature_name} was clicked but selected state could not be verified.",
    )
    return "selected"


def ensure_deep_think_enabled(page: Any) -> str:
    control = first_visible_near_composer(page, DEEP_THINK_SELECTORS, timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS)
    if control is None:
        raise RuntimeError("GLM deep-think control was not found.")
    if deep_think_text_selected(locator_text(control)):
        return "already enabled"

    control.click()
    option = first_visible(page, all_option_selectors(TARGET_DEEP_THINK_VARIANTS), timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS)
    if option is None:
        raise RuntimeError("GLM deep-think Max option was not available.")
    option.click()

    wait_until(
        page,
        lambda: deep_think_text_selected(locator_text(first_visible_near_composer(page, DEEP_THINK_SELECTORS, timeout_ms=250))),
        DEFAULT_NAVIGATION_TIMEOUT_MS,
        "GLM deep-think Max was selected but selected state could not be verified.",
    )
    return "enabled"


def deep_think_text_selected(text: str) -> bool:
    normalized = normalized_label(text)
    return "deepthink" in normalized and "max" in normalized


def ensure_web_search_enabled(page: Any) -> str:
    control = first_web_search_control(page, timeout_ms=DEFAULT_NAVIGATION_TIMEOUT_MS)
    if control is None:
        raise RuntimeError("GLM web-search control was not found or the prompt-bar controls were ambiguous.")
    if semantic_toggle_on(control):
        return "already enabled"
    control.click()

    def enabled() -> bool:
        refreshed = first_web_search_control(page, timeout_ms=250)
        return bool(refreshed and semantic_toggle_on(refreshed))

    wait_until(page, enabled, DEFAULT_NAVIGATION_TIMEOUT_MS, "GLM web-search was clicked but enabled state could not be verified.")
    return "enabled"


def first_web_search_control(page: Any, timeout_ms: int) -> Any | None:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while True:
        candidates = web_search_control_candidates(page)
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise RuntimeError("GLM web-search control matched multiple prompt-bar controls; refusing an ambiguous click.")
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(100)


def web_search_control_candidates(page: Any) -> list[Any]:
    composer_box = visible_box(first_visible(page, COMPOSER_SELECTORS, timeout_ms=100))
    if composer_box is None:
        return []
    matches: list[Any] = []
    seen: set[str] = set()
    for selector in WEB_SEARCH_SELECTORS:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 20)
        except PlaywrightError:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            signature = locator_identity(candidate)
            if signature in seen:
                continue
            seen.add(signature)
            try:
                if not candidate.is_visible(timeout=100):
                    continue
            except PlaywrightError:
                continue
            candidate_box = visible_box(candidate)
            if candidate_box is None or not boxes_share_composer_band(composer_box, candidate_box):
                continue
            if web_search_control_candidate(candidate, candidate_box):
                matches.append(candidate)
    return matches


def locator_identity(locator: Any) -> str:
    try:
        return str(locator.evaluate("(element) => element.outerHTML.slice(0, 300)"))
    except PlaywrightError:
        return str(id(locator))


def web_search_control_candidate(locator: Any, box: dict[str, float]) -> bool:
    signal_text = locator_signal_text(locator).lower()
    signal_words = set(re.findall(r"[a-z0-9]+", signal_text))
    signal = normalized_label(signal_text)
    if signal_words.intersection(WEB_SEARCH_SIGNAL_TERMS) or any(phrase in signal for phrase in ("websearch", "internetsearch", "browsetheweb")):
        return True
    if any(term in signal for term in ("send", "upload", "file", "more", "deepthink")):
        return False
    try:
        attributes = locator.evaluate(
            """(element) => ({
                tag: element.tagName.toLowerCase(),
                type: element.getAttribute("type") || "",
                dataActive: element.getAttribute("data-active"),
                disabled: element.matches(":disabled") || element.getAttribute("aria-disabled") === "true",
                inForm: Boolean(element.closest("form")),
                svgCount: element.querySelectorAll("svg").length,
            })"""
        )
    except PlaywrightError:
        return False
    if attributes.get("tag") != "button" or attributes.get("type") != "button":
        return False
    if attributes.get("dataActive") not in {"true", "false"}:
        return False
    if attributes.get("disabled") or not attributes.get("inForm") or int(attributes.get("svgCount") or 0) == 0:
        return False
    width = float(box.get("width") or 0)
    height = float(box.get("height") or 0)
    if width <= 0 or height <= 0:
        return False
    ratio = max(width / height, height / width)
    return width <= 48 and height <= 48 and ratio <= 1.35


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
    raise RuntimeError(f"{CRITICAL_ERROR_PREFIX} GLM file upload input was not available for the current account or page state.")


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
        raise RuntimeError(f"{CRITICAL_ERROR_PREFIX} Timed out waiting for GLM attachment upload to finish") from exc


def verify_attachments_attached(page: Any, paths: list[Path]) -> None:
    deadline = time.monotonic() + (ATTACHMENT_VERIFY_TIMEOUT_MS / 1000)
    last_error = ""
    missing = [path.name for path in paths]
    while time.monotonic() < deadline:
        text = visible_upload_state_text(page)
        last_error = upload_error_from_text(text)
        if last_error:
            raise RuntimeError(f"{CRITICAL_ERROR_PREFIX} GLM attachment upload failed: {last_error}")
        missing = attachment_names_missing(text, paths)
        if not missing:
            return
        page.wait_for_timeout(100)
    joined = ", ".join(missing)
    raise RuntimeError(f"{CRITICAL_ERROR_PREFIX} GLM attachment upload was not confirmed before send: {joined}")


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
        raise RuntimeError("GLM composer was not found. The session may be expired or the UI changed.")
    composer.click()
    tag_name = composer.evaluate("element => element.tagName.toLowerCase()")
    editable = composer.evaluate("element => element.isContentEditable")
    if tag_name == "textarea":
        composer.fill(prompt)
    elif editable:
        page.keyboard.insert_text(prompt)
    else:
        raise RuntimeError("GLM composer is neither a textarea nor a contenteditable input.")


def click_send(page: Any) -> None:
    def send_enabled() -> bool:
        button = first_visible(page, SEND_BUTTON_SELECTORS, timeout_ms=250)
        if button is None:
            return False
        try:
            return button.is_enabled(timeout=250)
        except PlaywrightError:
            return False

    wait_until(page, send_enabled, DEFAULT_NAVIGATION_TIMEOUT_MS, "GLM send button did not become enabled.")
    button = first_visible(page, SEND_BUTTON_SELECTORS, timeout_ms=1000)
    if button is None:
        raise RuntimeError("GLM send button disappeared before click.")
    try:
        button.click()
    except PlaywrightError as exc:
        raise RuntimeError("GLM send button was visible but could not be clicked.") from exc


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
        raise RuntimeError(f"{CRITICAL_ERROR_PREFIX} GLM prompt was not confirmed as sent.") from exc


# ─── RESPONSE EXTRACTION ──────────────────────────────────────────────────

def assistant_message_count(page: Any) -> int:
    return len(assistant_message_snapshot(page))


def user_message_count(page: Any) -> int:
    return max((safe_count(page, selector) for selector in USER_MESSAGE_SELECTORS), default=0)


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
                const transientLine = (line) => /^(?:thinking(?:[.．。…]+)?|skip|searching(?:[.．。…]+)?|generating(?:[.．。…]+)?|analyzing(?:[.．。…]+)?|reading(?:[.．。…]+)?|loading(?:[.．。…]+)?|please\\s+wait(?:[.．。…]+)?)$/i.test(line.trim());
                const cleanAssistantText = (value) => value
                    .replace(/\\\\n/g, "\\n")
                    .split("\\n")
                    .map((line) => line.replace(/^\\s*Thought\\s+Process\\s*(?:[>›▸]\\s*)?/i, "").trim())
                    .filter((line) => line && !transientLine(line))
                    .join("\\n")
                    .trim();
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
                const cleanedText = cleanAssistantText(text);
                const visibleStop = Array.from(document.querySelectorAll(stopSelectors)).some((node) => visible(node) && !disabled(node));
                const visibleBusy = Array.from(document.querySelectorAll('[aria-busy="true"]')).some((node) => visible(node));
                const busy = visibleStop || visibleBusy;
                const state = window.__oarGlmState || {text: "", since: Date.now()};
                if (text !== state.text) {
                    window.__oarGlmState = {text, since: Date.now()};
                    return false;
                }
                window.__oarGlmState = state;
                return newTexts.length > 0 && cleanedText.length > 0 && !busy && Date.now() - state.since >= stableMs;
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
        raise RuntimeError("Timed out waiting for GLM to finish responding.") from exc

    content = latest_new_assistant_text(page, previous_snapshot)
    if not content:
        raise RuntimeError("GLM finished but no assistant response text could be extracted.")
    return content


def latest_new_assistant_text(page: Any, previous_snapshot: Sequence[str]) -> str:
    for text in reversed(assistant_texts_after_snapshot(assistant_message_texts(page), previous_snapshot)):
        cleaned = clean_assistant_text(text)
        if cleaned:
            return cleaned
    return ""


def latest_assistant_text(page: Any) -> str:
    for text in reversed(assistant_message_texts(page)):
        cleaned = clean_assistant_text(text)
        if cleaned:
            return cleaned
    return ""


def clean_assistant_text(text: str) -> str:
    lines: list[str] = []
    for raw_line in str(text or "").replace("\\n", "\n").splitlines():
        line = re.sub(r"^\s*Thought\s+Process\s*(?:[>›▸]\s*)?", "", raw_line, flags=re.IGNORECASE).strip()
        if not line or TRANSIENT_ASSISTANT_LINE_PATTERN.fullmatch(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def substantive_assistant_text(text: str) -> bool:
    return bool(clean_assistant_text(text))
