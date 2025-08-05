#!/usr/bin/env python3
"""Placeholder for future in-person audio processing."""

import json
import sys


def main() -> None:
    _payload = json.load(sys.stdin)
    print("[rt_audio] received payload", flush=True)


if __name__ == "__main__":
    main()

