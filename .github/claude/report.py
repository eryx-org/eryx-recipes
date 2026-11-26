"""Print what a `claude -p --output-format json` run cost, to the log and
the job summary: its outcome, dollars spent of $BUDGET, and turns.

    python3 -I .github/claude/report.py RESULT.json

The result is the CLI's last JSON line. Prints "no result" if there isn't
one (the CLI crashed), and never fails, so the caller keeps the CLI's own
exit status.
"""

import json
import os
import sys


def main():
    try:
        lines = [l for l in open(sys.argv[1], encoding="utf-8").read().splitlines() if l.startswith("{")]
        result = json.loads(lines[-1]) if lines else {}
    except (OSError, ValueError):
        result = {}
    line = (f"Claude: {result.get('subtype', 'no result')}, "
            f"${result.get('total_cost_usd', 0):.2f} of ${os.environ.get('BUDGET', '?')}, "
            f"{result.get('num_turns', '?')} turns")
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            print(line, file=f)


if __name__ == "__main__":
    main()
