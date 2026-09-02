"""Gateway core: fail-closed configuration, channel registry, and JSONL logs.

The core is intentionally thin and does not know about ``ext/``. Concrete
channels live in ``ext/<channel>/`` and register themselves when imported;
``cli.py`` is the registration point.

This module also owns the tool exit-code contract. Channels need these codes
because ``ext/deliver`` returns them to callers. Importing the CLI layer into
``ext/`` for four constants would reverse the dependency. ``cli.py`` re-exports
them, so consumers can continue to import the contract from
``tool_gateway.cli``.
"""

# Shared tool exit-code contract. Schedulers classify outcomes by these codes;
# see also ``shared/adapters/base.py`` and ``STOP_TO_EXIT``. Code 2 is not part
# of the contract: ``shared/cliargs`` maps argparse usage errors to code 1.
EXIT_OK = 0           # success
EXIT_QUOTA = 75       # EX_TEMPFAIL: quota or subscription window; wait and retry
EXIT_TRANSIENT = 111  # transient network or 5xx failure; retry is appropriate
EXIT_FAIL = 1         # permanent failure, including configuration and usage errors
