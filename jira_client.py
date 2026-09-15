"""Shared Jira client and field helpers used by scraper and mail pipeline."""

from __future__ import annotations

import json
import logging
import re
import time

import pandas as pd
import requests

log = logging.getLogger("jira_client")

TESTCASE_TECHNOLOGY_FIELD = "customfield_22640"
TESTCASE_TEST_AREA_FIELD = "customfield_33578"
TESTCASE_TYPE_FIELD = "customfield_33354"


class XrayError(RuntimeError):
    """Raised when an Xray page cannot be retrieved completely."""


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


def field_value(value, empty=""):
    """Convert a Jira/Xray field into a single cell-friendly value."""
    if value is None:
        return empty
    if isinstance(value, dict):
        for key in ("name", "value", "key", "displayName"):
            if value.get(key) is not None:
                return value[key]
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        parts = [str(field_value(item, empty="")) for item in value]
        parts = [part for part in parts if part]
        return ", ".join(parts) if parts else empty
    return value


def jira_field_value(value):
    """Convert a Jira field into a CSV cell value, or None when empty."""
    values = extract_jira_option_values(value)
    if not values:
        return None
    return ", ".join(str(item) for item in values)


def jira_field_has_exact_value(value, expected: str) -> bool:
    """Check whether a Jira field contains an exact option."""
    expected = expected.strip().casefold()
    return any(
        option.strip().casefold() == expected
        for option_value in extract_jira_option_values(value)
        for option in re.split(r"[,;]", str(option_value))
    )


class JiraClient:
    def __init__(self, base_url: str, token: str, xray_cfg=None):
        self.base_url = base_url.rstrip("/")
        self.xray_cfg = xray_cfg
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        })

    def search(self, jql: str, fields=None, max_results: int = 200) -> pd.DataFrame:
        fields = fields or [
            "key",
            "summary",
            "customfield_34442",  # Compiler
        ]
        url = f"{self.base_url}/rest/api/2/search"
        all_issues = []
        start_at = 0

        while True:
            payload = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results,
                "fields": fields,
            }

            resp = self.session.post(url, json=payload, timeout=30)
            if not resp.ok:
                log.warning(
                    "Jira search failed %s: %s",
                    resp.status_code,
                    resp.text[:300],
                )
                return pd.DataFrame()

            data = resp.json()
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
                "compiler": jira_field_value(f.get("customfield_34442")),
            })
        return pd.DataFrame(rows)

    def get_testcase_filter_fields(self, test_keys: list[str]) -> dict[str, dict]:
        """Return raw Technology and Test Area values for the requested Test issues."""
        metadata = {}
        unique_keys = list(dict.fromkeys(key for key in test_keys if key))

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
            url = f"{self.base_url}/rest/api/2/search"
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
            url = f"{self.base_url}/rest/api/2/search"
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
                        str(field_value(issue_fields.get(regression_field), empty=""))
                        if regression_field
                        else ""
                    ),
                })

        return rows

    def _xray_get(self, path: str, params: dict | None = None):
        """GET an Xray Server/Data Center REST resource using the Jira PAT."""
        if self.xray_cfg is None:
            raise XrayError("Xray settings were not configured on JiraClient")

        url = f"{self.base_url}/rest/raven/latest/api/{path.lstrip('/')}"
        retryable_statuses = {429, 500, 502, 503, 504}

        for attempt in range(self.xray_cfg.xray_retry_count + 1):
            try:
                resp = self.session.get(
                    url,
                    params=params,
                    timeout=(10, self.xray_cfg.xray_timeout_seconds),
                )
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as exc:
                if attempt >= self.xray_cfg.xray_retry_count:
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

            if resp.status_code in retryable_statuses and attempt < self.xray_cfg.xray_retry_count:
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

    def get_xray_execution_tests(self, execution_key: str) -> list[dict]:
        if self.xray_cfg is None:
            raise XrayError("Xray settings were not configured on JiraClient")

        page_size = self.xray_cfg.xray_page_size
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
