"""SIM_CLOCK rejects unknown values instead of silently meaning real_time.

SIM_CLOCK is only ever compared `== "virt_time"`, so before this validation any
misspelling fell through to wall-paced real_time while the caller believed the
lockstep virtual clock was engaged — and it produced no warning, no banner
difference a reader would notice, and a corpus whose declared clock was wrong.
Every other enum knob in channel_sim validates; this one did not, which is why
'virtual' survived across the whole driver fleet.

'virtual' is deliberately NOT accepted as a synonym. Aliasing it to virt_time
would retroactively convert every existing real-time run into a virtual-clock
run, which is a physics change disguised as a typo fix.
"""
import pytest

from conftest import load_sim


def test_valid_clocks_load():
    assert load_sim(SIM_CLOCK="real_time").SIM_CLOCK == "real_time"
    assert load_sim(SIM_CLOCK="virt_time", SIM_TRANSPORT="sock").SIM_CLOCK \
        == "virt_time"


def test_default_is_real_time():
    assert load_sim().SIM_CLOCK == "real_time"


@pytest.mark.parametrize("bad", ["virtual", "virt", "fast", "VIRTUAL_TIME"])
def test_unknown_clock_is_rejected(bad):
    with pytest.raises(SystemExit) as e:
        load_sim(SIM_CLOCK=bad, SIM_TRANSPORT="sock")
    assert "SIM_CLOCK" in str(e.value)


def test_the_specific_historical_typo_names_itself():
    """'virtual' was the fleet-wide spelling; the message must say so, because
    the reader's next question is 'but it worked before'."""
    with pytest.raises(SystemExit) as e:
        load_sim(SIM_CLOCK="virtual", SIM_TRANSPORT="sock")
    msg = str(e.value)
    assert "virtual" in msg and "virt_time" in msg
    assert "real_time" in msg
