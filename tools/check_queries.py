#!/usr/bin/env python3
"""Thin wrapper around nr2grafana.livecheck (kept for back-compat).

Usage:
  python3 tools/check_queries.py <dashboard.json|dir> \
      [--prom http://localhost:9090] [--loki http://localhost:3100]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nr2grafana.livecheck import check_files  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--prom", default="http://localhost:9090")
    ap.add_argument("--loki", default="http://localhost:3100")
    args = ap.parse_args()
    passed, failed, skipped, _ = check_files(
        args.inputs, args.prom, args.loki, log=print)
    print("\n%d targets: %d passed, %d failed, %d skipped "
          "(tempo/passthrough)" % (passed + failed + skipped, passed,
                                   failed, skipped))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
