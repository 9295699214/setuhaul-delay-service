"""Run every query in data_issues.sql and write reports/data_issues_report.md.

    python -m analysis.run_issues            # the supplied snapshot (data/setuhaul_freight_operations.db)
    python -m analysis.run_issues path.db    # any SQLite file with the same tables
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.db import PRISTINE_DB, connect  # noqa: E402

SQL_FILE = ROOT / "analysis" / "data_issues.sql"


def load_queries(path=SQL_FILE) -> list[dict]:
    text = path.read_text()
    blocks = re.split(r"\n(?=-- \d+\. )", text)
    out = []
    for b in blocks:
        m = re.match(r"-- (\d+)\. (.+)\n-- Why it matters: (.+)\n", b)
        if not m:
            continue
        out.append({"n": int(m.group(1)), "title": m.group(2).strip(), "why": m.group(3).strip(),
                    "sql": b[m.end():].strip().rstrip(";")})
    return out


def run(db_path=PRISTINE_DB) -> list[dict]:
    conn = connect(db_path)
    results = []
    for q in load_queries():
        cur = conn.execute(q["sql"])
        cols = [d[0] for d in cur.description]
        rows = [tuple(r) for r in cur.fetchall()]
        results.append({**q, "cols": cols, "rows": rows})
    conn.close()
    return results


def to_markdown(results) -> str:
    found = sum(1 for r in results if r["rows"])
    md = ["# Data issues report", "",
          f"{len(results)} checks run against the supplied snapshot (4 Aug 2026), **{found} found problems**. "
          "Each check is a query in `analysis/data_issues.sql`; the rows below are the evidence. "
          "Checks with no rows are guards that pass on this snapshot.", "",
          "| # | Check | Rows |", "|---|---|---|"]
    md += [f"| {r['n']} | {r['title']} | {len(r['rows'])} |" for r in results]
    for r in results:
        md += ["", f"## {r['n']}. {r['title']}", "", f"Why it matters: {r['why']}", ""]
        if not r["rows"]:
            md.append("No rows. Clean on this snapshot.")
            continue
        md.append("| " + " | ".join(r["cols"]) + " |")
        md.append("|" + "---|" * len(r["cols"]))
        for row in r["rows"][:10]:
            md.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
        md += ["", "<details><summary>Query</summary>", "", "```sql", r["sql"], "```", "", "</details>"]
    return "\n".join(md) + "\n"


if __name__ == "__main__":
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else PRISTINE_DB
    res = run(db)
    out = ROOT / "reports" / "data_issues_report.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text(to_markdown(res))
    for r in res:
        print(f"{r['n']:>2}. {r['title']:<60} {len(r['rows'])} rows")
    print(f"Wrote {out}")
