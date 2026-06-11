"""Domain layer: pure logic, no I/O.

Modules here may import from alfred.ports, alfred.errors, stdlib, and
pydantic. They must never import from alfred.adapters or alfred.runtime,
and must never open files, sockets, or databases directly. All effects
flow through injected ports.
"""
