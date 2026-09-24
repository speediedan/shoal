"""SHOAL (Stigmergic Heterogeneous Open Agent Lattice).

The command line is `shoal` (`shoal.cli:main`), or `python3 shoal/cli.py` from a checkout.
The modules import one another flat: each puts its own directory on `sys.path`, so they run
the same way as a script, a console script, or a systemd unit's ExecStart.
"""

__version__ = "0.1.0.dev0"
