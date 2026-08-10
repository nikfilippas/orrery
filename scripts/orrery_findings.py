"""Structured reviewer findings: the schema, its validator, and rendering.

A review is an artefact with the same standing as an evidence packet. It
is schema-validated, bound to exactly what it judged, and read by the
merge gate. Everything a provider wrote is untrusted text until it has
been through here.

The validator is written out rather than delegated to `jsonschema`
because the kit has no runtime dependencies and this schema is small and
closed. The same document is also handed to the provider, which enforces
it server-side; validating again locally is deliberate, since a provider
that ignores or partly honours the flag must not pass silently.
"""

from __future__ import annotations

import json
import re
from typing import Any

SCHEMA_VERSION = 1

SEVERITIES = ("blocking", "advisory")
CATEGORIES = (
    "correctness",
    "security",
    "compatibility",
    "performance",
    "maintainability",
    "test-coverage",
)
DISPOSITIONS = ("still_present", "resolved", "withdrawn")
VERDICTS = ("clean", "changes_required")

# Bounds exist so a review cannot become a denial-of-service or a wall of
# text in the operator's terminal. The contract validator already bounds
# its own title to 200 characters, and these follow it.
MAX_STATEMENT = 400
MAX_PARAGRAPH = 1200
MAX_ITEMS = 50
MAX_FINDINGS = 40
MAX_PATH = 400

_FINDING_ID = re.compile(r"^F-[0-9]{2,3}$")
_LINES = re.compile(r"^[0-9]+(-[0-9]+)?$")
_PROTOCOL_TOKEN = re.compile(r"ORRERY")


class FindingsError(Exception):
    """A review document that cannot be trusted as written."""


def sanitise_provider_text(text: str) -> str:
    """Render provider-derived text inert for a terminal and for Orrery.

    Control and escape bytes are dropped, and Orrery's own protocol token
    is broken with an interior middle dot, so a delegate cannot forge a
    consent marker or move the operator's cursor. A reviewer may
    legitimately quote either, which is why this rewrites rather than
    refuses.
    """
    kept = "".join(
        ch
        for ch in text
        if ch in "\n\t" or 0x20 <= ord(ch) < 0x7F or ord(ch) >= 0xA0
    )
    return _PROTOCOL_TOKEN.sub("OR·RERY", kept)


def review_schema(carried_ids: list[str] | None = None) -> dict[str, Any]:
    """The JSON Schema handed to the provider and used to validate.

    Strict structured output demands every property be required, so
    `carried` is always present and nullable rather than conditionally
    required; the VALIDATOR is what demands real carried entries exactly
    when `carried_ids` were supplied, and refuses them otherwise.
    """
    # Two provider meta-rules shape this schema, both discovered live and
    # both pinned by the suite: every node carries an explicit "type",
    # and every object lists every property in "required", because
    # OpenAI's structured-output validator refuses anything less. A field
    # that is optional by design is therefore nullable rather than
    # omittable, and the runner strips transport nulls at parse so the
    # validator and the stored document keep absence semantics.
    evidence = {
        "type": "object",
        "additionalProperties": False,
        "required": ["file", "lines", "reproduction"],
        "properties": {
            "file": {"type": ["string", "null"], "maxLength": MAX_PATH},
            "lines": {"type": ["string", "null"], "pattern": _LINES.pattern},
            "reproduction": {"type": ["string", "null"], "maxLength": MAX_PARAGRAPH},
        },
    }
    finding = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "id", "severity", "category", "statement", "failure_scenario",
            "evidence", "proposed_resolution", "addresses", "confidence",
        ],
        "properties": {
            "id": {"type": "string", "pattern": _FINDING_ID.pattern},
            "severity": {"type": "string", "enum": list(SEVERITIES)},
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "statement": {"type": "string", "maxLength": MAX_STATEMENT},
            "failure_scenario": {"type": ["string", "null"], "maxLength": MAX_PARAGRAPH},
            "evidence": {"type": "array", "items": evidence, "maxItems": MAX_ITEMS},
            "proposed_resolution": {
                "type": ["array", "null"],
                "items": {"type": "string", "maxLength": MAX_PARAGRAPH},
                "maxItems": MAX_ITEMS,
            },
            "addresses": {"type": ["string", "null"], "pattern": _FINDING_ID.pattern},
            "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        },
    }
    carried = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "disposition", "evidence"],
        "properties": {
            "id": {"type": "string", "pattern": _FINDING_ID.pattern},
            "disposition": {"type": "string", "enum": list(DISPOSITIONS)},
            "evidence": {"type": ["array", "null"], "items": evidence, "maxItems": MAX_ITEMS},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "v", "task_id", "verdict", "findings", "carried",
            "unable_to_verify",
        ],
        "properties": {
            "v": {"type": "integer", "const": SCHEMA_VERSION},
            "task_id": {"type": "string", "maxLength": 64},
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "findings": {
                "type": "array", "items": finding, "maxItems": MAX_FINDINGS
            },
            "carried": {
                "type": ["array", "null"], "items": carried,
                "maxItems": MAX_FINDINGS,
            },
            "unable_to_verify": {
                "type": "array",
                "items": {"type": "string", "maxLength": MAX_PARAGRAPH},
                "maxItems": MAX_ITEMS,
            },
        },
    }


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FindingsError(f"{field} must be a non-empty string")
    if len(value) > limit:
        raise FindingsError(f"{field} exceeds {limit} characters")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise FindingsError(f"{field} must be an array")
    if len(value) > MAX_ITEMS:
        raise FindingsError(f"{field} holds more than {MAX_ITEMS} entries")
    return [_text(item, f"{field}[]", MAX_PARAGRAPH) for item in value]


def _keys(document: Any, allowed: set[str], required: set[str], field: str) -> None:
    if not isinstance(document, dict):
        raise FindingsError(f"{field} must be an object")
    unknown = sorted(set(document) - allowed)
    if unknown:
        raise FindingsError(f"{field} has unknown fields: {', '.join(unknown)}")
    missing = sorted(required - set(document))
    if missing:
        raise FindingsError(f"{field} is missing: {', '.join(missing)}")


def _evidence(entries: Any, field: str, exists: Any) -> tuple[list[dict], list[str]]:
    """Validated evidence entries, and the reasons any were dropped."""
    if not isinstance(entries, list):
        raise FindingsError(f"{field} must be an array")
    if len(entries) > MAX_ITEMS:
        raise FindingsError(f"{field} holds more than {MAX_ITEMS} entries")
    kept: list[dict] = []
    dropped: list[str] = []
    for index, entry in enumerate(entries):
        _keys(entry, {"file", "lines", "reproduction"}, set(), f"{field}[{index}]")
        # A blank reproduction is a claim with no content, weighed exactly
        # like a path that is not in the tree: the entry goes and the
        # finding is weakened rather than the document being thrown away.
        blank_reproduction = False
        if "reproduction" in entry:
            command = entry["reproduction"]
            if not isinstance(command, str) or len(command) > MAX_PARAGRAPH:
                raise FindingsError(
                    f"{field}[{index}].reproduction must be a bounded string"
                )
            if not command.strip():
                dropped.append(f"{field}[{index}]: blank reproduction")
                blank_reproduction = True
                entry = {k: v for k, v in entry.items() if k != "reproduction"}
        if "file" in entry:
            path = _text(entry["file"], f"{field}[{index}].file", MAX_PATH)
            if "lines" in entry and not _LINES.match(str(entry["lines"])):
                raise FindingsError(f"{field}[{index}].lines must be N or N-M")
            # A path that is not in the reviewed tree, or that is not a
            # regular file inside it, means the reviewer described
            # something it did not read. That is a defect in the claim,
            # not in the document, so the entry goes and the finding is
            # weakened rather than the whole review being thrown away.
            if exists is not None and not exists(path):
                dropped.append(f"evidence path not found at commit: {path}")
                continue
        if "file" not in entry and "reproduction" not in entry:
            if blank_reproduction:
                continue
            raise FindingsError(f"{field}[{index}] needs a file or a reproduction")
        kept.append(dict(entry))
    return kept, dropped


def _supports_blocking(evidence: list[dict]) -> bool:
    return any("file" in entry or "reproduction" in entry for entry in evidence)


def validate_review(
    document: Any,
    *,
    task_id: str,
    carried_ids: list[str] | None = None,
    path_exists: Any = None,
) -> dict[str, Any]:
    """Validate a review, applying the downgrades the design defines.

    Returns the accepted document together with `downgrades`, which the
    runner records in the ledger rather than in the review: the stored
    review stays exactly as its reviewer produced it.

    Order matters and is fixed. Consistency between the verdict and the
    findings is judged on the document as received, then downgrades are
    applied, then the verdict is recomputed. The gate reads findings and
    never the verdict, so a recomputed verdict is a convenience for
    people rather than an input to a decision.
    """
    carried_ids = list(carried_ids or [])
    allowed = {"v", "task_id", "verdict", "findings", "carried", "unable_to_verify"}
    required = {"v", "task_id", "verdict", "findings", "unable_to_verify"}
    if carried_ids:
        required.add("carried")
    _keys(document, allowed, required, "$")

    if document["v"] != SCHEMA_VERSION:
        raise FindingsError(f"$.v must be {SCHEMA_VERSION}")
    if document["task_id"] != task_id:
        raise FindingsError("$.task_id does not match the task under review")
    if document["verdict"] not in VERDICTS:
        raise FindingsError(f"$.verdict must be one of {', '.join(VERDICTS)}")

    findings = document["findings"]
    if not isinstance(findings, list):
        raise FindingsError("$.findings must be an array")
    if len(findings) > MAX_FINDINGS:
        raise FindingsError(f"$.findings holds more than {MAX_FINDINGS} entries")

    seen: set[str] = set()
    accepted: list[dict[str, Any]] = []
    downgrades: list[dict[str, str]] = []
    for index, raw in enumerate(findings):
        field = f"$.findings[{index}]"
        _keys(
            raw,
            {
                "id", "severity", "category", "statement", "failure_scenario",
                "evidence", "proposed_resolution", "addresses", "confidence",
            },
            {"id", "severity", "category", "statement", "evidence"},
            field,
        )
        identifier = raw["id"]
        if not isinstance(identifier, str) or not _FINDING_ID.match(identifier):
            raise FindingsError(f"{field}.id must look like F-01")
        if identifier in seen:
            raise FindingsError(f"{field}.id is not unique: {identifier}")
        seen.add(identifier)
        if raw["severity"] not in SEVERITIES:
            raise FindingsError(f"{field}.severity must be blocking or advisory")
        if raw["category"] not in CATEGORIES:
            raise FindingsError(f"{field}.category is not a known category")
        _text(raw["statement"], f"{field}.statement", MAX_STATEMENT)
        if "failure_scenario" in raw:
            _text(raw["failure_scenario"], f"{field}.failure_scenario", MAX_PARAGRAPH)
        if "proposed_resolution" in raw:
            _string_list(raw["proposed_resolution"], f"{field}.proposed_resolution")
        if raw.get("addresses") is not None:
            if not _FINDING_ID.match(str(raw["addresses"])):
                raise FindingsError(f"{field}.addresses must be a finding id")
            if raw["addresses"] not in carried_ids:
                raise FindingsError(
                    f"{field}.addresses names a finding this review was not given"
                )
        # Null is both allowed by the schema and required to be present
        # under strict structured output, so a reviewer with nothing to
        # claim says so with null; rejecting that threw away the whole
        # review. The bound is the schema's and is enforced here too,
        # because a provider that ignores the schema must not pass.
        confidence = raw.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise FindingsError(f"{field}.confidence must be a number or null")
            if not 0 <= confidence <= 1:
                raise FindingsError(f"{field}.confidence must be between 0 and 1")

        evidence, dropped = _evidence(raw["evidence"], f"{field}.evidence", path_exists)
        finding = dict(raw)
        finding["evidence"] = evidence
        for reason in dropped:
            downgrades.append({"id": identifier, "reason": reason})

        if finding["severity"] == "blocking":
            if not _supports_blocking(evidence):
                downgrades.append(
                    {"id": identifier, "reason": "blocking finding without evidence"}
                )
                finding["severity"] = "advisory"
            elif "failure_scenario" not in finding:
                downgrades.append(
                    {
                        "id": identifier,
                        "reason": "blocking finding without a failure scenario",
                    }
                )
                finding["severity"] = "advisory"
        accepted.append(finding)

    blocking_as_received = any(f["severity"] == "blocking" for f in findings
                               if isinstance(f, dict))
    if document["verdict"] == "clean" and blocking_as_received:
        raise FindingsError("$.verdict is clean but a finding claims blocking")
    if document["verdict"] == "changes_required" and not blocking_as_received:
        raise FindingsError("$.verdict is changes_required with no blocking finding")

    carried = _carried(document.get("carried"), carried_ids, path_exists)
    unable = _string_list(document["unable_to_verify"], "$.unable_to_verify")

    blocking = [f for f in accepted if f["severity"] == "blocking"]
    return {
        "document": {
            "v": SCHEMA_VERSION,
            "task_id": task_id,
            "verdict": "changes_required" if blocking else "clean",
            "findings": accepted,
            "carried": carried,
            "unable_to_verify": unable,
        },
        "downgrades": downgrades,
        "blocking_ids": [f["id"] for f in blocking],
    }


def _carried(
    entries: Any, carried_ids: list[str], path_exists: Any
) -> list[dict[str, Any]]:
    """Every carried finding accounted for, exactly once.

    Silence is the failure this exists to prevent: a re-review that says
    nothing about a finding must not thereby close it. Requiring the
    array and matching its id set against what the runner supplied makes
    saying nothing impossible rather than merely discouraged.
    """
    if not carried_ids:
        if entries:
            raise FindingsError("$.carried was supplied but nothing was carried in")
        return []
    if not isinstance(entries, list):
        raise FindingsError("$.carried must be an array")
    seen: list[str] = []
    accounted: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        field = f"$.carried[{index}]"
        _keys(entry, {"id", "disposition", "evidence"}, {"id", "disposition"}, field)
        identifier = entry["id"]
        if identifier not in carried_ids:
            raise FindingsError(f"{field}.id was not carried into this review")
        if identifier in seen:
            raise FindingsError(f"{field}.id appears twice")
        seen.append(identifier)
        if entry["disposition"] not in DISPOSITIONS:
            raise FindingsError(
                f"{field}.disposition must be one of {', '.join(DISPOSITIONS)}"
            )
        evidence, _dropped = _evidence(
            entry.get("evidence", []), f"{field}.evidence", path_exists
        )
        if entry["disposition"] == "resolved" and not _supports_blocking(evidence):
            # "I fixed it" is a claim like any other.
            raise FindingsError(f"{field} claims resolved without evidence")
        accounted.append({**entry, "evidence": evidence})
    missing = sorted(set(carried_ids) - set(seen))
    if missing:
        raise FindingsError(
            "$.carried does not account for: " + ", ".join(missing)
        )
    return accounted


def render_review(document: dict[str, Any]) -> str:
    """A review as a person should see it, with every field made inert."""
    lines = [
        f"review of {sanitise_provider_text(str(document['task_id']))}: "
        f"{document['verdict']}"
    ]
    for finding in document["findings"]:
        lines.append(
            f"  [{finding['severity']}] {finding['id']} "
            f"({finding['category']}) "
            f"{sanitise_provider_text(finding['statement'])}"
        )
        for entry in finding["evidence"]:
            if "file" in entry:
                where = sanitise_provider_text(entry["file"])
                if "lines" in entry:
                    where += f":{entry['lines']}"
                lines.append(f"      {where}")
            elif "reproduction" in entry:
                lines.append(
                    f"      repro: {sanitise_provider_text(entry['reproduction'])}"
                )
    for carried in document.get("carried", []):
        lines.append(f"  carried {carried['id']}: {carried['disposition']}")
    for note in document["unable_to_verify"]:
        lines.append(f"  unverified: {sanitise_provider_text(note)}")
    return "\n".join(lines)


def _strip_nulls(node: Any, key: str | None = None) -> Any:
    """Collapse transport nulls back into absence.

    The all-required schema makes optional fields nullable, so a
    provider under strict structured output sends null where a field
    would simply be absent. Null and absent mean the same thing to the
    validator and to the stored document. Only an EVIDENCE entry reduced
    to nothing is dropped, because that is the one place an empty object
    means "no claim"; anywhere else an emptied element must reach the
    validator and fail, or a malformed finding would vanish instead of
    being refused.
    """
    if isinstance(node, dict):
        return {
            name: _strip_nulls(value, name)
            for name, value in node.items()
            if value is not None
        }
    if isinstance(node, list):
        stripped = [_strip_nulls(item, key) for item in node]
        if key == "evidence":
            return [item for item in stripped if item != {}]
        return stripped
    return node


def parse_review(text: str) -> Any:
    """A provider's final message as JSON, however it wrapped it."""
    try:
        return _strip_nulls(json.loads(text))
    except json.JSONDecodeError as exc:
        raise FindingsError(f"the review is not JSON: {exc}") from exc
