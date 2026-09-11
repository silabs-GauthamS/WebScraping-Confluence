import csv
import re
from collections import defaultdict
from datetime import date
from html import escape
from pathlib import Path

import win32com.client


SCRIPT_DIR = Path(__file__).resolve().parent

OUTPUT_DIR = SCRIPT_DIR / "confluence_output"
if not OUTPUT_DIR.exists():
    OUTPUT_DIR = SCRIPT_DIR / "confluence_ouput"

RECIPIENTS = [
    "VenkataRamanaKumar.Rajanala@silabs.com",
    "Gautham.Sharma@silabs.com",
]
CC_RECIPIENTS = []

REPORT_PERIOD = "31st Aug to 4th Sep"
BUILD_NAME = "WC-4.1.2-IFC1FC/IFC2FC"
BUILD_DETAILS = {
    "FW": "SiWG917-B.2.16.5.2.0.2, SiWG917-B.2.16.5.2.0.4",
    "Encryption": "Encrypted",
    "Card used": "SOC-4338A A14 and NCP-4346A A13",
    "Monolithic": "Received on 19th Aug (sisdk-2026.6/2843/), 2851",
}
XRAY_RAIL_LINK = "https://confluence.silabs.com/spaces/EN/pages/887398550/WC_4.1.2"


CELL_STYLE = "border:1px solid #c9d1d9;padding:7px 8px;vertical-align:top;"
HEADER_STYLE = (
    CELL_STYLE
    + "background:#d9eaf7;color:#172b4d;font-weight:bold;text-align:center;"
)


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


def execution_parts(summary, compiler):
    """Extract the displayed test-plan name and board from an execution summary."""
    text = (summary or "").strip()
    tail = text.split("-917-", 1)[-1] if "-917-" in text else text
    compiler_tokens = [
        "GCC_LTO_PSRAM",
        "LLVM_LTO_PSRAM",
        "GCC_LTO",
        "LLVM_LTO",
        "GCC_PSRAM",
        "LLVM_PSRAM",
        "GCC",
        "LLVM",
    ]
    match = re.match(
        rf"(?P<test_plan>.+?)-(?:{'|'.join(compiler_tokens)})-(?P<board>.+)$",
        tail,
        re.IGNORECASE,
    )
    if not match:
        return text or "-", "-"

    test_plan = match.group("test_plan").strip()
    if test_plan == "smoke_tests":
        test_plan = "SANITY_TESTPLAN"
    return test_plan, match.group("board").strip()


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
    technology = (row.get("technology") or "").casefold()
    test_area = (row.get("test_area") or "").strip().casefold()
    return "wlan" in technology or test_area == "functionality"


def execution_table(rows, metadata, include_board):
    headings = ["Test_plan"]
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

    body_rows = []
    for row in rows:
        test_plan, board = execution_parts(row.get("summary"), row.get("compiler"))
        values = [safe(test_plan)]
        if include_board:
            values.append(safe(board))
        values += [
            safe(metadata.get(row.get("test_plan_key"))),
            safe(row.get("compiler")),
            str(number(row, "total_testcases")),
            str(coverage(row)),
            str(number(row, "passed")),
            str(number(row, "failed")),
            str(number(row, "retest")),
            str(number(row, "untested")),
            str(number(row, "untriaged")),
            str(number(row, "blocked")),
            str(number(row, "not_applicable")),
            defects_html(row.get("defects")),
        ]
        body_rows.append(
            "<tr>" + "".join(f'<td style="{CELL_STYLE}">{value}</td>' for value in values) + "</tr>"
        )

    header = "".join(f'<th style="{HEADER_STYLE}">{heading}</th>' for heading in headings)
    if not body_rows:
        body_rows.append(
            f'<tr><td colspan="{len(headings)}" style="{CELL_STYLE}">No execution data found.</td></tr>'
        )
    return (
        '<table role="presentation" style="border-collapse:collapse;width:100%;font-size:12px;">'
        f"<thead><tr>{header}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>"
    )


def jira_table(execution_rows):
    """Build one Jira row per defect key referenced by the execution CSV."""
    compilers_by_defect = defaultdict(set)
    for row in execution_rows:
        for key in (row.get("defects") or "").split(","):
            key = key.strip()
            if key and row.get("compiler"):
                compilers_by_defect[key].add(row["compiler"].strip())

    # Missing values: test_executions.csv only contains defect keys. Defect
    # Summary, Status, Priority and IsRegressionIssue are not present in any
    # supplied CSV, so those cells remain "-" until Jira defect data is exported.
    headings = ["Issue key", "Compiler", "Summary", "Status", "Priority", "IsRegressionIssue"]
    body_rows = []
    for key in sorted(compilers_by_defect):
        link = f'<a href="https://jira.silabs.com/browse/{escape(key)}">{escape(key)}</a>'
        values = [link, safe(", ".join(sorted(compilers_by_defect[key]))), "-", "-", "-", "-"]
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


def build_report_html(soc_rows, ncp_rows, soc_metadata, ncp_metadata):
    detail_rows = "".join(
        f'<tr><th style="{CELL_STYLE}background:#f3f6f9;text-align:left;">{escape(label)}</th>'
        f'<td style="{CELL_STYLE}">{escape(value)}</td></tr>'
        for label, value in BUILD_DETAILS.items()
    )
    all_rows = soc_rows + ncp_rows

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:20px;background:#f4f6f8;color:#172b4d;font-family:Arial,sans-serif;font-size:14px;">
  <div style="max-width:1400px;margin:auto;border:1px solid #dfe1e6;padding:24px;">
    <p>Hi Everyone,</p>
    <p>Please find the below complete Execution status of <strong>{escape(BUILD_NAME)}</strong>
       build [{escape(REPORT_PERIOD)}].</p>

    <h2 style="font-size:17px;color:#0052cc;margin:24px 0 10px;">Build Details:</h2>
    <table role="presentation" style="border-collapse:collapse;min-width:560px;">{detail_rows}</table>

    <h2 style="font-size:17px;color:#0052cc;margin:28px 0 10px;">SoC Execution Update:</h2>
    {execution_table(soc_rows, soc_metadata, include_board=True)}

    <h2 style="font-size:17px;color:#0052cc;margin:28px 0 10px;">NCP Execution Update:</h2>
    {execution_table(ncp_rows, ncp_metadata, include_board=False)}

    <h2 style="font-size:17px;color:#0052cc;margin:28px 0 10px;">Jira Details:</h2>
    {jira_table(all_rows)}

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

    soc_rows = [row for row in execution_rows if row.get("test_plan_key") in soc_metadata]
    ncp_rows = [row for row in execution_rows if row.get("test_plan_key") in ncp_metadata]

    mail = win32com.client.Dispatch("Outlook.Application").CreateItem(0)
    mail.To = "; ".join(RECIPIENTS)
    mail.CC = "; ".join(CC_RECIPIENTS)
    mail.Subject = f"Weekly Test Report - {BUILD_NAME} - {date.today():%d %b %Y}"
    mail.HTMLBody = build_report_html(soc_rows, ncp_rows, soc_metadata, ncp_metadata)
    mail.Display()


if __name__ == "__main__":
    main()
