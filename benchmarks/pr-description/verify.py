"""Bounded PR preference and factual checks; not a prose-quality judge."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

PAYLOAD = {}  # prepare.py embeds immutable input hashes and preference identity.
REQUIRED_HEADINGS = ["Description", "Changes", "Verification"]


def evaluate(workspace: Path) -> dict[str, object]:
    expected = PAYLOAD["input_sha256"]
    actual = {}
    unsafe = []
    for path in sorted(workspace.rglob("*")):
        if path.is_symlink():
            unsafe.append(path.relative_to(workspace).as_posix())
        elif path.is_file() and path.relative_to(workspace).as_posix() != "PR.md":
            actual[path.relative_to(workspace).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    output = workspace / "PR.md"
    text = output.read_text() if output.is_file() and not output.is_symlink() else ""
    headings = re.findall(r"^##\s+(.+?)\s*$", text, re.MULTILINE)
    sections = {
        match[1]: match[2].strip()
        for match in re.finditer(r"(?ms)^##\s+([^\n]+)\n(.*?)(?=^##\s|\Z)", text)
    }
    verification = sections.get("Verification", "")
    actions = re.findall(r"^\d+\.\s+.+", verification, re.MULTILINE)
    fenced_commands = re.findall(r"```(?:sh|bash|shell|console)\n(.*?)```", text, re.DOTALL)
    commands = [
        line.strip() for block in fenced_commands for line in block.splitlines() if line.strip()
    ]
    inline_commands = re.findall(r"`((?:npm|node|devps|git|brew|fzf)\b[^`\n]*)`", text)
    prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    placeholder_prose = re.sub(r"`[^`\n]*`", "", prose)
    prose = re.sub(r"(?m)^(?:#{1,6}\s|[-*]\s|\d+\.\s).*$", "", prose)
    paragraphs = [part for part in re.split(r"\n\s*\n", prose) if part.strip()]
    ci_echo = re.findall(
        r"\b(?:npm\s+(?:run\s+)?(?:test|typecheck|smoke)|(?:all\s+)?(?:CI\s+)?(?:checks|tests)\s+(?:passed|pass|are green))\b",
        text,
        re.IGNORECASE,
    )
    fabricated_local = re.findall(
        r"\b(?:I|we)\s+(?:ran|tested|verified|confirmed)\b|\b(?:tests?|checks?)\s+(?:passed|pass)\s+locally\b|\blocally\s+(?:verified|tested)\b",
        text,
        re.IGNORECASE,
    )
    preference = {
        "required_headings": headings == REQUIRED_HEADINGS,
        "completed_sections": all(sections.get(name) for name in REQUIRED_HEADINGS),
        "no_placeholders": not re.search(
            r"\b(?:TODO|TBD)\b|<[^>\n]+>|\[insert\b", placeholder_prose, re.IGNORECASE
        ),
        "ordered_human_actions": len(actions) >= 2,
        "feature_command": bool(
            re.search(
                r"(?:`devps(?:\s+[^`]*)?`|(?:node\s+)?\.?/dist/cli\.js|node\s+dist/cli\.js)",
                verification,
            )
        ),
        "feature_keys_and_expected_result": all(
            re.search(pattern, verification, re.IGNORECASE)
            for pattern in (
                r"browse",
                r"search|filter",
                r"(?:`/`|press\s+/)",
                r"\bEnter\b",
                r"\bEsc(?:ape)?\b",
                r"\b(?:confirm|expect|check|should|appears|returns|clears|hidden)\b",
            )
        ),
        "no_routine_ci_echo": not ci_echo,
        "no_unsupported_media": not re.search(r"!\[|<img\b|<video\b", text, re.IGNORECASE),
    }
    factual = {
        "captured_inputs_unchanged": actual == expected and not unsafe,
        "output_present": bool(text.strip()),
        "no_claimed_local_execution": not fabricated_local,
    }
    corrections = {
        "required_headings": "Use exactly these second-level headings in order: "
        + ", ".join(REQUIRED_HEADINGS)
        + f". Observed: {headings!r}.",
        "completed_sections": "Fill each required section with relevant content.",
        "no_placeholders": "Replace unfinished TODO, TBD, or placeholder prose with actual content.",
        "ordered_human_actions": "Put a numbered sequence of reviewer actions and expected results in Verification.",
        "feature_command": "Include a command to launch the captured feature in Verification, such as node dist/cli.js or devps, with necessary setup.",
        "feature_keys_and_expected_result": "Explain how to check browse mode, press / to search, use Enter to retain a filter, and use Esc to clear it, with expected results.",
        "no_routine_ci_echo": "Remove routine CI commands and check-status restatements; keep feature-specific human verification steps.",
        "no_unsupported_media": "Remove media references: no screenshots or feature media were supplied.",
        "captured_inputs_unchanged": "Restore the captured inputs and remove additional files other than PR.md; this is a description-only task.",
        "output_present": "Deliver a nonempty PR.md file.",
        "no_claimed_local_execution": "Remove claims that you performed local checks or verification; no such execution was allowed.",
    }
    return {
        "pass": all(preference.values()) and all(factual.values()),
        "tool": "pr-description-contract-v2",
        "preference_guide_sha256": PAYLOAD["preference_guide_sha256"],
        "criteria": {"preference_compliance": preference, "factual_and_delivery": factual},
        "feedback": [
            {"criterion": name, "required_correction": corrections[name]}
            for name, passed in {**preference, **factual}.items()
            if not passed
        ],
        "metrics": {
            "words": len(re.findall(r"\S+", text)),
            "prose_paragraphs": len(paragraphs),
            "ordered_actions": len(actions),
            "fenced_command_lines": len(commands),
            "inline_command_occurrences": len(inline_commands),
        },
        "evidence": {
            "headings": headings,
            "commands": commands,
            "inline_commands": inline_commands,
            "routine_ci_echoes": ci_echo,
            "local_execution_claims": fabricated_local,
            "unsafe_paths": unsafe,
        },
        "limits": [
            "Targeted checks do not prove all behavioral claims correct or prose readable.",
            "Word and paragraph counts are measurements, not mandatory limits or quality scores.",
            "Independent review must assess human usability, verbosity, command accuracy and remaining unsupported claims.",
        ],
    }


if __name__ == "__main__":
    try:
        result = evaluate(Path.cwd())
    except (OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"pass": None, "error": str(error)}))
        raise SystemExit(2)
    print(json.dumps(result))
    raise SystemExit(0 if result["pass"] else 1)
