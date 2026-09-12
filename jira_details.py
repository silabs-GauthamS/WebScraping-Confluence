import argparse
import csv
import logging
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("jira_details")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR / "confluence_output"
CSV_COLUMNS = [
    "issue_key",
    "compiler",
    "summary",
    "status",
    "priority",
    "is_regression_issue",
]


def required_env(primary, fallback=None):
    value = os.getenv(primary)
    if not value and fallback:
        value = os.getenv(fallback)
    if not value:
        names = f"{primary} or {fallback}" if fallback else primary
        raise RuntimeError(f"Missing required environment variable: {names}")
    return value


def field_value(value):
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(filter(None, (field_value(item) for item in value)))
    if isinstance(value, dict):
        return str(
            value.get("value")
            or value.get("name")
            or value.get("displayName")
            or value.get("key")
            or ""
        )
    return str(value)


def read_defect_compilers(input_path):
    """Return {defect_key: {compiler, ...}} from test_executions.csv."""
    compilers_by_defect = {}
    with input_path.open(encoding="utf-8-sig", newline="") as csv_file:
        for row in csv.DictReader(csv_file):
            compiler = (row.get("compiler") or "").strip()
            for defect in (row.get("defects") or "").split(","):
                defect = defect.strip().upper()
                if not defect:
                    continue
                compilers_by_defect.setdefault(defect, set())
                if compiler:
                    compilers_by_defect[defect].add(compiler)
    return compilers_by_defect


class JiraClient:
    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

    def regression_field_id(self):
        response = self.session.get(
            f"{self.base_url}/rest/api/2/field",
            timeout=30,
        )
        response.raise_for_status()
        for field in response.json():
            normalized_name = re.sub(
                r"[^a-z0-9]",
                "",
                str(field.get("name") or "").casefold(),
            )
            if normalized_name == "isregressionissue":
                return field.get("id")
        log.warning("Jira field 'IsRegressionIssue' was not found")
        return None

    def get_open_issues(self, issue_keys):
        """Fetch issues whose Jira status category is not Done."""
        regression_field = self.regression_field_id()
        rows = []

        for offset in range(0, len(issue_keys), 100):
            key_batch = issue_keys[offset:offset + 100]
            quoted_keys = ", ".join(f'"{key}"' for key in key_batch)
            fields = ["summary", "status", "priority"]
            if regression_field:
                fields.append(regression_field)

            response = self.session.post(
                f"{self.base_url}/rest/api/2/search",
                json={
                    "jql": (
                        f"key in ({quoted_keys}) "
                        "AND statusCategory != Done ORDER BY key"
                    ),
                    "startAt": 0,
                    "maxResults": len(key_batch),
                    "fields": fields,
                },
                timeout=30,
            )
            response.raise_for_status()

            for issue in response.json().get("issues", []):
                issue_fields = issue.get("fields") or {}
                rows.append({
                    "issue_key": issue.get("key") or "",
                    "compiler": "",
                    "summary": issue_fields.get("summary") or "",
                    "status": (issue_fields.get("status") or {}).get("name") or "",
                    "priority": (issue_fields.get("priority") or {}).get("name") or "",
                    "is_regression_issue": (
                        field_value(issue_fields.get(regression_field))
                        if regression_field
                        else ""
                    ),
                })

        return rows


def write_csv(output_path, rows):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Export open Jira issues linked to failed Xray test cases"
    )
    parser.add_argument(
        "--out-dir",
        "-o",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory containing test_executions.csv and receiving jira_details.csv",
    )
    args = parser.parse_args()

    load_dotenv()
    input_path = args.out_dir / "test_executions.csv"
    output_path = args.out_dir / "jira_details.csv"

    if not input_path.exists():
        log.error("Input file not found: %s", input_path)
        return 1

    try:
        jira_base_url = required_env("JIRA_BASE_URL", "CONFLUENCE_BASE_URL")
        jira_token = required_env("JIRA_TOKEN", "CONFLUENCE_TOKEN")
        compilers_by_defect = read_defect_compilers(input_path)

        if compilers_by_defect:
            client = JiraClient(jira_base_url, jira_token)
            rows = client.get_open_issues(sorted(compilers_by_defect))
            for row in rows:
                row["compiler"] = ", ".join(
                    sorted(compilers_by_defect.get(row["issue_key"], set()))
                )
        else:
            rows = []

        write_csv(output_path, rows)
    except (OSError, RuntimeError, requests.RequestException, ValueError) as exc:
        log.error("Could not create Jira details CSV: %s", exc)
        return 1

    log.info("Wrote %s (%s open issues)", output_path, len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
