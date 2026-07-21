"""Tests for the external adapter registry + de-hardcoded root. A project must be able to register a modem WITHOUT editing sweep_runner, and
the repository root must derive from the file (no hardcoded absolute path), env-overridable.

Run:  cd skywave && python3 -m pytest tests/test_adapter_registry.py -q
"""
import json
import os

import pytest

from skywave import sweep_runner


def test_no_hardcoded_absolute_root():
    # BENCH_ROOT is env-derived (defaulting to the cwd), never a hardcoded absolute path
    # and never the buried package dir (which would drop run artifacts into the install).
    assert sweep_runner.BENCH_ROOT != os.path.dirname(os.path.abspath(sweep_runner.__file__))
    src = open(sweep_runner.__file__).read()
    assert 'os.environ.get("BENCH_ROOT")' in src          # env-derived, not hardcoded
    assert "os.getcwd()" in src                            # defaults to cwd, not the file's dir


def test_builtins_present_without_external():
    ad = sweep_runner.load_adapters(root="/nonexistent", extra_path=None)
    assert {"loopback", "mercury"} <= set(ad)
    assert ad["mercury"]["module"] == "skywave.adapters.mercury"


def test_external_registry_adds_and_resolves_script(tmp_path):
    (tmp_path / "my_arq_pipe.py").write_text("# stub\n")
    reg = {"mymodem": {"script": "my_arq_pipe.py", "kill_pad": 42}}
    (tmp_path / "adapters.json").write_text(json.dumps(reg))
    ad = sweep_runner.load_adapters(root=str(tmp_path))
    assert "mymodem" in ad and "loopback" in ad           # merged, not replaced
    # relative script resolved against the registry file's dir (ship-alongside)
    assert ad["mymodem"]["script"] == str(tmp_path / "my_arq_pipe.py")
    assert ad["mymodem"]["kill_pad"] == 42
    assert ad["mymodem"]["extra_env"] == {}                # defaulted


def test_external_overrides_builtin(tmp_path):
    reg = {"mercury": {"script": "mercury_adapter.py", "kill_pad": 999,
                       "extra_env": {"MERCURY_BIN": "/opt/merc"}}}
    (tmp_path / "adapters.json").write_text(json.dumps(reg))
    ad = sweep_runner.load_adapters(root=str(tmp_path))
    assert ad["mercury"]["kill_pad"] == 999
    assert ad["mercury"]["extra_env"] == {"MERCURY_BIN": "/opt/merc"}


def test_bench_adapters_env_path(tmp_path):
    reg = {"envmodem": {"script": "/abs/path/env_pipe.py"}}
    p = tmp_path / "extra.json"
    p.write_text(json.dumps(reg))
    ad = sweep_runner.load_adapters(root="/nonexistent", extra_path=str(p))
    assert ad["envmodem"]["script"] == "/abs/path/env_pipe.py"   # absolute kept as-is


def test_missing_script_key_errors(tmp_path):
    (tmp_path / "adapters.json").write_text(json.dumps({"bad": {"kill_pad": 10}}))
    with pytest.raises(SystemExit, match="missing 'script'"):
        sweep_runner.load_adapters(root=str(tmp_path))


def test_bad_json_errors(tmp_path):
    (tmp_path / "adapters.json").write_text("{not json")
    with pytest.raises(SystemExit, match="bad adapter registry"):
        sweep_runner.load_adapters(root=str(tmp_path))


def test_resolve_adapter_unknown_lists_known():
    with pytest.raises(SystemExit, match="unknown modem 'nope'.*Known:"):
        sweep_runner.resolve_adapter("nope", adapters={"loopback": {}, "mercury": {}})


def test_resolve_adapter_known_returns_cfg():
    cfg = {"script": "x.py", "kill_pad": 5, "extra_env": {}}
    assert sweep_runner.resolve_adapter("x", adapters={"x": cfg}) is cfg
