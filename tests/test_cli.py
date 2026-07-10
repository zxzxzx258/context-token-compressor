from __future__ import annotations

import tomllib

import pytest

from ctc import __version__
from ctc.main import main


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == "Context Token Compressor 1.0.0"


def test_cli_check_config_is_sanitized(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CTC_LOCAL_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("CTC_ADMIN_TOKEN", "private-admin-value")

    main(["--check-config"])

    output = capsys.readouterr().out.strip()
    assert output == "Context Token Compressor configuration is valid"
    assert "private-admin-value" not in output


def test_package_versions_match():
    with open("pyproject.toml", "rb") as handle:
        project_version = tomllib.load(handle)["project"]["version"]

    assert project_version == __version__
