"""Bounded, opt-in source excerpts for a Jev review.

Code evidence is deliberately separate from the normal metadata-only payload.
Callers must name the exact commits, Python paths, and inclusive line ranges
before an excerpt is read.  The resulting object is suitable for an in-memory
preview; it is never written by this module.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import JgError
from .safety import canonical_json, digest


CODE_EVIDENCE_PROFILE = "code"
CODE_EVIDENCE_SCHEMA_VERSION = 1
DEFAULT_MAX_EXCERPTS = 8
DEFAULT_MAX_LINES_PER_EXCERPT = 80
DEFAULT_MAX_TOTAL_LINES = 240
DEFAULT_MAX_TOTAL_BYTES = 24_000

_HEX_TIP = re.compile(r"\A[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
_SAFE_PYTHON_PATH = re.compile(r"\A(?!/)(?!.*(?:^|/)\.\.?/)(?!.*//)[^\x00]+\.py\Z")
_DENIED_PATH_COMPONENT = re.compile(r"(?i)(?:secret|credential|config|token|password|passwd|private[_-]?key|api[_-]?key)")

# These patterns intentionally err on the side of refusing an excerpt.  The
# text of a match is never returned to a caller, logged, or put in an error.
_SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("pem_private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("provider_token", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_\-]{16,}|github_pat_[A-Za-z0-9_\-]{16,}|xox[baprs]-[A-Za-z0-9-]{16,})\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("authorization_header", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}")),
    ("secret_assignment", re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
        r"\s*[:=]\s*(?:[\"'][^\"']{1,}[\"']|[^\s,;]{8,})"
    )),
)


def _tip(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _HEX_TIP.fullmatch(value):
        raise JgError(f"{label} must be a full Git commit ID")
    return value.lower()


def _path(value: Any) -> str:
    if not isinstance(value, str) or not _SAFE_PYTHON_PATH.fullmatch(value):
        raise JgError("code evidence paths must be relative Python files")
    if any(component.startswith(".") or _DENIED_PATH_COMPONENT.search(component)
           for component in value.split("/")):
        raise JgError("code evidence path is in a sensitive directory")
    return value


def _range(spec: Mapping[str, Any]) -> tuple[str, int, int]:
    path = _path(spec.get("path"))
    start = spec.get("start_line", spec.get("start"))
    end = spec.get("end_line", spec.get("end"))
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 1
        or end < start
    ):
        raise JgError("code evidence ranges must use positive inclusive line numbers")
    return path, start, end


def sensitive_findings(text: str) -> list[str]:
    """Return stable finding classes without returning sensitive source text."""
    if not isinstance(text, str):
        raise JgError("code evidence excerpt must be text")
    return [name for name, pattern in _SENSITIVE_PATTERNS if pattern.search(text)]


def scan_sensitive_content(text: str) -> bool:
    """Return whether text is unsafe to send to a provider."""
    return bool(sensitive_findings(text))


def _reject_sensitive(text: str) -> None:
    if sensitive_findings(text):
        raise JgError("code evidence contains sensitive material; refusing the excerpt")


def _git(root: Path, *args: str, allow_failure: bool = False) -> bytes | None:
    completed = subprocess.run(
        ("git", "-C", str(root), *args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        if allow_failure:
            return None
        raise JgError("unable to read pinned Git code evidence")
    return completed.stdout


def _resolve_tip(root: Path, tip: str, label: str) -> str:
    resolved = _git(root, "rev-parse", "--verify", f"{tip}^{{commit}}")
    if resolved is None:
        raise JgError(f"unable to resolve {label} tip")
    actual = resolved.decode("ascii", "strict").strip().lower()
    if actual != tip:
        raise JgError(f"{label} tip changed while collecting code evidence")
    return actual


def _resolve_ref(root: Path, ref: str, expected_tip: str, label: str) -> None:
    resolved = _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}", allow_failure=True)
    if resolved is None or resolved.decode("ascii", "strict").strip().lower() != expected_tip:
        raise JgError(f"{label} ref moved since code evidence approval")


def _blob(root: Path, tip: str, path: str, *, allow_missing: bool = False) -> str | None:
    result = _git(root, "rev-parse", "--verify", f"{tip}:{path}", allow_failure=allow_missing)
    if result is None:
        return None
    blob = result.decode("ascii", "strict").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40,64}", blob):
        raise JgError("pinned code evidence object is not a Git blob ID")
    kind = _git(root, "cat-file", "-t", blob, allow_failure=allow_missing)
    if kind is None or kind.decode("ascii", "strict").strip() != "blob":
        if allow_missing:
            return None
        raise JgError("approved code evidence path is not a regular file")
    return blob


def _blob_text(root: Path, blob: str) -> str:
    raw = _git(root, "cat-file", "blob", blob)
    assert raw is not None
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        raise JgError("approved code evidence is not UTF-8 text") from None
    if "\x00" in text:
        raise JgError("approved code evidence contains a NUL byte")
    return text


def _excerpt(text: str, start: int, end: int) -> str:
    lines = text.splitlines(keepends=True)
    if end > len(lines):
        raise JgError("approved code evidence range is outside the pinned blob")
    value = "".join(lines[start - 1:end])
    if not value:
        raise JgError("approved code evidence range is empty")
    return value


def _normalise_approval(spec: Any) -> Mapping[str, Any]:
    if isinstance(spec, Mapping):
        return spec
    if isinstance(spec, Sequence) and not isinstance(spec, (str, bytes)) and len(spec) == 3:
        return {"path": spec[0], "start_line": spec[1], "end_line": spec[2]}
    raise JgError("approved code evidence ranges must be objects or (path, start, end) tuples")


def build_code_evidence(
    repo: str | Path,
    source_tip: str,
    main_tip: str,
    approved_ranges: Iterable[Mapping[str, Any] | Sequence[Any]],
    *,
    source_ref: str | None = None,
    main_ref: str | None = None,
    max_excerpts: int = DEFAULT_MAX_EXCERPTS,
    max_lines_per_excerpt: int = DEFAULT_MAX_LINES_PER_EXCERPT,
    max_total_lines: int = DEFAULT_MAX_TOTAL_LINES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Read only explicitly approved Python ranges from immutable Git blobs."""
    if max_excerpts < 1 or max_lines_per_excerpt < 1 or max_total_lines < 1 or max_total_bytes < 1:
        raise JgError("code evidence bounds must be positive")
    root = Path(repo).expanduser().resolve()
    if not root.exists():
        raise JgError(f"repository does not exist: {root}")
    source_tip = _tip(source_tip, "source")
    main_tip = _tip(main_tip, "main")
    _resolve_tip(root, source_tip, "source")
    _resolve_tip(root, main_tip, "main")
    if source_ref is not None:
        _resolve_ref(root, source_ref, source_tip, "source")
    if main_ref is not None:
        _resolve_ref(root, main_ref, main_tip, "main")
    approvals = [_normalise_approval(item) for item in approved_ranges]
    if len(approvals) > max_excerpts:
        raise JgError("approved code evidence exceeds the excerpt count bound")
    excerpts: list[dict[str, Any]] = []
    total_lines = 0
    total_bytes = 0
    for approval in approvals:
        path, start, end = _range(approval)
        if end - start + 1 > max_lines_per_excerpt:
            raise JgError("approved code evidence range exceeds the line bound")
        total_lines += end - start + 1
        if total_lines > max_total_lines:
            raise JgError("approved code evidence exceeds the total line bound")
        source_blob = _blob(root, source_tip, path)
        assert source_blob is not None
        main_blob = _blob(root, main_tip, path, allow_missing=True)
        text = _excerpt(_blob_text(root, source_blob), start, end)
        _reject_sensitive(text)
        encoded = text.encode("utf-8")
        total_bytes += len(encoded)
        if total_bytes > max_total_bytes:
            raise JgError("approved code evidence exceeds the byte bound")
        excerpt_sha256 = hashlib.sha256(encoded).hexdigest()
        excerpts.append({
            "path": path,
            "source_tip": source_tip,
            "main_tip": main_tip,
            "source_blob": source_blob,
            "main_blob": main_blob,
            "blob": source_blob,
            "range": {"start_line": start, "end_line": end},
            "excerpt_sha256": excerpt_sha256,
            "sha256": excerpt_sha256,
            "text": text,
        })
    result: dict[str, Any] = {
        "kind": "jev-code-evidence",
        "schema_version": CODE_EVIDENCE_SCHEMA_VERSION,
        "source_tip": source_tip,
        "main_tip": main_tip,
        "source_ref": source_ref,
        "main_ref": main_ref,
        "excerpt_count": len(excerpts),
        "total_lines": total_lines,
        "total_bytes": total_bytes,
        "excerpts": excerpts,
    }
    result["evidence_sha256"] = code_evidence_digest(result)
    return result


def _checked_record(record: Mapping[str, Any]) -> None:
    if record.get("kind") != "jev-code-evidence" or record.get("schema_version") != CODE_EVIDENCE_SCHEMA_VERSION:
        raise JgError("invalid code evidence record")
    if not isinstance(record.get("excerpts"), list):
        raise JgError("code evidence record lacks excerpts")
    for ref_key in ("source_ref", "main_ref"):
        if record.get(ref_key) is not None and (not isinstance(record[ref_key], str) or not record[ref_key].strip()):
            raise JgError("code evidence record has an invalid named ref")
    for item in record["excerpts"]:
        if not isinstance(item, Mapping):
            raise JgError("invalid code evidence excerpt")
        path, start, end = _range({"path": item.get("path"), **(item.get("range") or {})})
        if item.get("source_tip") != record.get("source_tip") or item.get("main_tip") != record.get("main_tip"):
            raise JgError("code evidence excerpt tips do not match its record")
        if not isinstance(item.get("source_blob"), str) or not re.fullmatch(r"[0-9a-f]{40,64}", item["source_blob"]):
            raise JgError("code evidence excerpt lacks a pinned source blob")
        if item.get("main_blob") is not None and not isinstance(item.get("main_blob"), str):
            raise JgError("code evidence excerpt has an invalid main blob")
        text = item.get("text")
        if not isinstance(text, str):
            raise JgError("code evidence excerpt lacks text")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != item.get("excerpt_sha256"):
            raise JgError("code evidence excerpt hash does not match its text")
        if item.get("sha256", item.get("excerpt_sha256")) != item.get("excerpt_sha256"):
            raise JgError("code evidence excerpt hash aliases do not match")
        if item.get("blob", item.get("source_blob")) != item.get("source_blob"):
            raise JgError("code evidence blob aliases do not match")
        if end - start + 1 < 1 or not path:
            raise JgError("invalid code evidence range")
        _reject_sensitive(text)


def code_evidence_digest(record: Mapping[str, Any]) -> str:
    """Digest the approved content while ignoring its self-referential digest."""
    without_digest = {key: value for key, value in record.items() if key != "evidence_sha256"}
    return digest(without_digest)


def approve_code_batch(record: Mapping[str, Any] | Iterable[Mapping[str, Any]], requests: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Create the digest an operator approves for a transient batch."""
    records = [record] if isinstance(record, Mapping) else list(record)
    for item in records:
        _checked_record(item)
    request_list = list(requests)
    individual = [code_evidence_digest(item) for item in records]
    evidence_sha = individual[0] if len(individual) == 1 else digest(sorted(individual))
    payload_sha = digest(request_list)
    return {
        "kind": "jev-code-batch-approval",
        "schema_version": CODE_EVIDENCE_SCHEMA_VERSION,
        "evidence_sha256": evidence_sha,
        "request_count": len(request_list),
        "payload_sha256": payload_sha,
        "approval_sha256": digest({"evidence_sha256": evidence_sha, "payload_sha256": payload_sha, "request_count": len(request_list)}),
    }


def revalidate_code_evidence(repo: str | Path, record: Mapping[str, Any], approved_digest: str | None = None) -> dict[str, Any]:
    """Re-read every pinned blob/range immediately before an approved request."""
    _checked_record(record)
    expected = code_evidence_digest(record)
    if record.get("evidence_sha256") not in (None, expected):
        raise JgError("code evidence self-digest is invalid")
    if approved_digest is not None and approved_digest != expected:
        raise JgError("approved code evidence digest does not match")
    rebuilt = build_code_evidence(
        repo,
        record["source_tip"],
        record["main_tip"],
        [
            {"path": item["path"], "start_line": item["range"]["start_line"], "end_line": item["range"]["end_line"]}
            for item in record["excerpts"]
        ],
        source_ref=record.get("source_ref"),
        main_ref=record.get("main_ref"),
        max_excerpts=max(len(record["excerpts"]), 1),
        max_lines_per_excerpt=max(item["range"]["end_line"] - item["range"]["start_line"] + 1 for item in record["excerpts"] or [{"range": {"end_line": 1, "start_line": 1}}]),
        max_total_lines=max(record.get("total_lines", 1), 1),
        max_total_bytes=max(record.get("total_bytes", 1), 1),
    )
    if code_evidence_digest(rebuilt) != expected:
        raise JgError("pinned code evidence changed; obtain a new approval")
    return rebuilt


def build_transient_preview(
    candidates: Mapping[str, Any],
    evidence_by_candidate: Mapping[str, Mapping[str, Any]],
    *,
    approved_batch: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a no-store preview; callers must keep the returned object in memory."""
    from .jev import JEV_ENDPOINT, QUESTION_VERSION, payload_for_candidate

    requests = []
    used_evidence: list[Mapping[str, Any]] = []
    for candidate in candidates.get("candidates", []):
        candidate_id = candidate.get("id")
        evidence = evidence_by_candidate.get(candidate_id)
        if evidence is None:
            raise JgError(f"missing approved code evidence for candidate {candidate_id}")
        _checked_record(evidence)
        used_evidence.append(evidence)
        requests.append(payload_for_candidate(candidate, CODE_EVIDENCE_PROFILE, evidence))
    payload_sha = digest(requests)
    evidence_digests = [code_evidence_digest(value) for value in used_evidence]
    evidence_sha = evidence_digests[0] if len(evidence_digests) == 1 else digest(sorted(evidence_digests))
    if approved_batch is not None:
        if approved_batch.get("payload_sha256") != payload_sha:
            raise JgError("approved code batch payload digest does not match preview")
        if approved_batch.get("request_count") != len(requests):
            raise JgError("approved code batch request count does not match preview")
        if approved_batch.get("evidence_sha256") != evidence_sha:
            raise JgError("approved code batch evidence digest does not match preview")
    result = {
        "kind": "jev-preview",
        "endpoint": JEV_ENDPOINT,
        "question_version": QUESTION_VERSION,
        "evidence_profile": CODE_EVIDENCE_PROFILE,
        "candidate_content_digest": candidates.get("content_digest"),
        "request_count": len(requests),
        "requests": requests,
        "payload_sha256": payload_sha,
        "payload_bytes": len(canonical_json(requests)),
        "code_evidence_sha256": evidence_sha,
        "network_performed": False,
        "storage": "transient",
        "no_store": True,
    }
    if approved_batch is not None:
        result["approval_sha256"] = approved_batch.get("approval_sha256")
    return result


def transient_preview_bytes(preview: Mapping[str, Any]) -> bytes:
    """Return exact request bytes for an operator review without storing them."""
    if preview.get("evidence_profile") != CODE_EVIDENCE_PROFILE or preview.get("no_store") is not True:
        raise JgError("request bytes are available only for a transient code preview")
    requests = preview.get("requests")
    if not isinstance(requests, list):
        raise JgError("transient code preview lacks requests")
    encoded = canonical_json(requests)
    if preview.get("payload_sha256") != digest(requests) or preview.get("payload_bytes") != len(encoded):
        raise JgError("transient code preview digest metadata is invalid")
    return encoded


def sanitize_provider_response(request: Mapping[str, Any], response: Any) -> dict[str, Any]:
    """Return only the provider fields allowed in a code-evidence ledger."""
    from .jev import validate_response

    validated = validate_response(dict(request), response)
    questions = request["questions"]
    answers: dict[str, dict[str, Any]] = {}
    for question_id, question in questions.items():
        answer = validated["answers"][question_id]
        if question.get("type") == "noul":
            answers[question_id] = {"noul": answer["noul"]}
        else:
            answers[question_id] = {
                "choice": answer["choice"],
                "confidence": answer["confidence"],
                "probabilities": dict(answer["probabilities"]),
            }
    return {
        "model": validated["model"],
        "usage": {
            "input_tokens": validated["usage"]["input_tokens"],
            "output_tokens": validated["usage"]["output_tokens"],
        },
        "answers": answers,
    }


# Readable aliases for callers that prefer an explicit verb.
validate_code_evidence = _checked_record
build_code_preview = build_transient_preview
