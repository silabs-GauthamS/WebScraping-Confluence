import csv
import os
import re
import time
from dotenv import load_dotenv
from datetime import date
from html import escape
from pathlib import Path

import win32com.client

load_dotenv()
SCRIPT_DIR = Path(__file__).resolve().parent

OUTPUT_DIR = SCRIPT_DIR / "confluence_output"

RECIPIENTS = [
    # "VenkataRamanaKumar.Rajanala@silabs.com",
    "Gautham.Sharma@silabs.com",
    # "adithyaa.a@silabs.com",
]
CC_RECIPIENTS = []

PAGE_ID = os.getenv("CONFLUENCE_PAGE_ID")
TITLE = os.getenv("CONFLUENCE_PAGE_TITLE")
XRAY_RAIL_LINK = f"https://confluence.silabs.com/spaces/EN/pages/{PAGE_ID}/{TITLE}"


CELL_STYLE = "border:1px solid #c9d1d9;padding:7px 8px;vertical-align:top;"
HIGHLIGHT_CELL_STYLE = CELL_STYLE + "background:#d0d0d0;"
HEADER_STYLE = (
    CELL_STYLE
    + "background:#d9eaf7;color:#172b4d;font-weight:bold;text-align:center;"
)
GROUP_CELL_STYLE = (
    CELL_STYLE
    + "text-align:center;vertical-align:middle;font-weight:bold;width:28px;line-height:1.15;"
)
HIGHLIGHT_MILESTONES = {"FC", "IFC3"}
MILESTONE_ORDER = {"IFC1": 0, "IFC2": 1, "IFC3": 2, "FC": 3}
TEST_PLAN_GROUPS = (
    ("sample_app", "OPN Validation"),
    ("iotreq", "Consequential Listing"),
)
GROUP_ORDER = {group: index for index, (_, group) in enumerate(TEST_PLAN_GROUPS)}


def milestone_sort_key(milestone):
    return (MILESTONE_ORDER.get(milestone, 99), milestone.casefold())


def row_group(test_plan):
    """Group key for merging. Unnamed plans group by their own plan name."""
    name = (test_plan or "").strip()
    folded = name.casefold()
    for prefix, group in TEST_PLAN_GROUPS:
        if folded.startswith(prefix):
            return group
    return name


def group_label(group):
    """Only named groups print a label; other merged cells stay empty."""
    return group if group in GROUP_ORDER else ""


def group_sort_key(group):
    return (GROUP_ORDER.get(group, 99), group.casefold())


def vertical_text(text):
    parts = []
    for character in text:
        if character == " ":
            parts.append("&nbsp;")
        else:
            parts.append(escape(character))
    return "<br>".join(parts)


def group_rowspans(items):
    spans = []
    index = 0
    while index < len(items):
        end = index + 1
        while end < len(items) and items[end]["group"] == items[index]["group"]:
            end += 1
        spans.append(end - index)
        spans.extend([0] * (end - index - 1))
        index = end
    return spans


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        return list(csv.DictReader(csv_file))


def find_csv(prefix):
    matches = sorted(OUTPUT_DIR.glob(f"{prefix}*.csv"))
    if not matches:
        raise FileNotFoundError(f"No file matching {prefix}*.csv in {OUTPUT_DIR}")
    return matches[0]


def safe(value, default="-"):
    if value is None or str(value).strip() == "":
        return default
    return escape(str(value).strip())


def number(row, field):
    try:
        return int(float(row.get(field) or 0))
    except (TypeError, ValueError):
        return 0


def plan_metadata(static_rows):
    """Return {test_plan_key: milestone} from a Confluence static table."""
    metadata = {}
    for row in static_rows:
        plan_match = re.search(r"SW_SQA_TE-\d+", row.get("Test Plan", ""))
        milestone_match = re.search(r"-(IFC\d+|FC)$", row.get("Release", "").strip())
        if plan_match:
            metadata[plan_match.group()] = milestone_match.group(1) if milestone_match else "-"
    return metadata


def execution_parts(summary):
    """Extract the test-plan name and board from a standardized execution summary."""
    text = (summary or "").strip()
    tail = text.split("-917-", 1)[-1] if "-917-" in text else text

    parts = tail.rsplit("-", 2)
    if len(parts) != 3:
        return text or "-", "-"

    test_plan, _, board = parts

    if test_plan.casefold() == "smoke_tests":
        test_plan = "SANITY_TESTPLAN"

    return test_plan.strip(), board.strip()


def defects_html(defects):
    keys = [key.strip() for key in (defects or "").split(",") if key.strip()]
    return "<br>".join(
        f'<a href="https://jira.silabs.com/browse/{escape(key)}">{escape(key)}</a>'
        for key in keys
    ) or "-"


def coverage(row):
    """Coverage is the number of executed test cases, matching the sample mail."""
    return sum(number(row, field) for field in ("passed", "failed", "retest", "blocked"))


def is_visible_execution(row):
    """Show executions containing at least one testcase retained by the scraper."""
    return number(row, "total_testcases") > 0


def cell_style(milestone):
    if milestone in HIGHLIGHT_MILESTONES:
        return HIGHLIGHT_CELL_STYLE
    return CELL_STYLE


def prepare_execution_rows(rows, metadata, include_board):
    """Normalize rows. Sort: Group, Milestone, Test plan, Board."""
    prepared = []
    for row in rows:
        test_plan, board = execution_parts(row.get("summary"))
        milestone = metadata.get(row.get("test_plan_key"), "-")
        prepared.append(
            {
                "group": row_group(test_plan),
                "test_plan": test_plan,
                "board": board,
                "milestone": milestone,
                "compiler": row.get("compiler"),
                "total": number(row, "total_testcases"),
                "coverage": coverage(row),
                "passed": number(row, "passed"),
                "failed": number(row, "failed"),
                "retest": number(row, "retest"),
                "untested": number(row, "untested"),
                "untriaged": number(row, "untriaged"),
                "blocked": number(row, "blocked"),
                "not_applicable": number(row, "not_applicable"),
                "defects": row.get("defects"),
            }
        )
    if include_board:
        prepared.sort(
            key=lambda item: (
                group_sort_key(item["group"]),
                milestone_sort_key(item["milestone"]),
                item["test_plan"].casefold(),
                item["board"].casefold(),
            )
        )
    else:
        prepared.sort(
            key=lambda item: (
                group_sort_key(item["group"]),
                milestone_sort_key(item["milestone"]),
                item["test_plan"].casefold(),
            )
        )
    return prepared


def execution_table(rows, metadata, include_board):
    headings = ["Group", "Test plan"]
    if include_board:
        headings.append("Board Details")
    headings += [
        "Milestone",
        "Compiler",
        "Total",
        "Coverage",
        "Passed",
        "Failed",
        "Retest",
        "Untested",
        "Untriaged",
        "Blocked",
        "Not Applicable",
        "Defects",
    ]

    items = prepare_execution_rows(rows, metadata, include_board)
    body_rows = []
    for item, rowspan in zip(items, group_rowspans(items)):
        style = cell_style(item["milestone"])
        cells = []
        if rowspan:
            cells.append(
                f'<td rowspan="{rowspan}" style="{GROUP_CELL_STYLE}">'
                f'{vertical_text(group_label(item["group"]))}</td>'
            )
        values = [safe(item["test_plan"])]
        if include_board:
            values.append(safe(item["board"]))
        values += [
            safe(item["milestone"]),
            safe(item["compiler"]),
            str(item["total"]),
            str(item["coverage"]),
            str(item["passed"]),
            str(item["failed"]),
            str(item["retest"]),
            str(item["untested"]),
            str(item["untriaged"]),
            str(item["blocked"]),
            str(item["not_applicable"]),
            defects_html(item["defects"]),
        ]
        cells.extend(f'<td style="{style}">{value}</td>' for value in values)
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    header = "".join(f'<th style="{HEADER_STYLE}">{heading}</th>' for heading in headings)
    if not body_rows:
        body_rows.append(
            f'<tr><td colspan="{len(headings)}" style="{CELL_STYLE}">No execution data found.</td></tr>'
        )
    return (
        '<table role="presentation" style="border-collapse:collapse;width:100%;font-size:12px;">'
        f"<thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
    )


def milestone_execution_tables(rows, metadata, include_board):
    """Build one execution table for each milestone represented in the rows."""
    grouped_rows = {}
    for row in rows:
        milestone = metadata.get(row.get("test_plan_key"), "-")
        grouped_rows.setdefault(milestone, []).append(row)

    if not grouped_rows:
        return execution_table([], metadata, include_board)

    sections = []
    for milestone in sorted(grouped_rows, key=milestone_sort_key):
        sections.append(
            f'<h3 style="font-size:15px;color:#172b4d;margin:20px 0 8px;">'
            f'{safe(milestone)} Milestone:</h3>'
            f'{execution_table(grouped_rows[milestone], metadata, include_board)}'
        )
    return "".join(sections)


def jira_table(jira_rows):
    """Build the Jira table from confluence_output/jira_details.csv."""
    headings = ["Issue key", "Compiler", "Summary", "Status", "Priority", "IsRegressionIssue"]
    body_rows = []
    for row in jira_rows:
        key = (row.get("issue_key") or "").strip()
        if not key:
            continue
        link = f'<a href="https://jira.silabs.com/browse/{escape(key)}">{escape(key)}</a>'
        values = [
            link,
            safe(row.get("compiler")),
            safe(row.get("summary")),
            safe(row.get("status")),
            safe(row.get("priority")),
            safe(row.get("is_regression_issue")),
        ]
        body_rows.append(
            "<tr>" + "".join(f'<td style="{CELL_STYLE}">{value}</td>' for value in values) + "</tr>"
        )

    header = "".join(f'<th style="{HEADER_STYLE}">{heading}</th>' for heading in headings)
    if not body_rows:
        body_rows.append(
            f'<tr><td colspan="{len(headings)}" style="{CELL_STYLE}">No linked defects found.</td></tr>'
        )
    return (
        '<table role="presentation" style="border-collapse:collapse;width:100%;font-size:12px;">'
        f"<thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
    )


def build_report_html(soc_rows, ncp_rows, jira_rows, soc_metadata, ncp_metadata):
    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:20px;background:#f4f6f8;color:#172b4d;font-family:Arial,sans-serif;font-size:14px;">
  <div style="max-width:1400px;margin:auto;border:1px solid #dfe1e6;padding:24px;">
    <p>Hi Everyone,</p>
    <p>Please find the below complete Execution status of <strong>{TITLE}</strong>.</p>

    <h2 style="font-size:17px;color:#0052cc;margin:28px 0 10px;">SoC Execution Update:</h2>
    {milestone_execution_tables(soc_rows, soc_metadata, include_board=True)}

    <h2 style="font-size:17px;color:#0052cc;margin:28px 0 10px;">NCP Execution Update:</h2>
    {milestone_execution_tables(ncp_rows, ncp_metadata, include_board=False)}

    <h2 style="font-size:17px;color:#0052cc;margin:28px 0 10px;">Jira Details:</h2>
    {jira_table(jira_rows)}

    <h2 style="font-size:17px;color:#0052cc;margin:28px 0 10px;">X-Ray Rail Link:</h2>
    <p><a href="{escape(XRAY_RAIL_LINK)}"><strong>{escape(XRAY_RAIL_LINK)}</strong></a></p>
    <p><b>Thank You,<br>Gautham Sharma</b></p>
  </div>
</body>
</html>"""


def main():
    soc_metadata = plan_metadata(read_csv(find_csv("static_table_1")))
    ncp_metadata = plan_metadata(read_csv(find_csv("static_table_2")))
    execution_rows = [
        row
        for row in read_csv(OUTPUT_DIR / "test_executions.csv")
        if is_visible_execution(row)
    ]
    jira_rows = read_csv(OUTPUT_DIR / "jira_details.csv")

    soc_rows = [row for row in execution_rows if row.get("test_plan_key") in soc_metadata]
    ncp_rows = [row for row in execution_rows if row.get("test_plan_key") in ncp_metadata]

    outlook = win32com.client.Dispatch("Outlook.Application")
    namespace = outlook.GetNamespace("MAPI")
    
    mail = outlook.CreateItem(0)
    mail.To = "; ".join(RECIPIENTS)
    mail.CC = "; ".join(CC_RECIPIENTS)
    mail.Subject = f"Weekly Test Report - {TITLE} - {date.today():%d %b %Y}"
    mail.HTMLBody = build_report_html(
        soc_rows, ncp_rows, jira_rows, soc_metadata, ncp_metadata
    )
    mail.Send()
    namespace.SendAndReceive(False)
    time.sleep(15)


if __name__ == "__main__":
    main()
