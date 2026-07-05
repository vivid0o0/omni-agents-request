---
name: omni-agents-request
description: Use when installing, configuring, extending, auditing, or troubleshooting OMNI-AGENTS-REQUEST (OAR), including responder agents, browser automation, saved sessions, command failures, installer updates, or release verification.
---

# OMNI-AGENTS-REQUEST

OAR sends one prompt to every responder in `agents/<name>.py`, writes private run artifacts under `responses/<request-name>/`, then calls `agents/COMBINER.py` to produce `FINAL.md`. `README.md` is the public contract. This skill is the implementation and operations contract for building real OAR responders.

## Architecture Contract

- `main.py` owns CLI parsing, responder discovery, auth orchestration, private artifact writing, diagnostics, and update routing.
- `agents/COMBINER.py` is reserved for synthesis only. It must declare `role: combiner`, `fanout: false`, and `combine(request, responses)`.
- Every responder is one import-safe Python file in `agents/<service>.py`.
- Managed responders may ship with the app, but custom responders must use their own filenames so updates can refresh managed files without overwriting user work.
- `responses/`, `agents/.auth/`, local logs, browser profiles, secrets, and generated files are private runtime state and must not be committed.

## Service Intake

Capture these facts before changing or creating a responder:

| Fact | Required decision |
|---|---|
| Service and exact model or mode | Literal `AGENT.model`; runtime selection target. |
| Thinking, research, tool, or agent mode | Literal `AGENT.thinking`; UI/API state that must be enabled before send. |
| Auth method | `requires_auth`, `login_url`, `auth_check`, and `--init-auth` behavior. |
| Browser automation or API | Playwright/browser profile flow or HTTP client flow. |
| Attachment support | Upload path, accepted file types, and completion signal. |
| Output completion signal | Streaming done condition and extraction selector/parser. |
| Failure modes | Rate limits, expired auth, unavailable models, blocked pages, upload failures, empty output. |

Do not invent account capabilities, model names, UI state, selectors, or output formats. Inspect the live service or ask for the missing fact.

## Agent File Structure

Use the same section order for every responder so agents can review and extend files quickly:

1. File header and imports.
2. Optional dependency guard.
3. `AGENT` literal metadata.
4. `FEATURES` map.
5. Constants and target model/mode labels.
6. `SELECTORS` map.
7. `ACTIONS` map.
8. OAR auth contract: `login_url()`, `auth_check(page)`, `browser_launch_options()`.
9. `run(request)` entrypoint.
10. Request and attachment parsing.
11. Browser/API client setup.
12. Page/API state detection.
13. Model, mode, tab, and toggle selection.
14. Prompt bar, send, cancel, and upload actions.
15. Message sent and message received extraction.
16. Small utility functions.

Keep `AGENT` statically readable. OAR discovers metadata without importing the file, so the assigned value must be literal Python data.

## Feature Map Contract

Every browser responder should map the UI or API surface explicitly:

```python
AGENT = {
    "name": "service-web",
    "role": "responder",
    "fanout": True,
    "requires_auth": True,
    "model": "exact target model",
    "thinking": "exact target mode",
    "description": "What this responder actually does.",
    "capabilities": {
        "text_prompt": True,
        "attachments": True,
        "prompt_bar": True,
        "send": True,
        "cancel": True,
        "message_sent": True,
        "message_received": True,
        "model_selection": True,
        "persistent_session": True,
        "isolated_browser_profile": True,
    },
}

FEATURES = {
    "model": "Select and verify the requested model.",
    "attachments": "Upload every attachment and wait for upload completion.",
    "prompt_bar": "Find an editable composer and write the exact prompt.",
    "send": "Activate a visible enabled send control.",
    "cancel": "Map the stop/cancel control for active generations.",
    "message_sent": "Capture pre-send state so the new response is identifiable.",
    "message_received": "Wait for a non-empty stable assistant response.",
}

SELECTORS = {
    "prompt_bar": (...,),
    "send": (...,),
    "cancel": (...,),
    "message_received": (...,),
}

ACTIONS = {
    "send": "fill prompt bar and activate send",
    "read_response": "wait for stable assistant output",
}
```

Maps are not decorative. They are the file-level index that tells future agents where each feature is represented and how runtime code should behave.

## Build Playbook

1. Read `README.md`.
2. Inspect the current app: `oar --version`, `oar list --json`, `oar validate --json`.
3. Inspect the target service manually before writing selectors or mode logic.
4. Create or edit `agents/<service>.py` using the file structure above.
5. Implement the smallest real prompt path first. No dummy output, cached output, or simulated service response.
6. Add model/mode selection and verify selected state before prompt submission.
7. Add attachments and verify the service accepted every file.
8. Add auth hooks and browser launch options only through OAR-owned profile paths.
9. Run `oar validate --json` after structural edits.
10. If `requires_auth` is true, run `oar --init-auth`, authenticate through the dashboard, then run `oar --doctor --json`.
11. Run a real end-to-end request and inspect `FINAL.md`, each responder artifact, `RUN.json`, and `log.jsonl`.

## Responder Contract

Every responder must be import-safe. Importing the module must not start browsers, make network calls, read private profiles, create files, or mutate state.

```python
def run(request):
    prompt = str(request.get("prompt", "")).strip()
    if not prompt:
        raise RuntimeError("empty prompt")
    return {
        "content": "real service response text",
        "metadata": {"service": AGENT["name"]},
    }
```

`request` fields:

| Field | Use |
|---|---|
| `prompt` | Strip and reject empty prompts. |
| `attachments` | Re-check every path before upload. Do not silently skip invalid files. |
| `created` | Diagnostic timestamp only. |
| `response_dir` | Private artifact directory for intentional debug artifacts only. |
| `agent.file` / `agent.stem` | Current responder identity. |
| `agent.auth_profile_dir` | OAR-owned isolated browser profile path. |

Return non-empty content. Raise clear errors for expired sessions, blocked pages, unsupported models, missing controls, upload failures, rate limits, CAPTCHA, empty responses, and UI/API contract changes.

## Optional Dependencies

Browser responders should stay import-safe when Playwright is absent:

```python
try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError as exc:
    _playwright_error = exc

    class PlaywrightError(Exception):
        pass

    def sync_playwright():
        raise RuntimeError("Playwright is required. Install it and browser binaries.") from _playwright_error
```

Raise dependency errors only inside the path that needs the dependency, usually `run()` or auth probing.

## Auth And Session State

Auth-required browser responders set `"requires_auth": True` and define:

```python
def login_url():
    return "https://service.example/"

def auth_check(page):
    return composer_visible(page) and account_controls_visible(page)

def browser_launch_options():
    return {
        "mode": "system-cdp",
        "browser": "chromium",
        "force_headed": True,
    }
```

Prevent auth_check false positives:

| Weak signal | Required stronger signal |
|---|---|
| Composer exists | Composer plus account/profile/workspace control. |
| URL is the app URL | URL plus authenticated UI state. |
| Login button absent | Positive authenticated element visible. |
| Profile directory exists | `.authenticated` marker plus `--doctor` validation. |

OAR stores sessions under `agents/.auth/<responder-id>/`. Never copy cookies, never read the user's real browser profile, never attach to the user's active browser, and never add parallel credential storage.

Allowed `browser_launch_options()` keys are `mode`, `browser`, `channel`, `executable_path`, `args`, `headless`, `force_headed`, `accept_downloads`, `locale`, and `viewport`. Do not set profile paths, storage state, cookies, CDP endpoints, or remote-debugging flags. For `system-cdp`, close the browser gracefully and wait for process exit so profile writes persist.

## Browser Automation

No fixed sleeps. Wait for the condition that proves the next step can start:

| Step | Required condition |
|---|---|
| Page loaded | App shell is visible and blocking interstitials are absent. |
| Login complete | `auth_check(page)` returns true. |
| Model selected | Requested model label is visibly active. |
| Mode selected | Requested toggles/tabs are verified active. |
| Prompt ready | Composer is visible, editable, enabled, and unobstructed. |
| Files uploaded | File input accepted paths and upload/progress indicators are gone. |
| Send clicked | Send control was visible/enabled and no immediate validation error appeared. |
| Response complete | Latest assistant output is non-empty, stable, and no stop/busy control remains. |

Selector order:

1. Accessible role and name.
2. Stable labels or placeholders.
3. Stable `data-testid` attributes.
4. Structural CSS only when semantic selectors are unavailable.

Do not use brittle positional selectors unless there is no alternative and surrounding state is also verified.

## Runtime Operations

Use these commands while building and auditing:

```bash
oar list --json
oar validate --json
oar --init-auth
oar --doctor --json
oar "small exact prompt" --timeout 300
oar responses --json
oar inspect <response-folder> --json
oar clean --keep 20
oar clean --keep 20 --delete
```

`clean` is dry-run by default. Use `--delete` only for intentional cleanup.

## Manual Verification Matrix

| Requirement | Command | Evidence |
|---|---|---|
| Static source compiles | `python -m py_compile main.py agents/COMBINER.py agents/*.py tests/test_oar.py` | Exit `0`. |
| Installer syntax valid | `bash -n install.sh` | Exit `0`. |
| Contracts valid | `oar validate --json` | `"ok": true`; all responder errors empty. |
| Discovery accurate | `oar list --json` | Expected files, auth flags, and metadata. |
| Auth state valid | `oar --doctor --json` | Auth responders show authenticated or actionable failures. |
| Auth TUI usable | `oar --init-auth` | Arrow-key dashboard, green/red dots, saved-session re-auth. |
| Real prompt works | `oar "small exact prompt" --timeout <seconds>` | `FINAL.md` and responder `.md` contain real output. |
| Attachments work | `oar "use attached file" --attach <file>` | Service actually received the file. |
| Failure artifacts work | Break a disposable responder | `RUN.json`, failed responder `.md`, and `log.jsonl` are written and redacted. |
| Release package builds | `python -m pip wheel . -w /tmp/oar-wheel-check --no-deps` | Wheel builds successfully. |
| Tests pass | `python -m unittest discover -s tests -v` | Full suite passes. |
| Remote install works | Raw GitHub installer in a temporary `HOME` | Version and `validate --json` pass. |

For release work, also test a no-optional-dependency environment and the live installed command after update.

## Evidence To Inspect

| File | Check |
|---|---|
| `FINAL.md` | Combined answer is present and not just an error wrapper. |
| `<responder>.md` | Each responder has real output or a clear failure block. |
| `RUN.json` | `ok`, responder list, failure messages, and file list match reality. |
| `log.jsonl` | Events exist, secrets are redacted, and raw private prompts or page dumps are absent. |

## Troubleshooting

| Symptom | Fix |
|---|---|
| `source tree is incomplete` | Verify `main.py`, `install.sh`, `README.md`, `SKILL.md`, `LICENSE`, `agents/COMBINER.py`, and managed responders exist. |
| `playwright is required` | Install Playwright and browser binaries, then rerun `oar --doctor --json`. |
| Saved login is red | Re-run `oar --init-auth`; validation failure does not mean the marker should be deleted. |
| Login succeeded but run fails | Re-check `auth_check`, mode selection, and browser shutdown persistence. |
| Bot check or CAPTCHA appears | Stop automation and complete required verification through the auth dashboard. |
| Rate limits or quota reached | Raise a clear error with service/mode and visible retry information. |
| Empty response | Fix response completion or extraction. Do not make the combiner hide it. |
| Attachment missing | Verify file exists, account supports upload, input accepts the file, and upload completion is observed. |
| `no responder succeeded` | Inspect each responder artifact and `RUN.json`; fix the first real responder failure. |
| `COMBINER.py` missing or invalid | Restore the reserved combiner file; never use it as a responder. |

## Avoid

- Do not hardcode behavior for one prompt, account, machine, viewport, or transient UI state.
- Do not return dummy, simulated, cached, guessed, or placeholder model responses.
- Do not use fixed sleeps for login, upload, send, or response completion.
- Do not silently skip failed auth, files, mode selection, blocked pages, rate limits, CAPTCHA, or empty output.
- Do not log tokens, cookies, authorization headers, raw private prompts, raw private file contents, or raw page dumps.
- Do not copy cookies, read real browser profiles, or attach to an active user browser.
- Do not put responder logic in `COMBINER.py`.
- Do not commit `agents/.auth/`, `responses/`, caches, build output, local logs, or private user agents.
- Do not claim completion from tests alone; prove the real `oar` command path works.

## Done Criteria

An OAR setup or release is not done until all applicable items are true:

- `README.md`, command behavior, tests, and this skill agree.
- `oar validate --json`, `oar list --json`, and `oar --doctor --json` have been run.
- Every requested responder has a real service-backed output path.
- Auth-required responders use only `agent.auth_profile_dir` and pass saved-session checks.
- Requested model/mode/toggles are selected and verified before send.
- Attachments, if requested, were manually verified with an actual file.
- `RUN.json`, `log.jsonl`, `FINAL.md`, and responder artifacts were inspected.
- Full tests and packaging checks passed for release work.
- The raw GitHub installer and live installed command were verified after push for release work.
