"""Every data-issue query must find its evidence in the stand-in snapshot,
and every query must at least run (so it is ready for the real database)."""
from analysis.run_issues import load_queries, run

EXPECTED = {  # check number -> a value that must appear in the evidence rows
    1: "JAI-D1-1030", 2: "SH-07", 3: "SH-08", 4: "SH-09", 5: "SH-10", 6: "SH-11", 7: "SH-12",
    8: "SH-02", 9: "AMD-A2-BAD", 10: "SH-13", 11: "SH-14", 12: "SH-15", 13: "+919800000018",
    14: "SH-16", 15: "SH-21",
}


def test_all_queries_parse():
    assert [q["n"] for q in load_queries()] == list(range(1, 16))


def test_each_issue_has_evidence(db_path):
    for r in run(db_path):
        flat = {str(v) for row in r["rows"] for v in row}
        assert EXPECTED[r["n"]] in flat, f"check {r['n']} ({r['title']}) found {r['rows']}"
