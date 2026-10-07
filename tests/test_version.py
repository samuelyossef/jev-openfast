import re
import tomllib
from pathlib import Path

from jev_ultrafast import __version__, demo


def test_version_comes_from_pyproject_and_is_exposed_to_the_ui():
    declared = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    assert __version__ == declared["project"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert demo.response_state(compact=True)["version"] == __version__


def test_version_flag_prints_and_exits_without_starting_the_server(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["jev", "--version"])
    demo.main()
    assert capsys.readouterr().out.strip() == f"JEV OpenFast Browser {__version__}"
