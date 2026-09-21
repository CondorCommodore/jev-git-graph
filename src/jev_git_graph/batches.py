"""Local-only partitioning of ambiguous comparisons into reviewable previews."""
from pathlib import Path
from collections import defaultdict, deque

from .artifacts import candidate_content_digest
from .errors import JgError
from .jev import JEV_MODEL, QUESTION_VERSION, build_preview, payload_for_candidate, save_checkpoint
from .safety import digest, read_json, write_json


_TIMING_FIELDS = frozenset({
    "started_at", "started_at_epoch_ms", "completed_at", "completed_at_epoch_ms", "latency_ms",
})


def _stable_record_digest(record):
    return digest({key: value for key, value in record.items() if key not in _TIMING_FIELDS})


def stratify_pending(candidates, previously_touched=()):
    """Favor untouched branch coverage, then round-robin evidence families."""
    touched = set(previously_touched)
    branch_seen = set()
    coverage, remainder = [], []
    for candidate in candidates:
        names = {endpoint.get("branch") for endpoint in candidate.get("endpoints", {}).values()}
        target = coverage if (names - branch_seen) and not (names & touched) else remainder
        target.append(candidate)
        branch_seen.update(names)
    buckets = defaultdict(deque)
    for candidate in remainder:
        buckets[tuple(candidate.get("reasons", []))].append(candidate)
    ordered = list(coverage)
    keys = sorted(buckets)
    while keys:
        next_keys = []
        for key in keys:
            ordered.append(buckets[key].popleft())
            if buckets[key]:
                next_keys.append(key)
        keys = next_keys
    return ordered


def prepare_batches(candidates_path, output, size=32, previous=(), evidence_profile="minimal"):
    if size < 1 or size > 1000:
        raise JgError("batch size must be between 1 and 1000")
    source = read_json(candidates_path)
    destination = Path(output)
    # Never overwrite an existing plan or its successful responses.
    if destination.exists() and any(destination.iterdir()):
        raise JgError("batch output must be a new empty directory")
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.stat().st_mode & 0o077:
        raise JgError("batch directory must be owner-only")
    attempted = set()
    previously_touched_candidates = set()
    for path in previous:
        ledger = read_json(path)
        if "attempts" not in ledger:
            raise JgError("previous results require an attempt ledger for request deduplication")
        attempted.update(item["request_sha256"] for item in ledger["attempts"])
        previously_touched_candidates.update(item.get("candidate_id") for item in ledger["attempts"] if item.get("candidate_id"))
    pending = []
    skipped_fact = skipped_attempt = 0
    for candidate in source["candidates"]:
        evidence = candidate["evidence"]
        if evidence.get("identical_tips") or evidence.get("a_ancestor_of_b") or evidence.get("b_ancestor_of_a"):
            skipped_fact += 1
            continue
        request_sha = digest(payload_for_candidate(candidate, evidence_profile))
        if request_sha in attempted:
            skipped_attempt += 1
            continue
        attempted.add(request_sha)
        pending.append(candidate)
    previously_touched_branches = {
        endpoint.get("branch")
        for candidate in source["candidates"] if candidate.get("id") in previously_touched_candidates
        for endpoint in candidate.get("endpoints", {}).values()
    }
    pending = stratify_pending(pending, previously_touched_branches)
    source_candidate_digest = digest(source)
    source_content_digest = source.get("content_digest") or candidate_content_digest(source)
    manifest = {"kind": "jev-batch-plan", "candidate_digest": source_candidate_digest,
                "candidate_content_digest": source_content_digest,
                "candidate_count": len(source["candidates"]), "pending_requests": len(pending),
                "fact_only_pairs": skipped_fact, "previously_attempted": skipped_attempt,
                "question_version": QUESTION_VERSION, "evidence_profile": evidence_profile,
                "model": JEV_MODEL,
                "network_performed": False, "batches": []}
    for offset in range(0, len(pending), size):
        name = f"batch-{offset // size + 1:04d}"
        subset = dict(source, candidates=pending[offset:offset + size])
        subset["candidate_count"] = len(subset["candidates"])
        subset["source_candidate_digest"] = source_candidate_digest
        subset["source_content_digest"] = source_content_digest
        subset["content_digest"] = candidate_content_digest(subset)
        preview = build_preview(subset, evidence_profile)
        write_json(destination / name / "candidates.json", subset)
        write_json(destination / name / "jev-preview.json", preview)
        manifest["batches"].append({"directory": name, "request_count": preview["request_count"],
                                    "payload_bytes": preview["payload_bytes"], "payload_sha256": preview["payload_sha256"]})
    write_json(destination / "batches.json", manifest)
    return destination / "batches.json"


def collect_batches(manifest_paths, candidates_path, output):
    if isinstance(manifest_paths, (str, Path)):
        manifest_paths = [manifest_paths]
    candidates = read_json(candidates_path)
    result = {"kind": "relations", "candidate_digest": digest(candidates),
              "repository_id": candidates.get("repository_id"),
              "candidate_content_digest": candidates.get("content_digest"),
              "source_batch_plan_sha256": [], "relations": [], "attempts": [],
              "network_performed": False, "missing_batches": 0, "limitations": []}
    seen = set()
    attempt_indexes = {}
    planned_requests = set()
    seen_relations = {}
    for manifest_value in manifest_paths:
        manifest_path = Path(manifest_value).resolve()
        manifest = read_json(manifest_path)
        if manifest.get("kind") != "jev-batch-plan" or manifest.get("candidate_digest") != digest(candidates):
            raise JgError("batch plan does not match the candidate artifact")
        expected_source_content_digest = candidates.get("content_digest") or candidate_content_digest(candidates)
        if manifest.get("candidate_content_digest") is not None and manifest["candidate_content_digest"] != expected_source_content_digest:
            raise JgError("batch plan does not match the candidate content")
        result["source_batch_plan_sha256"].append(digest(manifest))
        for batch in manifest["batches"]:
            directory = (manifest_path.parent / batch["directory"]).resolve()
            if directory.parent != manifest_path.parent:
                raise JgError("batch directory escapes the plan directory")
            batch_candidates_path = directory / "candidates.json"
            if batch_candidates_path.exists():
                batch_candidates = read_json(batch_candidates_path)
                source_candidate_digest = batch_candidates.get("source_candidate_digest")
                batch_source_content_digest = batch_candidates.get("source_content_digest")
                has_new_provenance = source_candidate_digest is not None or batch_source_content_digest is not None
                if source_candidate_digest is not None and source_candidate_digest != digest(candidates):
                    raise JgError("batch candidates do not match the source candidate artifact")
                if batch_source_content_digest is not None and batch_source_content_digest != expected_source_content_digest:
                    raise JgError("batch candidates do not match the source candidate content")
                if has_new_provenance and batch_candidates.get("content_digest") != candidate_content_digest(batch_candidates):
                    raise JgError("batch candidates content digest does not match its subset")
                if not has_new_provenance:
                    legacy_content_digest = batch_candidates.get("content_digest")
                    if legacy_content_digest is not None and legacy_content_digest != expected_source_content_digest:
                        raise JgError("legacy batch candidates content digest does not match its source")
                    result["limitations"].append(f"legacy_batch_provenance_missing:{batch['directory']}")
            preview = read_json(directory / "jev-preview.json")
            if digest(preview["requests"]) != batch["payload_sha256"]:
                raise JgError("batch preview no longer matches its plan")
            expected = {digest(request): request["state"]["candidate_id"] for request in preview["requests"]}
            planned_requests.update(expected)
            checkpoint = directory / "relations.json"
            if not checkpoint.exists():
                result["missing_batches"] += 1
                continue
            ledger = read_json(checkpoint)
            if ledger.get("source_preview_sha256") != batch["payload_sha256"]:
                raise JgError("batch results do not match their preview")
            successes = set()
            successful_candidates = set()
            for attempt in ledger.get("attempts", []):
                sha = attempt.get("request_sha256")
                candidate_id = attempt.get("candidate_id")
                if sha not in expected or candidate_id != expected[sha]:
                    raise JgError("duplicate or mismatched attempt in batch results")
                if sha in seen:
                    prior = result["attempts"][attempt_indexes[sha]]
                    if _stable_record_digest(prior) != _stable_record_digest(attempt):
                        raise JgError("conflicting duplicate request attempt")
                    continue
                seen.add(sha)
                attempt_indexes[sha] = len(result["attempts"])
                result["attempts"].append(attempt)
                if attempt.get("status") == "succeeded":
                    successes.add(sha)
                    successful_candidates.add(candidate_id)
            response_ids = set()
            for relation in ledger.get("relations", []):
                candidate_id = relation.get("candidate_id")
                judgment_id = relation.get("judgment_id")
                relation_id = judgment_id or f"legacy:{candidate_id}"
                request_sha = relation.get("request_sha256") or judgment_id
                relation_identity = request_sha or relation_id
                if relation_identity in seen_relations:
                    if _stable_record_digest(seen_relations[relation_identity]) != _stable_record_digest(relation):
                        raise JgError("conflicting duplicate relation response")
                    continue
                valid_success = judgment_id in successes if judgment_id else candidate_id in successful_candidates
                if not valid_success or relation_id in response_ids:
                    raise JgError("response has no unique successful attempt")
                response_ids.add(relation_id)
                seen_relations[relation_identity] = relation
                result["relations"].append(relation)
            if len(response_ids) != len(successes):
                raise JgError("successful attempt is missing its response")
            result["network_performed"] |= bool(ledger.get("network_performed"))
    result["unattempted_requests"] = len(planned_requests - seen)
    destination = Path(output)
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.stat().st_mode & 0o077:
        raise JgError("aggregate directory must be owner-only")
    target = destination / "relations.json"
    if target.exists():
        raise JgError("aggregate destination exists; use a new output directory")
    save_checkpoint(target, result)
    return target
