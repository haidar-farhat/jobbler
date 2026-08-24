"""Specialised agents.

The walking skeleton runs only the application agent, whose behaviour lives in
`orchestrator.run_loop` plus the reasoner. Discovery and analysis land in Phases 6 and 3;
their capability sets are already declared in `policy.capabilities` so the boundaries exist
before the code does.
"""
