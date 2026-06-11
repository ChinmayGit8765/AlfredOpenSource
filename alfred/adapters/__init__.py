"""Adapters: implementations of ports against real external systems.

Adapters may import from alfred.ports, alfred.domain.schemas, alfred.config,
and alfred.errors. The domain never imports from here; wiring happens only
at the composition root.
"""
