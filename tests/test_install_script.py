from pathlib import Path


def test_linux_installer_keeps_strict_failure_and_ctc_names():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "install_linux.sh").read_text(
        encoding="utf-8"
    )

    assert "set -euo pipefail" in script
    assert "CTC_NONINTERACTIVE" in script
    assert "systemctl enable --now ctc.service" in script
    assert "systemctl enable --now ctc.service ||" not in script
