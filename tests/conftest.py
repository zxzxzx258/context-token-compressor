from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def secure_test_defaults(monkeypatch):
    monkeypatch.setenv("CTC_ADMIN_TOKEN", "test-admin-token")
    monkeypatch.delenv("CTC_PROXY_TOKEN", raising=False)
    monkeypatch.delenv("CTC_TRUSTED_PROXY_HOSTS", raising=False)
    monkeypatch.delenv("CTC_MAX_BODY_BYTES", raising=False)
    monkeypatch.delenv("CTC_ALLOW_PROFILE_HEADER", raising=False)
