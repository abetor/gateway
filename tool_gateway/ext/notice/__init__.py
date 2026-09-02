"""Version 2 message envelope: one delivery policy for every tool.

One message is one envelope with ``from``, ``about``, ``class``, ``title``,
``lines``, ``name``, ``action``, ``urgency``, ``dedupe``, and ``live`` fields.
Classes come from a configuration-extensible registry and each class has a
template file. Availability windows, deferred queues, and retries belong here,
not in callers. The transport remains in ``ext/deliver``.
"""
