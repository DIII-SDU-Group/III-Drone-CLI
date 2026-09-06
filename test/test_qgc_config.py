from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from iii import qgc_config

ROOT = Path(__file__).resolve().parents[3]
RELEASE_ID = "c" * 64


def args(tmp_path: Path, **values):
    environment = {
        "WORKSPACE_DIR": str(ROOT),
        "III_QGC_SETTINGS_PATH": str(
            tmp_path / ".config/QGroundControl.org/QGroundControl.ini"
        ),
        "III_QGC_CONFIGURATION_STATE_ROOT": str(tmp_path / "state"),
        "III_QGC_KEY_POLICY": str(ROOT / "deployment/qgc/key-policy.json"),
        "III_QGC_MANAGED_SETTINGS": str(ROOT / "deployment/qgc/managed-settings.json"),
        "III_DEPLOYMENT_SCHEMA_ROOT": str(ROOT / "deployment/schemas/v1"),
    }
    defaults = {
        "release_id": RELEASE_ID,
        "qgc_version": "5.0.8",
        "profile": "real",
        "clean_exit": False,
        "_iii_environment": environment,
        "_iii_retained_plan": None,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def test_qgc_config_apply_is_backup_first_and_preserves_local_preferences(
    tmp_path, monkeypatch
):
    command = args(tmp_path)
    settings = Path(command._iii_environment["III_QGC_SETTINGS_PATH"])
    settings.parent.mkdir(parents=True)
    settings.write_text(
        "[General]\nsavePath=/home/operator/Flights\n\n"
        "[MAVLinkLogGroup]\nEnableAutoUpload=true\nPublicLog=true\n"
    )
    settings.chmod(0o600)
    monkeypatch.setattr(qgc_config, "_unit_active", lambda: False)
    preflight = qgc_config.apply_preflight(command)
    command._iii_retained_plan = {"preflight": preflight}

    result = qgc_config.apply(command)

    assert result.outcome.value == "success"
    text = settings.read_text()
    assert "savePath=/home/operator/Flights" in text
    assert "EnableAutoUpload=false" in text
    assert "PublicLog=false" in text
    assert result.payload["backup_id"]


def test_qgc_explicit_capture_is_redacted_and_diff_is_read_only(tmp_path, monkeypatch):
    command = args(tmp_path)
    settings = Path(command._iii_environment["III_QGC_SETTINGS_PATH"])
    settings.parent.mkdir(parents=True)
    settings.write_text(
        "[General]\nmavlink2SigningKey=private\ntelemetrySaveNotArmed=true\n"
        "forwardMavlink=true\nforwardMavlinkHostName=127.0.0.1:14551\n"
        "saveCsvTelemetry=false\nsendGCSHeartbeat=true\ntelemetrySave=true\n"
        "\n[MAVLinkLogGroup]\nEnableAutoUpload=false\nPublicLog=false\n"
    )
    settings.chmod(0o600)
    monkeypatch.setattr(qgc_config, "_unit_active", lambda: True)
    preflight = qgc_config.capture_preflight(command)
    command._iii_retained_plan = {"preflight": preflight}
    captured = qgc_config.capture(command)
    assert captured.outcome.value == "success"
    assert "private" not in captured.payload.__str__()

    diff_args = args(tmp_path, capture_id=captured.payload["capture_id"])
    diff = qgc_config.diff(diff_args)
    assert diff.outcome.value == "success"
    assert diff.payload["changes"] == [
        {
            "key": "General/telemetrySaveNotArmed",
            "baseline": False,
            "captured": True,
        }
    ]


def test_clean_exit_capture_requires_inactive_unit(tmp_path, monkeypatch):
    command = args(tmp_path, clean_exit=True)
    settings = Path(command._iii_environment["III_QGC_SETTINGS_PATH"])
    settings.parent.mkdir(parents=True)
    settings.write_text("[General]\nvalue=one\n")
    settings.chmod(0o600)
    monkeypatch.setattr(qgc_config, "_unit_active", lambda: True)
    try:
        qgc_config.capture_preflight(command)
    except ValueError as exc:
        assert "inactive" in str(exc)
    else:
        raise AssertionError("clean-exit capture accepted a running QGC unit")
