"""The cross-process auth bridge must gate on the TOKEN, not on the file's age.

Every test is PURE: no network, no browser. File I/O is confined to tmp_path.

THE DEFECT, MEASURED 2026-08-23
-------------------------------
`get_auth_state()` rejected any `auth_state.json` older than 300 seconds. But
that file is written ONLY by `TokenManager._export_auth_state`, whose only
caller is `extract()`, which runs only on a token refresh -- and the token
lives 3600 seconds. `get_token()` short-circuits on the cached token, so
nothing rewrites the file in between.

So the file is fresh for 300s of every 3600s cycle and stale for the other
3300. An agent measured it directly: `exported_at` 2127s old while the token
it held had 1469s of life left and worked. The bridge -- the whole mechanism
by which a second process reaches Naukri without fighting for the Chrome
profile lock -- was therefore unusable 91.7% of the time, and its error said
"stale" about a credential that was fine.

File age was always a PROXY for "is the token still good". The token carries
the answer in its own `exp` claim, so ask that instead, and keep the proxy
only for a token whose claim will not decode.
"""

import json
import time

import pytest


def _jwt(exp):
    """A structurally valid unsigned JWT carrying `exp`. Not a credential."""
    import base64

    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return "%s.%s.%s" % (seg({"alg": "HS256", "typ": "JWT"}), seg({"exp": exp}), "sig")


def _write_state(tmp_path, monkeypatch, token, age_seconds):
    from naukri_server import auth_bridge

    target = tmp_path / "auth_state.json"
    target.write_text(json.dumps({
        "token": token,
        "cookies": "nauk_at=x",
        "exported_at": time.time() - age_seconds,
        "cdp_port": 9223,
    }))
    monkeypatch.setattr(auth_bridge, "_AUTH_STATE_FILE", target)
    return target


class TestALiveTokenInAnOldFileIsUsable:

    def test_old_file_live_token_is_accepted(self, tmp_path, monkeypatch):
        """THE RED ONE. The exact state measured: file 2127s old, token good.

        Before the fix this raised ValueError("Auth state is 2127s old").
        """
        from naukri_server.auth_bridge import get_auth_state

        _write_state(tmp_path, monkeypatch,
                     token=_jwt(time.time() + 1469), age_seconds=2127)
        state = get_auth_state()
        assert state["cookies"] == "nauk_at=x"

    def test_headers_are_built_from_that_state(self, tmp_path, monkeypatch):
        """The caller's actual entry point, not just the reader beneath it."""
        from naukri_server.auth_bridge import get_auth_headers

        token = _jwt(time.time() + 1469)
        _write_state(tmp_path, monkeypatch, token=token, age_seconds=2127)
        headers = get_auth_headers()
        assert headers["Authorization"] == "Bearer %s" % token
        assert headers["cookie"] == "nauk_at=x"


class TestADeadTokenIsRejectedHoweverFreshTheFile:
    """The controls. A gate that only ever opens is not a gate."""

    def test_expired_token_in_a_brand_new_file_is_rejected(self, tmp_path, monkeypatch):
        """The false-POSITIVE the old age-gate could not see.

        Age said 5 seconds, so the old code handed back a token that had
        already expired. Freshness of the file was never the question.
        """
        from naukri_server.auth_bridge import get_auth_state

        _write_state(tmp_path, monkeypatch,
                     token=_jwt(time.time() - 60), age_seconds=5)
        with pytest.raises(ValueError, match="expired"):
            get_auth_state()

    def test_token_expiring_within_the_margin_is_rejected(self, tmp_path, monkeypatch):
        """Do not hand out a credential that dies mid-sweep."""
        from naukri_server.auth_bridge import get_auth_state

        _write_state(tmp_path, monkeypatch,
                     token=_jwt(time.time() + 30), age_seconds=5)
        with pytest.raises(ValueError):
            get_auth_state()

    def test_missing_file_still_raises(self, tmp_path, monkeypatch):
        from naukri_server import auth_bridge
        from naukri_server.auth_bridge import get_auth_state

        monkeypatch.setattr(auth_bridge, "_AUTH_STATE_FILE", tmp_path / "nope.json")
        with pytest.raises(FileNotFoundError):
            get_auth_state()

    def test_state_with_no_token_is_rejected(self, tmp_path, monkeypatch):
        from naukri_server import auth_bridge
        from naukri_server.auth_bridge import get_auth_state

        target = tmp_path / "auth_state.json"
        target.write_text(json.dumps({"cookies": "c=v", "exported_at": time.time()}))
        monkeypatch.setattr(auth_bridge, "_AUTH_STATE_FILE", target)
        with pytest.raises(ValueError):
            get_auth_state()


class TestTheAgeProxySurvivesOnlyWhereTheClaimDoesNot:

    def test_undecodable_exp_falls_back_to_the_age_check(self, tmp_path, monkeypatch):
        """With no readable `exp` there is no better signal than file age.

        Keeping the old behaviour here is deliberate: the fallback is the
        weaker instrument, so it applies only where the strong one is blind.
        """
        from naukri_server.auth_bridge import get_auth_state

        _write_state(tmp_path, monkeypatch, token="not-a-jwt", age_seconds=2127)
        with pytest.raises(ValueError, match="old"):
            get_auth_state()

    def test_undecodable_exp_but_fresh_file_is_accepted(self, tmp_path, monkeypatch):
        from naukri_server.auth_bridge import get_auth_state

        _write_state(tmp_path, monkeypatch, token="not-a-jwt", age_seconds=10)
        assert get_auth_state()["token"] == "not-a-jwt"

    def test_the_error_says_which_gate_refused(self, tmp_path, monkeypatch):
        """A bridge that says "stale" about a live token cost an agent a run."""
        from naukri_server.auth_bridge import get_auth_state

        _write_state(tmp_path, monkeypatch,
                     token=_jwt(time.time() - 60), age_seconds=5)
        with pytest.raises(ValueError) as caught:
            get_auth_state()
        message = str(caught.value).lower()
        assert "token" in message
        assert "old" not in message, (
            "an expired token must not be reported as a stale FILE: %s" % message
        )

    def test_the_error_never_carries_the_token(self, tmp_path, monkeypatch):
        """This file holds the operator's live credential and the cookie header."""
        from naukri_server.auth_bridge import get_auth_state

        token = _jwt(time.time() - 60)
        _write_state(tmp_path, monkeypatch, token=token, age_seconds=5)
        with pytest.raises(ValueError) as caught:
            get_auth_state()
        message = str(caught.value)
        assert token not in message
        assert "nauk_at=x" not in message
        for segment in token.split("."):
            assert segment not in message, "a token segment leaked into the error"
