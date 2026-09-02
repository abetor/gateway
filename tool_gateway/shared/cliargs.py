"""Argument parser that maps usage errors to the tool contract's exit code 1.

Standard argparse exits with code 2 for invalid arguments, while this tool's
CLI contract has no code 2. The subclass changes only the exit code; usage and
the stderr message remain intact. Subparsers inherit the class automatically,
so subcommand usage errors also exit with code 1.
"""
from __future__ import annotations

import argparse
import sys


class ArgumentParser(argparse.ArgumentParser):
    """An ``ArgumentParser`` whose usage errors exit with status 1."""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")
