#!/usr/bin/env python3
"""Validate desired GitHub protections and produce a GET-only semantic diff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "supply-chain/github-protection.json"
DEFAULT_SCHEMA = ROOT / "supply-chain/github-protection.schema.json"


class ProtectionError(RuntimeError):
    pass


def validate_json_schema(instance: Any, schema: Any, path: str = "$") -> None:
    if not isinstance(schema, dict):
        raise ProtectionError(f"schema node must be an object at {path}")
    if "const" in schema and instance != schema["const"]:
        raise ProtectionError(f"schema const mismatch at {path}")
    expected_type = schema.get("type")
    if expected_type == "object" and not isinstance(instance, dict):
        raise ProtectionError(f"schema expected object at {path}")
    if expected_type == "array" and not isinstance(instance, list):
        raise ProtectionError(f"schema expected array at {path}")
    if isinstance(instance, dict):
        required = schema.get("required") or []
        missing = [key for key in required if key not in instance]
        if missing:
            raise ProtectionError(f"schema missing fields at {path}: {','.join(missing)}")
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(instance) - set(properties))
            if unknown:
                raise ProtectionError(f"schema unknown fields at {path}: {','.join(unknown)}")
        for key, child_schema in properties.items():
            if key in instance:
                validate_json_schema(instance[key], child_schema, f"{path}.{key}")
    if isinstance(instance, list):
        if len(instance) < int(schema.get("minItems", 0)):
            raise ProtectionError(f"schema array is too short at {path}")
        if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
            raise ProtectionError(f"schema array is too long at {path}")
        if "items" in schema:
            for index, value in enumerate(instance):
                validate_json_schema(value, schema["items"], f"{path}[{index}]")
    for child_schema in schema.get("allOf") or []:
        if "contains" in child_schema:
            if not isinstance(instance, list):
                raise ProtectionError(f"schema contains requires array at {path}")
            matches = 0
            for value in instance:
                try:
                    validate_json_schema(value, child_schema["contains"], path)
                except ProtectionError:
                    continue
                matches += 1
            if matches < int(child_schema.get("minContains", 1)):
                raise ProtectionError(f"schema contains minimum failed at {path}")
            if "maxContains" in child_schema and matches > int(child_schema["maxContains"]):
                raise ProtectionError(f"schema contains maximum failed at {path}")
        else:
            validate_json_schema(instance, child_schema, path)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtectionError(f"cannot read {path.name}: {type(exc).__name__}") from exc


def validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise ProtectionError("protection manifest must be an object")
    if set(manifest) != {
        "schema_version",
        "repository",
        "mode",
        "mutation_allowed",
        "branch_rulesets",
        "environments",
    }:
        raise ProtectionError("protection manifest has missing or unknown top-level fields")
    if manifest.get("schema_version") != 1 or manifest.get("repository") != "1fear/TakSklad":
        raise ProtectionError("protection manifest identity is invalid")
    if manifest.get("mode") != "desired-read-only" or manifest.get("mutation_allowed") is not False:
        raise ProtectionError("protection manifest must be read-only")
    rulesets = manifest.get("branch_rulesets")
    environments = manifest.get("environments")
    if not isinstance(rulesets, list) or len(rulesets) != 1:
        raise ProtectionError("exactly one main ruleset is required")
    if not isinstance(environments, list) or len(environments) != 1:
        raise ProtectionError("exactly one production environment is required")
    ruleset = rulesets[0]
    if (
        ruleset.get("name") != "TakSklad main release gate"
        or ruleset.get("target") != "branch"
        or ruleset.get("enforcement") != "active"
        or ruleset.get("bypass_actors") != []
    ):
        raise ProtectionError("main ruleset identity/enforcement/bypass policy is invalid")
    ref_name = (ruleset.get("conditions") or {}).get("ref_name") or {}
    if ref_name != {"include": ["refs/heads/main"], "exclude": []}:
        raise ProtectionError("main ruleset must target only refs/heads/main")
    rules = ruleset.get("rules")
    if not isinstance(rules, list):
        raise ProtectionError("main ruleset rules must be a list")
    by_type = {str(rule.get("type")): rule for rule in rules if isinstance(rule, dict)}
    required_types = {"deletion", "non_fast_forward", "required_linear_history", "required_status_checks"}
    if set(by_type) != required_types or len(rules) != len(required_types):
        raise ProtectionError("main ruleset has missing, duplicate or unknown rules")
    status = by_type["required_status_checks"].get("parameters") or {}
    if status != {
        "strict_required_status_checks_policy": True,
        "do_not_enforce_on_create": False,
        "required_status_checks": [{"context": "Release gate"}],
    }:
        raise ProtectionError("required status-check policy must be strict Release gate")
    environment = environments[0]
    if set(environment) != {
        "name",
        "wait_timer",
        "prevent_self_review",
        "can_admins_bypass",
        "required_reviewers",
        "deployment_branch_policy",
    }:
        raise ProtectionError("production environment has missing or unknown fields")
    if environment.get("name") != "production" or environment.get("wait_timer") != 0:
        raise ProtectionError("production environment identity/wait timer is invalid")
    if environment.get("can_admins_bypass") is not False:
        raise ProtectionError("production administrator bypass must be disabled")
    if environment.get("prevent_self_review") is not False:
        raise ProtectionError("single-operator production review policy must remain explicit")
    if environment.get("required_reviewers") != [{"type": "User", "login": "1fear"}]:
        raise ProtectionError("production required reviewer is invalid")
    if environment.get("deployment_branch_policy") != {
        "protected_branches": True,
        "custom_branch_policies": False,
    }:
        raise ProtectionError("production deployments must be restricted to protected branches")
    validate_json_schema(manifest, load_json(DEFAULT_SCHEMA))
    return manifest


def gh_get(endpoint: str) -> Any:
    completed = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", endpoint],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ProtectionError(f"GitHub GET unavailable for {endpoint}: exit={completed.returncode}")
    return json.loads(completed.stdout)


def fetch_live(repository: str, ruleset_name: str) -> dict[str, Any]:
    rulesets = gh_get(f"repos/{repository}/rulesets")
    if not isinstance(rulesets, list):
        raise ProtectionError("GitHub rulesets response is invalid")
    selected = next((item for item in rulesets if item.get("name") == ruleset_name), None)
    detail = None
    if selected is not None:
        identifier = selected.get("id")
        if not isinstance(identifier, int):
            raise ProtectionError("GitHub ruleset id is invalid")
        detail = gh_get(f"repos/{repository}/rulesets/{identifier}")
    environment = gh_get(f"repos/{repository}/environments/production")
    return {"ruleset": detail, "environment": environment}


def _actual_rules_by_type(ruleset: Any) -> dict[str, Any]:
    if not isinstance(ruleset, dict) or not isinstance(ruleset.get("rules"), list):
        return {}
    return {str(item.get("type")): item for item in ruleset["rules"] if isinstance(item, dict)}


def semantic_diff(manifest: dict[str, Any], live: dict[str, Any], *, source: str) -> dict[str, Any]:
    desired_ruleset = manifest["branch_rulesets"][0]
    desired_environment = manifest["environments"][0]
    actual_ruleset = live.get("ruleset")
    actual_environment = live.get("environment")
    actual_rule_types = _actual_rules_by_type(actual_ruleset)
    checks: list[tuple[str, Any, Any]] = [
        ("branch_ruleset.exists", True, isinstance(actual_ruleset, dict)),
        ("branch_ruleset.name", desired_ruleset["name"], (actual_ruleset or {}).get("name")),
        ("branch_ruleset.target", "branch", (actual_ruleset or {}).get("target")),
        ("branch_ruleset.enforcement", "active", (actual_ruleset or {}).get("enforcement")),
        ("branch_ruleset.bypass_actors", [], (actual_ruleset or {}).get("bypass_actors")),
        (
            "branch_ruleset.conditions.ref_name",
            desired_ruleset["conditions"]["ref_name"],
            ((actual_ruleset or {}).get("conditions") or {}).get("ref_name"),
        ),
    ]
    for rule_type in ("deletion", "non_fast_forward", "required_linear_history"):
        checks.append((f"branch_ruleset.rules.{rule_type}", True, rule_type in actual_rule_types))
    desired_status = next(rule for rule in desired_ruleset["rules"] if rule["type"] == "required_status_checks")
    checks.extend(
        [
            (
                "branch_ruleset.rules.required_status_checks",
                desired_status.get("parameters"),
                (actual_rule_types.get("required_status_checks") or {}).get("parameters"),
            ),
            ("environment.exists", True, isinstance(actual_environment, dict)),
            ("environment.can_admins_bypass", False, (actual_environment or {}).get("can_admins_bypass")),
            (
                "environment.deployment_branch_policy",
                desired_environment["deployment_branch_policy"],
                (actual_environment or {}).get("deployment_branch_policy"),
            ),
        ]
    )
    protection_rules = (actual_environment or {}).get("protection_rules") or []
    reviewer_rule = next((item for item in protection_rules if item.get("type") == "required_reviewers"), None)
    wait_rule = next((item for item in protection_rules if item.get("type") == "wait_timer"), None)
    actual_reviewers = []
    if reviewer_rule:
        for item in reviewer_rule.get("reviewers") or []:
            reviewer = item.get("reviewer") or {}
            actual_reviewers.append({"type": item.get("type"), "login": reviewer.get("login")})
    checks.extend(
        [
            ("environment.wait_timer", 0, (wait_rule or {}).get("wait_timer", 0)),
            ("environment.prevent_self_review", False, (reviewer_rule or {}).get("prevent_self_review")),
            ("environment.required_reviewers", desired_environment["required_reviewers"], actual_reviewers),
        ]
    )
    differences = []
    exact = 0
    for path, desired, actual in checks:
        status = "exact" if desired == actual else "pending"
        exact += int(status == "exact")
        differences.append({"path": path, "status": status, "desired": desired, "actual": actual})
    return {
        "schema_version": 1,
        "repository": manifest["repository"],
        "source": source,
        "read_only": True,
        "mutation_count": 0,
        "settings_count": len(differences),
        "exact_count": exact,
        "pending_count": len(differences) - exact,
        "settings": differences,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = validate_manifest(load_json(args.manifest.resolve()))
        if args.validate_only:
            print("GITHUB_PROTECTION_MANIFEST_OK mutation_allowed=0")
            return 0
        if args.snapshot:
            live = load_json(args.snapshot.resolve())
            source = "snapshot"
        else:
            live = fetch_live(manifest["repository"], manifest["branch_rulesets"][0]["name"])
            source = "github-read-only-live"
        result = semantic_diff(manifest, live, source=source)
        serialized = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
        print(
            "GITHUB_PROTECTION_DIFF "
            f"source={source} settings={result['settings_count']} exact={result['exact_count']} "
            f"pending={result['pending_count']} mutation_count=0"
        )
        for item in result["settings"]:
            if item["status"] != "exact":
                print(f"GITHUB_PROTECTION_PENDING path={item['path']}")
        return 0 if result["pending_count"] == 0 else 1
    except ProtectionError as exc:
        print(f"GITHUB_PROTECTION_ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
