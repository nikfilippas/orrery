"""Trajectory stall detection for delegated runs.

The wrapper tails a delegate's event stream; this module reduces those
events to signatures and judges repetition. A signature is the tool,
a digest of its normalised input, a digest of its outcome, an error
class for failures, and a success flag: counts and digests only, never
content, because reasoning is never evidence. Three rules read a ring
of recent signatures:

- repeat: three consecutive FAILING calls with identical tool, input
  and outcome. Success never counts; re-reading a file or deliberately
  re-running a passing test is ordinary verification.
- error-class: three consecutive failures with the same class, where a
  class digests the tool together with the first line of its error,
  never a bare exit status.
- no-progress: twelve consecutive events without one success, where
  success means a tool call completed without an error.

A verdict additionally requires its evidence to span at least two poll
observations and thirty seconds, so a deliberate rapid repetition
completes before judgement begins. Tests may lower that span only when
their KIT_FAKE_BIN marker is present; production always keeps the
conservative threshold. Parsers are total: an unknown or
truncated line is skipped, a stream with no parseable events never
triggers, and detection is additive to the budget machinery, which
remains the outer authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import deque
from dataclasses import dataclass

RING = 16
REPEAT_RUN = 3
ERROR_RUN = 3
NO_PROGRESS_RUN = 12
MIN_OBSERVATIONS = 2
# Tests may shorten the evidence span only through their fake-bin marker;
# production always keeps the conservative thirty-second default. Read once
# so a running detector cannot be changed by a delegate's environment.
try:
    MIN_SPAN_SECONDS = float(os.environ.get("ORRERY_STALL_MIN_SPAN", "30"))
except ValueError:
    MIN_SPAN_SECONDS = 30.0
if not os.environ.get("KIT_FAKE_BIN"):
    MIN_SPAN_SECONDS = max(30.0, MIN_SPAN_SECONDS)

FORMATS = ("claude-stream-json", "codex-jsonl")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0].strip()[:200] if text.strip() else ""


_GENERIC_FAILURE = re.compile(r"(?:exit code \d+|command failed|error)", re.I)


def _error_class(tool: str, text: str) -> str | None:
    """Classify a specific error line, never a generic exit outcome."""
    line = _first_line(text)
    if not line or _GENERIC_FAILURE.fullmatch(line):
        return None
    return _digest(f"{tool}:{line}")


@dataclass(frozen=True)
class Signature:
    tool: str
    input_digest: str
    outcome_digest: str
    error_class: str | None  # None on success or generic failure
    failed: bool
    observation: int
    at: float


def _result_text(content) -> str:
    """A tool_result's content as text: a string, or text blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join(parts)
    return ""


class _ClaudeParser:
    """assistant tool_use blocks paired with user tool_result blocks."""

    def __init__(self):
        self.pending: dict[str, tuple[str, str]] = {}

    def parse(self, event: dict, observation: int, at: float):
        signatures = []
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                identifier = block.get("id")
                if not isinstance(identifier, str):
                    continue
                tool = str(block.get("name") or "tool")
                payload = json.dumps(block.get("input"), sort_keys=True, default=str)
                self.pending[identifier] = (tool, _digest(payload))
        elif event.get("type") == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                known = self.pending.pop(str(block.get("tool_use_id")), None)
                if known is None:
                    continue
                tool, input_digest = known
                text = _result_text(block.get("content"))
                failed = bool(block.get("is_error"))
                signatures.append(Signature(
                    tool=tool,
                    input_digest=input_digest,
                    outcome_digest=_digest(text),
                    error_class=_error_class(tool, text) if failed else None,
                    failed=failed,
                    observation=observation,
                    at=at,
                ))
        return signatures


class _CodexParser:
    """Completed Codex commands and successful non-command work items."""

    def parse(self, event: dict, observation: int, at: float):
        if event.get("type") != "item.completed":
            return []
        item = event.get("item")
        if not isinstance(item, dict):
            return []
        item_type = item.get("type")
        if not isinstance(item_type, str):
            return []
        if item_type != "command_execution":
            status = item.get("status")
            # Provider-controlled; an unhashable value must be skipped,
            # not allowed to raise out of the poll as a TypeError.
            if not isinstance(status, str) or status not in {"completed", "succeeded"}:
                return []
            identity = {
                name: item.get(name)
                for name in ("id", "call_id", "path", "file_path")
                if name in item
            }
            digest = _digest(json.dumps(identity, sort_keys=True, default=str))
            return [Signature(
                tool=item_type,
                input_digest=digest,
                outcome_digest=digest,
                error_class=None,
                failed=False,
                observation=observation,
                at=at,
            )]
        command = str(item.get("command") or "")
        output = str(item.get("aggregated_output") or "")
        exit_code = item.get("exit_code")
        failed = not (isinstance(exit_code, int) and exit_code == 0)
        return [Signature(
            tool="command",
            input_digest=_digest(command),
            outcome_digest=_digest(f"{exit_code}:{output}"),
            error_class=_error_class("command", output) if failed else None,
            failed=failed,
            observation=observation,
            at=at,
        )]


class Detector:
    """Feed raw stream text per poll; consult verdict() after each feed."""

    def __init__(self, stream_format: str):
        if stream_format not in FORMATS:
            raise ValueError(f"unknown stream format: {stream_format}")
        self.parser = (
            _ClaudeParser() if stream_format == "claude-stream-json"
            else _CodexParser()
        )
        self.ring: deque[Signature] = deque(maxlen=RING)
        self.remainder = ""
        self.observation = 0
        self.events_seen = 0

    def feed(self, chunk: str, now: float) -> None:
        """New bytes from the merged stream; partial final lines are kept
        for the next poll rather than parsed as broken JSON."""
        self.observation += 1
        text = self.remainder + chunk
        lines = text.split("\n")
        self.remainder = lines.pop()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            self.events_seen += 1
            for signature in self.parser.parse(event, self.observation, now):
                self.ring.append(signature)

    def _spans(self, window: list[Signature]) -> bool:
        observations = {s.observation for s in window}
        span = window[-1].at - window[0].at
        return len(observations) >= MIN_OBSERVATIONS and span >= MIN_SPAN_SECONDS

    def verdict(self) -> dict | None:
        ring = list(self.ring)
        if len(ring) >= REPEAT_RUN:
            tail = ring[-REPEAT_RUN:]
            if (
                all(s.failed for s in tail)
                and len({(s.tool, s.input_digest, s.outcome_digest) for s in tail}) == 1
                and self._spans(tail)
            ):
                return {"rule": "repeat", "counts": {"run": REPEAT_RUN}}
        if len(ring) >= ERROR_RUN:
            tail = ring[-ERROR_RUN:]
            classes = {s.error_class for s in tail}
            # None is the absence of a class, not a class: generic or
            # empty failures must never cluster with each other.
            if (
                all(s.failed for s in tail)
                and len(classes) == 1
                and None not in classes
                and self._spans(tail)
            ):
                return {"rule": "error-class", "counts": {"run": ERROR_RUN}}
        if len(ring) >= NO_PROGRESS_RUN:
            tail = ring[-NO_PROGRESS_RUN:]
            if all(s.failed for s in tail) and self._spans(tail):
                return {
                    "rule": "no-progress",
                    "counts": {"run": NO_PROGRESS_RUN},
                }
        return None
