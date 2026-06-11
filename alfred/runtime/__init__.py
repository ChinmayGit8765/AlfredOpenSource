"""Runtime: the composition root, core loop, scheduler, and CLI.

The only layer allowed to import both domain and adapters; all wiring
of adapters into ports happens here and nowhere else.
"""
