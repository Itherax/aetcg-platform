#!/usr/bin/env python3
"""
Run the scenario suite and print one line per scenario: ID, pass/fail, title.

    python scripts/scenario_suite.py            # table on stdout
    python scripts/scenario_suite.py --json out.json

Exit status is 0 even when scenarios fail: the point of this script is the
scorecard, not gating.  Use `python -m pytest tests/scenarios` to gate.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    out_json = None
    if "--json" in sys.argv:
        out_json = Path(sys.argv[sys.argv.index("--json") + 1])
    cmd = [sys.executable, "-m", "pytest", "tests/scenarios", "-q", "-rA", "--tb=no", "-p", "no:cacheprovider",
           "--no-header", "-o", "console_output_style=classic"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    results = {}
    for line in proc.stdout.splitlines():
        m = re.match(r"^(PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS) tests/scenarios/test_scenarios.py::(\w+)", line)
        if m:
            results[m.group(2)] = m.group(1)
    # titles from docstrings
    src = (ROOT / "tests/scenarios/test_scenarios.py").read_text()
    titles = dict(re.findall(r'def (test_s\d+\w*)\([^)]*\):\n\s+"""(S\d+[^\n]*?)"""', src))
    rows = []
    for name in sorted(titles):
        title = titles[name]
        sid = title.split(" ")[0]
        rows.append({"id": sid, "test": name, "result": results.get(name, "NOT RUN"), "title": title})
    passed = sum(r["result"] == "PASSED" for r in rows)
    print(f"{'ID':5} {'RESULT':8} TITLE")
    for r in rows:
        print(f"{r['id']:5} {r['result']:8} {r['title']}")
    print(f"\n{passed}/{len(rows)} scenarios pass")
    if out_json:
        out_json.write_text(json.dumps({"passed": passed, "total": len(rows), "rows": rows}, indent=2))
    if proc.returncode not in (0, 1):
        print(proc.stdout[-3000:], proc.stderr[-3000:])


if __name__ == "__main__":
    main()
