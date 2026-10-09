"""Guard: committed text files must not carry customer identifiers.

The canonical AWS documentation example account id (123456789012) and
all-zero ids are the only 12-digit account-shaped runs allowed. Real
account ids, like real hostnames or cluster names, belong in placeholders.
"""

import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNT_RE = re.compile(r"(?<![0-9A-Za-z.])(\d{12})(?![0-9A-Za-z]|\.\d)")
ROUND_RE = re.compile(r"^[1-9]0{11}$")
ALLOWED_IDS = {"123456789012", "000000000000", "111111111111",
               "999999999999"}
TEXT_EXT = (".py", ".md", ".json", ".yaml", ".yml", ".txt", ".toml",
            ".cfg", ".sh", ".html", ".js")


def _tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True)
    if out.returncode != 0:
        return []
    return [os.path.join(ROOT, p) for p in out.stdout.split("\n") if p]


class NoCustomerIdentifiersTests(unittest.TestCase):
    def test_no_real_account_ids(self):
        offenders = []
        for path in _tracked_files():
            if not path.endswith(TEXT_EXT) or not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    for m in ACCOUNT_RE.finditer(line):
                        if m.group(1) not in ALLOWED_IDS \
                                and not ROUND_RE.match(m.group(1)):
                            offenders.append("%s:%d: %s" % (
                                os.path.relpath(path, ROOT), lineno,
                                m.group(1)))
        self.assertEqual(offenders, [], "real-looking AWS account ids "
                         "found in tracked files; use 123456789012")


if __name__ == "__main__":
    unittest.main()
