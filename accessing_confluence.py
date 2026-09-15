from io import StringIO
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import logging
import re

import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import pandas as pd

from jira_client import (
    JiraClient,
    XrayError,
    field_value,
    jira_field_has_exact_value,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("confluence_scraper")


class Config:
    def __init__(self):
        load_dotenv()
        self.confluence_base_url = self._require("CONFLUENCE_BASE_URL").rstrip("/")
        self.confluence_token = self._require("CONFLUENCE_TOKEN")
        self.jira_base_url = os.getenv("JIRA_BASE_URL", self.confluence_base_url).rstrip("/")
        self.jira_token = os.getenv("JIRA_TOKEN", self.confluence_token)
        self.page_id = os.getenv("CONFLUENCE_PAGE_ID")
        self.filter_technology = os.getenv("TECHNOLOGY", "WLAN")
        self.filter_test_area = os.getenv("TEST_AREA", "FUNCTIONAL")
        try:
            self.xray_page_size = max(10, int(os.getenv("XRAY_PAGE_SIZE", "50")))
            self.xray_timeout_seconds = max(30, int(os.getenv("XRAY_TIMEOUT_SECONDS", "90")))
            self.xray_retry_count = max(0, int(os.getenv("XRAY_RETRY_COUNT", "2")))
        except ValueError:
            log.warning("Invalid Xray settings; using page size 50, timeout 90, retries 2")
            self.xray_page_size = 50
            self.xray_timeout_seconds = 90
            self.xray_retry_count = 2
        try:
            self.xray_workers = max(1, int(os.getenv("XRAY_WORKERS", "5")))
        except ValueError:
            log.warning("Invalid XRAY_WORKERS setting; using 5 workers")
            self.xray_workers = 5

    @staticmethod
    def _require(key: str) -> str:
        val = os.getenv(key)
        if not val:
            log.error(f"Missing required env val: {key}")
            sys.exit(1)
        return val


def make_jira_client(cfg: Config) -> JiraClient:
    return JiraClient(cfg.jira_base_url, cfg.jira_token, xray_cfg=cfg)


class ConfluenceClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {cfg.confluence_token}",
            "Accept": "application/json",
        })

    def get_page_storage_html(self, page_id: str) -> str:
        url = f"{self.cfg.confluence_base_url}/rest/api/content/{page_id}"
        params = {"expand": "body.storage,version,title"}
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        log.info(
            "Fetched page %s (id=%s, version=%s)",
            data.get("title"),
            page_id,
            data.get("version", {}).get("number"),
        )
        return data["body"]["storage"]["value"]


def xray_test_key(test: dict) -> str | None:
    """Extract a Jira Test key from the response shapes used by Xray versions."""
    for field in ("key", "testKey", "testIssueKey"):
        if test.get(field):
            return str(test[field]).strip().upper()

    for field in ("test", "issue"):
        nested = test.get(field)
        if isinstance(nested, dict):
            for key_field in ("key", "testKey", "testIssueKey"):
                if nested.get(key_field):
                    return str(nested[key_field]).strip().upper()
    return None


def extract_static_tables(storage_html: str) -> list[pd.DataFrame]:
    soup = BeautifulSoup(storage_html, "lxml")
    tables = []
    for table in soup.find_all("table"):
        try:
            df = pd.read_html(StringIO(str(table)))[0]
            tables.append(df)
        except ValueError:
            continue
    log.info(f"Found {len(tables)} static table(s) in storage format")
    return tables


JIRA_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9_]*-\d{5}\b", re.IGNORECASE)


def extract_jira_keys(value) -> list[str]:
    if pd.isna(value):
        return []
    return [key.upper() for key in JIRA_KEY_PATTERN.findall(str(value))]


def find_test_plan_keys(tables: list[pd.DataFrame]) -> list[str]:
    keys = []
    for table_number, df in enumerate(tables, start=1):
        test_plan_columns = [
            column for column in df.columns
            if str(column).strip().casefold() == "test plan"
        ]
        for column in test_plan_columns:
            for value in df[column]:
                keys.extend(extract_jira_keys(value))
        if test_plan_columns:
            log.info("Read Test Plan key(s) from static table %s", table_number)

    return list(dict.fromkeys(keys))


def fetch_test_executions_for_plans(jira: JiraClient, test_plan_keys: list[str]) -> pd.DataFrame:
    result_frames = []
    for test_plan_key in test_plan_keys:
        jql = (
            'issuetype = "Xray Test Execution" '
            f'AND "Test Plan" = "{test_plan_key}" '
            "ORDER BY key"
        )
        log.info("Fetching Test Execution issues for Test Plan %s", test_plan_key)
        df = jira.search(jql)
        if df.empty:
            log.warning("No Test Execution issues found for Test Plan %s", test_plan_key)
            continue
        df.insert(0, "test_plan_key", test_plan_key)
        result_frames.append(df)

    if not result_frames:
        return pd.DataFrame()
    return pd.concat(result_frames, ignore_index=True)


STATUS_COLUMN_MAP = {
    "PASS": "passed",
    "PASSED": "passed",
    "FAIL": "failed",
    "FAILED": "failed",
    "RETEST": "retest",
    "TODO": "untested",
    "UNTESTED": "untested",
    "UNTRIAGED": "untriaged",
    "BLOCKED": "blocked",
    "NOT APPLICABLE": "not_applicable",
    "NOT_APPLICABLE": "not_applicable",
    "N/A": "not_applicable",
}


def build_xray_execution_summary(jira: JiraClient, execution_key: str, cfg: Config) -> dict:
    """Aggregate matching WLAN/Functionality testcases into one execution summary."""
    tests = jira.get_xray_execution_tests(execution_key)
    test_keys = [key for test in tests if (key := xray_test_key(test))]
    testcase_metadata = jira.get_testcase_filter_fields(test_keys)
    filtered_tests = []

    log.info(
        "Test Execution %s: extracted %s testcase keys and retrieved Jira fields for %s",
        execution_key,
        len(test_keys),
        len(testcase_metadata),
    )

    if tests and not test_keys:
        log.warning(
            "No Jira testcase keys could be extracted for %s. Sample Xray record: %s",
            execution_key,
            json.dumps(tests[0], default=str)[:1000],
        )
    elif test_keys and not testcase_metadata:
        log.warning(
            "Jira returned no testcase field records for %s. Sample keys: %s",
            execution_key,
            ", ".join(test_keys[:5]),
        )

    for test in tests:
        test_key = xray_test_key(test)
        fields = testcase_metadata.get(test_key, {})
        if (
            jira_field_has_exact_value(fields.get("technology"), cfg.filter_technology)
            and (
                jira_field_has_exact_value(fields.get("test_area"), cfg.filter_test_area)
                or jira_field_has_exact_value(
                    fields.get("test_case_type"), cfg.filter_test_area
                )
            )
        ):
            filtered_tests.append(test)

    log.info(
        "Test Execution %s: retained %s of %s testcases after testcase-level "
        "Technology=%s and (Test Area=%s or Test Case Type=%s) filtering",
        execution_key,
        len(filtered_tests),
        len(tests),
        cfg.filter_technology,
        cfg.filter_test_area,
        cfg.filter_test_area,
    )
    counts = Counter()
    defects = set()

    for test in filtered_tests:
        status = str(field_value(test.get("status"), empty="Unknown") or "Unknown").strip()
        counts[status] += 1

        for defect in test.get("defects") or []:
            if isinstance(defect, dict):
                defect = defect.get("key") or defect.get("id")
            if defect:
                defects.add(str(defect).strip().upper())

    defects = jira.get_active_issue_keys(sorted(defects))

    row = {
        "key": execution_key,
        "total_testcases": len(filtered_tests),
        "passed": 0,
        "failed": 0,
        "retest": 0,
        "untested": 0,
        "untriaged": 0,
        "blocked": 0,
        "not_applicable": 0,
        "defects": ", ".join(sorted(defects)),
    }
    for status, count in counts.items():
        column = STATUS_COLUMN_MAP.get(status.upper())
        if column:
            row[column] += count
    return row


def build_execution_summary_worker(cfg: Config, execution_key: str) -> dict:
    """Build one execution summary using a session owned by this worker."""
    jira = make_jira_client(cfg)
    try:
        return build_xray_execution_summary(jira, execution_key, cfg)
    except XrayError as exc:
        log.error("Could not build Xray summary for %s: %s", execution_key, exc)
        return {
            "key": execution_key,
            "total_testcases": None,
            "passed": None,
            "failed": None,
            "retest": None,
            "untested": None,
            "untriaged": None,
            "blocked": None,
            "not_applicable": None,
            "defects": None,
        }
    finally:
        jira.session.close()


def add_xray_execution_summaries(
    cfg: Config,
    test_executions: pd.DataFrame,
) -> pd.DataFrame:
    """Add Xray summaries by processing Test Executions concurrently."""
    execution_keys = list(test_executions["key"].dropna().unique())
    if not execution_keys:
        return test_executions

    summaries = []
    max_workers = min(cfg.xray_workers, len(execution_keys))
    log.info(
        "Building %s Xray execution summaries with %s workers",
        len(execution_keys),
        max_workers,
    )

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(build_execution_summary_worker, cfg, execution_key): execution_key
            for execution_key in execution_keys
        }
        for future in as_completed(futures):
            execution_key = futures[future]
            summaries.append(future.result())
            log.info("Completed Xray summary for Test Execution %s", execution_key)

    return test_executions.merge(pd.DataFrame(summaries), on="key", how="left")


def main():
    cfg = Config()
    page_id = cfg.page_id
    output_dir = "confluence_output"
    if not page_id:
        log.error("No page id provided")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)

    confluence = ConfluenceClient(cfg)
    jira = make_jira_client(cfg)

    storage_html = confluence.get_page_storage_html(page_id)

    static_tables = extract_static_tables(storage_html)
    for i, df in enumerate(static_tables, start=1):
        out_path = os.path.join(output_dir, f"static_table_{i}.csv")
        df.to_csv(out_path, index=False)
        log.info(f"Wrote {out_path} ({len(df)} rows)")

    test_plan_keys = find_test_plan_keys(static_tables)
    if test_plan_keys:
        log.info("Found %s unique Test Plan key(s): %s", len(test_plan_keys), ", ".join(test_plan_keys))
        test_executions = fetch_test_executions_for_plans(jira, test_plan_keys)
        if not test_executions.empty:
            test_executions = add_xray_execution_summaries(cfg, test_executions)
            out_path = os.path.join(output_dir, "test_executions.csv")
            test_executions.to_csv(out_path, index=False)
            log.info("Wrote %s (%s rows)", out_path, len(test_executions))
    else:
        log.warning("No Jira Test Plan keys were found in a 'Test Plan' column")

    if not static_tables:
        log.warning("No Confluence tables were found")


if __name__ == "__main__":
    main()
