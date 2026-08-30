#!/usr/bin/env python3
"""Rebuild a downstream-local project calibration Issue from durable GitHub comments.

No upstream writes, no repository-content writes, no external dependencies.
"""

import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

OUTCOME_MARKER = "[PROJECT-CALIBRATION-OUTCOME-v1]"
STATE_MARKER = "[PROJECT-LOCAL-CALIBRATION-STATE-v1]"
STATE_TITLE = "[PROJECT-LOCAL-CALIBRATION] Derived model routing prior"
SCHEMA_VERSION = "1.0"
DEFAULT_HALF_LIFE_DAYS = 90.0
MAX_SEARCH_RESULTS = 1000
ALLOWED_ROLES = {"bounded_worker", "validator"}
ALLOWED_RISK = {"low", "medium", "high", "critical"}
ALLOWED_ATTRIBUTION = {"assigned_executor", "execution_strategy", "unattributed"}


class CalibrationError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(value, fallback=None):
    if not value:
        return fallback or utc_now()
    value = str(value).strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return fallback or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def bool_or_none(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "pass", "passed"}:
            return True
        if lowered in {"false", "no", "fail", "failed"}:
            return False
    return None


def int_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def valid_timestamp(value):
    if not nonempty_string(value):
        return False
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None


def extract_outcome_blocks(body):
    if not body or OUTCOME_MARKER not in body:
        return []
    pattern = re.compile(
        re.escape(OUTCOME_MARKER) + r"\s*```json\s*(.*?)\s*```",
        re.DOTALL | re.IGNORECASE,
    )
    blocks = []
    for raw in pattern.findall(body):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            blocks.append({"__parse_error__": str(exc)})
            continue
        blocks.append(value)
    return blocks


def validate_event(event):
    errors = []
    if not isinstance(event, dict):
        return ["event_not_object"]
    if event.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version")
    if event.get("terminal") is not True:
        errors.append("terminal_true_required")
    if not nonempty_string(event.get("task_ref")):
        errors.append("task_ref")
    if not nonempty_string(event.get("task_class")):
        errors.append("task_class")
    if event.get("risk_level") not in ALLOWED_RISK:
        errors.append("risk_level")
    if not valid_timestamp(event.get("accepted_at")):
        errors.append("accepted_at")
    revision = int_or_none(event.get("revision", 1))
    if revision is None or revision < 1:
        errors.append("revision")
    samples = event.get("samples")
    if not isinstance(samples, list) or not samples:
        errors.append("samples")
    else:
        for index, sample in enumerate(samples):
            errors.extend(f"sample[{index}].{e}" for e in validate_sample(sample))
    return errors


def validate_sample(sample):
    errors = []
    if not isinstance(sample, dict):
        return ["not_object"]
    role = sample.get("role")
    if role not in ALLOWED_ROLES:
        errors.append("role")
    attribution = sample.get("attribution", "assigned_executor")
    if attribution not in ALLOWED_ATTRIBUTION:
        errors.append("attribution")
    if attribution == "assigned_executor":
        if not nonempty_string(sample.get("model_id")):
            errors.append("model_id")
        if not nonempty_string(sample.get("model_semantic_key")):
            errors.append("model_semantic_key")
        if not nonempty_string(sample.get("reasoning_semantic_key")):
            errors.append("reasoning_semantic_key")
    elif attribution == "execution_strategy":
        if not nonempty_string(sample.get("strategy_key")):
            errors.append("strategy_key")

    rework = int_or_none(sample.get("rework_count", 0))
    if rework is None or rework < 0:
        errors.append("rework_count")

    if role == "bounded_worker":
        outcome = sample.get("worker_outcome")
        if not isinstance(outcome, dict):
            errors.append("worker_outcome")
        else:
            if bool_or_none(outcome.get("final_accepted")) is None:
                errors.append("worker_outcome.final_accepted")
            if bool_or_none(outcome.get("accepted_first_pass")) is None:
                errors.append("worker_outcome.accepted_first_pass")
    elif role == "validator":
        outcome = sample.get("validator_outcome")
        if not isinstance(outcome, dict):
            errors.append("validator_outcome")
        else:
            if bool_or_none(outcome.get("review_completed")) is None:
                errors.append("validator_outcome.review_completed")
            for key in (
                "material_findings_confirmed",
                "false_positive_confirmed",
                "missed_defect_confirmed",
            ):
                value = outcome.get(key)
                if value is not None:
                    parsed = int_or_none(value)
                    if parsed is None or parsed < 0:
                        errors.append(f"validator_outcome.{key}")
    return errors


def event_identity(event):
    return str(event["task_ref"]).strip()


def event_revision(event):
    return int_or_none(event.get("revision", 1)) or 1


def choose_latest_events(parsed_events):
    """Keep the latest terminal normalization per task_ref.

    A higher revision (or later comment at the same revision) may correct an older
    well-identified malformed normalization. Unparseable/unidentified events
    remain fail-closed because the extractor cannot prove which task revision
    supersedes them.
    """
    chosen = {}
    unscoped_malformed = []
    for entry in parsed_events:
        event = entry.get("event")
        errors = entry.get("errors", [])
        task_ref = event.get("task_ref") if isinstance(event, dict) else None
        revision = int_or_none(event.get("revision", 1)) if isinstance(event, dict) else None
        if not nonempty_string(task_ref) or revision is None or revision < 1:
            if errors:
                unscoped_malformed.append({
                    "comment_id": entry.get("comment_id"),
                    "errors": errors,
                })
            continue
        key = task_ref.strip()
        candidate = (revision, int(entry.get("comment_id") or 0))
        current = chosen.get(key)
        if current is None or candidate > current["order"]:
            chosen[key] = {**entry, "order": candidate}

    valid = []
    malformed = list(unscoped_malformed)
    for key, entry in sorted(chosen.items()):
        if entry.get("errors"):
            malformed.append({
                "task_ref": key,
                "revision": entry["order"][0],
                "comment_id": entry.get("comment_id"),
                "errors": entry["errors"],
            })
        else:
            valid.append(entry)
    return valid, malformed


def decay_weight(accepted_at, now, half_life_days):
    age_days = max(0.0, (now - accepted_at).total_seconds() / 86400.0)
    return math.exp(-math.log(2.0) * age_days / half_life_days)


def evidence_strength(effective_samples):
    if effective_samples < 2.0:
        return "insufficient"
    if effective_samples < 5.0:
        return "developing"
    return "established"


def ratio(num, den):
    if den <= 0:
        return None
    return round(num / den, 4)


def weighted_mean(total, weight):
    if weight <= 0:
        return None
    return round(total / weight, 4)


def add_bool_metric(bucket, prefix, value, weight):
    parsed = bool_or_none(value)
    if parsed is None:
        return
    bucket[prefix + "_known_weight"] += weight
    bucket[prefix + "_true_weight"] += weight if parsed else 0.0
    bucket[prefix + "_known_count"] += 1
    bucket[prefix + "_true_count"] += 1 if parsed else 0


def aggregate(events, now=None, half_life_days=DEFAULT_HALF_LIFE_DAYS):
    now = now or utc_now()
    groups = {}
    strategy_groups = {}
    ignored_unattributed = 0

    def get_group(container, key, common):
        if key not in container:
            container[key] = {
                **common,
                "_metrics": defaultdict(float),
                "_dispatch": Counter(),
                "_runtime": Counter(),
                "_latency": Counter(),
                "_cost": Counter(),
            }
        return container[key]

    for entry in events:
        event = entry["event"]
        accepted_at = parse_time(event.get("accepted_at"), entry.get("created_at"))
        weight = decay_weight(accepted_at, now, half_life_days)
        for sample in event["samples"]:
            attribution = sample.get("attribution", "assigned_executor")
            role = sample["role"]
            common = {
                "role": role,
                "task_class": event["task_class"],
                "risk_level": event["risk_level"],
            }
            if attribution == "assigned_executor":
                common.update({
                    "model_id": sample["model_id"],
                    "model_semantic_key": sample["model_semantic_key"],
                    "reasoning_semantic_key": sample["reasoning_semantic_key"],
                })
                key = (
                    role,
                    event["task_class"],
                    event["risk_level"],
                    sample["model_semantic_key"],
                    sample["reasoning_semantic_key"],
                )
                bucket = get_group(groups, key, common)
            elif attribution == "execution_strategy":
                common.update({"strategy_key": sample["strategy_key"]})
                key = (role, event["task_class"], event["risk_level"], sample["strategy_key"])
                bucket = get_group(strategy_groups, key, common)
            else:
                ignored_unattributed += 1
                continue

            metrics = bucket["_metrics"]
            metrics["samples"] += 1
            metrics["effective_samples"] += weight
            rework = int_or_none(sample.get("rework_count", 0)) or 0
            metrics["rework_total"] += rework
            metrics["weighted_rework_total"] += rework * weight
            metrics["weight_total"] += weight

            execution = sample.get("execution") or {}
            if nonempty_string(execution.get("dispatch_route")):
                bucket["_dispatch"][execution["dispatch_route"]] += 1
            if nonempty_string(execution.get("runtime_profile")):
                bucket["_runtime"][execution["runtime_profile"]] += 1
            efficiency = sample.get("efficiency") or {}
            if nonempty_string(efficiency.get("latency_bucket")):
                bucket["_latency"][efficiency["latency_bucket"]] += 1
            if nonempty_string(efficiency.get("cost_bucket")):
                bucket["_cost"][efficiency["cost_bucket"]] += 1

            if role == "bounded_worker":
                outcome = sample["worker_outcome"]
                add_bool_metric(metrics, "final_accepted", outcome.get("final_accepted"), weight)
                add_bool_metric(metrics, "accepted_first_pass", outcome.get("accepted_first_pass"), weight)
                add_bool_metric(metrics, "tests_passed", outcome.get("tests_passed"), weight)
                violation = any(
                    bool_or_none(outcome.get(name)) is True
                    for name in ("scope_violation", "permission_violation", "safety_violation")
                )
                add_bool_metric(metrics, "any_violation", violation, weight)
            else:
                outcome = sample["validator_outcome"]
                add_bool_metric(metrics, "review_completed", outcome.get("review_completed"), weight)
                for name in (
                    "material_findings_confirmed",
                    "false_positive_confirmed",
                    "missed_defect_confirmed",
                ):
                    value = int_or_none(outcome.get(name))
                    if value is not None:
                        metrics[name + "_known_count"] += 1
                        metrics[name + "_total"] += value
                        metrics[name + "_weighted_total"] += value * weight
                        metrics[name + "_known_weight"] += weight

    def finalize(container):
        output = []
        for _, bucket in sorted(container.items(), key=lambda kv: kv[0]):
            metrics = bucket.pop("_metrics")
            weight_total = metrics.get("weight_total", 0.0)
            base = dict(bucket)
            dispatch = base.pop("_dispatch")
            runtime = base.pop("_runtime")
            latency = base.pop("_latency")
            cost = base.pop("_cost")
            summary = {
                "sample_count": int(metrics.get("samples", 0)),
                "effective_sample_count": round(metrics.get("effective_samples", 0.0), 3),
                "evidence_strength": evidence_strength(metrics.get("effective_samples", 0.0)),
                "mean_rework_count": round(metrics.get("rework_total", 0.0) / max(metrics.get("samples", 1), 1), 4),
                "weighted_mean_rework_count": weighted_mean(metrics.get("weighted_rework_total", 0.0), weight_total),
            }
            if base["role"] == "bounded_worker":
                for prefix in ("final_accepted", "accepted_first_pass", "tests_passed", "any_violation"):
                    summary[prefix + "_rate"] = ratio(
                        metrics.get(prefix + "_true_weight", 0.0),
                        metrics.get(prefix + "_known_weight", 0.0),
                    )
            else:
                summary["review_completed_rate"] = ratio(
                    metrics.get("review_completed_true_weight", 0.0),
                    metrics.get("review_completed_known_weight", 0.0),
                )
                for name in (
                    "material_findings_confirmed",
                    "false_positive_confirmed",
                    "missed_defect_confirmed",
                ):
                    summary[name + "_weighted_mean"] = weighted_mean(
                        metrics.get(name + "_weighted_total", 0.0),
                        metrics.get(name + "_known_weight", 0.0),
                    )
                    summary[name + "_total"] = int(metrics.get(name + "_total", 0.0))
                    summary[name + "_known_samples"] = int(metrics.get(name + "_known_count", 0.0))
            base["metrics"] = summary
            base["covariates"] = {
                "dispatch_route_counts": dict(sorted(dispatch.items())),
                "runtime_profile_counts": dict(sorted(runtime.items())),
                "latency_bucket_counts": dict(sorted(latency.items())),
                "cost_bucket_counts": dict(sorted(cost.items())),
            }
            output.append(base)
        return output

    return {
        "model_groups": finalize(groups),
        "strategy_groups": finalize(strategy_groups),
        "unattributed_sample_count": ignored_unattributed,
    }


def local_policy_enabled(path):
    if not path or not os.path.exists(path):
        return True
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    match = re.search(
        r"(?ms)^model_calibration:\s*\n(?P<body>(?:^[ \t]+.*\n?)*)",
        text,
    )
    if not match:
        return True
    body = match.group("body")
    enabled = re.search(r"(?m)^[ \t]+enabled:\s*(true|false)\s*(?:#.*)?$", body, re.I)
    return not enabled or enabled.group(1).lower() == "true"


class GitHubAPI:
    def __init__(self, repo, token):
        if "/" not in repo:
            raise CalibrationError("GITHUB_REPOSITORY must be owner/name")
        self.repo = repo
        self.token = token
        self.base = "https://api.github.com"

    def request(self, method, path, data=None):
        url = self.base + path
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "project-local-calibration/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise CalibrationError(
                f"GitHub API {method} {path} failed: {exc.code} {detail[:500]}"
            ) from exc

    def paged(self, path):
        page = 1
        while True:
            sep = "&" if "?" in path else "?"
            values = self.request("GET", f"{path}{sep}per_page=100&page={page}")
            if not isinstance(values, list):
                raise CalibrationError(f"expected list from {path}")
            for value in values:
                yield value
            if len(values) < 100:
                break
            page += 1

    def search_event_issues(self):
        query = f'repo:{self.repo} in:comments "{OUTCOME_MARKER}"'
        encoded = urllib.parse.urlencode({"q": query, "per_page": 100, "page": 1})
        first = self.request("GET", f"/search/issues?{encoded}")
        total = int(first.get("total_count", 0))
        incomplete = bool(first.get("incomplete_results", False))
        items = list(first.get("items", []))
        if total > MAX_SEARCH_RESULTS:
            return items, total, True
        pages = (total + 99) // 100
        for page in range(2, pages + 1):
            encoded = urllib.parse.urlencode({"q": query, "per_page": 100, "page": page})
            payload = self.request("GET", f"/search/issues?{encoded}")
            incomplete = incomplete or bool(payload.get("incomplete_results", False))
            items.extend(payload.get("items", []))
        return items, total, incomplete

    def issue_comments(self, number):
        return list(self.paged(f"/repos/{self.repo}/issues/{number}/comments"))

    def find_state_issues(self):
        query = f'repo:{self.repo} is:issue in:title "{STATE_TITLE}"'
        encoded = urllib.parse.urlencode({"q": query, "per_page": 100, "page": 1})
        payload = self.request("GET", f"/search/issues?{encoded}")
        return [item for item in payload.get("items", []) if item.get("title") == STATE_TITLE]

    def create_state_issue(self, body):
        return self.request("POST", f"/repos/{self.repo}/issues", {"title": STATE_TITLE, "body": body})

    def update_state_issue(self, number, body):
        return self.request(
            "PATCH",
            f"/repos/{self.repo}/issues/{number}",
            {"body": body, "state": "open"},
        )


def collect_entries(api, trigger_issue_number=None):
    items, total, search_incomplete = api.search_event_issues()
    issue_numbers = {int(item["number"]) for item in items}
    if trigger_issue_number:
        issue_numbers.add(int(trigger_issue_number))
    entries = []
    for number in sorted(issue_numbers):
        for comment in api.issue_comments(number):
            for event in extract_outcome_blocks(comment.get("body", "")):
                errors = ["json_parse"] if "__parse_error__" in event else validate_event(event)
                entries.append({
                    "issue_number": number,
                    "comment_id": comment.get("id"),
                    "created_at": parse_time(comment.get("created_at")),
                    "event": event,
                    "errors": errors,
                })
    chosen, malformed = choose_latest_events(entries)
    scan_complete = not search_incomplete and total <= MAX_SEARCH_RESULTS and not malformed
    return chosen, malformed, {
        "search_total_matching_items": total,
        "scanned_item_count": len(issue_numbers),
        "search_limit": MAX_SEARCH_RESULTS,
        "search_incomplete": search_incomplete or total > MAX_SEARCH_RESULTS,
        "malformed_terminal_outcomes": malformed,
        "complete": scan_complete,
    }


def render_state(repo, chosen, scan, aggregates, half_life_days, enabled=True):
    state = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "repository_scope": "same_repository_only",
        "enabled": enabled,
        "usable_for_routing": bool(enabled and scan.get("complete")),
        "authority": {
            "derived_cache_only": True,
            "task_result_validation_acceptance_remain_authoritative": True,
            "deleting_state_does_not_delete_evidence": True,
            "rebuildable_from_durable_comments": True,
        },
        "privacy": {
            "upstream_write": False,
            "central_telemetry": False,
            "adopter_registration": False,
            "required_cost_or_latency_telemetry": False,
        },
        "aggregation_policy": {
            "single_global_model_score": False,
            "roles_separate": True,
            "group_dimensions": [
                "role",
                "task_class",
                "risk_level",
                "model_semantic_key",
                "reasoning_semantic_key",
            ],
            "dispatch_and_runtime_are_covariates_not_model_identity": True,
            "recency_half_life_days": half_life_days,
            "material_internal_delegation_uses_strategy_attribution": True,
        },
        "scan": scan,
        "terminal_task_count": len(chosen),
        **aggregates,
    }
    rendered = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True)
    return (
        f"{STATE_MARKER}\n\n"
        "Derived/rebuildable project-local routing prior. This Issue is not Task, Result, Review, "
        "Acceptance, Lead-claim, or source-code authority. Delete/rebuild it from durable facts at any time.\n\n"
        "```json\n" + rendered + "\n```\n"
    )


def update_state(api, body):
    state_issues = api.find_state_issues()
    if len(state_issues) > 1:
        raise CalibrationError("multiple exact project-local calibration state Issues found")
    if state_issues:
        return api.update_state_issue(int(state_issues[0]["number"]), body)
    return api.create_state_issue(body)


def main():
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GH_TOKEN", "").strip() or os.environ.get("GITHUB_TOKEN", "").strip()
    policy_path = os.environ.get("PROJECT_CALIBRATION_POLICY_PATH", ".agent/LOCAL_POLICY.yaml")
    trigger = os.environ.get("TRIGGER_ISSUE_NUMBER", "").strip()
    trigger_number = int(trigger) if trigger.isdigit() else None
    half_life = float(
        os.environ.get("PROJECT_CALIBRATION_HALF_LIFE_DAYS", DEFAULT_HALF_LIFE_DAYS)
    )
    if half_life <= 0:
        raise CalibrationError("half life must be positive")

    api = GitHubAPI(repo, token)
    enabled = local_policy_enabled(policy_path)
    if not enabled:
        scan = {
            "complete": True,
            "disabled_by_local_policy": True,
            "search_total_matching_items": 0,
            "scanned_item_count": 0,
            "search_limit": MAX_SEARCH_RESULTS,
            "search_incomplete": False,
            "malformed_terminal_outcomes": [],
        }
        body = render_state(
            repo,
            [],
            scan,
            {"model_groups": [], "strategy_groups": [], "unattributed_sample_count": 0},
            half_life,
            enabled=False,
        )
        update_state(api, body)
        print("project-local calibration disabled by LOCAL_POLICY")
        return 0

    chosen, _malformed, scan = collect_entries(api, trigger_number)
    aggregates = aggregate(chosen, half_life_days=half_life)
    body = render_state(repo, chosen, scan, aggregates, half_life, enabled=True)
    update_state(api, body)
    if not scan["complete"]:
        print(
            "calibration state updated fail-closed: usable_for_routing=false",
            file=sys.stderr,
        )
        return 2
    print(f"calibration rebuilt from {len(chosen)} terminal outcome event(s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except CalibrationError as exc:
        print(f"CALIBRATION_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)

