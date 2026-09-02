"""Webhook HTTP ingress for push events.

Any accepted POST selects and spawns a configured route command. The channel is
limited to route matching, spawning, and logging; business logic belongs to the
invoked tools.
"""
