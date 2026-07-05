# omni-agents-request (OAR)

OAR sends your prompt to every configured LLM in `agents/<name>.py` (including services driven through browser automation), then calls `agents/COMBINER.py` to merge everything into the final response.

OAR is agent-driven, so your local agent handles setup and troubleshooting. You just specify which service, model, and thinking level to use, and it takes care of the rest. It works especially well for browser automation against services that do not expose the exact model or workflow through an API. This is great because:
1) It's free.
2) You can use different variants such as Deep Research or Agent Mode.

# Why use OAR?

- **Smarter:** You get broader and deeper responses than asking one AI. Multiple models provide multiple perspectives, which is especially useful for code review and research.
- **No headache:** Your agent handles everything smoothly thanks to SKILL.md. Login is a one-time, one-click affair through `oar --init-auth`.
- **Sub-agents:** Get smart one-shot subagents that answer the request intelligently at no extra cost, because it's completely free when using browser automation (which is the main focus of this tool).
- **Full control:** Each LLM endpoint lives in `agents/`, so you can connect any service you like: custom proxy, OAuth, API, browser automation, etc. It's also fully open source, so you can do whatever you want with it!
- **Freedom:** Browser automation gives you more freedom; you're not restricted to API-exposed models, you can use UI-only features such as Deep Research and Agent Mode. You can also use services that don't even expose an API, like NotebookLM.
- **Ready-to-use:** The release includes managed ChatGPT and GLM browser-automation responders under `agents/`, and the same responder contract works for other services.
- **Tip:** You can use multiple instances of the same model, just duplicate and rename the file.

## Smart install

Fill this prompt template and give it to your favorite local agent:

```text
Read this skill first:
https://raw.githubusercontent.com/vivid0o0/omni-agents-request/main/SKILL.md
> IMPORTANT: Make sure to save this in your skills dir.

Install and configure omni-agents-request on this machine with help from SKILL.md
Repository:
https://github.com/vivid0o0/omni-agents-request

Services to include:
- <service name>: <model>, thinking level <level>, auth method <API/OAuth/Browser Automation>
- <service name>: <model>, thinking level <level>, auth method <API/OAuth/Browser Automation>
...

If any service uses browser automation, run `oar --init-auth` afterward and log in to that service when the browser window opens.

NOTE: This is a prompt template, if any <...> field is missing, ask the user the questions directly.
```

## Usage

```bash
oar "Your prompt here"
oar "Analyze this file" --attach path/to/file.ext
```

## Response output

Each run creates a folder named after the request:

```text
responses/<request-name>/
├── <agent-name>.md # Each agent gets their own `<agent-name>.md` file. The final combined answer is in `FINAL.md`
├── FINAL.md
└── log.jsonl
```

## Login and sessions

Browser-automation responders that need a login never touch your real browser. Each one gets its own isolated, persistent browser profile under `agents/.auth/<agent-name>/`.

- The first time a prompt run needs a responder with no saved session, OAR opens a browser window for that one login.
- Managed browser responders run without a visible login window after auth. Bot-sensitive services can use an off-screen system browser with the same isolated OAR profile when Chrome headless is blocked.
- `oar --init-auth` opens an arrow-key auth dashboard. It lists auth-required responders only, sorts saved sessions first, uses green/red dots for session state, and lets you authenticate or re-authenticate any listed responder.
- `oar --doctor` reports session validity per service, so an expired login shows up as a clear per-service message instead of a failed run.

## Commands

- `oar` / `omni-agents-request`

| Flag | Description |
|---|---|
| `--attach, -a <path>` | Attach files. Can be repeated, comma-separated, or followed by multiple paths. |
| `--list, -ls` | List configured responder agents, combiner status, and auth session status. |
| `--json, --jsn` | Print structured JSON output. |
| `--timeout, --to <seconds>` | Limit total wait time for responder agents. Default: 86400 seconds. |
| `--agents-dir, --adir, -adir <path>` | Use a custom agents directory. |
| `--logs-dir, -ldir <path>` | Use a custom responses/logs directory. |
| `--update, -upd` | Updates OAR, preserves custom agents and responses, and refreshes managed runtime files. |
| `--doctor, -doc` | Run doctor diagnostics, including per-service session checks. |
| `--init-auth, -ia` | Open the auth dashboard for browser-automation responders. |
| `--version` | Print the installed OAR version. |

## Runtime structure

```text
omni-agents-request/
├── agents/
│   ├── COMBINER.py
│   ├── chatgpt_web.py
│   ├── glm_web.py
│   └── .auth/
│       └── <agent-name>/        # isolated browser profile per browser-automation responder, gitignored
├── responses/
├── install.sh
├── main.py
├── README.md
└── SKILL.md
```

Managed responders live in `agents/` and are discovered directly. For custom variants, duplicate a managed responder under a new filename before editing it, then run `oar --init-auth` if it declares `requires_auth`.

## Exit behavior

| Case | Exit |
|---|---:|
| Successful final result | `0` |
| Usage or attachment error | `2` |
| No responders configured | `1` |
| No responder succeeded | `1` |
| Combiner missing or failed | `1` |

> **Full technical info in [SKILL.md](SKILL.md).**

## Requirements

- Linux or macOS
- Python 3
- A local agent that has tool calls, vision, and preferably a skills dir for the SKILL.md. Basically any modern agent works: Hermes Agent, Claude Code, OpenClaw, etc.
> IMPORTANT: Use a capable model for setup since it needs decent coding capabilities. So anything similar or smarter than GPT-5.5 / fable 5

## ⚠️ WARNING

Use at your own risk! Make sure to follow the targeted services' policies, since browser automation is a grey area.

## License

[MIT](LICENSE)
