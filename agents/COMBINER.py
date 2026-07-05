# COMBINER.py -- OAR combiner agent
# description: Reserved combiner-only agent that merges successful responder outputs into one final answer.
# Tags: combiner, agents, final-response, synthesis
# date: 2026-07-06

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


# ─── METADATA ─────────────────────────────────────────────────────────────

AGENT = {
    "name": "COMBINER",
    "role": "combiner",
    "fanout": False,
    "mode": "local",
    "model_source_agent": "",
    "model": "",
    "thinking": "",
    "description": "Combines successful OAR agent responses. This reserved file is never used as a responder.",
}

SYSTEM_PROMPT = """
You are the OAR combiner. You receive the original request and multiple completed agent responses.
Produce one final answer that is clearer, stricter, more complete, and less redundant than any single response.
Use supported information from the responses, remove duplicates, preserve important disagreement, state uncertainty plainly, and do not invent unsupported claims.
""".strip()

TRANSIENT_RESPONSE_LINE_PATTERN = re.compile(
    r"^(?:thinking(?:[.．。…]+)?|skip|searching(?:[.．。…]+)?|generating(?:[.．。…]+)?|analyzing(?:[.．。…]+)?|reading(?:[.．。…]+)?|loading(?:[.．。…]+)?|please\s+wait(?:[.．。…]+)?)$",
    re.IGNORECASE,
)

LANGUAGE_LABELS = {
    "bash": "bash",
    "shell": "bash",
    "sh": "sh",
    "zsh": "zsh",
    "fish": "fish",
    "python": "python",
    "python3": "python",
    "javascript": "javascript",
    "typescript": "typescript",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "sql": "sql",
}


# ─── COMBINE ENTRYPOINT ───────────────────────────────────────────────────

def combine(request: dict[str, Any], responses: list[dict[str, Any]]) -> dict[str, Any]:
    successful = meaningful_responses(responses)
    if not successful:
        raise RuntimeError("combiner received no successful agent content")
    if str(AGENT.get("mode", "local")).lower() == "model":
        return combine_with_model(request, successful)
    prompt = str(request.get("prompt", "")).strip()
    sections = build_sections(successful)
    return {
        "content": render_final(prompt, sections, successful),
        "metadata": {
            "mode": "local",
            "source_count": len(successful),
            "sources": [source_name(item) for item in successful],
        },
    }


def combine_with_model(request: dict[str, Any], responses: list[dict[str, Any]]) -> dict[str, Any]:
    source = str(AGENT.get("model_source_agent") or "").strip()
    if not source:
        raise RuntimeError("model combiner mode requires AGENT['model_source_agent']")
    module = load_source_agent(source)
    complete = getattr(module, "complete", None)
    if not callable(complete):
        raise RuntimeError("model_source_agent must define complete(messages, model, thinking)")
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": render_model_prompt(request, responses)},
    ]
    content = complete(messages, str(AGENT.get("model", "")), str(AGENT.get("thinking", "")))
    return {
        "content": str(content).strip() + "\n",
        "metadata": {
            "mode": "model",
            "source_count": len(responses),
            "sources": [source_name(item) for item in responses],
            "model_source_agent": source,
        },
    }


def load_source_agent(source: str) -> Any:
    candidate = Path(source).expanduser()
    if not candidate.is_absolute():
        combiner_dir = Path(__file__).resolve().parent
        candidates = [combiner_dir / candidate, combiner_dir.parent / candidate]
        candidate = next((path for path in candidates if path.is_file()), candidates[0])
    if not candidate.is_file():
        raise RuntimeError(f"model_source_agent not found: {source}")
    module_name = f"oar_combiner_source_{hashlib.sha256(str(candidate).encode()).hexdigest()[:16]}"
    spec = importlib.util.spec_from_file_location(module_name, candidate)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load model_source_agent: {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def render_model_prompt(request: dict[str, Any], responses: list[dict[str, Any]]) -> str:
    lines = ["Original request:", str(request.get("prompt", "")).strip(), "", "Agent responses:"]
    for index, response in enumerate(responses, 1):
        lines.append("")
        lines.append(f"## {index}. {source_name(response)}")
        lines.append(str(response.get("content", "")).strip())
    return "\n".join(lines).strip()


# ─── SYNTHESIS ────────────────────────────────────────────────────────────

def meaningful_responses(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    successful: list[dict[str, Any]] = []
    for response in responses:
        cleaned = clean_body(str(response.get("content", "")))
        if not cleaned:
            continue
        clone = dict(response)
        clone["content"] = cleaned
        successful.append(clone)
    return successful


def build_sections(responses: list[dict[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    buckets: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for response in responses:
        name = source_name(response)
        content = normalize_text(str(response.get("content", "")))
        for title, body in split_sections(content):
            key = classify_section(title, body)
            cleaned = clean_body(body)
            if cleaned:
                buckets[key].append((name, cleaned))
    return dict(buckets)


def render_final(prompt: str, sections: dict[str, list[tuple[str, str]]], responses: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    if prompt:
        lines.extend(["# Final Answer", ""])
    wrote = False
    for key in ["answer", "findings", "steps", "risks", "uncertainty", "other"]:
        merged = merge_items(sections.get(key, []))
        if not merged:
            continue
        title = {
            "answer": "Answer",
            "findings": "Findings",
            "steps": "Recommended Steps",
            "risks": "Risks and Caveats",
            "uncertainty": "Uncertainty",
            "other": "Additional Notes",
        }[key]
        lines.extend([f"## {title}", "", *merged, ""])
        wrote = True
    if not wrote:
        lines.extend(["## Answer", ""])
        for response in responses:
            content = clean_body(str(response.get("content", "")))
            if content:
                lines.extend([content, ""])
    lines.extend(["## Sources", ""])
    for response in responses:
        lines.append(f"- {source_name(response)}")
    return "\n".join(lines).strip() + "\n"


# ─── SECTION HANDLING ─────────────────────────────────────────────────────

def split_sections(content: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"(?m)^(#{1,3})\s+(.+?)\s*$", content))
    if not matches:
        return [("answer", content)]
    sections: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        sections.append(("answer", content[: matches[0].start()].strip()))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        sections.append((match.group(2).strip(), content[start:end].strip()))
    return sections


def classify_section(title: str, body: str) -> str:
    text = f"{title} {body[:200]}".lower()
    if any(word in text for word in ("risk", "caveat", "warning", "issue", "problem", "security", "privacy")):
        return "risks"
    if any(word in text for word in ("uncertain", "unknown", "not sure", "cannot verify", "assumption")):
        return "uncertainty"
    if any(word in text for word in ("step", "todo", "next", "recommend", "fix", "install", "run")):
        return "steps"
    if any(word in text for word in ("finding", "analysis", "evidence", "because", "reason")):
        return "findings"
    return "answer"


def merge_items(items: list[tuple[str, str]]) -> list[str]:
    seen: set[str] = set()
    lines: list[str] = []
    for _source, body in items:
        for paragraph in paragraph_units(body):
            key = fingerprint(paragraph)
            if key in seen:
                continue
            seen.add(key)
            formatted = paragraph.strip()
            if not formatted:
                continue
            lines.append(formatted)
    return lines


# ─── TEXT NORMALIZATION ───────────────────────────────────────────────────

def paragraph_units(text: str) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    units: list[str] = []
    for block in blocks:
        if "```" in block:
            units.append(block)
            continue
        code_block = language_label_code_block(block)
        if code_block:
            units.append(code_block)
            continue
        bullet_lines = [line.strip() for line in block.splitlines() if line.strip().startswith(("- ", "* "))]
        if len(bullet_lines) >= 2:
            units.extend(bullet_lines)
        else:
            units.append(" ".join(line.strip() for line in block.splitlines() if line.strip()))
    return units


def language_label_code_block(block: str) -> str:
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if len(lines) < 2:
        return ""
    language = LANGUAGE_LABELS.get(lines[0].lower())
    if not language:
        return ""
    code = "\n".join(lines[1:]).strip()
    if not code:
        return ""
    return f"```{language}\n{code}\n```"


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_body(text: str) -> str:
    text = normalize_text(text)
    text = re.sub(r"(?im)^\s*(final answer|answer)\s*:\s*", "", text)
    return remove_transient_response_lines(text).strip()


def remove_transient_response_lines(text: str) -> str:
    lines: list[str] = []
    for raw_line in str(text or "").replace("\\n", "\n").splitlines():
        line = re.sub(r"^\s*Thought\s+Process\s*(?:[>›▸]\s*)?", "", raw_line, flags=re.IGNORECASE).strip()
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        if TRANSIENT_RESPONSE_LINE_PATTERN.fullmatch(line):
            continue
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def fingerprint(text: str) -> str:
    lowered = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return " ".join(lowered.split()[:80])


def source_name(response: dict[str, Any]) -> str:
    metadata = response.get("metadata") if isinstance(response.get("metadata"), dict) else {}
    return str(metadata.get("name") or response.get("name") or response.get("file") or "agent")
