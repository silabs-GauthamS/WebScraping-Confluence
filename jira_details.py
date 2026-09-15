import csv
import logging
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

from jira_client import JiraClient


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("jira_details")

SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "confluence_output"
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


def write_csv(output_path, rows):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main():
    load_dotenv()
    input_path = OUTPUT_DIR / "test_executions.csv"
    output_path = OUTPUT_DIR / "jira_details.csv"

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
