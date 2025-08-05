#!/usr/bin/env python3
"""Placeholder for future video download processing."""

import json
import sys


def main() -> None:
    _payload = json.load(sys.stdin)
    print("[download] received payload", flush=True)


if __name__ == "__main__":
    main()

