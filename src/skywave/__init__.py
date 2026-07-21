"""skywave -- HF/VHF channel simulator and comparative modem test harness."""

import os

__version__ = "0.1.0"


def child_env(base=None):
    """A subprocess environment with skywave's source root on ``PYTHONPATH``.

    The harness spawns helpers (``channel_sim``, ``sock_alsa_shim``, the modem
    adapters) as child processes. An editable or regular install makes
    ``import skywave`` work in those children unconditionally; this helper is
    the belt-and-suspenders that also lets a *source checkout* (a bare ``src/``
    tree, no install) resolve the package, by putting the directory that
    contains ``skywave/`` at the front of the child's ``PYTHONPATH``.
    """
    env = dict(os.environ if base is None else base)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (root, env.get("PYTHONPATH", "")) if p)
    return env
