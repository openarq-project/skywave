"""skywave-channel's command line: every channel_sim setting as a flag.

channel_sim is configured by environment variables, read when the module is
imported. This parses the command line and writes each option given into the
environment BEFORE that import, so a flag and its variable are the same
setting by construction. One naming rule, no second table to keep in sync:
drop a leading SIM_, lowercase, underscores to hyphens

    SIM_WATTERSON=poor  ->  --watterson poor   (also --fade, hfchan's name)
    SIGMA=300           ->  --sigma 300
    SIM_HALF_DUPLEX=1   ->  --half-duplex      (--no-half-duplex sets 0)

Precedence: command line > environment > profile (--profile) > default.
The settings themselves are listed in sim_knobs.py.
"""
import argparse
import os
import re
import sys
import textwrap

from skywave.sim_knobs import GROUPS, KNOBS

# Extra names for a few settings, where another skywave tool already uses one.
ALIASES = {"SIM_WATTERSON": ("--fade",)}

EPILOG = """\
Every option is also an environment variable, by one rule: SIM_WATTERSON is
--watterson, SIGMA is --sigma. A flag beats the variable, which beats a
--profile, which beats the built-in default. Harnesses that set variables and
pass no flags behave exactly as before.

examples:
  skywave-channel --profile poor --listen 0.0.0.0
  skywave-channel --listen 0.0.0.0 --fade poor --sigma 300
  skywave-channel --transport sock --clock virt_time --sock-dir /tmp/armsim \\
                  --half-duplex --ptt --sigma 0
"""


def flag_name(env):
    """The flag for an environment variable: SIM_FADE_SCHEDULE -> --fade-schedule."""
    name = env[4:] if env.startswith("SIM_") else env
    return "--" + name.lower().replace("_", "-")


def _flags_for_names(text, names):
    """A help line as this screen should read it: a setting mentioned by its
    variable name (SIM_FADE_SCHEDULE) is shown by its flag (--fade-schedule)."""
    return re.sub(r"\b[A-Z][A-Z0-9_]*[A-Z0-9]\b",
                  lambda m: flag_name(m.group(0)) if m.group(0) in names else m.group(0),
                  text)


def build_parser():
    from skywave import watterson
    presets = textwrap.fill(
        "fade presets (--fade): "
        + ", ".join(k for k in watterson.PRESETS if k != "off"), 78)
    p = argparse.ArgumentParser(
        prog="skywave-channel", allow_abbrev=False, usage="%(prog)s [options]",
        epilog=EPILOG + "\n" + presets + "\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Two-station HF channel simulator: sits between two modems'\n"
                    "audio and adds noise, fading, rig effects and half-duplex "
                    "keying.")
    groups = {g: p.add_argument_group(g) for g in GROUPS}
    envs = {k[0] for k in KNOBS}
    for env, group, kind, metavar, text in KNOBS:
        names = (flag_name(env),) + ALIASES.get(env, ())
        text = _flags_for_names(text, envs)
        text = text.replace("%", "%%")          # argparse %-formats help strings
        if kind == "flag":
            groups[group].add_argument(*names, dest=env, default=None, help=text,
                                       action=argparse.BooleanOptionalAction)
        else:
            groups[group].add_argument(*names, dest=env, default=None, help=text,
                                       metavar=metavar or "VALUE")
    return p


def apply_argv(argv):
    """Parse `argv` and write each option given into os.environ, overriding a
    variable already set there. Exits on --help or a bad option."""
    args = build_parser().parse_args(argv)
    for env, *_ in KNOBS:
        value = getattr(args, env)
        if value is None:
            continue
        os.environ[env] = ("1" if value else "0") if isinstance(value, bool) else value


def main(argv=None):
    apply_argv(sys.argv[1:] if argv is None else argv)
    from skywave import channel_sim            # reads the settings on import
    return channel_sim.main()


if __name__ == "__main__":
    sys.exit(main())
