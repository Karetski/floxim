"""Stub harness for the conformance kit: prints a recorded stream and exits 0.

Usage: python replay.py <stream.jsonl> -- <the adapter's arguments, ignored>
"""

import sys

if sys.argv[1:2] == ["--version"]:
    print("replay 0")
    sys.exit(0)
sys.stdin.read()
with open(sys.argv[1], encoding="utf-8") as stream:
    sys.stdout.write(stream.read())
