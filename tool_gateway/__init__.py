"""tool-gateway exposes local tools through Telegram and webhooks.

It provides notification delivery and a shared audit log. The entry point is
``cli.py``, the core lives in ``core/``, channel implementations live in
``ext/``, and locally owned shared code lives in ``shared/``. The gateway stays
thin: routes spawn tool CLIs, business logic remains in those tools, and calls
flow only from the gateway to tools.
"""
