from io import StringIO
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import sys
import logging
import re
import time
import requests
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("confluence_scraper")

TESTCASE_TECHNOLOGY_FIELD = "customfield_22640"
TESTCASE_TEST_AREA_FIELD = "customfield_33578"
TESTCASE_TYPE_FIELD = "customfield_33354"

class Config:
    def __init__(self):
        load_dotenv()
        self.confluence_base_url = self._require("CONFLUENCE_BASE_URL").rstrip("/")
        self.confluence_token = self._require("CONFLUENCE_TOKEN")
        self.jira_base_url = os.getenv("JIRA_BASE_URL", self.confluence_base_url).rstrip("/")
        self.jira_token = os.getenv("JIRA_TOKEN", self.confluence_token)
        self.page_id = os.getenv("CONFLUENCE_PAGE_ID")
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
    def _require(key:str) -> str:
        val = os.getenv(key)
        if not val:
            log.error(f"Missing required env val: {key}")
            sys.exit(1)
        return val

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
        params = {"expand":"body.storage,version,title"}
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


class XrayError(RuntimeError):
    """Raised when an Xray page cannot be retrieved completely."""


class JiraClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {cfg.jira_token}",
            "Accept": "application/json",
        })

    def search(self, jql: str, fields=None, max_results: int=200) -> pd.DataFrame:
        fields = fields or [
            "key", "summary", "status", "assignee", "priority", "updated",
            "issuetype", "duedate", 
            "customfield_34841",  # Test Area
            "customfield_34940",  # Technology
            "customfield_34442",  # Compiler
        ]
        url = f"{self.cfg.jira_base_url}/rest/api/2/search"
        all_issues = []
        start_at = 0

        while True:
            payload = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results,
                "fields": fields
            }

            resp = self.session.post(url, json=payload, timeout=30)
            if not resp.ok:
                log.warning(f"Jira search failed {resp.status_code}: {resp.text[:300]}")
                return pd.DataFrame()

            data=resp.json()
            all_issues.extend(data.get("issues", []))
            total = data.get("total", 0)
            start_at += len(data.get("issues", []))
            if start_at >= total or not data.get("issues"):
                break

        rows = []
        for issue in all_issues:
            f = issue.get("fields", {})
            rows.append({
                "key": issue.get("key"),
                "summary": f.get("summary"),
                "status": (f.get("status") or {}).get("name"),
                "assignee": (f.get("assignee") or {}).get("displayName"),
                "priority": (f.get("priority") or {}).get("name"),
                "updated": f.get("updated"),
                "issue_type": (f.get("issuetype") or {}).get("name"),
                "due_date": f.get("duedate"),
                "test_area": jira_field_value(f.get("customfield_34841")),
                "technology": jira_field_value(f.get("customfield_34940")),
                "compiler": jira_field_value(f.get("customfield_34442")),
            })
        return pd.DataFrame(rows)

    def get_testcase_filter_fields(self, test_keys: list[str]) -> dict[str, dict]:
        """Return raw Technology and Test Area values for the requested Test issues."""
        metadata = {}
        unique_keys = list(dict.fromkeys(key for key in test_keys if key))

        # Keep each JQL request reasonably small while still using Jira pagination.
        for offset in range(0, len(unique_keys), 100):
            key_batch = unique_keys[offset:offset + 100]
            quoted_keys = ", ".join(f'"{key}"' for key in key_batch)
            payload = {
                "jql": f"key in ({quoted_keys})",
                "startAt": 0,
                "maxResults": len(key_batch),
                "fields": [
                    TESTCASE_TEST_AREA_FIELD,
                    TESTCASE_TECHNOLOGY_FIELD,
                    TESTCASE_TYPE_FIELD,
                ],
            }
            url = f"{self.cfg.jira_base_url}/rest/api/2/search"
            resp = self.session.post(url, json=payload, timeout=30)
            if not resp.ok:
                raise XrayError(
                    "Could not retrieve testcase Technology/Test Area fields: "
                    f"HTTP {resp.status_code}: {resp.text[:300]}"
                )

            for issue in resp.json().get("issues", []):
                fields = issue.get("fields", {})
                issue_key = str(issue.get("key") or "").strip().upper()
                metadata[issue_key] = {
                    "test_area": fields.get(TESTCASE_TEST_AREA_FIELD),
                    "technology": fields.get(TESTCASE_TECHNOLOGY_FIELD),
                    "test_case_type": fields.get(TESTCASE_TYPE_FIELD),
                }

        return metadata

    def get_active_issue_keys(self, issue_keys: list[str]) -> set[str]:
        """Return issues whose Jira status category is not Done."""
        active_keys = set()
        unique_keys = list(dict.fromkeys(key for key in issue_keys if key))

        for offset in range(0, len(unique_keys), 100):
            key_batch = unique_keys[offset:offset + 100]
            quoted_keys = ", ".join(f'"{key}"' for key in key_batch)
            payload = {
                "jql": (
                    f"key in ({quoted_keys}) "
                    'AND statusCategory != "Done"'
                ),
                "startAt": 0,
                "maxResults": len(key_batch),
                "fields": ["key"],
            }
            url = f"{self.cfg.jira_base_url}/rest/api/2/search"
            resp = self.session.post(url, json=payload, timeout=30)
            if not resp.ok:
                raise XrayError(
                    "Could not retrieve defect statuses: "
                    f"HTTP {resp.status_code}: {resp.text[:300]}"
                )

            active_keys.update(
                str(issue.get("key") or "").strip().upper()
                for issue in resp.json().get("issues", [])
                if issue.get("key")
            )

        return active_keys

    def _xray_get(self, path: str, params: dict | None = None):
        """GET an Xray Server/Data Center REST resource using the Jira PAT."""
        url = f"{self.cfg.jira_base_url}/rest/raven/latest/api/{path.lstrip('/')}"
        retryable_statuses = {429, 500, 502, 503, 504}

        for attempt in range(self.cfg.xray_retry_count + 1):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    timeout=(10, self.cfg.xray_timeout_seconds),
                )
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
                if attempt >= self.cfg.xray_retry_count:
                    raise XrayError(
                        f"Xray request failed after {attempt + 1} attempts: {path}: {exc}"
                    ) from exc
                delay = 2 ** attempt
                log.warning("Xray request error for %s; retrying in %ss", path, delay)
                time.sleep(delay)
                continue

            if resp.ok:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise XrayError(f"Xray returned non-JSON data for {path}") from exc

            if resp.status_code in retryable_statuses and attempt < self.cfg.xray_retry_count:
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = min(30, max(1, int(retry_after)))
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                log.warning(
                    "Xray returned HTTP %s for %s; retrying in %ss",
                    resp.status_code,
                    path,
                    delay,
                )
                time.sleep(delay)
                continue

            raise XrayError(
                f"Xray request failed HTTP {resp.status_code} for {path}: {resp.text[:300]}"
            )

    @staticmethod
    def _items_from_xray_response(data) -> list[dict]:
        """Accept the list/objects used by different Xray API versions."""
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("tests", "results", "values", "items"):
                if isinstance(data.get(key), list):
                    return [item for item in data[key] if isinstance(item, dict)]
        return []

    @staticmethod
    def _value(value):
        """Make an Xray/Jira value suitable for one CSV cell."""
        if value is None:
            return None
        if isinstance(value, dict):
            for key in ("name", "value", "key", "displayName"):
                if value.get(key) is not None:
                    return value[key]
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, list):
            return ", ".join(str(JiraClient._value(item)) for item in value)
        return value

    def get_xray_execution_tests(self, execution_key: str) -> list[dict]:
        page_size = self.cfg.xray_page_size
        page = 1
        tests = []
        seen_ids = set()

        while True:
            data = self._xray_get(
                f"testexec/{execution_key}/test",
                params={"detailed": "true", "page": page, "limit": page_size},
            )
            page_items = self._items_from_xray_response(data)
            for item in page_items:
                identity = item.get("id") or (item.get("key"), item.get("rank"))
                if identity not in seen_ids:
                    seen_ids.add(identity)
                    tests.append(item)

            total = data.get("total") if isinstance(data, dict) else None
            try:
                total = int(total) if total is not None else None
            except (TypeError, ValueError):
                total = None

            log.info(
                "Fetched Xray page %s for %s: %s records (%s collected%s)",
                page,
                execution_key,
                len(page_items),
                len(tests),
                f"/{total}" if total is not None else "",
            )

            if not page_items or len(page_items) < page_size:
                break
            if total is not None and len(tests) >= total:
                break
            page += 1

        if not tests:
            log.warning("Xray returned no test cases for Test Execution %s", execution_key)
        return tests

def extract_jira_option_values(value) -> list:
    """Extract individual option values from a Jira field."""
    if value is None:
        return []

    if isinstance(value, list):
        values = []
        for item in value:
            values.extend(extract_jira_option_values(item))
        return values

    if isinstance(value, dict):
        option = (
            value.get("value")
            or value.get("name")
            or value.get("displayName")
            or value.get("key")
        )
        return extract_jira_option_values(option)

    return [value]


def jira_field_value(value):
    """Convert a Jira field into a value suitable for a CSV cell."""
    values = extract_jira_option_values(value)

    if not values:
        return None

    return ", ".join(str(item) for item in values)


def jira_field_has_exact_value(value, expected: str) -> bool:
    """Check whether a Jira field contains an exact option."""
    expected = expected.strip().casefold()

    return any(
        option.strip().casefold() == expected
        for value in extract_jira_option_values(value)
        for option in re.split(r"[,;]", str(value))
    )


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

FILTER_TECHNOLOGY = os.getenv("TECHNOLOGY", "WLAN")
FILTER_TEST_AREA = os.getenv("TEST_AREA","FUNCTIONAL")


def build_xray_execution_summary(jira: JiraClient, execution_key: str) -> dict:
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
            jira_field_has_exact_value(fields.get("technology"), FILTER_TECHNOLOGY)
            and (
                jira_field_has_exact_value(fields.get("test_area"), FILTER_TEST_AREA)
                or jira_field_has_exact_value(
                    fields.get("test_case_type"), FILTER_TEST_AREA
                )
            )
        ):
            filtered_tests.append(test)

    # if tests and not filtered_tests and testcase_metadata:
    #     observed_values = [
    #         {
    #             "key": key,
    #             "technology": JiraClient._value(fields.get("technology")),
    #             "test_area": JiraClient._value(fields.get("test_area")),
    #             "test_case_type": JiraClient._value(fields.get("test_case_type")),
    #         }
    #         for key, fields in list(testcase_metadata.items())[:10]
    #     ]
    #     log.warning(
    #         "No testcase matched the filter for %s. Sample Jira field values: %s",
    #         execution_key,
    #         json.dumps(observed_values, default=str),
    #     )

    log.info(
        "Test Execution %s: retained %s of %s testcases after testcase-level "
        "Technology=%s and (Test Area=%s or Test Case Type=%s) filtering",
        execution_key,
        len(filtered_tests),
        len(tests),
        FILTER_TECHNOLOGY,
        FILTER_TEST_AREA,
        FILTER_TEST_AREA,
    )
    counts = Counter()
    defects = set()

    for test in filtered_tests:
        status = str(JiraClient._value(test.get("status")) or "Unknown").strip()
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
        "defect_count": len(defects),
        "defects": ", ".join(sorted(defects)),
        "xray_status_counts": json.dumps(dict(sorted(counts.items()))),
    }
    for status, count in counts.items():
        column = STATUS_COLUMN_MAP.get(status.upper())
        if column:
            row[column] += count
    return row


def build_execution_summary_worker(cfg: Config, execution_key: str) -> dict:
    """Build one execution summary using a session owned by this worker."""
    jira = JiraClient(cfg)
    try:
        summary = build_xray_execution_summary(jira, execution_key)
        summary["xray_fetch_error"] = None
        return summary
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
            "defect_count": None,
            "defects": None,
            "xray_status_counts": None,
            "xray_fetch_error": str(exc),
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
    jira = JiraClient(cfg)

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
