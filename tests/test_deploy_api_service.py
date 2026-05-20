from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_api_systemd_unit_runs_separate_api_entrypoint():
    unit = (ROOT / "deploy" / "funpay-ns-api.service").read_text(encoding="utf-8")

    assert "ExecStart=/opt/funpay-ns-bot/.venv/bin/python -m src.api.main" in unit
    assert "EnvironmentFile=/opt/funpay-ns-bot/.env" in unit
    assert "User=bot" in unit
    assert "ReadWritePaths=/opt/funpay-ns-bot/data /opt/funpay-ns-bot/logs" in unit


def test_update_script_manages_api_service_when_enabled():
    script = (ROOT / "deploy" / "update.sh").read_text(encoding="utf-8")

    assert "systemctl stop funpay-ns-api" in script
    assert "deploy/funpay-ns-api.service" in script
    assert "systemctl start funpay-ns-api" in script


def test_deploy_docs_include_api_smoke_check():
    docs = (ROOT / "deploy" / "README.md").read_text(encoding="utf-8")

    assert "funpay-ns-api" in docs
    assert "src.tools.check_web_api" in docs
    assert "WEB_API_TOKEN" in docs
