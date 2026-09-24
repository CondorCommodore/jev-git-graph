from __future__ import annotations

import hashlib
import argparse
import json
import math
from pathlib import Path

from jev_git_graph.group_requests import (
    approved_presence_preview,
    build_group_requests,
    build_two_sided_evidence,
)
from jev_git_graph.groups import build_groups
from jev_git_graph.safety import digest, write_json
from jev_git_graph.snapshot import load_snapshot


parser = argparse.ArgumentParser(description="Prepare a bounded three-case Jev pilot from a saved snapshot and projection.")
parser.add_argument("--run-dir", type=Path, required=True,
                    help="Directory containing snapshot.json, relationship-projection.json, and projection-measurement.json")
parser.add_argument("--output-dir", type=Path,
                    help="New private output directory (default: RUN_DIR/pilot-3-code-main)")
args = parser.parse_args()
base = args.run_dir.expanduser().resolve()
snapshot_path = base / "snapshot.json"
projection_path = base / "relationship-projection.json"
measurement_path = base / "projection-measurement.json"
output = args.output_dir.expanduser().resolve() if args.output_dir else base / "pilot-3-code-main"
snapshot, object_repo = load_snapshot(snapshot_path)
measurement = json.loads(measurement_path.read_text())
projection_bytes = projection_path.read_bytes()
projection_sha = hashlib.sha256(projection_bytes.rstrip(b"\n")).hexdigest()
projection_file_sha = hashlib.sha256(projection_bytes).hexdigest()
if projection_sha != measurement["projection_sha256"]:
    raise SystemExit("projection bytes do not match the saved measurement")
projection = json.loads(projection_path.read_text())
if (projection.get("kind") != "jev-full-context-relationship-projection"
        or projection.get("snapshot_digest") != snapshot.get("snapshot_digest")
        or projection.get("main_tip") != snapshot.get("main", {}).get("tip")
        or projection.get("snapshot_lock_sha256") != measurement.get("snapshot_lock_sha256")):
    raise SystemExit("projection and pinned snapshot do not match")
branch_by_name = {item["name"]: item for item in snapshot["branches"]}
destination_by_id = {item["id"]: item for item in projection["destination_units"]}
selected = []
used_branches: set[str] = set()
for unit in sorted(projection["source_units"], key=lambda row: (row.get("branch", ""), row.get("id", ""))):
    branch = branch_by_name.get(unit.get("branch"))
    if (unit.get("kind") != "python_definition" or not str(unit.get("path", "")).endswith(".py")
            or unit.get("main_tip") != snapshot["main"]["tip"]
            or not branch or branch.get("eligible") is not True
            or branch.get("tip") != unit.get("source_tip")
            or unit.get("branch") in used_branches):
        continue
    destination_ids = unit.get("destination_ids", [])
    if len(destination_ids) != 1:
        continue
    destination = destination_by_id.get(destination_ids[0])
    if (not destination or destination.get("kind") != "python_definition"
            or destination.get("name") != unit.get("name")
            or not str(destination.get("path", "")).endswith(".py")):
        continue
    selected.append((unit, destination, branch))
    used_branches.add(unit["branch"])
    if len(selected) == 3:
        break
if len(selected) != 3:
    raise SystemExit(f"projection yielded only {len(selected)} eligible uniquely matched Python cases")

units = []
destinations = {}
branches = {}
edges = []
range_rows = []
evidence_by_id = {}
selection_rows = []
for index, (source, destination, branch) in enumerate(selected, 1):
    source_id = source["id"]
    destination_id = destination["id"]
    selected_branch = branches.setdefault(branch["name"], {
        "name": branch["name"], "tip": branch["tip"], "eligible": True,
        "unit_ids": [], "exclusion_reasons": [],
    })
    selected_branch["unit_ids"].append(source_id)
    unit = dict(source)
    unit["source"] = {"branch": source["branch"], "path": source["path"], "blob": source["source_blob"]}
    unit["destination_ids"] = [destination_id]
    unit["limitations"] = sorted(set(unit.get("limitations", [])) | {"three_case_projection_subset"})
    units.append(unit)
    destinations[destination_id] = dict(destination)
    edges.extend(edge for edge in projection["contribution_edges"]
                 if edge.get("source_id") == source_id and edge.get("destination_id") == destination_id)
    evidence_id = f"focused-8c-case-{index}"
    ranges = [{
        "evidence_id": evidence_id,
        "source_path": source["path"], "source_range": source["range"],
        "destination_path": destination["path"], "destination_range": destination["range"],
    }]
    range_rows.append({"contribution_id": source_id, "source_tip": source["source_tip"],
                       "destination_tip": source["main_tip"], "ranges": ranges})
    evidence = build_two_sided_evidence(
        object_repo, source["source_tip"], source["main_tip"], ranges,
        max_total_bytes=24_000, max_excerpt_pairs=8, max_total_lines=240,
    )
    record = evidence["records"][0]
    if (record["source"]["blob"] != source["source_blob"]
            or record["destination"]["blob"] != destination["blob"]):
        raise SystemExit(f"selected case {source_id} range does not match its pinned source/main blob IDs")
    evidence_by_id[source_id] = evidence
    selection_rows.append({
        "contribution_id": source_id, "branch": source["branch"], "source_tip": source["source_tip"],
        "main_tip": source["main_tip"], "source_path": source["path"], "source_range": source["range"],
        "source_blob": source["source_blob"], "destination_id": destination_id,
        "destination_path": destination["path"], "destination_range": destination["range"],
        "destination_blob": destination["blob"], "evidence_id": evidence_id,
    })

contributions = {
    "kind": "contributions", "schema_version": 1,
    "repository_id": snapshot["repository_id"], "snapshot_digest": snapshot["snapshot_digest"],
    "main": snapshot["main"], "branches": list(branches.values()), "units": units,
    "destination_units": list(destinations.values()), "edges": edges, "paths": [],
    "limitations": ["three_case_projection_subset", "cohort_relationship_context_non_exhaustive"],
}
contributions["contributions_digest"] = digest(contributions)
groups = build_groups(contributions)
range_manifest = {
    "kind": "branch-presence-range-manifest", "schema_version": 1,
    "snapshot_digest": snapshot["snapshot_digest"],
    "contributions_digest": contributions["contributions_digest"],
    "groups_digest": groups["groups_digest"], "ranges": range_rows,
}
study = {
    "kind": "presence-study", "schema_version": 2,
    "repository_id": contributions["repository_id"],
    "snapshot_digest": contributions["snapshot_digest"],
    "contributions_digest": contributions["contributions_digest"],
    "groups_digest": groups["groups_digest"],
    "selection_policy": "focused-pilot-case-list-v1",
    "cases": [{"contribution_id": row["contribution_id"]} for row in selection_rows],
    "selection_uses_model_answers": False, "provider_dispatch_approved": False,
}
study["study_digest"] = digest(study)
selection_digest = study["study_digest"]
range_manifest["selection_digest"] = selection_digest
kwargs = {"selected_contribution_ids": [row["contribution_id"] for row in selection_rows],
          "selection_digest": selection_digest, "max_groups": 3, "max_requests": 8,
          "max_request_bytes": 64_000, "model_settings": {"model": "jev-latest"}}
initial_plan = build_group_requests(contributions, groups, evidence_by_id, **kwargs)
estimated_input_tokens = math.ceil(initial_plan["payload_bytes"] / 4) + 256 * initial_plan["request_count"]
plan = build_group_requests(
    contributions, groups, evidence_by_id, **kwargs,
    estimated_input_tokens=estimated_input_tokens,
    max_provider_tokens=estimated_input_tokens + 512 * initial_plan["request_count"],
    token_estimator="serialized_utf8_bytes_plus_256_per_request_v1",
)
preview = approved_presence_preview(plan)

output.mkdir(mode=0o700, parents=True, exist_ok=False)
output.chmod(0o700)
write_json(output / "three-case-contributions.json", contributions)
write_json(output / "three-case-groups.json", groups)
write_json(output / "three-case-ranges.json", range_manifest)
write_json(output / "three-case-study.json", study)
write_json(output / "model-settings.json", {"model": "jev-latest"})
write_json(output / "three-case-selection.json", {
    "kind": "focused-jev-pilot-selection", "schema_version": 1,
    "snapshot_digest": snapshot["snapshot_digest"], "main_tip": snapshot["main"]["tip"],
    "projection_sha256": projection_sha, "selection_digest": selection_digest,
    "cases": selection_rows,
    "candidate_selection": "one eligible branch unit per branch with exactly one same-name Python main candidate",
    "limitations": ["three-case pilot only", "projection subset is non-exhaustive", "project requirements not supplied"],
})
write_json(output / "jev-request-manifest.json", {
    "kind": "branch-presence-approved-manifest", "schema_version": 1,
    "snapshot_digest": snapshot["snapshot_digest"],
    "contributions_digest": contributions["contributions_digest"],
    "groups_digest": groups["groups_digest"], "selection_digest": selection_digest,
    "range_manifest_digest": digest(range_manifest),
    "model_settings": preview["model_settings"],
    "model_settings_digest": preview["model_settings_digest"],
    "request_budgets": preview["request_budgets"], "request_count": preview["request_count"],
    "payload_bytes": preview["payload_bytes"],
    "request_bytes_by_chunk": preview["request_bytes_by_chunk"],
    "payload_sha256": preview["payload_sha256"], "approval_sha256": preview["approval_sha256"],
    "request_case_ids": [[item.get("contribution_id") for item in request["state"].get("contributions", [])]
                         for request in preview["requests"]],
    "source_excerpt_bytes_transient_only": sum(item["total_bytes"] for item in evidence_by_id.values()),
    "source_excerpts_persisted": False, "network_performed": False,
    "project_utility_assessment": "UNKNOWN: project requirements not provided",
    "control_arm": "not dispatched; must use same evidence after separate authorization",
})
print(json.dumps({
    "output": str(output), "snapshot_digest": snapshot["snapshot_digest"],
    "main_tip": snapshot["main"]["tip"], "projection_sha256": projection_sha,
    "projection_file_sha256": projection_file_sha,
    "case_ids": [row["contribution_id"] for row in selection_rows],
    "payload_sha256": preview["payload_sha256"], "approval_sha256": preview["approval_sha256"],
    "request_count": preview["request_count"], "payload_bytes": preview["payload_bytes"],
    "source_excerpt_bytes_transient_only": sum(item["total_bytes"] for item in evidence_by_id.values()),
    "source_excerpts_persisted": False, "network_performed": False,
}, sort_keys=True))
