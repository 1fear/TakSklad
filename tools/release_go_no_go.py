#!/usr/bin/env python3
import argparse
import json
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_PATH = PROJECT_ROOT / "outputs" / "taksklad_acceptance" / "ACCEPTANCE_RESULTS.md"
RESULT_TEMPLATE_NAME = "ACCEPTANCE_RESULTS_TEMPLATE.md"

REQUIRED_GO_CHECKS = [
    "Telegram import принят.",
    "SkladBot matching принят.",
    "Windows desktop acceptance принят.",
    "Критичных дефектов нет.",
    "Rollback понятен.",
    "`version.json` проверен и `mandatory=true`.",
]

GO_LINE = "GO к подготовке release 2.0."
NO_GO_LINE = "NO-GO, релиз откладывается."
ACCEPTANCE_CHECK_SECTIONS = [
    "1. Preflight",
    "2. Telegram Import",
    "3. SkladBot Matching",
    "4. Windows Desktop Acceptance",
    "5. Cleanup",
]
REQUIRED_SECTIONS = ACCEPTANCE_CHECK_SECTIONS + [
    "6. Defects / Known Issues",
    "7. Go / No-Go",
]
CRITICAL_SEVERITIES = {
    "critical",
    "blocker",
    "p0",
    "p1",
    "крит",
    "критично",
    "критическая",
    "блокер",
}
RESOLVED_STATUSES = {
    "done",
    "fixed",
    "closed",
    "resolved",
    "accepted",
    "принято",
    "исправлено",
    "закрыто",
    "решено",
}


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def checked_lines(text):
    lines = {}
    for item in checkbox_items(text):
        lines[item["label"]] = item["checked"]
    return lines


def checked_lines_by_section(text, title):
    return checked_lines(section_text(text, title))


def checkbox_items(text):
    items = []
    for line in text.splitlines():
        match = re.match(r"^\s*-\s*\[([xX ])\]\s*(.+?)\s*$", line)
        if match:
            items.append({
                "checked": match.group(1).lower() == "x",
                "label": normalize(match.group(2)),
            })
    return items


def section_text(text, title):
    pattern = re.compile(rf"^##\s+{re.escape(title)}\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    next_match = re.search(r"^##\s+", text[match.end():], re.MULTILINE)
    end = match.end() + next_match.start() if next_match else len(text)
    return text[match.end():end]


def unresolved_critical_defects(text):
    defects = []
    section = section_text(text, "6. Defects / Known Issues")
    if not section:
        return defects
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or "---" in line or "Severity" in line:
            continue
        cells = [normalize(cell) for cell in line.strip("|").split("|")]
        if len(cells) < 6 or not any(cells):
            continue
        defect_id, scenario, symptom, severity, _solution, status = cells[:6]
        if not defect_id and not scenario and not symptom:
            continue
        severity_key = severity.casefold()
        status_key = status.casefold()
        if severity_key in CRITICAL_SEVERITIES and status_key not in RESOLVED_STATUSES:
            defects.append({
                "id": defect_id,
                "scenario": scenario,
                "severity": severity,
                "status": status,
            })
    return defects


def template_acceptance_checks(template_text):
    checks = []
    for title in ACCEPTANCE_CHECK_SECTIONS:
        for item in checkbox_items(section_text(template_text, title)):
            checks.append({
                "section": title,
                "label": item["label"],
            })
    return checks


def evaluate_acceptance_results(text, required_acceptance_checks=None):
    lines = checked_lines(text)
    problems = []

    for title in REQUIRED_SECTIONS:
        if not section_text(text, title).strip():
            problems.append(f"required section is missing: {title}")

    for title in ACCEPTANCE_CHECK_SECTIONS:
        section = section_text(text, title)
        if not section.strip():
            continue
        items = checkbox_items(section)
        if not items:
            problems.append(f"required section has no checkboxes: {title}")
            continue
        for item in items:
            if item["checked"] is not True:
                problems.append(f"required acceptance checkbox is not checked in {title}: {item['label']}")

    if required_acceptance_checks:
        section_maps = {
            title: checked_lines_by_section(text, title)
            for title in ACCEPTANCE_CHECK_SECTIONS
        }
        for required in required_acceptance_checks:
            title = required["section"]
            label = required["label"]
            if label not in section_maps.get(title, {}):
                problems.append(f"required acceptance checkbox is missing in {title}: {label}")

    for label in REQUIRED_GO_CHECKS:
        if label not in lines:
            problems.append(f"required GO checkbox is missing: {label}")
        elif lines.get(label) is not True:
            problems.append(f"required GO checkbox is not checked: {label}")

    if GO_LINE not in lines:
        problems.append(f"GO line is missing: {GO_LINE}")
    elif lines.get(GO_LINE) is not True:
        problems.append(f"GO line is not checked: {GO_LINE}")
    if NO_GO_LINE not in lines:
        problems.append(f"NO-GO line is missing: {NO_GO_LINE}")
    if lines.get(NO_GO_LINE) is True:
        problems.append(f"NO-GO line is checked: {NO_GO_LINE}")

    critical_defects = unresolved_critical_defects(text)
    for defect in critical_defects:
        problems.append(
            "unresolved critical defect: "
            f"{defect.get('id') or '-'} {defect.get('scenario') or '-'} "
            f"severity={defect.get('severity')} status={defect.get('status')}"
        )

    return {
        "status": "go" if not problems else "no_go",
        "problems": problems,
        "checked": {label: lines.get(label) is True for label in REQUIRED_GO_CHECKS + [GO_LINE, NO_GO_LINE]},
        "critical_defects": critical_defects,
    }


def evaluate_file(path):
    path = Path(path)
    if not path.exists():
        return {
            "status": "no_go",
            "problems": [f"acceptance results file not found: {path}"],
            "path": str(path),
        }
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        return {
            "status": "no_go",
            "problems": [f"cannot read acceptance results file: {exc}"],
            "path": str(path),
        }
    required_acceptance_checks = None
    template_path = path.with_name(RESULT_TEMPLATE_NAME)
    if template_path.exists():
        try:
            required_acceptance_checks = template_acceptance_checks(template_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "status": "no_go",
                "problems": [f"cannot read acceptance template file: {exc}"],
                "path": str(path),
                "template_path": str(template_path),
            }
    result = evaluate_acceptance_results(
        text,
        required_acceptance_checks=required_acceptance_checks,
    )
    result["path"] = str(path)
    if template_path.exists():
        result["template_path"] = str(template_path)
    return result


def parse_args():
    parser = argparse.ArgumentParser(description="TakSklad 2.0 acceptance GO/NO-GO gate.")
    parser.add_argument(
        "--results",
        default=str(DEFAULT_RESULTS_PATH),
        help="Filled acceptance results markdown file.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    result = evaluate_file(args.results)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "go" else 3


if __name__ == "__main__":
    sys.exit(main())
