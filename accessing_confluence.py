from io import StringIO
import os
import sys
import argparse
import logging
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger("confluence_scraper")

class Config:
    def __init__(self):
        load_dotenv()
        self.confluence_base_url = self._require("CONFLUENCE_BASE_URL").rstrip("/")
        self.confluence_email = self._require("CONFLUENCE_EMAIL")
        self.confluence_token = self._require("CONFLUENCE_TOKEN")
        self.jira_base_url = os.getenv("JIRA_BASE_URL", self.confluence_base_url).rstrip("/")
        self.jira_email = os.getenv("JIRA_MAIL", self.confluence_email)
        self.jira_token = os.getenv("JIRA_TOKEN", self.confluence_token)
        self.page_id = os.getenv("CONFLUENCE_PAGE_ID")

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
        url = f"{self.cfg.confluence_base_url}/rest/api/content/{self.cfg.page_id}"
        params = {"expand":"body.storage,version,title"}
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        log.info(f"Fetched page {data.get("title")} (id={self.cfg.page_id}, version={data.get("version", {}).get("number")})")
        return data["body"]["storage"]["value"]

class JiraClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(cfg.jira_email, cfg.jira_token)
        self.session.headers.update({"Accept":"application/json"})

    def search(self, jql: str, fields=None, max_results: int=200) -> pd.DataFrame:
        fields = fields or ['key', 'summary', 'status', 'assignee', 'priority', 'updated']
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
                "updated": f.get("updated")
            })
        return pd.DataFrame(rows)

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

def extract_jira_macros(storage_html: str) -> list[dict]:
    soup = BeautifulSoup(storage_html, "lxml")
    macros = []
    for macro in soup.find_all("ac:structured-macro", attrs={"ac:name":"jira"}):
        params={}
        for param in macro.find_all("ac:parameter"):
            name = param.get("ac:name")
            param[name] = param.get_text(strip=True)
        macros.append(params)
    log.info(f"Found {len(macros)} Jira macro(s) in storage format")
    return macros

def build_jql_for_macro(params: dict) -> str | None:
    if params.get("jqlQuery"):
        return params["jqlQuery"]
    if params.get("key"):
        return f'issuekey = "{params["key"]}"'
    if params.get("filter"):
        filter_id = params["filter"].replace("filter-", "")
        return f"filter = {filter_id}"
    return None

def main():
    parser = argparse.ArgumentParser(description="Scrape a Confluence page including Jira tables")
    parser.add_argument("--page-id", help="Confluence page id")
    parser.add_argument("-out-dir", default="./confluence_output", help="Directory to write csv's into")
    args = parser.parse_args()

    cfg = Config()
    page_id = args.page_id or cfg.page_id
    if not page_id:
        log.error("No page id provided")
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)

    confluence = ConfluenceClient(cfg)
    jira = JiraClient(cfg)

    storage_html = confluence.get_page_storage_html(page_id)

    static_tables = extract_static_tables(storage_html)
    for i, df in enumerate(static_tables, start=1):
        out_path = os.path.join(args.out_dir, f"static_table_{i}.csv")
        df.to_csv(out_path, index=False)
        log.info(f"Wrote {out_path} ({len(df)} rows)")

    jira_macros = extract_jira_macros(storage_html)
    for i, params in enumerate(jira_macros, start=1):
        jql = build_jql_for_macro(params)
        if not jql:
            log.warning(f"Jira macro {i} has no resolvable filter {params}")
            continue

        log.info(f"Resolving macro {i} using JQL")
        df = jira.search(jql)
        if df.empty:
            log.warning(f"Jira macro {i} returned no rows")
            continue

        out_path = os.path.join(args.out_dir, f"jira_macro_table_{i}.csv")
        df.to_csv(out_path, index=False)
        log.info(f"Wrote {out_path} ({len(df)} rows)")

        if not static_tables and not jira_macros:
            log.warning("No Jira macros or Confluence tables present")

if __name__ == "__main__":
    main()