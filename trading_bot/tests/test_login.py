"""
tests/test_login.py — Test suite for AngelOne authentication module.

Run:
    pytest trading_bot/tests/test_login.py -v

These tests mock SmartAPI so they work without real credentials.
"""

import os
from unittest.mock import patch, MagicMock

import pytest


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_login_state():
    """Reset the login module's cached session before each test."""
    from trading_bot.auth import login
    login._session = None
    login._auth_token = ""
    login._feed_token = ""
    login._refresh_token = ""
    yield
    login._session = None
    login._auth_token = ""
    login._feed_token = ""
    login._refresh_token = ""


@pytest.fixture
def mock_env(monkeypatch):
    """Set dummy credentials so config reads them."""
    monkeypatch.setenv("ANGEL_API_KEY", "test_api_key")
    monkeypatch.setenv("ANGEL_CLIENT_ID", "T12345")
    monkeypatch.setenv("ANGEL_PASSWORD", "testpass")
    monkeypatch.setenv("ANGEL_TOTP_KEY", "JBSWY3DPEHPK3PXP")  # known base32 test key


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestTOTP:
    """TOTP generation tests."""

    def test_generate_totp_returns_6_digits(self, mock_env):
        # Reload config to pick up env vars
        from trading_bot import config
        config.ANGEL_TOTP_KEY = os.environ["ANGEL_TOTP_KEY"]

        from trading_bot.auth.login import _generate_totp
        totp = _generate_totp()
        assert len(totp) == 6
        assert totp.isdigit()

    def test_generate_totp_fails_without_key(self):
        from trading_bot import config
        config.ANGEL_TOTP_KEY = ""

        from trading_bot.auth.login import _generate_totp
        with pytest.raises(ValueError, match="ANGEL_TOTP_KEY"):
            _generate_totp()


class TestAuthenticate:
    """SmartConnect login tests (mocked)."""

    @patch("trading_bot.auth.login.SmartConnect")
    def test_successful_login(self, MockSmartConnect, mock_env):
        from trading_bot import config
        config.ANGEL_API_KEY = os.environ["ANGEL_API_KEY"]
        config.ANGEL_CLIENT_ID = os.environ["ANGEL_CLIENT_ID"]
        config.ANGEL_PASSWORD = os.environ["ANGEL_PASSWORD"]
        config.ANGEL_TOTP_KEY = os.environ["ANGEL_TOTP_KEY"]

        mock_obj = MagicMock()
        mock_obj.generateSession.return_value = {
            "status": True,
            "data": {
                "jwtToken": "jwt_test_token_12345",
                "refreshToken": "refresh_test_token_12345",
            },
        }
        mock_obj.getfeedToken.return_value = "feed_test_token_12345"
        MockSmartConnect.return_value = mock_obj

        from trading_bot.auth.login import authenticate, is_logged_in
        session = authenticate()

        assert session is mock_obj
        assert is_logged_in()
        mock_obj.generateSession.assert_called_once()

    @patch("trading_bot.auth.login.SmartConnect")
    def test_login_failure_raises(self, MockSmartConnect, mock_env):
        from trading_bot import config
        config.ANGEL_API_KEY = os.environ["ANGEL_API_KEY"]
        config.ANGEL_CLIENT_ID = os.environ["ANGEL_CLIENT_ID"]
        config.ANGEL_PASSWORD = os.environ["ANGEL_PASSWORD"]
        config.ANGEL_TOTP_KEY = os.environ["ANGEL_TOTP_KEY"]

        mock_obj = MagicMock()
        mock_obj.generateSession.return_value = {
            "status": False,
            "message": "Invalid credentials",
        }
        MockSmartConnect.return_value = mock_obj

        from trading_bot.auth.login import authenticate
        with pytest.raises(RuntimeError, match="Invalid credentials"):
            authenticate()

    def test_missing_credentials_raises(self):
        from trading_bot import config
        config.ANGEL_API_KEY = ""
        config.ANGEL_CLIENT_ID = ""
        config.ANGEL_PASSWORD = ""
        config.ANGEL_TOTP_KEY = ""

        from trading_bot.auth.login import authenticate
        with pytest.raises(ValueError, match="not configured"):
            authenticate()


class TestLogout:
    """Logout tests."""

    @patch("trading_bot.auth.login.SmartConnect")
    def test_logout_clears_session(self, MockSmartConnect, mock_env):
        from trading_bot import config
        config.ANGEL_API_KEY = os.environ["ANGEL_API_KEY"]
        config.ANGEL_CLIENT_ID = os.environ["ANGEL_CLIENT_ID"]
        config.ANGEL_PASSWORD = os.environ["ANGEL_PASSWORD"]
        config.ANGEL_TOTP_KEY = os.environ["ANGEL_TOTP_KEY"]

        mock_obj = MagicMock()
        mock_obj.generateSession.return_value = {
            "status": True,
            "data": {
                "jwtToken": "jwt_123",
                "refreshToken": "ref_123",
            },
        }
        mock_obj.getfeedToken.return_value = "feed_123"
        MockSmartConnect.return_value = mock_obj

        from trading_bot.auth.login import authenticate, logout, is_logged_in
        authenticate()
        assert is_logged_in()

        logout()
        assert not is_logged_in()

    def test_logout_when_not_logged_in_is_noop(self):
        from trading_bot.auth.login import logout, is_logged_in
        assert not is_logged_in()
        logout()  # should not raise
        assert not is_logged_in()


class TestCachedSession:
    """Session caching behavior."""

    @patch("trading_bot.auth.login.SmartConnect")
    def test_second_call_returns_cached(self, MockSmartConnect, mock_env):
        from trading_bot import config
        config.ANGEL_API_KEY = os.environ["ANGEL_API_KEY"]
        config.ANGEL_CLIENT_ID = os.environ["ANGEL_CLIENT_ID"]
        config.ANGEL_PASSWORD = os.environ["ANGEL_PASSWORD"]
        config.ANGEL_TOTP_KEY = os.environ["ANGEL_TOTP_KEY"]

        mock_obj = MagicMock()
        mock_obj.generateSession.return_value = {
            "status": True,
            "data": {
                "jwtToken": "jwt_abc",
                "refreshToken": "ref_abc",
            },
        }
        mock_obj.getfeedToken.return_value = "feed_abc"
        MockSmartConnect.return_value = mock_obj

        from trading_bot.auth.login import authenticate
        s1 = authenticate()
        s2 = authenticate()
        assert s1 is s2
        # generateSession called only once
        mock_obj.generateSession.assert_called_once()
