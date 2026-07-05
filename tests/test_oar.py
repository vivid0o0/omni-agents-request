# tests/test_oar.py -- OAR regression tests
# description: Runtime, privacy, installer, response artifact, CLI, managed-responder, and release-tree regression tests for OAR.
# Tags: tests, regression, privacy, installer, cli, release, responders
# date: 2026-07-07
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import zipfile
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
COMBINER = ROOT / "agents" / "COMBINER.py"
INSTALL = ROOT / "install.sh"
CHATGPT_AGENT = ROOT / "agents" / "chatgpt_web.py"
GLM_AGENT = ROOT / "agents" / "glm_web.py"
MANAGED_RESPONDERS = [CHATGPT_AGENT, GLM_AGENT]


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_main_module():
    return load_module(MAIN, "oar_main_under_test")


def call_dispatch(args: list[str]) -> tuple[int, str, str]:
    module = load_main_module()
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = module.dispatch(args)
    return code, stdout.getvalue(), stderr.getvalue()


class OarRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="oar-test-"))
        self.agents = self.tmp / "agents"
        self.responses = self.tmp / "responses"
        self.agents.mkdir()
        self.responses.mkdir()
        shutil.copy(COMBINER, self.agents / "COMBINER.py")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_agent(self, name: str, body: str) -> Path:
        path = self.agents / name
        path.write_text(textwrap.dedent(body).strip() + "\n", encoding="utf-8")
        return path

    def test_default_local_combiner_success_and_private_artifacts(self) -> None:
        self.write_agent("alpha.py", '''
            AGENT = {"name": "alpha", "role": "responder", "model": "fake", "thinking": "low"}
            def run(request):
                return {"content": "Use Python 3.10 or newer. token=sk-THISSHOULDBEREDACTED999"}
        ''')
        code, stdout, stderr = call_dispatch(["run", "list", "--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        self.assertEqual(code, 0, stderr)
        self.assertIn("# Final Answer", stdout)
        self.assertIn("Use Python 3.10 or newer", stdout)
        self.assertIn("[REDACTED]", stdout)
        folder = next(self.responses.iterdir())
        for name in ["alpha.md", "FINAL.md", "RUN.json", "log.jsonl"]:
            self.assertTrue((folder / name).exists(), name)
            self.assertEqual(stat.S_IMODE((folder / name).stat().st_mode), 0o600, name)
        self.assertNotIn("THISSHOULDBEREDACTED", (folder / "FINAL.md").read_text(encoding="utf-8"))
        log = (folder / "log.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("Use Python", log)
        self.assertNotIn("THISSHOULDBEREDACTED", log)
        manifest = json.loads((folder / "RUN.json").read_text(encoding="utf-8"))
        self.assertIn("RUN.json", manifest["files"])

    def test_command_word_prompt_uses_run_escape(self) -> None:
        self.write_agent("echo.py", '''
            AGENT = {"name": "echo", "role": "responder"}
            def run(request): return request["prompt"]
        ''')
        code, stdout, stderr = call_dispatch(["run", "doctor", "--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        self.assertEqual(code, 0, stderr)
        self.assertIn("doctor", stdout)

    def test_bare_flag_aliases_do_not_rewrite_prompt_words(self) -> None:
        self.write_agent("echo.py", '''
            AGENT = {"name": "echo", "role": "responder"}
            def run(request): return request["prompt"]
        ''')
        code, stdout, stderr = call_dispatch(["please", "go", "to", "1", "--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        self.assertEqual(code, 0, stderr)
        self.assertIn("please go to 1", stdout)

    def test_json_errors_are_structured(self) -> None:
        code, stdout, stderr = call_dispatch(["hello", "--agents-dir", str(self.agents), "--responses-dir", str(self.responses), "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "")
        payload = json.loads(stdout)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["exit_code"], 1)
        self.assertIn("no responder", payload["error"])

    def test_doctor_creates_missing_responses_directory(self) -> None:
        missing_responses = self.tmp / "missing-responses"
        code, stdout, _ = call_dispatch(["doctor", "--agents-dir", str(self.agents), "--responses-dir", str(missing_responses), "--json"])
        self.assertEqual(code, 1)
        payload = json.loads(stdout)
        responses_check = next(item for item in payload["checks"] if item["name"] == "responses")
        responders_check = next(item for item in payload["checks"] if item["name"] == "responders")
        self.assertTrue(responses_check["ok"])
        self.assertFalse(responders_check["ok"])
        self.assertTrue(missing_responses.is_dir())

    def test_external_combiner_mode_uses_adapter(self) -> None:
        text = (self.agents / "COMBINER.py").read_text(encoding="utf-8")
        text = text.replace('"mode": "local"', '"mode": "model"')
        text = text.replace('"model_source_agent": ""', '"model_source_agent": "agents/modeler.py"')
        (self.agents / "COMBINER.py").write_text(text, encoding="utf-8")
        self.write_agent("modeler.py", '''
            AGENT = {"name": "modeler", "role": "responder", "model": "adapter"}
            def run(request): return "responder output"
            def complete(messages, model, thinking): return "model-combined-output"
        ''')
        combiner = load_module(self.agents / "COMBINER.py", "combiner_test")
        result = combiner.combine({"prompt": "external", "attachments": []}, [{"agent": "modeler", "content": "responder output"}])
        self.assertEqual(result["content"].strip(), "model-combined-output")

    def test_timeout_helper_marks_process_timeout(self) -> None:
        module = load_main_module()
        result = module.run_subprocess([sys.executable, "-c", "import time; time.sleep(2)"], 1, ROOT)
        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.exit_code, 0)

    def test_timeout_helper_does_not_wait_on_escaped_child_pipe(self) -> None:
        module = load_main_module()
        code = """
import subprocess
import sys
import time

subprocess.Popen(
    [sys.executable, "-c", "import time; print('child-ready', flush=True); time.sleep(5)"],
    stdout=sys.stdout,
    stderr=sys.stderr,
    start_new_session=True,
)
time.sleep(5)
"""
        started = time.monotonic()
        result = module.run_subprocess([sys.executable, "-c", code], 1, ROOT)
        elapsed = time.monotonic() - started
        self.assertFalse(result.ok)
        self.assertTrue(result.timed_out)
        self.assertLess(elapsed, 2.5)

    def test_default_generation_timeouts_are_long_running(self) -> None:
        module = load_main_module()
        self.assertEqual(module.DEFAULT_TIMEOUT, 24 * 60 * 60)
        self.assertEqual(module.DEFAULT_COMBINER_TIMEOUT, 24 * 60 * 60)

    def test_validate_reports_invalid_metadata(self) -> None:
        self.write_agent("bad.py", '''
            AGENT = {"name": "bad", "role": "wrong"}
            def run(request): return "bad"
        ''')
        code, stdout, _ = call_dispatch(["validate", "--agents-dir", str(self.agents), "--json"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout)["ok"])

    def test_validate_ignores_package_marker_files(self) -> None:
        (self.agents / "__init__.py").write_text("# package marker\n", encoding="utf-8")
        code, stdout, _ = call_dispatch(["validate", "--agents-dir", str(self.agents), "--json"])
        self.assertEqual(code, 0)
        files = [item["file"] for item in json.loads(stdout)["files"]]
        self.assertEqual(files, ["COMBINER.py"])

    def test_listing_does_not_execute_agent_module_body(self) -> None:
        marker = self.tmp / "imported.txt"
        self.write_agent("side_effect.py", f'''
            AGENT = {{"name": "side-effect", "role": "responder"}}
            from pathlib import Path
            Path({str(marker)!r}).write_text("executed", encoding="utf-8")
            def run(request): return "ok"
        ''')
        code, stdout, stderr = call_dispatch(["--list", "--agents-dir", str(self.agents), "--json"])
        self.assertEqual(code, 0, stderr)
        self.assertIn("side-effect", stdout)
        self.assertFalse(marker.exists())

    def test_agent_artifacts_and_auth_profiles_do_not_collide_after_slugging(self) -> None:
        self.write_agent("a b.py", '''
            AGENT = {"name": "space", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def run(request): return "space"
        ''')
        self.write_agent("a-b.py", '''
            AGENT = {"name": "dash", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def run(request): return "dash"
        ''')
        module = load_main_module()
        args = module.build_parser().parse_args(["--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        config = module.runtime_config(args)
        specs = [module.agent_spec(path) for path in sorted(self.agents.glob("a*.py"))]
        for spec in specs:
            profile = module.auth_profile_dir(config, spec)
            profile.mkdir(parents=True)
            module.write_text_atomic(module.auth_marker(profile), "ok\n")

        code, stdout, stderr = call_dispatch(["collision", "--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        self.assertEqual(code, 0, stderr)
        self.assertIn("space", stdout)
        self.assertIn("dash", stdout)
        folder = next(self.responses.iterdir())
        agent_files = sorted(path.name for path in folder.glob("*.md") if path.name != "FINAL.md")
        self.assertEqual(len(agent_files), 2, agent_files)
        self.assertEqual(len(set(agent_files)), 2, agent_files)
        profiles = [module.auth_profile_dir(config, spec).name for spec in specs]
        self.assertEqual(len(set(profiles)), 2, profiles)

    def test_auth_required_agent_receives_its_isolated_profile_path(self) -> None:
        agent = self.write_agent("needs_auth.py", '''
            from pathlib import Path
            AGENT = {"name": "needs-auth", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def run(request):
                profile = Path(request["agent"]["auth_profile_dir"])
                return {"content": f"profile={profile.name};exists={profile.is_dir()}"}
        ''')
        module = load_main_module()
        args = module.build_parser().parse_args(["--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        config = module.runtime_config(args)
        spec = module.agent_spec(agent)
        profile = module.auth_profile_dir(config, spec)
        profile.mkdir(parents=True)
        module.write_text_atomic(module.auth_marker(profile), "ok\n")
        code, stdout, stderr = call_dispatch(["auth profile", "--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        self.assertEqual(code, 0, stderr)
        self.assertIn(f"profile={profile.name};exists=True", stdout)

    def test_auth_context_can_use_system_cdp_browser_with_isolated_profile(self) -> None:
        agent = self.write_agent("system_browser.py", '''
            AGENT = {"name": "system-browser", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def browser_launch_options():
                return {"mode": "system-cdp", "executable_path": "__EXECUTABLE__"}
            def run(request): return "ok"
        '''.replace("__EXECUTABLE__", sys.executable))
        module = load_main_module()
        spec = module.agent_spec(agent)
        profile = self.agents / ".auth" / "system-browser"
        launched: dict[str, object] = {}

        class FakeProcess:
            def __init__(self, command: list[str]):
                launched["command"] = command
                self.returncode = None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = 0

            def wait(self, timeout=None):
                return self.returncode

            def kill(self) -> None:
                self.returncode = -9

        class FakeContext:
            pages = []
            closed = False

            def new_page(self):
                return object()

            def close(self) -> None:
                self.closed = True

        class FakeBrowser:
            def __init__(self):
                self.contexts = [FakeContext()]
                self.closed = False

            def close(self) -> None:
                self.closed = True

        fake_browser = FakeBrowser()
        fake_engine = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda endpoint: fake_browser))
        original_popen = module.subprocess.Popen
        original_port = module.find_open_port
        original_wait = module.wait_for_cdp_endpoint
        try:
            module.subprocess.Popen = lambda command, **_: FakeProcess(command)
            module.find_open_port = lambda: 48123
            module.wait_for_cdp_endpoint = lambda port, timeout: f"http://127.0.0.1:{port}"
            with module.auth_browser_context(fake_engine, spec, profile, headless=False) as context:
                self.assertIs(context, fake_browser.contexts[0])
        finally:
            module.subprocess.Popen = original_popen
            module.find_open_port = original_port
            module.wait_for_cdp_endpoint = original_wait

        command = launched["command"]
        self.assertEqual(command[0], str(Path(sys.executable).resolve()))
        self.assertIn(f"--user-data-dir={profile}", command)
        self.assertIn("--remote-debugging-port=48123", command)
        self.assertNotIn(str(Path.home() / ".config"), " ".join(command))
        self.assertTrue(fake_browser.closed)

    def test_system_cdp_waits_for_browser_exit_before_terminating_process(self) -> None:
        agent = self.write_agent("system_browser.py", '''
            AGENT = {"name": "system-browser", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def browser_launch_options():
                return {"mode": "system-cdp", "executable_path": "__EXECUTABLE__"}
            def run(request): return "ok"
        '''.replace("__EXECUTABLE__", sys.executable))
        module = load_main_module()
        spec = module.agent_spec(agent)
        launched: dict[str, object] = {}

        class FakeProcess:
            def __init__(self, command: list[str]):
                self.command = command
                self.returncode = None
                self.terminated = False
                self.wait_calls: list[float | None] = []
                launched["process"] = self

            def poll(self):
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = 0

            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                if launched.get("browser_closed"):
                    self.returncode = 0
                    return 0
                raise subprocess.TimeoutExpired(self.command, timeout)

            def kill(self) -> None:
                self.returncode = -9

        class FakeContext:
            pages = []

        class FakeBrowser:
            def __init__(self):
                self.contexts = [FakeContext()]

            def close(self) -> None:
                launched["browser_closed"] = True

        fake_browser = FakeBrowser()
        fake_engine = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda endpoint: fake_browser))
        original_popen = module.subprocess.Popen
        original_port = module.find_open_port
        original_wait = module.wait_for_cdp_endpoint
        try:
            module.subprocess.Popen = lambda command, **_: FakeProcess(command)
            module.find_open_port = lambda: 48123
            module.wait_for_cdp_endpoint = lambda port, timeout: f"http://127.0.0.1:{port}"
            with module.auth_browser_context(fake_engine, spec, self.agents / ".auth" / "system-browser", headless=False):
                pass
        finally:
            module.subprocess.Popen = original_popen
            module.find_open_port = original_port
            module.wait_for_cdp_endpoint = original_wait

        process = launched["process"]
        self.assertFalse(process.terminated)
        self.assertTrue(process.wait_calls)

    def test_system_cdp_hidden_window_strategy_avoids_chrome_headless_flag(self) -> None:
        agent = self.write_agent("hidden_browser.py", '''
            AGENT = {"name": "hidden-browser", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def browser_launch_options():
                return {
                    "mode": "system-cdp",
                    "executable_path": "__EXECUTABLE__",
                    "headless_strategy": "hidden-window",
                    "viewport": {"width": 1200, "height": 800},
                }
            def run(request): return "ok"
        '''.replace("__EXECUTABLE__", sys.executable))
        module = load_main_module()
        spec = module.agent_spec(agent)
        launched: dict[str, object] = {}

        class FakeProcess:
            def __init__(self, command: list[str]):
                launched["command"] = command
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def terminate(self) -> None:
                return None

            def kill(self) -> None:
                return None

        class FakeBrowser:
            contexts = [object()]

            def close(self) -> None:
                return None

        fake_engine = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda endpoint: FakeBrowser()))
        original_popen = module.subprocess.Popen
        original_port = module.find_open_port
        original_wait = module.wait_for_cdp_endpoint
        try:
            module.subprocess.Popen = lambda command, **_: FakeProcess(command)
            module.find_open_port = lambda: 48123
            module.wait_for_cdp_endpoint = lambda port, timeout: f"http://127.0.0.1:{port}"
            with module.auth_browser_context(fake_engine, spec, self.agents / ".auth" / "hidden-browser", headless=True):
                pass
        finally:
            module.subprocess.Popen = original_popen
            module.find_open_port = original_port
            module.wait_for_cdp_endpoint = original_wait

        command = launched["command"]
        self.assertNotIn("--headless=new", command)
        self.assertIn("--window-position=-32000,-32000", command)
        self.assertIn("--window-size=1200,800", command)

    def test_validate_rejects_browser_launch_options_that_set_user_profile(self) -> None:
        self.write_agent("unsafe_browser.py", '''
            AGENT = {"name": "unsafe-browser", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def browser_launch_options():
                return {"mode": "system-cdp", "args": ["--user-data-dir=/tmp/leak"]}
            def run(request): return "ok"
        ''')
        code, stdout, _ = call_dispatch(["validate", "--agents-dir", str(self.agents), "--json"])
        payload = json.loads(stdout)
        errors = "\n".join(error for item in payload["files"] for error in item["errors"])
        self.assertEqual(code, 1)
        self.assertIn("browser_launch_options", errors)
        self.assertIn("user-data-dir", errors)

    def test_validate_rejects_ambiguous_browser_executable_selection(self) -> None:
        self.write_agent("ambiguous_browser.py", '''
            AGENT = {"name": "ambiguous-browser", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def browser_launch_options():
                return {"mode": "playwright", "channel": "chrome", "executable_path": "/usr/bin/google-chrome"}
            def run(request): return "ok"
        ''')
        code, stdout, _ = call_dispatch(["validate", "--agents-dir", str(self.agents), "--json"])
        payload = json.loads(stdout)
        errors = "\n".join(error for item in payload["files"] for error in item["errors"])
        self.assertEqual(code, 1)
        self.assertIn("channel", errors)
        self.assertIn("executable_path", errors)

    def test_auth_validation_failure_does_not_delete_saved_session_marker(self) -> None:
        agent = self.write_agent("flaky_auth.py", '''
            AGENT = {"name": "flaky-auth", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return False
            def run(request): return "ok"
        ''')
        module = load_main_module()
        args = module.build_parser().parse_args(["doctor", "--agents-dir", str(self.agents), "--responses-dir", str(self.responses), "--json"])
        config = module.runtime_config(args)
        spec = module.agent_spec(agent)
        profile = module.auth_profile_dir(config, spec)
        profile.mkdir(parents=True)
        module.write_text_atomic(module.auth_marker(profile), "ok\n")
        original_probe = module.probe_auth
        try:
            module.probe_auth = lambda *_, **__: (False, "session missing or expired")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = module.command_doctor(args)
        finally:
            module.probe_auth = original_probe
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(stdout.getvalue())["ok"])
        self.assertTrue(module.auth_marker(profile).exists())

    def test_auth_tui_lists_only_auth_targets_with_green_sorted_first(self) -> None:
        saved = self.write_agent("saved_auth.py", '''
            AGENT = {"name": "saved-auth", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def run(request): return "saved"
        ''')
        self.write_agent("missing_auth.py", '''
            AGENT = {"name": "missing-auth", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def run(request): return "missing"
        ''')
        self.write_agent("plain.py", '''
            AGENT = {"name": "plain", "role": "responder"}
            def run(request): return "plain"
        ''')
        module = load_main_module()
        args = module.build_parser().parse_args(["--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        config = module.runtime_config(args)
        saved_spec = module.agent_spec(saved)
        profile = module.auth_profile_dir(config, saved_spec)
        profile.mkdir(parents=True)
        module.write_text_atomic(module.auth_marker(profile), "ok\n")
        specs = [module.agent_spec(path) for path in module.discover_agents(config.paths.agents)]
        targets = module.auth_dashboard_specs(config, specs)
        screen = module.render_auth_dashboard(config, specs, selected=0, color=True)
        self.assertEqual([item.name for item in targets], ["saved-auth", "missing-auth"])
        self.assertIn("saved-auth", screen)
        self.assertIn("missing-auth", screen)
        self.assertNotIn("plain", screen)
        self.assertNotIn("not required", screen)
        self.assertNotIn("missing]", screen)
        self.assertIn("\033[32m●\033[0m", screen)
        self.assertIn("\033[31m●\033[0m", screen)
        self.assertIn("▶", screen)
        self.assertIn("authenticate", screen.lower())

    def test_auth_menu_selection_can_reauthenticate_saved_agent(self) -> None:
        saved = self.write_agent("saved_auth.py", '''
            AGENT = {"name": "saved-auth", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def run(request): return "saved"
        ''')
        missing = self.write_agent("missing_auth.py", '''
            AGENT = {"name": "missing-auth", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return True
            def run(request): return "missing"
        ''')
        module = load_main_module()
        args = module.build_parser().parse_args(["--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        config = module.runtime_config(args)
        specs = [module.agent_spec(path) for path in [missing, saved]]
        saved_profile = module.auth_profile_dir(config, module.agent_spec(saved))
        saved_profile.mkdir(parents=True)
        module.write_text_atomic(module.auth_marker(saved_profile), "ok\n")
        selected = module.parse_auth_selection("1", config, module.auth_dashboard_specs(config, specs))
        self.assertEqual([item.name for item in selected], ["saved-auth"])

    def test_auth_tui_arrow_navigation_wraps(self) -> None:
        module = load_main_module()
        self.assertEqual(module.next_auth_index(0, 3, 1), 1)
        self.assertEqual(module.next_auth_index(0, 3, -1), 2)
        self.assertEqual(module.next_auth_index(2, 3, 1), 0)
        self.assertEqual(module.next_auth_index(0, 0, 1), 0)

    def test_auth_tui_reads_arrow_key_sequence_from_raw_file_descriptor(self) -> None:
        module = load_main_module()
        original_stdin = sys.stdin
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"\x1b[B")
            os.close(write_fd)
            write_fd = -1
            with os.fdopen(read_fd, "r", encoding="utf-8") as handle:
                read_fd = -1
                sys.stdin = handle
                self.assertEqual(module.read_auth_key(), "down")
        finally:
            sys.stdin = original_stdin
            if write_fd != -1:
                os.close(write_fd)
            if read_fd != -1:
                os.close(read_fd)

    def test_auth_tui_live_validation_can_mark_saved_session_red_without_deleting_marker(self) -> None:
        agent = self.write_agent("stale_auth.py", '''
            AGENT = {"name": "stale-auth", "role": "responder", "requires_auth": True}
            def login_url(): return "https://example.com"
            def auth_check(page): return False
            def run(request): return "stale"
        ''')
        module = load_main_module()
        args = module.build_parser().parse_args(["--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        config = module.runtime_config(args)
        spec = module.agent_spec(agent)
        profile = module.auth_profile_dir(config, spec)
        profile.mkdir(parents=True)
        module.write_text_atomic(module.auth_marker(profile), "ok\n")
        original_probe = module.probe_auth
        try:
            module.probe_auth = lambda *_, **__: (False, "session missing or expired")
            status_cache = module.collect_auth_session_statuses(config, [spec], validate_saved=True)
        finally:
            module.probe_auth = original_probe
        screen = module.render_auth_dashboard(config, [spec], color=True, status_cache=status_cache)
        self.assertFalse(status_cache[module.auth_status_key(spec)])
        self.assertTrue(module.auth_marker(profile).exists())
        self.assertIn("\033[31m●\033[0m", screen)
        self.assertNotIn("\033[32m●\033[0m", screen)

    def test_safe_json_redacts_nested_secret_containers_and_basic_auth(self) -> None:
        module = load_main_module()
        payload = module.safe_json(
            {
                "Authorization": "Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
                "cookies": [{"name": "session", "value": "plain-cookie-secret"}],
                "session_saved": False,
                "metadata": {"api_key": {"value": "nested-secret"}},
            }
        )
        rendered = json.dumps(payload, sort_keys=True)
        self.assertNotIn("QWxhZGRpbjpvcGVuIHNlc2FtZQ", rendered)
        self.assertNotIn("plain-cookie-secret", rendered)
        self.assertNotIn("nested-secret", rendered)
        self.assertFalse(payload["session_saved"])

    def test_attachment_errors_and_public_metadata(self) -> None:
        missing = self.tmp / "missing.txt"
        code, _, _ = call_dispatch(["attach", "--attach", str(missing), "--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        self.assertEqual(code, 2)
        self.assertEqual(list(self.responses.iterdir()), [])

    def test_attachment_parser_accepts_split_space_paths_and_comma_lists(self) -> None:
        module = load_main_module()
        spaced = self.tmp / "Screenshot From 2026-07-05 23-14-02.png"
        first = self.tmp / "1.png"
        second = self.tmp / "2.png"
        for path in [spaced, first, second]:
            path.write_text(path.name, encoding="utf-8")

        raw = ["compare", "-a", *str(spaced).split(" "), "-a", f"{first},", str(second)]
        args = module.build_parser().parse_args(module.normalize_argv(raw))

        self.assertEqual(args.prompt, ["compare"])
        self.assertEqual(args.attach, [str(spaced), str(first), str(second)])
        self.assertEqual([item["name"] for item in module.build_attachments(args.attach)], [spaced.name, first.name, second.name])

    def test_attachment_parser_accepts_mixed_comma_and_space_lists(self) -> None:
        module = load_main_module()
        files = [self.tmp / f"{index}.png" for index in range(1, 8)]
        for path in files:
            path.write_text(path.name, encoding="utf-8")

        raw = [
            "compare",
            "-a",
            f"{files[0]},",
            f"{files[1]},",
            f"{files[2]},",
            str(files[3]),
            "-a",
            f"{files[4]},",
            str(files[5]),
            str(files[6]),
        ]
        args = module.build_parser().parse_args(module.normalize_argv(raw))

        self.assertEqual(args.prompt, ["compare"])
        self.assertEqual(args.attach, [str(path) for path in files])

    def test_attachment_parser_keeps_prompt_after_existing_attachment(self) -> None:
        module = load_main_module()
        attachment = self.tmp / "input.png"
        attachment.write_text("image", encoding="utf-8")

        args = module.build_parser().parse_args(module.normalize_argv(["-a", str(attachment), "prompt", "after", "file"]))

        self.assertEqual(args.attach, [str(attachment)])
        self.assertEqual(args.prompt, ["prompt", "after", "file"])

    def test_attachment_parser_keeps_dotted_prompt_after_existing_attachment(self) -> None:
        module = load_main_module()
        attachment = self.tmp / "input.png"
        attachment.write_text("image", encoding="utf-8")

        args = module.build_parser().parse_args(
            module.normalize_argv(["-a", str(attachment), "compare", "with", "example.com"])
        )

        self.assertEqual(args.attach, [str(attachment)])
        self.assertEqual(args.prompt, ["compare", "with", "example.com"])

    def test_attachment_parser_reports_missing_split_path_as_attachment(self) -> None:
        module = load_main_module()
        args = module.build_parser().parse_args(module.normalize_argv(["prompt", "-a", "Missing", "Screenshot.png"]))

        self.assertEqual(args.attach, ["Missing Screenshot.png"])
        with self.assertRaisesRegex(module.UsageError, "attachment not found: Missing Screenshot.png"):
            module.build_attachments(args.attach)

    def test_unicode_prompt_and_duplicate_output_names(self) -> None:
        module = load_main_module()
        first = module.make_unique_dir(self.responses, "اختبار 日本 test")
        second = module.make_unique_dir(self.responses, "اختبار 日本 test")
        self.assertNotEqual(first.name, second.name)
        self.assertTrue(first.name)
        self.assertTrue(second.name)

    def test_support_commands_json_and_clean_dry_run(self) -> None:
        code, stdout, _ = call_dispatch(["responses", "--responses-dir", str(self.responses), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["responses"], [])
        folder = self.responses / "old"
        folder.mkdir()
        code, stdout, _ = call_dispatch(["clean", "--responses-dir", str(self.responses), "--keep", "0", "--json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(stdout)["dry_run"])
        self.assertTrue(folder.exists())

    def test_inspect_command_after_run(self) -> None:
        self.write_agent("alpha.py", '''
            AGENT = {"name": "alpha", "role": "responder", "model": "fake"}
            def run(request): return "ok"
        ''')
        code, _, stderr = call_dispatch(["inspect me", "--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        self.assertEqual(code, 0, stderr)
        folder = next(self.responses.iterdir()).name
        code, stdout, _ = call_dispatch(["inspect", folder, "--responses-dir", str(self.responses), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout)
        self.assertTrue(payload["ok"])
        self.assertIn("RUN.json", payload["files"])

    def test_double_dash_prompt_does_not_enable_json_mode(self) -> None:
        code, stdout, stderr = call_dispatch(["--agents-dir", str(self.agents), "--responses-dir", str(self.responses), "--", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("no responder", stderr)
        self.assertEqual(list(self.responses.iterdir()), [])

    def test_failure_output_reports_saved_artifact_directory(self) -> None:
        self.write_agent("bad.py", '''
            AGENT = {"name": "bad", "role": "responder"}
            def run(request): raise RuntimeError("agent exploded")
        ''')
        code, stdout, stderr = call_dispatch(["fail visibly", "--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("no responder succeeded", stderr)
        self.assertIn("Saved:", stderr)
        folder = next(self.responses.iterdir())
        self.assertTrue((folder / "RUN.json").exists())
        self.assertTrue((folder / "bad.md").exists())

    def test_critical_agent_failure_prevents_partial_final_answer(self) -> None:
        self.write_agent("critical_upload.py", '''
            AGENT = {"name": "critical-upload", "role": "responder"}
            def run(request): raise RuntimeError("CRITICAL: attachment upload was not confirmed")
        ''')
        self.write_agent("partial.py", '''
            AGENT = {"name": "partial", "role": "responder"}
            def run(request): return "partial answer"
        ''')

        code, stdout, stderr = call_dispatch(["critical fail", "--agents-dir", str(self.agents), "--responses-dir", str(self.responses)])

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("critical responder failure", stderr)
        self.assertIn("critical-upload", stderr)
        folder = next(self.responses.iterdir())
        self.assertFalse((folder / "FINAL.md").exists())
        manifest = json.loads((folder / "RUN.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["ok"])
        self.assertIsNone(manifest["final"])

    def test_critical_agent_failure_cancels_slow_active_responders(self) -> None:
        self.write_agent("critical_upload.py", '''
            AGENT = {"name": "critical-upload", "role": "responder"}
            def run(request): raise RuntimeError("CRITICAL: attachment upload was not confirmed")
        ''')
        self.write_agent("slow.py", '''
            import time
            AGENT = {"name": "slow", "role": "responder"}
            def run(request):
                time.sleep(5)
                return "late answer"
        ''')

        started = time.monotonic()
        code, stdout, stderr = call_dispatch([
            "critical fail",
            "--agents-dir", str(self.agents),
            "--responses-dir", str(self.responses),
            "--max-workers", "2",
        ])
        elapsed = time.monotonic() - started

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("critical responder failure", stderr)
        self.assertLess(elapsed, 2.0)

    def test_timeout_is_total_for_all_responders(self) -> None:
        self.write_agent("slow_one.py", '''
            import time
            AGENT = {"name": "slow-one", "role": "responder"}
            def run(request):
                time.sleep(5)
                return "one"
        ''')
        self.write_agent("slow_two.py", '''
            import time
            AGENT = {"name": "slow-two", "role": "responder"}
            def run(request):
                time.sleep(5)
                return "two"
        ''')
        started = time.monotonic()
        code, stdout, stderr = call_dispatch([
            "total timeout",
            "--agents-dir", str(self.agents),
            "--responses-dir", str(self.responses),
            "--timeout", "1",
            "--max-workers", "1",
        ])
        elapsed = time.monotonic() - started
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("no responder succeeded", stderr)
        self.assertLess(elapsed, 1.8)


class InstallerAndPackagingTests(unittest.TestCase):
    def test_installer_syntax_and_custom_bin_export(self) -> None:
        subprocess.run(["bash", "-n", str(INSTALL)], check=True)
        with tempfile.TemporaryDirectory(prefix="oar-install-") as tmp:
            home = Path(tmp) / "home"
            install_dir = Path(tmp) / "app"
            bin_dir = Path(tmp) / "bin custom"
            home.mkdir()
            env = {**os.environ, "HOME": str(home), "OAR_INSTALL_DIR": str(install_dir), "OAR_BIN_DIR": str(bin_dir), "PATH": os.environ.get("PATH", "")}
            result = subprocess.run(["bash", str(INSTALL)], cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue((bin_dir / "oar").exists())
            self.assertTrue((bin_dir / "omni-agents-request").exists())
            self.assertTrue((install_dir / "agents" / "chatgpt_web.py").exists())
            self.assertTrue((install_dir / "agents" / "glm_web.py").exists())
            profile = (home / ".profile").read_text(encoding="utf-8")
            self.assertIn(str(bin_dir), profile)
            subprocess.run(["bash", "-n", str(home / ".profile")], check=True)
            uninstall = subprocess.run(["bash", str(INSTALL), "--uninstall"], cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr + uninstall.stdout)
            self.assertFalse((bin_dir / "oar").exists())
            self.assertFalse(install_dir.exists())

    def test_installer_reports_incomplete_local_update_source_cleanly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oar-incomplete-source-") as tmp:
            root = Path(tmp)
            source = root / "src"
            home = root / "home"
            install_dir = root / "app"
            bin_dir = root / "bin"
            (source / "agents").mkdir(parents=True)
            home.mkdir()
            for name in ["main.py", "install.sh", "README.md", "SKILL.md", "LICENSE"]:
                shutil.copy(ROOT / name, source / name)
            shutil.copy(COMBINER, source / "agents" / "COMBINER.py")
            env = {**os.environ, "HOME": str(home), "OAR_INSTALL_DIR": str(install_dir), "OAR_BIN_DIR": str(bin_dir), "OAR_REPO_URL": str(source), "PATH": os.environ.get("PATH", "")}
            result = subprocess.run(["bash", str(INSTALL), "--update"], cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
            output = result.stderr + result.stdout
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("source tree is incomplete", output)
            self.assertNotIn("tar:", output)
            self.assertNotIn("curl:", output)

    def test_update_preserves_user_agents_responses_and_backs_up_local_combiner(self) -> None:
        subprocess.run(["bash", "-n", str(INSTALL)], check=True)
        with tempfile.TemporaryDirectory(prefix="oar-update-") as tmp:
            home = Path(tmp) / "home"
            install_dir = Path(tmp) / "app"
            bin_dir = Path(tmp) / "bin"
            home.mkdir()
            env = {**os.environ, "HOME": str(home), "OAR_INSTALL_DIR": str(install_dir), "OAR_BIN_DIR": str(bin_dir), "OAR_REPO_URL": str(ROOT), "PATH": os.environ.get("PATH", "")}
            result = subprocess.run(["bash", str(INSTALL)], cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            user_agent = install_dir / "agents" / "custom.py"
            saved_response = install_dir / "responses" / "keep" / "FINAL.md"
            user_agent.write_text('AGENT = {"name": "custom", "role": "responder"}\ndef run(request): return "custom"\n', encoding="utf-8")
            saved_response.parent.mkdir()
            saved_response.write_text("saved\n", encoding="utf-8")
            local_combiner = "# local combiner edit\nAGENT = {\"name\": \"COMBINER\", \"role\": \"combiner\", \"fanout\": False}\ndef combine(request, responses): return \"local\"\n"
            (install_dir / "agents" / "COMBINER.py").write_text(local_combiner, encoding="utf-8")
            (install_dir / "main.py").write_text("broken installed source\n", encoding="utf-8")
            update = subprocess.run(["bash", str(install_dir / "install.sh"), "--update"], cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(update.returncode, 0, update.stderr + update.stdout)
            self.assertTrue(user_agent.exists())
            self.assertTrue(saved_response.exists())
            self.assertNotEqual((install_dir / "main.py").read_text(encoding="utf-8"), "broken installed source\n")
            self.assertTrue((install_dir / "agents" / "chatgpt_web.py").exists())
            self.assertTrue((install_dir / "agents" / "glm_web.py").exists())
            self.assertEqual(stat.S_IMODE(user_agent.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(saved_response.stat().st_mode), 0o600)
            self.assertEqual((install_dir / "agents" / "COMBINER.py").read_text(encoding="utf-8"), COMBINER.read_text(encoding="utf-8"))
            backups = sorted((install_dir / ".install" / "backups").glob("COMBINER.local.*.py"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), local_combiner)
            second_update = subprocess.run(["bash", str(install_dir / "install.sh"), "--update"], cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
            self.assertEqual(second_update.returncode, 0, second_update.stderr + second_update.stdout)
            backups_after_second_update = sorted((install_dir / ".install" / "backups").glob("COMBINER.local.*.py"))
            self.assertEqual([path.name for path in backups_after_second_update], [path.name for path in backups])
            self.assertEqual(backups_after_second_update[0].read_text(encoding="utf-8"), local_combiner)

    def test_installed_script_remembers_custom_paths_without_environment(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oar-path-memory-") as tmp:
            home = Path(tmp) / "home"
            install_dir = Path(tmp) / "custom app"
            bin_dir = Path(tmp) / "custom bin"
            home.mkdir()
            install_env = {
                **os.environ,
                "HOME": str(home),
                "OAR_INSTALL_DIR": str(install_dir),
                "OAR_BIN_DIR": str(bin_dir),
                "OAR_REPO_URL": str(ROOT),
                "PATH": os.environ.get("PATH", ""),
            }
            result = subprocess.run(["bash", str(INSTALL)], cwd=ROOT, env=install_env, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            (install_dir / "main.py").write_text("broken installed source\n", encoding="utf-8")
            runtime_env = {**os.environ, "HOME": str(home), "OAR_REPO_URL": str(ROOT), "PATH": os.environ.get("PATH", "")}
            update = subprocess.run(["bash", str(install_dir / "install.sh"), "--update"], cwd=ROOT, env=runtime_env, text=True, capture_output=True, timeout=60)
            self.assertEqual(update.returncode, 0, update.stderr + update.stdout)
            self.assertNotEqual((install_dir / "main.py").read_text(encoding="utf-8"), "broken installed source\n")
            check = subprocess.run(["bash", str(install_dir / "install.sh"), "--check"], cwd=ROOT, env=runtime_env, text=True, capture_output=True, timeout=60)
            self.assertEqual(check.returncode, 0, check.stderr + check.stdout)
            uninstall = subprocess.run(["bash", str(install_dir / "install.sh"), "--uninstall"], cwd=ROOT, env=runtime_env, text=True, capture_output=True, timeout=60)
            self.assertEqual(uninstall.returncode, 0, uninstall.stderr + uninstall.stdout)
            self.assertFalse(install_dir.exists())
            self.assertFalse((bin_dir / "oar").exists())

    def test_uninstall_refuses_non_oar_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oar-uninstall-") as tmp:
            home = Path(tmp) / "home"
            target = Path(tmp) / "not-oar"
            bin_dir = Path(tmp) / "bin"
            home.mkdir()
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("important\n", encoding="utf-8")
            env = {**os.environ, "HOME": str(home), "OAR_INSTALL_DIR": str(target), "OAR_BIN_DIR": str(bin_dir), "PATH": os.environ.get("PATH", "")}
            result = subprocess.run(["bash", str(INSTALL), "--uninstall"], cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(sentinel.exists())

    def test_gitignore_keeps_combiner_trackable_and_private_agents_ignored(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("agents/*", text)
        self.assertIn("!agents/__init__.py", text)
        self.assertIn("!agents/COMBINER.py", text)
        self.assertIn("!agents/chatgpt_web.py", text)
        self.assertIn("!agents/glm_web.py", text)
        self.assertNotIn("\nagents/\n", text)

    def test_versions_are_consistent(self) -> None:
        main = load_module(MAIN, "version_main")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        install_text = INSTALL.read_text(encoding="utf-8")
        self.assertIn(f'version = "{main.APP_VERSION}"', pyproject)
        self.assertIn('license = "MIT"', pyproject)
        self.assertIn('license-files = ["LICENSE"]', pyproject)
        self.assertIn('authors = [{ name = "vivid0o0" }]', pyproject)
        self.assertIn('oar = "main:main"', pyproject)
        self.assertIn('omni-agents-request = "main:main"', pyproject)
        self.assertIn(f'APP_VERSION="{main.APP_VERSION}"', install_text)
        self.assertIn('DEFAULT_REPO="https://github.com/vivid0o0/omni-agents-request"', install_text)

    def test_license_uses_readme_owner(self) -> None:
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 vivid0o0", license_text)
        self.assertNotIn("contributors", license_text.lower())

    def test_readme_command_aliases_match_safe_parser_flags(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("`--json, --jsn`", readme)
        self.assertIn("`--timeout, --to <seconds>`", readme)
        self.assertIn("`--agents-dir, --adir, -adir <path>`", readme)
        self.assertIn("[SKILL.md](SKILL.md)", readme)
        self.assertNotIn("(SKILL.md)[SKILL.md]", readme)
        self.assertNotIn("`--agents-dir, - adir <path>`", readme)

    def test_release_zip_tree_is_exact(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oar-zip-") as tmp:
            zip_path = Path(tmp) / "release.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for relative in ["OMNI-AGENTS-REQUEST/", "OMNI-AGENTS-REQUEST/agents/", "OMNI-AGENTS-REQUEST/responses/"]:
                    archive.writestr(relative, "")
                for relative in ["agents/__init__.py", "agents/COMBINER.py", "agents/chatgpt_web.py", "agents/glm_web.py", "install.sh", "main.py", "README.md", "SKILL.md", "LICENSE"]:
                    archive.write(ROOT / relative, f"OMNI-AGENTS-REQUEST/{relative}")
            with zipfile.ZipFile(zip_path) as archive:
                names = sorted(archive.namelist())
            self.assertEqual(names, sorted([
                "OMNI-AGENTS-REQUEST/",
                "OMNI-AGENTS-REQUEST/agents/",
                "OMNI-AGENTS-REQUEST/agents/__init__.py",
                "OMNI-AGENTS-REQUEST/agents/COMBINER.py",
                "OMNI-AGENTS-REQUEST/agents/chatgpt_web.py",
                "OMNI-AGENTS-REQUEST/agents/glm_web.py",
                "OMNI-AGENTS-REQUEST/responses/",
                "OMNI-AGENTS-REQUEST/install.sh",
                "OMNI-AGENTS-REQUEST/main.py",
                "OMNI-AGENTS-REQUEST/README.md",
                "OMNI-AGENTS-REQUEST/SKILL.md",
                "OMNI-AGENTS-REQUEST/LICENSE",
            ]))

    def test_wheel_contains_cli_entry_points_and_combiner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="oar-wheel-") as tmp:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "-w", tmp],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            wheel = next(Path(tmp).glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())
                entry_points = next(name for name in names if name.endswith("entry_points.txt"))
                entry_text = archive.read(entry_points).decode("utf-8")
            self.assertIn("main.py", names)
            self.assertIn("agents/__init__.py", names)
            self.assertIn("agents/COMBINER.py", names)
            self.assertIn("agents/chatgpt_web.py", names)
            self.assertIn("agents/glm_web.py", names)
            self.assertIn("oar = main:main", entry_text)
            self.assertIn("omni-agents-request = main:main", entry_text)

    def test_managed_responders_compile_and_declare_contracts(self) -> None:
        for path in MANAGED_RESPONDERS:
            subprocess.run([sys.executable, "-m", "py_compile", str(path)], check=True)
            module = load_module(path, f"{path.stem}_contract")
            text = path.read_text(encoding="utf-8")
            self.assertEqual(module.AGENT["role"], "responder")
            self.assertTrue(module.AGENT["requires_auth"])
            self.assertTrue(callable(module.login_url))
            self.assertTrue(callable(module.auth_check))
            self.assertTrue(callable(module.browser_launch_options))
            self.assertEqual(module.browser_launch_options()["mode"], "system-cdp")
            self.assertFalse(module.browser_launch_options()["force_headed"])
            self.assertEqual(module.DEFAULT_RESPONSE_TIMEOUT_MS, 24 * 60 * 60 * 1000)
            self.assertLessEqual(module.STABLE_RESPONSE_MS, 600)
            self.assertLessEqual(module.ATTACHMENT_VERIFY_TIMEOUT_MS, 30000)
            self.assertLessEqual(module.SEND_CONFIRM_TIMEOUT_MS, 15000)
            self.assertTrue(callable(module.run))
            self.assertTrue(callable(module.cancel_generation))
            self.assertIn("auth_profile_dir", text)
            for name in ["FEATURES", "SELECTORS", "ACTIONS"]:
                self.assertIn(name, module.__dict__)
            for key in ["attachments", "prompt_bar", "send", "cancel", "message_sent", "message_received"]:
                self.assertIn(key, module.FEATURES)
            for key in ["prompt_bar", "send", "cancel", "message_received"]:
                self.assertIn(key, module.SELECTORS)

    def test_managed_responder_targets_match_release_contract(self) -> None:
        chatgpt = load_module(CHATGPT_AGENT, "chatgpt_target_contract")
        glm = load_module(GLM_AGENT, "glm_target_contract")
        self.assertEqual(chatgpt.AGENT["name"], "chatgpt-web")
        self.assertEqual(chatgpt.AGENT["model"], "gpt 5.5")
        self.assertEqual(chatgpt.AGENT["thinking"], "high")
        self.assertIn("GPT-5.5", chatgpt.TARGET_MODEL_LABELS)
        self.assertIn("High", chatgpt.TARGET_THINKING_LABELS)
        self.assertEqual(chatgpt.browser_launch_options()["headless_strategy"], "hidden-window")
        self.assertIn('[data-message-author-role="user"]', chatgpt.USER_MESSAGE_SELECTORS)
        self.assertIn("deep_research_tab", chatgpt.FEATURES)
        self.assertIn("agent_mode_tab", chatgpt.FEATURES)
        self.assertEqual(chatgpt.attachment_names_missing("Attached files: omni-connector.zip", [Path("/tmp/omni-connector.zip")]), [])
        self.assertEqual(chatgpt.attachment_names_missing("Composer is empty", [Path("/tmp/omni-connector.zip")]), ["omni-connector.zip"])
        self.assertIn("failed", chatgpt.upload_error_from_text("Upload failed for omni-connector.zip").lower())
        self.assertEqual(chatgpt.assistant_texts_after_snapshot(["old", "old"], ["old", "old"]), [])
        self.assertEqual(chatgpt.assistant_texts_after_snapshot(["old", "old", "old"], ["old", "old"]), ["old"])
        self.assertEqual(chatgpt.assistant_texts_after_snapshot(["old", "old", "new"], ["old", "old"]), ["new"])
        self.assertEqual(glm.AGENT["name"], "glm-web")
        self.assertEqual(glm.AGENT["model"], "glm 5.2")
        self.assertTrue(glm.AGENT["capabilities"]["web_search"])
        self.assertTrue(glm.AGENT["capabilities"]["deep_think"])
        self.assertIn("GLM-5.2", glm.TARGET_MODEL_LABELS)
        self.assertEqual(glm.browser_launch_options()["headless_strategy"], "true-headless")
        self.assertEqual(glm.attachment_names_missing("Files: omni-connector.zip", [Path("/tmp/omni-connector.zip")]), [])
        self.assertEqual(glm.attachment_names_missing("No attachment chip", [Path("/tmp/omni-connector.zip")]), ["omni-connector.zip"])
        self.assertIn("failed", glm.upload_error_from_text("Upload failed for omni-connector.zip").lower())
        self.assertEqual(glm.assistant_texts_after_snapshot(["old", "old"], ["old", "old"]), [])
        self.assertEqual(glm.assistant_texts_after_snapshot(["old", "old", "old"], ["old", "old"]), ["old"])
        self.assertEqual(glm.assistant_texts_after_snapshot(["old", "old", "new"], ["old", "old"]), ["new"])
        self.assertEqual(glm.MAX_GENERATION_ATTEMPTS, 1)
        self.assertEqual(glm.SERVICE_RETRY_BACKOFF_MS, 0)

    def test_managed_responder_upload_settle_uses_upload_timeout(self) -> None:
        for path, label in [(CHATGPT_AGENT, "ChatGPT"), (GLM_AGENT, "GLM")]:
            module = load_module(path, f"{path.stem}_upload_timeout_contract")

            class FakePage:
                def __init__(self) -> None:
                    self.timeout = None

                def wait_for_function(self, script: str, timeout: int = 0) -> None:
                    self.timeout = timeout
                    raise module.PlaywrightTimeoutError("stuck")

            page = FakePage()
            with self.assertRaisesRegex(RuntimeError, f"CRITICAL: Timed out waiting for {label} attachment upload"):
                module.wait_for_uploads_to_settle(page)
            self.assertEqual(page.timeout, module.ATTACHMENT_VERIFY_TIMEOUT_MS)

    def test_managed_responder_send_confirmation_is_critical(self) -> None:
        for path, label in [(CHATGPT_AGENT, "ChatGPT"), (GLM_AGENT, "GLM")]:
            module = load_module(path, f"{path.stem}_send_confirmation_contract")

            class FakePage:
                def __init__(self) -> None:
                    self.timeout = None

                def wait_for_function(self, script: str, arg=None, timeout: int = 0) -> None:
                    self.timeout = timeout
                    raise module.PlaywrightTimeoutError("not sent")

            page = FakePage()
            with self.assertRaisesRegex(RuntimeError, f"CRITICAL: {label} prompt was not confirmed as sent"):
                module.wait_for_message_sent(page, [])
            self.assertEqual(page.timeout, module.SEND_CONFIRM_TIMEOUT_MS)

    def test_chatgpt_mode_selectors_do_not_match_sidebar_model_buttons(self) -> None:
        chatgpt = load_module(CHATGPT_AGENT, "chatgpt_selector_contract")
        broad_sidebar_selectors = {
            'button[aria-label*="model" i]',
            'button:has-text("ChatGPT")',
            'button:has-text("GPT")',
        }
        self.assertTrue(broad_sidebar_selectors.isdisjoint(chatgpt.MODEL_CONTROL_SELECTORS))
        for selector in chatgpt.MODEL_CONTROL_SELECTORS:
            if "data-testid" in selector:
                continue
            self.assertTrue(selector.startswith(("main ", "header ")), selector)

    def test_chatgpt_hidden_target_model_gets_specific_error(self) -> None:
        chatgpt = load_module(CHATGPT_AGENT, "chatgpt_hidden_model_contract")

        class FakeLocator:
            def evaluate(self, script: str) -> str:
                return '{"value":"gpt-5.5","label":"GPT-5.5"}'

        class FakePage:
            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator()

        error = chatgpt.mode_unavailable_error(FakePage(), ("GPT-5.5",), "ChatGPT model", "control was not found")
        self.assertIn("hidden page data", str(error))
        self.assertIn("not exposed as a selectable UI control", str(error))

    def test_chatgpt_intelligence_picker_selectors_are_scoped(self) -> None:
        chatgpt = load_module(CHATGPT_AGENT, "chatgpt_intelligence_picker_contract")
        self.assertIn("intelligence_picker", chatgpt.SELECTORS)
        self.assertIn('main button[aria-expanded]:has-text("High")', chatgpt.INTELLIGENCE_CONTROL_SELECTORS)
        self.assertEqual(chatgpt.INTELLIGENCE_MENU_SELECTOR, '[data-testid="composer-intelligence-picker-content"]')

        model_selectors = chatgpt.scoped_intelligence_option_selectors(("GPT-5.5",), ("menuitem",))
        thinking_selectors = chatgpt.scoped_intelligence_option_selectors(("High",), ("menuitemradio",))

        self.assertTrue(all(selector.startswith(chatgpt.INTELLIGENCE_MENU_SELECTOR) for selector in model_selectors))
        self.assertTrue(all(selector.startswith(chatgpt.INTELLIGENCE_MENU_SELECTOR) for selector in thinking_selectors))
        self.assertTrue(any('[role="menuitem"]' in selector for selector in model_selectors))
        self.assertTrue(any('[role="menuitemradio"]' in selector for selector in thinking_selectors))

    def test_chatgpt_composer_band_rejects_sidebar_controls(self) -> None:
        chatgpt = load_module(CHATGPT_AGENT, "chatgpt_composer_band_contract")
        composer = {"x": 520, "y": 450, "width": 790, "height": 130}
        prompt_bar_control = {"x": 1141, "y": 533, "width": 77, "height": 36}
        sidebar_control = {"x": 14, "y": 539, "width": 34, "height": 36}

        self.assertTrue(chatgpt.boxes_share_composer_band(composer, prompt_bar_control))
        self.assertFalse(chatgpt.boxes_share_composer_band(composer, sidebar_control))

    def test_glm_promptbar_selectors_match_live_controls(self) -> None:
        glm = load_module(GLM_AGENT, "glm_promptbar_selector_contract")
        self.assertIn('form [type="button"]:has-text("Deep Think")', glm.DEEP_THINK_SELECTORS)
        self.assertIn('form button[type="button"][data-active]', glm.WEB_SEARCH_SELECTORS)
        self.assertTrue(glm.deep_think_text_selected("Deep Think Max"))
        self.assertFalse(glm.deep_think_text_selected("Deep Think"))
        self.assertFalse(glm.deep_think_text_selected("Deep Space WebGL Sim"))
        self.assertTrue(glm.exact_transient_label("OK", "OK"))
        self.assertFalse(glm.exact_transient_label("OAR_GLM_DOM_OK Format", "OK"))
        self.assertTrue(glm.conversation_route_active(SimpleNamespace(url="https://chat.z.ai/c/7b7c84d5-dfa3-4097-98f5-f2e5d36be02f")))
        self.assertFalse(glm.conversation_route_active(SimpleNamespace(url="https://chat.z.ai/")))

    def test_glm_response_selectors_target_assistant_not_user_prompt(self) -> None:
        glm = load_module(GLM_AGENT, "glm_response_selector_contract")
        self.assertIn(".chat-assistant", glm.ASSISTANT_SELECTORS)
        self.assertIn(".chat-user", glm.USER_MESSAGE_SELECTORS)
        self.assertIn('button#sidebar-new-chat-button:has-text("New Chat")', glm.NEW_CHAT_SELECTORS)
        self.assertNotIn("article", glm.ASSISTANT_SELECTORS)
        self.assertNotIn('[class*="markdown" i]', glm.ASSISTANT_SELECTORS)
        self.assertEqual(glm.clean_assistant_text("Thought Process >\\nOAR_GLM_OK"), "OAR_GLM_OK")
        self.assertEqual(glm.clean_assistant_text("Thought Process OAR_GLM_OK"), "OAR_GLM_OK")
        self.assertEqual(glm.clean_assistant_text("Thinking...\nSkip"), "")
        self.assertEqual(glm.clean_assistant_text("Thinking...\nSkip\nActual answer"), "Actual answer")
        self.assertFalse(glm.substantive_assistant_text("Thinking...\nSkip"))
        self.assertTrue(glm.substantive_assistant_text("Thinking...\nSkip\nActual answer"))
        self.assertIn("transient service error", glm.service_error_response("Model is currently at capacity. Please try again later."))
        self.assertIn("transient service error", glm.service_error_response("rate-limited"))
        self.assertEqual(glm.service_error_response("OAR_GLM_OK"), "")

    def test_glm_composer_band_rejects_sidebar_controls(self) -> None:
        glm = load_module(GLM_AGENT, "glm_composer_band_contract")
        composer = {"x": 481, "y": 399, "width": 742, "height": 40}
        web_toggle = {"x": 515, "y": 456, "width": 28, "height": 28}
        deep_think = {"x": 1049, "y": 456, "width": 140, "height": 28}
        sidebar_control = {"x": 6, "y": 1520, "width": 249, "height": 36}

        self.assertTrue(glm.boxes_share_composer_band(composer, web_toggle))
        self.assertTrue(glm.boxes_share_composer_band(composer, deep_think))
        self.assertFalse(glm.boxes_share_composer_band(composer, sidebar_control))

    def test_glm_web_search_candidate_rejects_promptbar_decoys(self) -> None:
        glm = load_module(GLM_AGENT, "glm_web_search_candidate_contract")
        box = {"x": 515, "y": 456, "width": 28, "height": 28}

        class FakeLocator:
            def __init__(self, text: str = "", attrs: dict[str, str] | None = None, payload: dict[str, object] | None = None):
                self.text = text
                self.attrs = attrs or {}
                self.payload = payload or {}

            def inner_text(self, timeout: int = 0) -> str:
                return self.text

            def get_attribute(self, name: str, timeout: int = 0) -> str | None:
                return self.attrs.get(name)

            def evaluate(self, script: str) -> dict[str, object]:
                return self.payload

        real_icon_payload = {"tag": "button", "type": "button", "dataActive": "false", "disabled": False, "inForm": True, "svgCount": 1}
        self.assertTrue(glm.web_search_control_candidate(FakeLocator(payload=real_icon_payload), box))
        self.assertFalse(glm.web_search_control_candidate(FakeLocator(attrs={"id": "upload-file-button", "aria-label": "More"}, payload=real_icon_payload), box))
        self.assertFalse(glm.web_search_control_candidate(FakeLocator(attrs={"aria-label": "Send Message"}, payload=real_icon_payload), box))
        self.assertFalse(glm.web_search_control_candidate(FakeLocator(payload={**real_icon_payload, "dataActive": None}), box))
        self.assertFalse(glm.web_search_control_candidate(FakeLocator(payload={**real_icon_payload, "svgCount": 0}), box))
        self.assertFalse(glm.web_search_control_candidate(FakeLocator(payload=real_icon_payload), {"x": 515, "y": 456, "width": 140, "height": 28}))

    def test_managed_responders_static_contract_loads_without_playwright(self) -> None:
        code = """
import importlib.util
import json
import tempfile
from pathlib import Path

payload = []
for relative in ["agents/chatgpt_web.py", "agents/glm_web.py"]:
    path = Path(relative).resolve()
    spec = importlib.util.spec_from_file_location(path.stem + "_no_site", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    item = {
        "agent": module.AGENT["name"],
        "mode": module.browser_launch_options()["mode"],
        "auth": callable(module.auth_check),
    }
    try:
        module.run({
            "prompt": "hello",
            "attachments": [],
            "agent": {"auth_profile_dir": tempfile.mkdtemp(prefix="oar-profile-")},
        })
    except RuntimeError as exc:
        item["run_error"] = str(exc)
    else:
        raise SystemExit(f"{relative} unexpectedly succeeded without Playwright")
    payload.append(item)
print(json.dumps(payload, sort_keys=True))
"""
        result = subprocess.run([sys.executable, "-S", "-c", code], cwd=ROOT, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual([item["agent"] for item in payload], ["chatgpt-web", "glm-web"])
        for item in payload:
            self.assertEqual(item["mode"], "system-cdp")
            self.assertTrue(item["auth"])
            self.assertIn("playwright is required", item["run_error"].lower())

    def test_chatgpt_auth_check_rejects_composer_without_account_signal(self) -> None:
        module = load_module(CHATGPT_AGENT, "chatgpt_agent_logged_out")

        class FakeLocator:
            def __init__(self, visible: bool):
                self.visible = visible
                self.first = self

            def is_visible(self, timeout: int = 0) -> bool:
                return self.visible

        class FakePage:
            def locator(self, selector: str) -> FakeLocator:
                login_selector = "Log in" in selector or "Sign up" in selector
                composer_selector = selector in module.COMPOSER_SELECTORS
                account_selector = selector in module.AUTHENTICATED_SELECTORS
                return FakeLocator(composer_selector and not login_selector and not account_selector)

        self.assertFalse(module.auth_check(FakePage()))

    def test_chatgpt_auth_check_accepts_composer_with_account_signal(self) -> None:
        module = load_module(CHATGPT_AGENT, "chatgpt_agent_logged_in")

        class FakeLocator:
            def __init__(self, visible: bool):
                self.visible = visible
                self.first = self

            def is_visible(self, timeout: int = 0) -> bool:
                return self.visible

        class FakePage:
            def locator(self, selector: str) -> FakeLocator:
                if "Log in" in selector or "Sign up" in selector:
                    return FakeLocator(False)
                return FakeLocator(selector in module.COMPOSER_SELECTORS or selector in module.AUTHENTICATED_SELECTORS)

        self.assertTrue(module.auth_check(FakePage()))

    def test_chatgpt_waits_for_delayed_authenticated_state(self) -> None:
        module = load_module(CHATGPT_AGENT, "chatgpt_agent_delayed_auth")
        calls = {"count": 0}

        class FakePage:
            def wait_for_timeout(self, timeout: int) -> None:
                return None

        original_auth_check = module.auth_check
        try:
            def delayed_auth_check(page):
                calls["count"] += 1
                return calls["count"] >= 3

            module.auth_check = delayed_auth_check
            self.assertTrue(module.wait_for_authenticated_state(FakePage(), 1000))
            self.assertEqual(calls["count"], 3)
        finally:
            module.auth_check = original_auth_check

    def test_chatgpt_system_cdp_context_waits_for_profile_flush_before_terminating(self) -> None:
        module = load_module(CHATGPT_AGENT, "chatgpt_agent_cdp_shutdown")
        launched: dict[str, object] = {}

        class FakeProcess:
            def __init__(self, command: list[str]):
                self.command = command
                self.returncode = None
                self.terminated = False
                self.wait_calls: list[float | None] = []
                launched["process"] = self

            def poll(self):
                return self.returncode

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = 0

            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                if launched.get("browser_closed"):
                    self.returncode = 0
                    return 0
                raise subprocess.TimeoutExpired(self.command, timeout)

            def kill(self) -> None:
                self.returncode = -9

        class FakeBrowser:
            contexts = [object()]

            def close(self) -> None:
                launched["browser_closed"] = True

        fake_engine = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda endpoint: FakeBrowser()))
        original_popen = module.subprocess.Popen
        original_port = module.find_open_port
        original_wait = module.wait_for_cdp_endpoint
        try:
            module.subprocess.Popen = lambda command, **_: FakeProcess(command)
            module.find_open_port = lambda: 48123
            module.wait_for_cdp_endpoint = lambda port, timeout: f"http://127.0.0.1:{port}"
            with module.system_cdp_context(
                fake_engine,
                Path(tempfile.mkdtemp(prefix="chatgpt-profile-")),
                {"executable_path": sys.executable},
                headless=False,
            ):
                pass
        finally:
            module.subprocess.Popen = original_popen
            module.find_open_port = original_port
            module.wait_for_cdp_endpoint = original_wait

        process = launched["process"]
        self.assertFalse(process.terminated)
        self.assertTrue(process.wait_calls)

    def test_managed_responder_browsers_share_agent_process_group_for_timeout_cleanup(self) -> None:
        for path in MANAGED_RESPONDERS:
            module = load_module(path, f"{path.stem}_process_group_contract")
            launched: dict[str, object] = {}

            class FakeProcess:
                returncode = 0

                def __init__(self, command: list[str], **kwargs):
                    launched["kwargs"] = kwargs

                def poll(self):
                    return self.returncode

                def wait(self, timeout=None):
                    return self.returncode

                def terminate(self) -> None:
                    return None

                def kill(self) -> None:
                    return None

            class FakeBrowser:
                contexts = [object()]

                def close(self) -> None:
                    return None

            fake_engine = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda endpoint: FakeBrowser()))
            original_popen = module.subprocess.Popen
            original_port = module.find_open_port
            original_wait = module.wait_for_cdp_endpoint
            try:
                module.subprocess.Popen = lambda command, **kwargs: FakeProcess(command, **kwargs)
                module.find_open_port = lambda: 48123
                module.wait_for_cdp_endpoint = lambda port, timeout: f"http://127.0.0.1:{port}"
                with module.system_cdp_context(
                    fake_engine,
                    Path(tempfile.mkdtemp(prefix="oar-agent-profile-")),
                    {"executable_path": sys.executable},
                    headless=True,
                ):
                    pass
            finally:
                module.subprocess.Popen = original_popen
                module.find_open_port = original_port
                module.wait_for_cdp_endpoint = original_wait

            self.assertFalse(launched["kwargs"].get("start_new_session", False), path.name)

    def test_skill_auth_guidance_is_service_generic(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8").lower()
        self.assertNotIn("chatgpt", skill)
        self.assertNotIn("z.ai", skill)
        self.assertIn("browser automation", skill)

    def test_skill_is_a_complete_agent_build_playbook(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        lower = skill.lower()
        for section in [
            "## Service Intake",
            "## Agent File Structure",
            "## Feature Map Contract",
            "## Build Playbook",
            "## Responder Contract",
            "## Optional Dependencies",
            "## Browser Automation",
            "## Auth And Session State",
            "## Runtime Operations",
            "## Manual Verification Matrix",
            "## Evidence To Inspect",
            "## Avoid",
            "## Troubleshooting",
            "## Done Criteria",
        ]:
            self.assertIn(section, skill)
        for phrase in [
            "validate --json",
            "list --json",
            "--init-auth",
            "--doctor",
            "agents/<service>.py",
            "AGENT",
            "FEATURES",
            "SELECTORS",
            "ACTIONS",
            "run(request)",
            "login_url",
            "auth_check",
            "isolated browser profile",
            "No fixed sleeps",
            "auth_check false positives",
            "browser_launch_options",
            "system-cdp",
            "response_dir",
            "agent.auth_profile_dir",
            "RUN.json",
            "log.jsonl",
            "python -m unittest discover -s tests -v",
            "python -m pip wheel .",
            "raw GitHub installer",
            "rate limits",
            "CAPTCHA",
            "never copy cookies",
            "real end-to-end request",
        ]:
            self.assertIn(phrase.lower(), lower)
        self.assertIn("oar clean --keep 20 --delete", skill)
        self.assertNotIn("--yes", skill)


class CombinerRegressionTests(unittest.TestCase):
    def test_local_combiner_preserves_multiline_markdown_blocks(self) -> None:
        combiner = load_module(COMBINER, "combiner_markdown_test")
        result = combiner.combine(
            {"prompt": "show command"},
            [
                {
                    "name": "agent1",
                    "content": "## Steps\n\nRun this:\n\n```bash\nprintf 'hello'\n```\n",
                    "metadata": {"name": "agent1"},
                }
            ],
        )
        content = result["content"]
        self.assertIn("```bash\nprintf 'hello'\n```", content)
        self.assertNotIn("```bash printf", content)

    def test_local_combiner_filters_transient_ui_responses(self) -> None:
        combiner = load_module(COMBINER, "combiner_transient_test")
        result = combiner.combine(
            {"prompt": "diagnose"},
            [
                {"name": "glm-web", "content": "Thinking...\nSkip", "metadata": {"name": "glm-web"}},
                {"name": "chatgpt-web", "content": "Real answer.", "metadata": {"name": "chatgpt-web"}},
            ],
        )

        content = result["content"]
        self.assertIn("Real answer.", content)
        self.assertNotIn("Thinking", content)
        self.assertNotIn("Skip", content)
        self.assertIn("- chatgpt-web", content)
        self.assertNotIn("- glm-web", content)

    def test_local_combiner_uses_sources_section_instead_of_inline_tags(self) -> None:
        combiner = load_module(COMBINER, "combiner_sources_test")
        result = combiner.combine(
            {"prompt": "compare"},
            [
                {"name": "agent1", "content": "First useful point.", "metadata": {"name": "agent1"}},
                {"name": "agent2", "content": "Second useful point.", "metadata": {"name": "agent2"}},
            ],
        )

        content = result["content"]
        body = content.split("## Sources", 1)[0]
        self.assertNotIn("[agent1]", body)
        self.assertNotIn("[agent2]", body)
        self.assertIn("- agent1", content)
        self.assertIn("- agent2", content)

    def test_local_combiner_converts_language_labels_to_code_blocks(self) -> None:
        combiner = load_module(COMBINER, "combiner_language_label_test")
        result = combiner.combine(
            {"prompt": "show command"},
            [
                {
                    "name": "agent1",
                    "content": "## Steps\n\nRun this:\n\nBash\nxfwm4 --replace & disown\n",
                    "metadata": {"name": "agent1"},
                }
            ],
        )

        content = result["content"]
        self.assertIn("```bash\nxfwm4 --replace & disown\n```", content)
        self.assertNotIn("Bash xfwm4", content)


if __name__ == "__main__":
    unittest.main()
