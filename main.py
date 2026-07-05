#!/usr/bin/env python3
# main.py -- OMNI-AGENTS-REQUEST runtime
# description: Runs configured OAR responder agents, manages browser-automation authentication, stores private artifacts, and combines successful responses.
# Tags: cli, agents, combiner, responses, auth, privacy, diagnostics
# date: 2026-07-07

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - POSIX terminals provide these.
    termios = None
    tty = None

APP_NAME = "OMNI-AGENTS-REQUEST"
APP_VERSION = "1.1.2"
COMBINER_FILE = "COMBINER.py"
AUTH_DIR_NAME = ".auth"
AUTH_MARKER_FILE = ".authenticated"
DEFAULT_TIMEOUT = 24 * 60 * 60
DEFAULT_COMBINER_TIMEOUT = 24 * 60 * 60
DEFAULT_MAX_WORKERS = max(1, (os.cpu_count() or 2) // 2)
DEFAULT_AUTH_CHECK_TIMEOUT = 15.0
DEFAULT_AUTH_LOGIN_TIMEOUT = 600.0
DEFAULT_BROWSER_START_TIMEOUT = 20.0
DEFAULT_BROWSER_EXIT_TIMEOUT = 8.0
AUTH_POLL_INTERVAL_MS = 500
LOG_SCHEMA = 1
MAX_FIELD_CHARS = 120_000
REDACTED = "[REDACTED]"
CONTENT_OMITTED = "[CONTENT_OMITTED]"
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
FORBIDDEN_BROWSER_OPTION_KEYS = {
    "user_data_dir",
    "userDataDir",
    "storage_state",
    "storageState",
    "cookies",
    "cookie_file",
    "profile",
    "profile_dir",
    "profile_path",
    "cdp_endpoint",
    "ws_endpoint",
}
FORBIDDEN_BROWSER_ARG_PREFIXES = (
    "--user-data-dir",
    "--profile-directory",
    "--remote-debugging-address",
    "--remote-debugging-port",
    "--remote-debugging-pipe",
)

SECRET_KEY_PATTERN = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|cookie|session|credential|private[_-]?key|access[_-]?key)"
)
SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,'\"]{4,}"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9._\-+/=]{12,}"),
    re.compile(r"(?i)(basic\s+)[a-z0-9+/=]{12,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|authorization|cookie|session)\s*[:=]\s*)[^\s,'\"]{4,}"),
    re.compile(r"(?i)\bsk-[a-z0-9_\-]{12,}\b"),
    re.compile(r"(?i)\bgh[pousr]_[a-z0-9_]{20,}\b"),
    re.compile(r"(?i)\bxox[baprs]-[a-z0-9\-]{20,}\b"),
)
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
CONTENT_KEY_NAMES = {"content", "body", "raw", "messages", "stdout", "stderr", "traceback"}
CRITICAL_ERROR_PREFIX = "CRITICAL:"


# ─── DATA MODEL ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    agents: Path
    responses: Path
    combiner: Path


@dataclass(frozen=True)
class RuntimeConfig:
    paths: RuntimePaths
    timeout: int
    combiner_timeout: int
    max_workers: int
    json_output: bool


@dataclass(frozen=True)
class AgentSpec:
    path: Path
    name: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ProcessResult:
    ok: bool
    stdout: str
    stderr: str
    timed_out: bool
    duration: float
    exit_code: int | None
    cancelled: bool = False


@dataclass(frozen=True)
class AgentResult:
    name: str
    file: str
    ok: bool
    content: str
    metadata: dict[str, Any]
    error: str | None
    duration: float
    timed_out: bool
    exit_code: int | None


class OarError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


class UsageError(OarError):
    def __init__(self, message: str):
        super().__init__(message, 2)


# ─── PRIVACY AND SERIALIZATION ────────────────────────────────────────────

def critical_error_message(message: str | None) -> str | None:
    if not message:
        return None
    text = str(message).strip()
    marker = text.find(CRITICAL_ERROR_PREFIX)
    if marker == -1:
        return None
    return text[marker + len(CRITICAL_ERROR_PREFIX):].strip() or text

def redact_text(value: str, limit: int = MAX_FIELD_CHARS) -> str:
    text = str(value)
    for pattern in SECRET_TEXT_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: match.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    if len(text) > limit:
        return text[:limit] + f"\n[TRUNCATED {len(text) - limit} chars]"
    return text


def safe_json(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for key, item in value.items():
            clean_key = redact_text(str(key), 500)
            lower_key = clean_key.lower()
            safe_scalar = isinstance(item, (int, float, bool)) or item is None
            if SECRET_KEY_PATTERN.search(str(key)):
                output[clean_key] = item if safe_scalar else REDACTED
            elif lower_key in CONTENT_KEY_NAMES:
                output[clean_key] = item if safe_scalar else CONTENT_OMITTED
            else:
                output[clean_key] = safe_json(item, depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [safe_json(item, depth + 1) for item in value[:250]]
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(str(value))


def emit_json(payload: dict[str, Any]) -> None:
    print(json.dumps(safe_json(payload), ensure_ascii=False, indent=2))


def emit_error(message: str, exit_code: int, json_output: bool) -> int:
    clean = redact_text(message)
    if json_output:
        emit_json({"ok": False, "error": clean, "exit_code": exit_code})
    else:
        print(clean, file=sys.stderr)
    return exit_code


def visible_width(text: str) -> int:
    return len(ANSI_PATTERN.sub("", text))


def fit_visible(text: str, width: int) -> str:
    output: list[str] = []
    visible = 0
    index = 0
    while index < len(text) and visible < width:
        if text[index] == "\033":
            end = text.find("m", index)
            if end == -1:
                break
            output.append(text[index : end + 1])
            index = end + 1
            continue
        output.append(text[index])
        visible += 1
        index += 1
    fitted = "".join(output)
    padding = max(0, width - visible_width(fitted))
    return fitted + (" " * padding)


# ─── FILESYSTEM HELPERS ───────────────────────────────────────────────────

def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def chmod_private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def chmod_private_dir(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass


def write_text_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        if mode == 0o600:
            chmod_private_file(path)
    finally:
        tmp.unlink(missing_ok=True)


def append_log(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    try:
        payload = {"schema": LOG_SCHEMA, "time": timestamp(), **safe_json(event)}
        os.write(fd, (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    chmod_private_file(path)


def file_stem(value: str, fallback: str = "agent") -> str:
    cleaned = re.sub(r"[^\w.\-]+", "-", str(value), flags=re.UNICODE).strip("-._")
    cleaned = re.sub(r"-+", "-", cleaned)
    return cleaned[:90].strip("-._") or fallback


def file_identity(path: Path) -> str:
    base = file_stem(path.stem)
    digest = hashlib.sha256(path.name.encode("utf-8", "ignore")).hexdigest()[:8]
    return f"{base}-{digest}"


def request_slug(prompt: str) -> str:
    base = redact_text(prompt, 500)
    cleaned = re.sub(r"[^\w.\-]+", "-", base.lower(), flags=re.UNICODE).strip("-._")
    cleaned = re.sub(r"-+", "-", cleaned)
    if not cleaned or REDACTED.lower() in cleaned:
        cleaned = "request-" + hashlib.sha256(prompt.encode("utf-8", "ignore")).hexdigest()[:12]
    return cleaned[:90].strip("-._") or "request"


def make_unique_dir(responses: Path, prompt: str) -> Path:
    responses.mkdir(parents=True, exist_ok=True)
    chmod_private_dir(responses)
    base = request_slug(prompt)
    for index in range(1, 10_000):
        name = base if index == 1 else f"{base}-{index}"
        candidate = responses / name
        try:
            candidate.mkdir(mode=0o700)
            chmod_private_dir(candidate)
            return candidate
        except FileExistsError:
            continue
    raise OarError("could not create response directory")


# ─── AGENT METADATA AND VALIDATION ────────────────────────────────────────

def read_agent_metadata(path: Path) -> dict[str, Any]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return {}
    for node in tree.body:
        value_node: ast.AST | None = None
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "AGENT" for target in node.targets):
                value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "AGENT":
            value_node = node.value
        if value_node is None:
            continue
        try:
            value = ast.literal_eval(value_node)
        except (SyntaxError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def public_agent_name(path: Path, metadata: dict[str, Any] | None = None) -> str:
    data = metadata if metadata is not None else read_agent_metadata(path)
    value = data.get("name") if isinstance(data, dict) else None
    name = str(value).strip() if value else path.stem
    return name or path.stem


def discover_agents(agents_dir: Path) -> list[Path]:
    if not agents_dir.exists():
        return []
    responders: list[Path] = []
    for path in sorted(agents_dir.glob("*.py"), key=lambda item: item.name.lower()):
        if path.name == COMBINER_FILE or path.name.startswith("_"):
            continue
        metadata = read_agent_metadata(path)
        if metadata.get("role") == "combiner" or metadata.get("fanout") is False:
            continue
        responders.append(path)
    return responders


def agent_spec(path: Path) -> AgentSpec:
    metadata = read_agent_metadata(path)
    return AgentSpec(path=path, name=public_agent_name(path, metadata), metadata=metadata)


def requires_auth(spec: AgentSpec) -> bool:
    return isinstance(spec.metadata, dict) and spec.metadata.get("requires_auth") is True


def auth_required_specs(specs: list[AgentSpec]) -> list[AgentSpec]:
    return [spec for spec in specs if requires_auth(spec)]


def browser_launch_option_errors(options: Any) -> list[str]:
    errors: list[str] = []
    if options is None:
        return errors
    if not isinstance(options, dict):
        return ["browser_launch_options() must return a dict"]

    for key in options:
        key_text = str(key)
        if key_text in FORBIDDEN_BROWSER_OPTION_KEYS:
            errors.append(f"browser_launch_options() must not set {key_text}")

    allowed_keys = {
        "mode",
        "browser",
        "channel",
        "executable_path",
        "args",
        "headless",
        "force_headed",
        "headless_strategy",
        "accept_downloads",
        "locale",
        "viewport",
    }
    for key in options:
        if str(key) not in allowed_keys and str(key) not in FORBIDDEN_BROWSER_OPTION_KEYS:
            errors.append(f"browser_launch_options() contains unsupported key: {key}")

    mode = str(options.get("mode", "playwright")).strip().lower()
    if mode not in {"playwright", "system-cdp"}:
        errors.append("browser_launch_options().mode must be playwright or system-cdp")

    browser = str(options.get("browser", "chromium")).strip().lower()
    if browser not in {"chromium", "firefox", "webkit"}:
        errors.append("browser_launch_options().browser must be chromium, firefox, or webkit")
    if mode == "system-cdp" and browser != "chromium":
        errors.append("browser_launch_options().mode system-cdp only supports chromium browsers")

    if options.get("channel") and options.get("executable_path"):
        errors.append("browser_launch_options() must not set both channel and executable_path")

    args = options.get("args", [])
    if args is None:
        args = []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        errors.append("browser_launch_options().args must be a list of strings")
    else:
        for item in args:
            for prefix in FORBIDDEN_BROWSER_ARG_PREFIXES:
                if item == prefix or item.startswith(prefix + "="):
                    errors.append(f"browser_launch_options().args must not set {prefix}")

    viewport = options.get("viewport")
    if viewport is not None:
        if not isinstance(viewport, dict) or not isinstance(viewport.get("width"), int) or not isinstance(viewport.get("height"), int):
            errors.append("browser_launch_options().viewport must contain integer width and height")

    for key in ("headless", "force_headed", "accept_downloads"):
        if key in options and not isinstance(options[key], bool):
            errors.append(f"browser_launch_options().{key} must be true or false")

    if "headless_strategy" in options and str(options["headless_strategy"]) not in {"true-headless", "hidden-window"}:
        errors.append("browser_launch_options().headless_strategy must be true-headless or hidden-window")

    if "locale" in options and options["locale"] is not None and not isinstance(options["locale"], str):
        errors.append("browser_launch_options().locale must be a string")

    if "executable_path" in options and options["executable_path"] is not None and not isinstance(options["executable_path"], str):
        errors.append("browser_launch_options().executable_path must be a string")

    if "channel" in options and options["channel"] is not None and not isinstance(options["channel"], str):
        errors.append("browser_launch_options().channel must be a string")

    return errors


def normalize_browser_launch_options(options: Any) -> dict[str, Any]:
    errors = browser_launch_option_errors(options)
    if errors:
        raise OarError("; ".join(errors))
    source = dict(options or {})
    mode = str(source.get("mode", "playwright")).strip().lower()
    normalized: dict[str, Any] = {
        "mode": mode,
        "browser": str(source.get("browser", "chromium")).strip().lower(),
        "args": list(source.get("args") or []),
    }
    for key in ("channel", "executable_path", "locale", "viewport", "headless", "force_headed", "headless_strategy", "accept_downloads"):
        if key in source and source[key] is not None:
            normalized[key] = source[key]
    return normalized


def static_browser_launch_options_errors(tree: ast.Module) -> list[str]:
    function = next((node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "browser_launch_options"), None)
    if function is None:
        return []
    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return) and node.value is not None]
    if not returns:
        return ["browser_launch_options() must return a dict"]
    errors: list[str] = []
    for node in returns:
        try:
            value = ast.literal_eval(node.value)
        except (SyntaxError, ValueError):
            continue
        errors.extend(browser_launch_option_errors(value))
    return errors


def validate_agent_file(path: Path) -> dict[str, Any]:
    metadata = read_agent_metadata(path)
    errors: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        tree = ast.Module(body=[], type_ignores=[])
        errors.append(f"invalid Python: {exc}")
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    role = metadata.get("role", "responder") if isinstance(metadata, dict) else "responder"
    if role not in {"responder", "combiner"}:
        errors.append("AGENT.role must be responder or combiner")
    if path.name == COMBINER_FILE:
        if role != "combiner" or metadata.get("fanout") is not False:
            errors.append("COMBINER.py must declare role combiner and fanout false")
        if "combine" not in functions:
            errors.append("COMBINER.py must define combine(request, responses)")
    else:
        if role == "combiner" or metadata.get("fanout") is False:
            errors.append("responder files must not opt out of fanout")
        if "run" not in functions:
            errors.append("responder must define run(request)")
        if isinstance(metadata, dict) and metadata.get("requires_auth") is True:
            if "login_url" not in functions:
                errors.append("responder declares requires_auth but is missing login_url()")
            if "auth_check" not in functions:
                errors.append("responder declares requires_auth but is missing auth_check(page)")
            errors.extend(static_browser_launch_options_errors(tree))
    return {"file": path.name, "ok": not errors, "metadata": safe_json(metadata), "errors": errors}


# ─── REQUEST MODEL ────────────────────────────────────────────────────────

def runtime_config(args: argparse.Namespace) -> RuntimeConfig:
    root = Path(__file__).resolve().parent
    agents = Path(args.agents_dir).expanduser().resolve() if args.agents_dir else root / "agents"
    responses_value = args.logs_dir
    responses = Path(responses_value).expanduser().resolve() if responses_value else root / "responses"
    return RuntimeConfig(
        paths=RuntimePaths(root=root, agents=agents, responses=responses, combiner=agents / COMBINER_FILE),
        timeout=max(1, int(args.timeout)),
        combiner_timeout=max(1, int(args.combiner_timeout)),
        max_workers=max(1, int(args.max_workers)),
        json_output=bool(args.json),
    )


def build_attachments(paths: list[str]) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    for item in paths:
        path = Path(item).expanduser().resolve()
        if not path.is_file():
            raise UsageError(f"attachment not found: {item}")
        stat = path.stat()
        attachments.append({"name": path.name, "path": str(path), "size": stat.st_size, "modified": int(stat.st_mtime)})
    return attachments


def public_request(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt": redact_text(str(request.get("prompt", ""))),
        "attachments": [
            {"name": item.get("name"), "size": item.get("size"), "modified": item.get("modified")}
            for item in request.get("attachments", [])
            if isinstance(item, dict)
        ],
    }


# ─── MODULE AND PROCESS EXECUTION ─────────────────────────────────────────

def load_python_module(path: Path, prefix: str) -> Any:
    module_name = f"{prefix}_{hashlib.sha256(str(path).encode()).hexdigest()[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module: {path.name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def run_subprocess(
    command: list[str],
    timeout: float,
    cwd: Path | None = None,
    cancel_event: threading.Event | None = None,
) -> ProcessResult:
    started = time.monotonic()
    process: subprocess.Popen[str] | None = None
    cancelled = False

    def stop_process() -> None:
        if not process or process.poll() is not None:
            return
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()

    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name == "posix",
        )
        deadline = started + timeout
        stdout = ""
        stderr = ""
        while True:
            if cancel_event is not None and cancel_event.is_set() and process.poll() is None:
                cancelled = True
                stop_process()
            remaining = deadline - time.monotonic()
            if remaining <= 0 and process.poll() is None:
                raise subprocess.TimeoutExpired(command, timeout)
            poll_timeout = max(0.01, min(0.2, remaining if remaining > 0 else 0.01))
            try:
                stdout, stderr = process.communicate(timeout=poll_timeout)
                return ProcessResult(
                    process.returncode == 0 and not cancelled,
                    stdout,
                    stderr,
                    False,
                    time.monotonic() - started,
                    process.returncode,
                    cancelled,
                )
            except subprocess.TimeoutExpired:
                if cancelled:
                    continue
    except subprocess.TimeoutExpired:
        if process and process.poll() is None:
            stop_process()
        stdout = ""
        stderr = ""
        if process:
            try:
                stdout, stderr = process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    stop_process()
        return ProcessResult(False, stdout, stderr, not cancelled, time.monotonic() - started, process.returncode, cancelled)


def run_python_worker(arguments: list[str], timeout: float, cancel_event: threading.Event | None = None) -> ProcessResult:
    command = [sys.executable, str(Path(__file__).resolve()), *arguments]
    return run_subprocess(command, timeout, Path(__file__).resolve().parent, cancel_event)


def worker_agent(agent_file: Path, request_file: Path, output_file: Path, auth_profile: Path | None = None) -> int:
    try:
        request = json.loads(request_file.read_text(encoding="utf-8"))
        request["agent"] = {
            "file": agent_file.name,
            "stem": agent_file.stem,
            "auth_profile_dir": str(auth_profile) if auth_profile else "",
        }
        module = load_python_module(agent_file, "oar_agent")
        run = getattr(module, "run", None)
        if not callable(run):
            raise RuntimeError("agent must define run(request)")
        started = time.monotonic()
        result = run(request)
        duration = time.monotonic() - started
        if isinstance(result, dict):
            content = str(result.get("content", ""))
            metadata = result.get("metadata", {})
        else:
            content = str(result)
            metadata = {}
        payload = {"ok": True, "content": content, "metadata": metadata, "duration": duration}
    except Exception as exc:
        payload = {
            "ok": False,
            "content": "",
            "metadata": {},
            "duration": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    write_text_atomic(output_file, json.dumps(payload, ensure_ascii=False), 0o600)
    return 0


def worker_combiner(combiner_file: Path, request_file: Path, responses_file: Path, output_file: Path) -> int:
    try:
        request = json.loads(request_file.read_text(encoding="utf-8"))
        responses = json.loads(responses_file.read_text(encoding="utf-8"))
        module = load_python_module(combiner_file, "oar_combiner")
        combine = getattr(module, "combine", None)
        if not callable(combine):
            raise RuntimeError("COMBINER.py must define combine(request, responses)")
        started = time.monotonic()
        result = combine(request, responses)
        duration = time.monotonic() - started
        content = result.get("content", "") if isinstance(result, dict) else result
        metadata = result.get("metadata", {}) if isinstance(result, dict) else {}
        payload = {"ok": True, "content": str(content), "metadata": metadata, "duration": duration}
    except Exception as exc:
        payload = {
            "ok": False,
            "content": "",
            "metadata": {},
            "duration": 0,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    write_text_atomic(output_file, json.dumps(payload, ensure_ascii=False), 0o600)
    return 0


def run_agent(
    spec: AgentSpec,
    request_file: Path,
    response_dir: Path,
    timeout: float,
    log_file: Path,
    cancel_event: threading.Event | None = None,
) -> AgentResult:
    output_file = response_dir / f".{file_identity(spec.path)}.agent.json"
    append_log(log_file, {"event": "agent_start", "agent": spec.name, "file": spec.path.name})
    profile_dir = spec.path.parent / AUTH_DIR_NAME / file_identity(spec.path)
    process = run_python_worker(["__agent__", str(spec.path), str(request_file), str(output_file), str(profile_dir)], timeout, cancel_event)
    if output_file.exists():
        try:
            payload = json.loads(output_file.read_text(encoding="utf-8"))
        except Exception as exc:
            payload = {"ok": False, "content": "", "metadata": {}, "error": f"invalid worker output: {exc}"}
        output_file.unlink(missing_ok=True)
    else:
        payload = {"ok": False, "content": "", "metadata": {}, "error": "agent did not produce output"}
    if process.timed_out:
        payload = {"ok": False, "content": "", "metadata": {}, "error": f"agent timed out after {timeout}s"}
    if process.cancelled:
        payload = {"ok": False, "content": "", "metadata": {}, "error": "agent cancelled after critical responder failure"}
    ok = bool(payload.get("ok")) and not process.timed_out and not process.cancelled and process.exit_code == 0
    result = AgentResult(
        name=spec.name,
        file=spec.path.name,
        ok=ok,
        content=redact_text(str(payload.get("content", ""))) if ok else "",
        metadata=safe_json(payload.get("metadata", {})) if isinstance(payload.get("metadata", {}), dict) else {},
        error=None if ok else redact_text(str(payload.get("error") or process.stderr or "agent failed")),
        duration=process.duration,
        timed_out=process.timed_out,
        exit_code=process.exit_code,
    )
    append_log(
        log_file,
        {
            "event": "agent_finish",
            "agent": spec.name,
            "ok": ok,
            "duration": round(process.duration, 4),
            "timed_out": process.timed_out,
            "cancelled": process.cancelled,
            "exit_code": process.exit_code,
            "stdout_chars": len(process.stdout or ""),
            "stderr_chars": len(process.stderr or ""),
            "error": result.error,
        },
    )
    return result


def skipped_agent_result(spec: AgentSpec, timeout: int, log_file: Path) -> AgentResult:
    result = AgentResult(
        name=spec.name,
        file=spec.path.name,
        ok=False,
        content="",
        metadata={},
        error=f"agent skipped after total timeout of {timeout}s",
        duration=0,
        timed_out=True,
        exit_code=None,
    )
    append_log(
        log_file,
        {
            "event": "agent_finish",
            "agent": spec.name,
            "ok": False,
            "duration": 0,
            "timed_out": True,
            "exit_code": None,
            "error": result.error,
        },
    )
    return result


def run_all_agents(specs: list[AgentSpec], request_file: Path, response_dir: Path, config: RuntimeConfig, log_file: Path) -> list[AgentResult]:
    max_workers = min(config.max_workers, len(specs))
    deadline = time.monotonic() + config.timeout
    pending = list(specs)
    active: dict[concurrent.futures.Future[AgentResult], AgentSpec] = {}
    results: list[AgentResult] = []
    cancel_event = threading.Event()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)

    def submit_next() -> bool:
        if not pending:
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        spec = pending.pop(0)
        active[executor.submit(run_agent, spec, request_file, response_dir, remaining, log_file, cancel_event)] = spec
        return True

    try:
        while len(active) < max_workers and submit_next():
            pass
        while active:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _ = concurrent.futures.wait(active, timeout=remaining, return_when=concurrent.futures.FIRST_COMPLETED)
            if not done:
                break
            for future in done:
                active.pop(future)
                result = future.result()
                results.append(result)
                if critical_error_message(result.error):
                    cancel_event.set()
                    pending.clear()
                    break
                submit_next()
            if cancel_event.is_set():
                break
    finally:
        for future in active:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

    for future, spec in list(active.items()):
        try:
            results.append(future.result(timeout=0.75))
        except concurrent.futures.TimeoutError:
            results.append(skipped_agent_result(spec, config.timeout, log_file))
    results.extend(skipped_agent_result(spec, config.timeout, log_file) for spec in pending)
    return sorted(results, key=lambda item: item.file.lower())


def run_combiner(config: RuntimeConfig, request_file: Path, response_dir: Path, results: list[AgentResult], log_file: Path) -> str:
    if not config.paths.combiner.is_file():
        raise OarError("agents/COMBINER.py is missing")
    critical = [(item, critical_error_message(item.error)) for item in results if not item.ok and critical_error_message(item.error)]
    if critical:
        item, detail = critical[0]
        append_log(log_file, {"event": "combiner_blocked", "reason": "critical_responder_failure", "agent": item.name, "error": detail})
        raise OarError(f"critical responder failure from {item.name}: {detail}")
    successful = [item for item in results if item.ok]
    if not successful:
        raise OarError("no responder succeeded")
    responses_payload = [
        {"name": item.name, "file": item.file, "content": item.content, "metadata": item.metadata, "duration": item.duration}
        for item in successful
    ]
    responses_file = response_dir / ".combiner.responses.json"
    output_file = response_dir / ".combiner.output.json"
    write_text_atomic(responses_file, json.dumps(responses_payload, ensure_ascii=False), 0o600)
    append_log(log_file, {"event": "combiner_start", "responses": len(successful)})
    process = run_python_worker(["__combiner__", str(config.paths.combiner), str(request_file), str(responses_file), str(output_file)], config.combiner_timeout)
    responses_file.unlink(missing_ok=True)
    if process.timed_out:
        raise OarError(f"combiner timed out after {config.combiner_timeout}s")
    if not output_file.exists():
        raise OarError(redact_text(process.stderr or "combiner did not produce output"))
    payload = json.loads(output_file.read_text(encoding="utf-8"))
    output_file.unlink(missing_ok=True)
    if process.exit_code != 0 or not payload.get("ok"):
        raise OarError(redact_text(str(payload.get("error") or process.stderr or "combiner failed")))
    append_log(
        log_file,
        {
            "event": "combiner_finish",
            "ok": True,
            "duration": round(process.duration, 4),
            "stdout_chars": len(process.stdout or ""),
            "stderr_chars": len(process.stderr or ""),
        },
    )
    return redact_text(str(payload.get("content", "")))


# ─── AUTHENTICATION (BROWSER PROFILES) ────────────────────────────────────
# Browser-automation responders authenticate through their own isolated,
# persistent browser profile under agents/.auth/<agent-name>/. OAR never reads,
# copies, or syncs cookies/session data from the user's real browser.

def load_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise OarError(
            "playwright is required for browser-automation responders. "
            "Install with: pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


def auth_root(config: RuntimeConfig) -> Path:
    return config.paths.agents / AUTH_DIR_NAME


def auth_profile_dir(config: RuntimeConfig, spec: AgentSpec) -> Path:
    return auth_root(config) / file_identity(spec.path)


def auth_marker(profile_dir: Path) -> Path:
    return profile_dir / AUTH_MARKER_FILE


def clear_auth_marker(profile_dir: Path) -> None:
    auth_marker(profile_dir).unlink(missing_ok=True)


def has_saved_session(profile_dir: Path) -> bool:
    """True only after a real successful auth_check pass, never from a bare profile
    directory. Chromium can leave a partial user-data-dir behind even when a launch
    fails (e.g. no display server), so directory existence alone is not proof of a
    working session."""
    return auth_marker(profile_dir).is_file()


def load_agent_auth_hooks(spec: AgentSpec) -> tuple[Any, Any]:
    module = load_python_module(spec.path, "oar_auth")
    login_url = getattr(module, "login_url", None)
    auth_check = getattr(module, "auth_check", None)
    if not callable(login_url) or not callable(auth_check):
        raise OarError(f"{spec.path.name} declares requires_auth but is missing login_url()/auth_check()")
    return login_url, auth_check


def load_agent_browser_launch_options(spec: AgentSpec) -> dict[str, Any]:
    module = load_python_module(spec.path, "oar_browser")
    hook = getattr(module, "browser_launch_options", None)
    if hook is None:
        return normalize_browser_launch_options({})
    if not callable(hook):
        raise OarError(f"{spec.path.name} browser_launch_options must be callable")
    try:
        return normalize_browser_launch_options(hook())
    except OarError:
        raise
    except Exception as exc:
        raise OarError(f"{spec.path.name} browser_launch_options failed: {type(exc).__name__}: {exc}") from exc


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
    raise OarError(f"browser CDP endpoint did not become ready: {last_error}")


def resolve_system_browser_executable(options: dict[str, Any]) -> str:
    explicit = str(options.get("executable_path") or os.environ.get("OAR_BROWSER_EXECUTABLE") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            return str(path.resolve())
        found = shutil.which(explicit)
        if found:
            return found
        raise OarError(f"browser executable not found: {explicit}")
    for command in BROWSER_COMMAND_CANDIDATES:
        found = shutil.which(command)
        if found:
            return found
    raise OarError("no Chrome-compatible browser was found; install Chrome, Chromium, Edge, or Brave, or set OAR_BROWSER_EXECUTABLE")


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


def wait_for_browser_process_exit(process: subprocess.Popen[Any], timeout: float = DEFAULT_BROWSER_EXIT_TIMEOUT) -> bool:
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


@contextlib.contextmanager
def system_cdp_browser_context(engine: Any, profile_dir: Path, options: dict[str, Any], headless: bool) -> Any:
    executable = resolve_system_browser_executable(options)
    port = find_open_port()
    strategy = str(options.get("headless_strategy") or "true-headless")
    hidden_window = bool(headless and strategy == "hidden-window")
    profile_dir.mkdir(parents=True, exist_ok=True)
    chmod_private_dir(profile_dir)
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
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name == "posix")
    browser = None
    try:
        endpoint = wait_for_cdp_endpoint(port, DEFAULT_BROWSER_START_TIMEOUT)
        browser = engine.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        yield context
    finally:
        if browser is not None:
            close_cdp_browser(browser)
        if not wait_for_browser_process_exit(process):
            terminate_browser_process(process)


@contextlib.contextmanager
def playwright_browser_context(engine: Any, profile_dir: Path, options: dict[str, Any], headless: bool) -> Any:
    browser_type = getattr(engine, options.get("browser", "chromium"))
    launch_options: dict[str, Any] = {"headless": headless}
    for key in ("channel", "executable_path", "args", "locale", "viewport", "accept_downloads"):
        if key in options:
            launch_options[key] = options[key]
    context = browser_type.launch_persistent_context(str(profile_dir), **launch_options)
    try:
        yield context
    finally:
        context.close()


@contextlib.contextmanager
def auth_browser_context(engine: Any, spec: AgentSpec, profile_dir: Path, headless: bool) -> Any:
    options = load_agent_browser_launch_options(spec)
    effective_headless = False if options.get("force_headed") else bool(options.get("headless", headless))
    if options["mode"] == "system-cdp":
        with system_cdp_browser_context(engine, profile_dir, options, effective_headless) as context:
            yield context
    else:
        with playwright_browser_context(engine, profile_dir, options, effective_headless) as context:
            yield context


def wait_for_auth(page: Any, auth_check: Any, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if auth_check(page):
                return True
        except Exception:
            pass
        try:
            page.wait_for_timeout(AUTH_POLL_INTERVAL_MS)
        except Exception:
            return False
    return False


def probe_auth(spec: AgentSpec, profile_dir: Path, headless: bool, timeout: float) -> tuple[bool, str]:
    sync_playwright = load_playwright()
    login_url, auth_check = load_agent_auth_hooks(spec)
    try:
        with sync_playwright() as engine:
            with auth_browser_context(engine, spec, profile_dir, headless=headless) as context:
                page = context.new_page()
                page.goto(str(login_url()), wait_until="domcontentloaded")
                if wait_for_auth(page, auth_check, timeout):
                    write_text_atomic(auth_marker(profile_dir), timestamp() + "\n", 0o600)
                    return True, "authenticated"
                return False, "session missing or expired"
    except OarError:
        raise
    except Exception as exc:
        return False, redact_text(f"probe failed: {type(exc).__name__}: {exc}")


def ensure_auth(config: RuntimeConfig, specs: list[AgentSpec], json_output: bool) -> None:
    """Bootstrap a headed login for any auth-required responder with no saved session yet."""
    missing = [spec for spec in auth_required_specs(specs) if not has_saved_session(auth_profile_dir(config, spec))]
    if not missing:
        return
    auth_root(config).mkdir(parents=True, exist_ok=True)
    chmod_private_dir(auth_root(config))
    for spec in missing:
        profile_dir = auth_profile_dir(config, spec)
        profile_dir.mkdir(parents=True, exist_ok=True)
        chmod_private_dir(profile_dir)
        if not json_output:
            print(f"No saved session for {spec.name}. Opening a browser window to log in...", file=sys.stderr)
        ok, detail = probe_auth(spec, profile_dir, headless=False, timeout=DEFAULT_AUTH_LOGIN_TIMEOUT)
        if not ok:
            raise OarError(f"authentication for {spec.name} failed: {detail}")
        if not json_output:
            print(f"{spec.name}: authenticated.", file=sys.stderr)


# ─── ARTIFACTS ────────────────────────────────────────────────────────────

def write_agent_artifact(response_dir: Path, result: AgentResult) -> str:
    base = file_stem(Path(result.file).stem)
    filename = base + ".md"
    if (response_dir / filename).exists():
        filename = f"{base}-{hashlib.sha256(result.file.encode('utf-8', 'ignore')).hexdigest()[:8]}.md"
    if result.ok:
        body = f"# {result.name}\n\n{result.content.strip()}\n"
    else:
        body = f"# {result.name}\n\nAgent failed.\n\n```text\n{result.error or 'unknown error'}\n```\n"
    write_text_atomic(response_dir / filename, body, 0o600)
    return filename


def write_manifest(
    response_dir: Path,
    request: dict[str, Any],
    results: list[AgentResult],
    files: list[str],
    ok: bool,
    error: str | None = None,
) -> None:
    payload = {
        "schema": 1,
        "created": timestamp(),
        "app": APP_NAME,
        "version": APP_VERSION,
        "ok": ok,
        "error": redact_text(error) if error else None,
        "request": public_request(request),
        "final": "FINAL.md" if ok else None,
        "agents": [
            {
                "name": item.name,
                "file": item.file,
                "ok": item.ok,
                "duration": round(item.duration, 4),
                "timed_out": item.timed_out,
                "error": item.error,
            }
            for item in results
        ],
        "files": [*files, *([] if not ok else ["FINAL.md"]), "RUN.json", "log.jsonl"],
    }
    write_text_atomic(response_dir / "RUN.json", json.dumps(safe_json(payload), ensure_ascii=False, indent=2), 0o600)


# ─── COMMANDS ─────────────────────────────────────────────────────────────

def command_run(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    prompt = " ".join(args.prompt).strip()
    if not prompt:
        raise UsageError("missing prompt")
    attachments = build_attachments(args.attach)
    responders = [agent_spec(path) for path in discover_agents(config.paths.agents)]
    if not responders:
        raise OarError("no responder agents configured")
    ensure_auth(config, responders, config.json_output)

    response_dir = make_unique_dir(config.paths.responses, prompt)
    log_file = response_dir / "log.jsonl"
    request = {"prompt": prompt, "attachments": attachments, "created": timestamp(), "response_dir": str(response_dir)}
    request_file = response_dir / ".request.json"
    results: list[AgentResult] = []
    agent_files: list[str] = []
    try:
        write_text_atomic(request_file, json.dumps(request, ensure_ascii=False), 0o600)
        append_log(log_file, {"event": "run_start", "request": public_request(request), "agents": [spec.path.name for spec in responders]})
        results = run_all_agents(responders, request_file, response_dir, config, log_file)
        agent_files = [write_agent_artifact(response_dir, result) for result in results]
        final = run_combiner(config, request_file, response_dir, results, log_file)
        write_text_atomic(response_dir / "FINAL.md", final.strip() + "\n", 0o600)
        write_manifest(response_dir, request, results, agent_files, True)
        append_log(log_file, {"event": "run_finish", "ok": True, "response_dir": str(response_dir)})
        if config.json_output:
            emit_json({"ok": True, "response_dir": str(response_dir), "final": final})
        else:
            print(final.strip())
            print(f"\nSaved: {response_dir}", file=sys.stderr)
        return 0
    except OarError as exc:
        if results:
            write_manifest(response_dir, request, results, agent_files, False, str(exc))
        append_log(log_file, {"event": "run_finish", "ok": False, "error": str(exc)})
        message = str(exc)
        if response_dir.exists():
            message = f"{message}\nSaved: {response_dir}"
        raise OarError(message, exc.exit_code) from exc
    finally:
        request_file.unlink(missing_ok=True)


def command_list(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    responders = [agent_spec(path) for path in discover_agents(config.paths.agents)]
    responder_entries = []
    for item in responders:
        needs_auth = requires_auth(item)
        responder_entries.append(
            {
                "file": item.path.name,
                "name": item.name,
                "requires_auth": needs_auth,
                "session_saved": has_saved_session(auth_profile_dir(config, item)) if needs_auth else None,
                "metadata": safe_json(item.metadata),
            }
        )
    payload = {
        "agents_dir": str(config.paths.agents),
        "combiner": {"exists": config.paths.combiner.exists(), "path": str(config.paths.combiner)},
        "responders": responder_entries,
    }
    if config.json_output:
        emit_json(payload)
    else:
        print(f"Combiner: {'ok' if payload['combiner']['exists'] else 'missing'}")
        print("Responders: none" if not responder_entries else "Responders:")
        for responder in responder_entries:
            suffix = ""
            if responder["requires_auth"]:
                suffix = "  [auth: saved]" if responder["session_saved"] else "  [auth: missing, run oar --init-auth]"
            print(f"- {responder['name']} ({responder['file']}){suffix}")
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    specs = [agent_spec(path) for path in discover_agents(config.paths.agents)]
    try:
        config.paths.responses.mkdir(parents=True, exist_ok=True)
        chmod_private_dir(config.paths.responses)
        responses_ok = config.paths.responses.is_dir()
    except OSError:
        responses_ok = False
    checks = [
        {"name": "python", "ok": sys.version_info >= (3, 10), "detail": sys.version.split()[0]},
        {"name": "agents", "ok": config.paths.agents.exists(), "detail": str(config.paths.agents)},
        {"name": "responses", "ok": responses_ok, "detail": str(config.paths.responses)},
        {"name": "combiner", "ok": config.paths.combiner.exists(), "detail": str(config.paths.combiner)},
        {"name": "responders", "ok": bool(specs), "detail": str(len(specs))},
    ]
    for spec in auth_required_specs(specs):
        profile_dir = auth_profile_dir(config, spec)
        check_name = f"auth:{spec.name}"
        if not has_saved_session(profile_dir):
            checks.append({"name": check_name, "ok": False, "detail": "no session, run oar --init-auth"})
            continue
        try:
            ok, detail = probe_auth(spec, profile_dir, headless=True, timeout=DEFAULT_AUTH_CHECK_TIMEOUT)
        except OarError as exc:
            ok, detail = False, str(exc)
        checks.append({"name": check_name, "ok": ok, "detail": detail if ok else f"{detail}, run oar --init-auth"})
    ok = all(item["ok"] for item in checks)
    if config.json_output:
        emit_json({"ok": ok, "checks": checks})
    else:
        for item in checks:
            print(f"{'ok' if item['ok'] else 'fail'} {item['name']}: {item['detail']}")
    return 0 if ok else 1


def auth_status_key(spec: AgentSpec) -> str:
    return file_identity(spec.path)


def auth_session_authenticated(config: RuntimeConfig, spec: AgentSpec, status_cache: dict[str, bool] | None = None) -> bool:
    if status_cache is not None:
        cached = status_cache.get(auth_status_key(spec))
        if cached is not None:
            return cached
    return has_saved_session(auth_profile_dir(config, spec))


def collect_auth_session_statuses(config: RuntimeConfig, specs: list[AgentSpec], validate_saved: bool) -> dict[str, bool]:
    statuses: dict[str, bool] = {}
    for spec in auth_required_specs(specs):
        profile_dir = auth_profile_dir(config, spec)
        if not has_saved_session(profile_dir):
            statuses[auth_status_key(spec)] = False
            continue
        if not validate_saved:
            statuses[auth_status_key(spec)] = True
            continue
        try:
            ok, _ = probe_auth(spec, profile_dir, headless=True, timeout=DEFAULT_AUTH_CHECK_TIMEOUT)
        except OarError:
            ok = False
        statuses[auth_status_key(spec)] = ok
    return statuses


def auth_dashboard_specs(config: RuntimeConfig, specs: list[AgentSpec], status_cache: dict[str, bool] | None = None) -> list[AgentSpec]:
    targets = [spec for spec in specs if requires_auth(spec)]
    return sorted(
        targets,
        key=lambda spec: (
            0 if auth_session_authenticated(config, spec, status_cache) else 1,
            spec.name.casefold(),
            spec.path.name.casefold(),
        ),
    )


def auth_status_dot(config: RuntimeConfig, spec: AgentSpec, color: bool = False, status_cache: dict[str, bool] | None = None) -> str:
    if not requires_auth(spec):
        return ""
    if auth_session_authenticated(config, spec, status_cache):
        return "\033[32m●\033[0m" if color else "●"
    return "\033[31m●\033[0m" if color else "●"


def next_auth_index(current: int, count: int, direction: int) -> int:
    if count <= 0:
        return 0
    return (current + direction) % count


def render_auth_dashboard(
    config: RuntimeConfig,
    specs: list[AgentSpec],
    selected: int = 0,
    color: bool = False,
    status_cache: dict[str, bool] | None = None,
) -> str:
    targets = auth_dashboard_specs(config, specs, status_cache)
    width = 74
    lines = [
        "╔" + "═" * width + "╗",
        "║" + " OAR AUTH SESSIONS ".center(width) + "║",
        "╠" + "═" * width + "╣",
    ]
    if not targets:
        lines.append("║" + fit_visible(" No auth-enabled responders found. ", width) + "║")
    else:
        active = selected % len(targets)
        for index, spec in enumerate(targets):
            pointer = "▶" if index == active else " "
            dot = auth_status_dot(config, spec, color, status_cache)
            line = f" {pointer} {dot} {spec.name} ({spec.path.name})"
            lines.append("║" + fit_visible(line, width) + "║")
    lines.extend(
        [
            "╠" + "═" * width + "╣",
            "║" + fit_visible(" ↑/↓ move  Enter authenticate  m auth red  a auth all  q quit ", width) + "║",
            "╚" + "═" * width + "╝",
        ]
    )
    return "\n".join(lines)


def parse_auth_selection(
    selection: str,
    config: RuntimeConfig,
    specs: list[AgentSpec],
    status_cache: dict[str, bool] | None = None,
) -> list[AgentSpec]:
    targets = auth_dashboard_specs(config, specs, status_cache)
    value = selection.strip().lower()
    if value in {"", "q", "quit", "exit"}:
        return []
    if value in {"m", "missing"}:
        return [spec for spec in targets if not auth_session_authenticated(config, spec, status_cache)]
    if value in {"a", "all"}:
        return targets
    selected: list[AgentSpec] = []
    for part in re.split(r"[,\s]+", value):
        if not part:
            continue
        if not part.isdigit():
            raise UsageError(f"invalid auth selection: {part}")
        index = int(part)
        if index < 1 or index > len(targets):
            raise UsageError(f"auth selection out of range: {part}")
        selected.append(targets[index - 1])
    return selected


@contextlib.contextmanager
def terminal_cbreak(stream: Any):
    if termios is None or tty is None:
        yield
        return
    fd = stream.fileno()
    previous = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, previous)


def read_stdin_character(timeout: float | None = None) -> str:
    fd = sys.stdin.fileno()
    if timeout is not None:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return ""
    data = os.read(fd, 1)
    return data.decode("utf-8", "ignore")


def read_auth_key() -> str:
    char = read_stdin_character()
    if char in {"\r", "\n"}:
        return "enter"
    if char == "\x03":
        raise KeyboardInterrupt
    if char == "\x1b":
        sequence = char
        while len(sequence) < 3:
            item = read_stdin_character(0.15)
            if not item:
                break
            sequence += item
        if sequence == "\x1b[A":
            return "up"
        if sequence == "\x1b[B":
            return "down"
        return "escape"
    return char.lower()


def select_auth_targets_tui(config: RuntimeConfig, specs: list[AgentSpec], status_cache: dict[str, bool] | None = None) -> list[AgentSpec]:
    targets = auth_dashboard_specs(config, specs, status_cache)
    if not targets:
        print(render_auth_dashboard(config, specs, color=sys.stdout.isatty(), status_cache=status_cache))
        return []

    selected = 0

    def draw() -> None:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write(render_auth_dashboard(config, targets, selected=selected, color=sys.stdout.isatty(), status_cache=status_cache))
        sys.stdout.write("\n")
        sys.stdout.flush()

    try:
        sys.stdout.write("\033[?25l")
        with terminal_cbreak(sys.stdin):
            while True:
                draw()
                key = read_auth_key()
                if key in {"up", "k"}:
                    selected = next_auth_index(selected, len(targets), -1)
                elif key in {"down", "j"}:
                    selected = next_auth_index(selected, len(targets), 1)
                elif key == "enter":
                    return [targets[selected]]
                elif key == "m":
                    return [spec for spec in targets if not auth_session_authenticated(config, spec, status_cache)]
                elif key == "a":
                    return targets
                elif key in {"q", "escape"}:
                    return []
    finally:
        sys.stdout.write("\033[?25h\033[2J\033[H")
        sys.stdout.flush()


def authenticate_specs(config: RuntimeConfig, specs: list[AgentSpec], json_output: bool) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for spec in specs:
        profile_dir = auth_profile_dir(config, spec)
        try:
            profile_dir.mkdir(parents=True, exist_ok=True)
            chmod_private_dir(profile_dir)
            if not json_output:
                print(f"Opening browser for {spec.name}. Log in, then it closes automatically once detected.")
            ok, detail = probe_auth(spec, profile_dir, headless=False, timeout=DEFAULT_AUTH_LOGIN_TIMEOUT)
            results.append({"name": spec.name, "ok": ok, "detail": detail})
        except OarError as exc:
            results.append({"name": spec.name, "ok": False, "detail": str(exc)})
        except Exception as exc:
            results.append({"name": spec.name, "ok": False, "detail": redact_text(f"{type(exc).__name__}: {exc}")})
    return results


def interactive_init_auth(config: RuntimeConfig, targets: list[AgentSpec]) -> int:
    print("Checking saved auth sessions...")
    status_cache = collect_auth_session_statuses(config, targets, validate_saved=True)
    selected = select_auth_targets_tui(config, targets, status_cache)
    if not selected:
        return 0
    results = authenticate_specs(config, selected, False)
    for spec, item in zip(selected, results):
        status_cache[auth_status_key(spec)] = bool(item["ok"])
    for item in results:
        print(f"{'ok' if item['ok'] else 'fail'} {item['name']}: {item['detail']}")
    print(render_auth_dashboard(config, targets, color=sys.stdout.isatty(), status_cache=status_cache))
    return 0 if all(item["ok"] for item in results) else 1


def command_init_auth(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    responders = [agent_spec(path) for path in discover_agents(config.paths.agents)]
    targets = auth_required_specs(responders)
    if not targets:
        message = "no responders require authentication"
        if config.json_output:
            emit_json({"ok": True, "message": message})
        else:
            print(message)
        return 0

    load_playwright()  # fail fast with a clear, actionable error if the dependency is missing
    auth_root(config).mkdir(parents=True, exist_ok=True)
    chmod_private_dir(auth_root(config))

    if not config.json_output and sys.stdin.isatty() and sys.stdout.isatty():
        return interactive_init_auth(config, responders)

    missing = [spec for spec in targets if not has_saved_session(auth_profile_dir(config, spec))]
    results = authenticate_specs(config, missing, config.json_output)
    for spec in targets:
        if spec not in missing:
            results.append({"name": spec.name, "ok": True, "detail": "saved session; use interactive --init-auth to re-authenticate"})

    ok_all = all(item["ok"] for item in results)
    if config.json_output:
        emit_json({"ok": ok_all, "results": results})
    else:
        for item in results:
            print(f"{'ok' if item['ok'] else 'fail'} {item['name']}: {item['detail']}")
    return 0 if ok_all else 1


def command_validate(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    files = [
        path
        for path in sorted(config.paths.agents.glob("*.py"), key=lambda item: item.name.lower())
        if path.name == COMBINER_FILE or not path.name.startswith("_")
    ] if config.paths.agents.exists() else []
    payload: dict[str, Any] = {"ok": True, "agents_dir": str(config.paths.agents), "files": []}
    if not files:
        payload["ok"] = False
        payload["files"].append({"file": None, "ok": False, "errors": ["agents directory contains no Python files"]})
    for path in files:
        item = validate_agent_file(path)
        payload["files"].append(item)
        payload["ok"] = bool(payload["ok"] and item["ok"])
    if config.json_output:
        emit_json(payload)
    else:
        for item in payload["files"]:
            print(f"{'ok' if item['ok'] else 'invalid'}: {item['file']}")
            for error in item["errors"]:
                print(f"  - {error}")
    return 0 if payload["ok"] else 1


def response_dirs(responses: Path) -> list[Path]:
    responses.mkdir(parents=True, exist_ok=True)
    chmod_private_dir(responses)
    return sorted([path for path in responses.iterdir() if path.is_dir()], key=lambda path: path.stat().st_mtime, reverse=True)


def command_responses(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    payload = [{"name": path.name, "path": str(path), "modified": int(path.stat().st_mtime)} for path in response_dirs(config.paths.responses)[:50]]
    if config.json_output:
        emit_json({"responses": payload})
    else:
        if not payload:
            print("No responses yet.")
        for item in payload:
            print(f"{item['name']}  {item['path']}")
    return 0


def remove_tree(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    path.rmdir()


def command_clean(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    keep = max(0, int(args.keep))
    removable = response_dirs(config.paths.responses)[keep:]
    removed: list[str] = []
    if not args.dry_run:
        for path in removable:
            remove_tree(path)
            removed.append(str(path))
    payload = {"dry_run": args.dry_run, "keep": keep, "matched": [str(path) for path in removable], "removed": removed}
    if config.json_output:
        emit_json(payload)
    else:
        print(f"{'Would remove' if args.dry_run else 'Removed'}: {len(removable)} response directories")
        for path in removable:
            print(path)
    return 0


def command_inspect(args: argparse.Namespace) -> int:
    config = runtime_config(args)
    name = str(args.inspect or "").strip()
    if not name:
        raise UsageError("missing response name")
    root = config.paths.responses.resolve()
    target = (root / name).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise UsageError("response name escapes responses directory") from exc
    files = sorted([path.name for path in target.iterdir() if path.is_file()]) if target.is_dir() else []
    payload = {"ok": target.is_dir(), "path": str(target), "files": files}
    if config.json_output:
        emit_json(payload)
    else:
        if not target.is_dir():
            raise OarError("response not found")
        for item in files:
            print(item)
    return 0 if target.is_dir() else 1


def command_update() -> int:
    installer = Path(__file__).resolve().parent / "install.sh"
    if not installer.exists():
        raise OarError("install.sh not found")
    return subprocess.call([str(installer), "--update"])


# ─── ARGUMENT PARSING ────────────────────────────────────────────────────

class OarArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def normalize_argv(argv: list[str]) -> list[str]:
    if not argv:
        return argv
    if argv[0] == "run":
        return argv[1:]
    command_aliases = {
        "list": "--list",
        "ls": "--list",
        "doctor": "--doctor",
        "doc": "--doctor",
        "responses": "--responses",
        "update": "--update",
        "upd": "--update",
        "version": "--version",
        "validate": "--validate",
        "clean": "--clean",
        "inspect": "--inspect",
        "init-auth": "--init-auth",
    }
    flag_aliases = {"jsn": "--json", "to": "--timeout", "adir": "--agents-dir", "ldir": "--logs-dir", "ia": "--init-auth"}
    attach_flags = {"--attach", "-a"}
    boolean_flags = {
        "-h",
        "--help",
        "--list",
        "-ls",
        "--doctor",
        "-doc",
        "--init-auth",
        "-ia",
        "--responses",
        "--validate",
        "--clean",
        "--json",
        "--jsn",
        "--delete",
        "--update",
        "-upd",
        "--version",
    }
    value_flags = {
        "--timeout",
        "--to",
        "to",
        "--agents-dir",
        "--adir",
        "-adir",
        "adir",
        "--logs-dir",
        "--responses-dir",
        "--ldir",
        "-ldir",
        "ldir",
        "--combiner-timeout",
        "--max-workers",
        "--keep",
        "--inspect",
    }
    option_flags = boolean_flags | value_flags | attach_flags

    normalized: list[str] = []
    positional_started = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            normalized.extend(argv[index:])
            break
        if index == 0 and token in command_aliases:
            normalized.append(command_aliases[token])
        elif not positional_started and token in flag_aliases:
            normalized.append(flag_aliases[token])
        elif token.startswith("--attach="):
            attach_value = token.split("=", 1)[1]
            if not attach_value:
                normalized.extend(["--attach", ""])
                index += 1
                continue
            attachments, prompt_tail = normalize_attachment_values([attach_value])
            append_normalized_attachments(normalized, attachments)
            normalized.extend(prompt_tail)
            positional_started = positional_started or bool(prompt_tail)
            index += 1
            continue
        else:
            normalized.append(token)
            if not token.startswith("-"):
                positional_started = True
        effective = normalized[-1]
        if effective in attach_flags:
            index += 1
            cluster: list[str] = []
            while index < len(argv):
                next_token = argv[index]
                if next_token == "--" or is_option_token(next_token, option_flags):
                    break
                cluster.append(next_token)
                index += 1
            if not cluster:
                continue
            normalized.pop()
            attachments, prompt_tail = normalize_attachment_values(cluster)
            append_normalized_attachments(normalized, attachments)
            normalized.extend(prompt_tail)
            positional_started = positional_started or bool(prompt_tail)
            continue
        if effective in value_flags and index + 1 < len(argv):
            index += 1
            normalized.append(argv[index])
        index += 1
    return normalized


def is_option_token(token: str, option_flags: set[str]) -> bool:
    return token in option_flags or token.startswith("--")


def append_normalized_attachments(argv: list[str], attachments: list[str]) -> None:
    for attachment in attachments:
        argv.extend(["--attach", attachment])


def normalize_attachment_values(values: list[str]) -> tuple[list[str], list[str]]:
    groups = split_attachment_groups(values)
    if not groups:
        return [], []
    if len(groups) > 1:
        attachments: list[str] = []
        for group in groups:
            group_attachments, _ = parse_attachment_group(group, allow_prompt_tail=False)
            attachments.extend(group_attachments)
        return attachments, []

    return parse_attachment_group(groups[0], allow_prompt_tail=True)


def split_attachment_groups(values: list[str]) -> list[list[str]]:
    groups: list[list[str]] = [[]]
    for value in values:
        parts = str(value).split(",")
        for offset, part in enumerate(parts):
            cleaned = clean_attachment_token(part)
            if cleaned:
                groups[-1].append(cleaned)
            if offset < len(parts) - 1:
                groups.append([])
    return [group for group in groups if group]


def parse_attachment_group(values: list[str], allow_prompt_tail: bool) -> tuple[list[str], list[str]]:
    cleaned = [clean_attachment_token(value) for value in values if clean_attachment_token(value)]
    if not cleaned:
        return [], []
    attachments: list[str] = []
    index = 0
    while index < len(cleaned):
        match = longest_existing_attachment(cleaned, index)
        if match is not None:
            attachment, next_index = match
            attachments.append(attachment)
            index = next_index
            continue
        remainder = cleaned[index:]
        joined = " ".join(remainder).strip()
        if not attachments or not allow_prompt_tail or looks_like_path(remainder, require_path_signal=True):
            attachments.append(joined)
            return attachments, []
        return attachments, remainder
    return attachments, []


def clean_attachment_token(value: str) -> str:
    cleaned = str(value).strip().strip(",").strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def longest_existing_attachment(values: list[str], start: int) -> tuple[str, int] | None:
    best: tuple[str, int] | None = None
    for end in range(start + 1, len(values) + 1):
        candidate = " ".join(values[start:end]).strip()
        if Path(candidate).expanduser().is_file():
            best = (candidate, end)
    return best


def looks_like_path(values: list[str], require_path_signal: bool = False) -> bool:
    text = " ".join(values)
    stripped = text.strip()
    has_path_signal = "/" in stripped or "\\" in stripped or stripped.startswith(("~", "."))
    return has_path_signal if require_path_signal else has_path_signal or bool(Path(stripped).suffix)


def wants_json(raw: list[str]) -> bool:
    for item in raw:
        if item == "--":
            break
        if item in {"--json", "--jsn", "jsn"}:
            return True
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = OarArgumentParser(prog="oar", description="Send one prompt to configured OAR agents and combine successful responses.", allow_abbrev=False)
    parser.add_argument("prompt", nargs="*", help="Prompt text")
    parser.add_argument("--attach", "-a", action="append", default=[], help="Attach file(s)")
    parser.add_argument("--list", "-ls", action="store_true", help="List configured responder agents and auth session status")
    parser.add_argument("--doctor", "-doc", action="store_true", help="Run diagnostics, including per-service session checks")
    parser.add_argument("--init-auth", "-ia", action="store_true", help="Log in to browser-automation responders with a missing or invalid session")
    parser.add_argument("--responses", action="store_true", help="List response directories")
    parser.add_argument("--validate", action="store_true", help="Validate agent file contracts")
    parser.add_argument("--clean", action="store_true", help="Remove old response directories")
    parser.add_argument("--inspect", nargs="?", const="", help="Inspect one response directory")
    parser.add_argument("--json", "--jsn", action="store_true", help="Print structured JSON")
    parser.add_argument("--timeout", "--to", type=int, default=DEFAULT_TIMEOUT, help="Responder timeout in seconds")
    parser.add_argument("--combiner-timeout", type=int, default=DEFAULT_COMBINER_TIMEOUT, help="Combiner timeout in seconds")
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS, help="Maximum concurrent responders")
    parser.add_argument("--agents-dir", "--adir", "-adir", help="Custom agents directory")
    parser.add_argument("--logs-dir", "--responses-dir", "--ldir", "-ldir", help="Custom responses directory")
    parser.add_argument("--keep", type=int, default=20, help="Response directories to keep when cleaning")
    parser.add_argument("--delete", dest="dry_run", action="store_false", default=True, help="Delete instead of dry-run for clean")
    parser.add_argument("--update", "-upd", action="store_true", help="Update through install.sh")
    parser.add_argument("--version", action="store_true", help="Print version")
    return parser


def route(args: argparse.Namespace) -> int:
    if args.version:
        print(APP_VERSION)
        return 0
    if args.update:
        return command_update()
    if args.list:
        return command_list(args)
    if args.doctor:
        return command_doctor(args)
    if args.init_auth:
        return command_init_auth(args)
    if args.responses:
        return command_responses(args)
    if args.validate:
        return command_validate(args)
    if args.clean:
        return command_clean(args)
    if args.inspect is not None:
        return command_inspect(args)
    return command_run(args)


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw and raw[0] == "__agent__":
        return worker_agent(Path(raw[1]), Path(raw[2]), Path(raw[3]), Path(raw[4]) if len(raw) > 4 else None)
    if raw and raw[0] == "__combiner__":
        return worker_combiner(Path(raw[1]), Path(raw[2]), Path(raw[3]), Path(raw[4]))
    json_requested = wants_json(raw)
    try:
        args = build_parser().parse_args(normalize_argv(raw))
        return route(args)
    except OarError as exc:
        return emit_error(str(exc), exc.exit_code, json_requested)
    except BrokenPipeError:
        return 1


def dispatch(argv: list[str] | None = None) -> int:
    try:
        return main(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else int(bool(code))


if __name__ == "__main__":
    raise SystemExit(main())
