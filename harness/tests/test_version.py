"""harness.toml pins the h it needs; an older h refuses."""

import json

import pytest

from harness import __version__, cli, config, init, version


def _pin(proj, spec):
    toml = proj / "harness.toml"
    toml.write_text(f'[harness]\nversion = "{spec}"\n' + toml.read_text(encoding="utf-8"),
                    encoding="utf-8")  # fmt: skip


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "harness.toml").write_text(
        '[project]\nname = "demo"\n', encoding="utf-8"
    )
    return tmp_path


@pytest.mark.parametrize(
    ("spec", "have", "ok"),
    [(">=0.2", "0.2.0", True), (">=0.2.1", "0.2.0", False), (">= 1.0", "0.9.9", False),
     (">=0.1.9", "0.2.0", True), (">=0.10", "0.9.0", False)],
)  # fmt: skip
def test_minimum_version(spec, have, ok):
    assert (version.mismatch(spec, have) is None) == ok


@pytest.mark.parametrize("bad", ["0.2", "==0.2", ">=0.2-rc", ">=", "latest"])
def test_a_bad_spec_is_a_config_error(proj, bad):
    _pin(proj, bad)
    with pytest.raises(config.ConfigError, match="harness.version"):
        config.load(proj)


def test_an_older_h_refuses_working_commands(proj, capsys):
    """B1."""
    _pin(proj, ">=99.0")
    assert cli.main(["-C", str(proj), "check"]) == 2
    err = capsys.readouterr().err
    assert f"requires h >=99.0, installed h {__version__}" in err and "update:" in err


def test_the_refusal_is_json_with_json(proj, capsys):
    _pin(proj, ">=99.0")
    assert cli.main(["-C", str(proj), "check", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "version" and out["required"] == ">=99.0"
    assert out["installed"] == __version__


def test_a_new_enough_h_runs_quietly(proj, capsys):
    """B2."""
    _pin(proj, f">={__version__}")
    assert cli.main(["-C", str(proj), "config"]) == 0
    assert cli.main(["-C", str(proj), "ratchet"]) == 0
    assert "requires h" not in capsys.readouterr().err


def test_no_pin_runs_as_before(proj, capsys):
    """B3."""
    assert cli.main(["-C", str(proj), "ratchet"]) == 0
    assert "requires h" not in capsys.readouterr().err


def test_doctor_and_config_run_on_a_mismatch(proj, capsys):
    """B4."""
    _pin(proj, ">=99.0")
    assert cli.main(["-C", str(proj), "doctor"]) == 1
    assert "FAIL harness.toml requires h >=99.0" in capsys.readouterr().out
    assert cli.main(["-C", str(proj), "config"]) == 0


def test_init_pins_a_new_project(tmp_path):
    """B5."""
    changes = init.run(config.load(tmp_path))
    assert f'create harness.toml ([harness] version = ">={__version__}")' in changes
    assert config.load(tmp_path).harness_version == f">={__version__}"


def test_init_only_advises_on_an_existing_unpinned_toml(proj):
    """B6."""
    before = (proj / "harness.toml").read_text(encoding="utf-8")
    changes = init.run(config.load(proj))
    assert any(c.startswith("tip: add `[harness] version") for c in changes)
    assert (proj / "harness.toml").read_text(encoding="utf-8") == before
    _pin(proj, f">={__version__}")
    assert not any(c.startswith("tip:") for c in init.run(config.load(proj)))
