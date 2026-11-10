"""Every data-issue query finds its evidence in the supplied snapshot (and guards stay clean)."""
from analysis.run_issues import load_queries, run

EXPECTED = {  # check -> a value that must appear in its evidence rows (None = must be clean)
    1: "APT1005", 2: "SLOT-JAI-030", 3: "APT1014A", 4: "SHP1009", 5: "APT1015A", 6: "VEH003", 7: "DRV007",
    8: "DRV004", 9: "SHP1011", 10: "APT1013A", 11: "THR009", 12: "OM004", 13: "APT1014A", 14: "SHP1018",
    15: "CON005", 16: "SHP1004", 17: "SHP1005", 18: "SHP1007", 19: None, 20: None, 21: None, 22: None, 23: None,
}


def test_all_queries_parse():
    assert [q["n"] for q in load_queries()] == list(range(1, 24))


def test_each_issue_has_evidence():
    for r in run():
        flat = {str(v) for row in r["rows"] for v in row}
        if EXPECTED[r["n"]] is None:
            assert r["rows"] == [], f"guard {r['n']} ({r['title']}) is not clean: {r['rows']}"
        else:
            assert EXPECTED[r["n"]] in flat, f"check {r['n']} ({r['title']}) found {r['rows']}"


def test_vehicle_conflicts_found_with_timezone_safe_math():
    rows = next(r for r in run() if r["n"] == 6)["rows"]
    assert {row[0] for row in rows} == {"VEH001", "VEH003", "VEH015"}
