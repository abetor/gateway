"""Canonical ``python3 -m tool_gateway`` entry point for source checkouts."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
