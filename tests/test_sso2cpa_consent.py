from __future__ import annotations

import json

import pytest

from lib import sso2cpa_core


class _Cookies:
    def set(self, *_args, **_kwargs):
        return None


class _Response:
    def __init__(
        self,
        *,
        status_code: int,
        url: str,
        text: str = "",
        headers: dict | None = None,
        payload: dict | None = None,
    ):
        self.status_code = status_code
        self.url = url
        self.text = text
        self.headers = headers or {}
        self._payload = payload or {}

    def json(self):
        return dict(self._payload)


class _Session:
    def __init__(self):
        self.cookies = _Cookies()
        self.gets = []
        self.posts = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        if url.endswith("/_next/static/chunks/consent.js"):
            return _Response(
                status_code=200,
                url=url,
                text=(
                    'createServerReference)("'
                    "40f70c0441dc4df05d0b05491ce97492ef6e2a247d"
                    '",callServer,void 0,findSourceMapURL,'
                    '"submitOAuth2Consent")'
                ),
            )
        return _Response(
            status_code=200,
            url="https://accounts.x.ai/oauth2/consent?client_id=test",
            text=(
                '<html><script src="/_next/static/chunks/consent.js">'
                "</script></html>"
            ),
        )

    def post(self, url, *, data, **kwargs):
        self.posts.append((url, data, kwargs))
        if url.startswith("https://accounts.x.ai/oauth2/consent"):
            return _Response(
                status_code=200,
                url=url,
                text='0:{"a":"$@1"}\n1:{"success":true,"code":"authorization-code"}',
            )
        return _Response(
            status_code=200,
            url=url,
            payload={
                "access_token": "access-token",
                "refresh_token": "refresh-token",
            },
        )


class _RedirectDeniedSession(_Session):
    def post(self, url, *, data, **kwargs):
        self.posts.append((url, data, kwargs))
        if url.startswith("https://accounts.x.ai/oauth2/consent"):
            return _Response(
                status_code=303,
                url=url,
                headers={
                    "Location": (
                        "http://127.0.0.1:56121/callback"
                        "?error=access_denied"
                        "&error_description=Access%20denied"
                        "&state=opaque"
                    )
                },
            )
        raise AssertionError("token exchange must not run after consent denial")


class _TokenDeniedSession(_Session):
    def post(self, url, *, data, **kwargs):
        self.posts.append((url, data, kwargs))
        if url.startswith("https://accounts.x.ai/oauth2/consent"):
            return _Response(
                status_code=200,
                url=url,
                text='0:{"a":"$@1"}\n1:{"success":true,"code":"authorization-code"}',
            )
        return _Response(
            status_code=400,
            url=url,
            text='{"error":"invalid_grant","error_description":"Access denied"}',
            payload={
                "error": "invalid_grant",
                "error_description": "Access denied",
            },
        )


class _MissingAccessTokenSession(_Session):
    def post(self, url, *, data, **kwargs):
        self.posts.append((url, data, kwargs))
        if url.startswith("https://accounts.x.ai/oauth2/consent"):
            return _Response(
                status_code=200,
                url=url,
                text='0:{"a":"$@1"}\n1:{"success":true,"code":"authorization-code"}',
            )
        return _Response(
            status_code=200,
            url=url,
            payload={
                "refresh_token": "refresh-secret-must-not-leak",
                "id_token": "identity-secret-must-not-leak",
                "error": "missing_access_token",
                "error_description": "token response incomplete",
            },
        )


class _NonJsonResponse(_Response):
    def json(self):
        raise ValueError("synthetic non-JSON response")


class _NonJsonTokenSession(_Session):
    token_body = "private-token=secret-token-body-must-not-leak"

    def post(self, url, *, data, **kwargs):
        self.posts.append((url, data, kwargs))
        if url.startswith("https://accounts.x.ai/oauth2/consent"):
            return _Response(
                status_code=200,
                url=url,
                text='0:{"a":"$@1"}\n1:{"success":true,"code":"authorization-code"}',
            )
        return _NonJsonResponse(
            status_code=200,
            url=url,
            text=self.token_body,
        )


class _MissingConsentCodeSession(_Session):
    def post(self, url, *, data, **kwargs):
        self.posts.append((url, data, kwargs))
        if url.startswith("https://accounts.x.ai/oauth2/consent"):
            return _Response(
                status_code=200,
                url=url,
                text=(
                    "consent-body-with-private-token="
                    "secret-consent-token-must-not-leak"
                ),
            )
        raise AssertionError("token exchange must not run without code")


class _Clock:
    def __init__(self, now: float):
        self.now = now

    def monotonic(self) -> float:
        return self.now


class _AdvancingAuthorizeSession(_Session):
    def __init__(self, clock: _Clock):
        super().__init__()
        self.clock = clock

    def get(self, url, **kwargs):
        response = super().get(url, **kwargs)
        if url.startswith(sso2cpa_core.AUTHORIZE_URL):
            self.clock.now += 6
        return response


class _DeadlineExpiringScriptSession:
    def __init__(self, clock: _Clock):
        self.clock = clock

    def get(self, _url, **_kwargs):
        self.clock.now += 6
        raise RuntimeError("script transport failed")


class _TimedAcquireLock:
    def __init__(self):
        self.acquire_timeouts = []
        self.released = False

    def acquire(self, *, timeout):
        self.acquire_timeouts.append(timeout)
        return False

    def release(self):
        self.released = True


def test_extract_next_action_id_is_bound_to_consent_action():
    script = (
        'createServerReference)("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        'callServer,void 0,findSourceMapURL,"someOtherAction");'
        'createServerReference)("40f70c0441dc4df05d0b05491ce97492ef6e2a247d",'
        'callServer,void 0,findSourceMapURL,"submitOAuth2Consent")'
    )

    assert (
        sso2cpa_core._extract_next_action_id(script)
        == "40f70c0441dc4df05d0b05491ce97492ef6e2a247d"
    )


def test_consent_uses_current_next_server_action_protocol(monkeypatch):
    session = _Session()
    monkeypatch.setattr(sso2cpa_core, "new_session", lambda _proxy="": session)
    monkeypatch.setattr(sso2cpa_core, "_CONSENT_ACTION_ID_CACHE", "")

    token = sso2cpa_core.sso_to_token("header.payload.signature")

    assert token["access_token"] == "access-token"
    consent_url, consent_body, consent_kwargs = session.posts[0]
    assert consent_url.startswith("https://accounts.x.ai/oauth2/consent")
    assert json.loads(consent_body)[0]["action"] == "allow"
    assert json.loads(consent_body)[0]["principalType"] == "User"
    assert consent_kwargs["headers"]["Next-Action"] == (
        "40f70c0441dc4df05d0b05491ce97492ef6e2a247d"
    )
    assert consent_kwargs["headers"]["Next-Router-State-Tree"]
    assert "%22(app)%22" in consent_kwargs["headers"]["Next-Router-State-Tree"]
    assert "%28app%29" not in consent_kwargs["headers"]["Next-Router-State-Tree"]
    requested_urls = [url for url, _kwargs in session.gets]
    assert "https://accounts.x.ai/" in requested_urls


def test_consent_refreshes_cached_action_when_deployment_chunks_change(
    monkeypatch,
):
    session = _Session()
    stale_action_id = "a" * 40
    monkeypatch.setattr(sso2cpa_core, "new_session", lambda _proxy="": session)
    monkeypatch.setattr(
        sso2cpa_core,
        "_CONSENT_ACTION_ID_CACHE",
        stale_action_id,
    )
    monkeypatch.setattr(
        sso2cpa_core,
        "_CONSENT_ACTION_ID_CACHE_KEY",
        "stale-deployment",
        raising=False,
    )

    token = sso2cpa_core.sso_to_token("header.payload.signature")

    assert token["access_token"] == "access-token"
    assert session.posts[0][2]["headers"]["Next-Action"] != stale_action_id
    assert session.posts[0][2]["headers"]["Next-Action"] == (
        "40f70c0441dc4df05d0b05491ce97492ef6e2a247d"
    )
    assert any(
        url.endswith("/_next/static/chunks/consent.js")
        for url, _kwargs in session.gets
    )


def test_parse_consent_code_classifies_server_action_access_denied():
    with pytest.raises(
        sso2cpa_core.OAuthAuthorizationDenied
    ) as raised:
        sso2cpa_core.parse_consent_code(
            '0:{"a":"$@1"}\n'
            '1:{"success":false,"error":"Access denied"}',
            status_code=200,
        )

    assert raised.value.stage == "consent_action"
    assert raised.value.http_status == 200
    assert raised.value.oauth_error == "access_denied"
    assert raised.value.transport == "rsc"


def test_consent_redirect_access_denied_is_not_reported_as_missing_code(
    monkeypatch,
):
    session = _RedirectDeniedSession()
    monkeypatch.setattr(sso2cpa_core, "new_session", lambda _proxy="": session)
    monkeypatch.setattr(sso2cpa_core, "_CONSENT_ACTION_ID_CACHE", "")

    with pytest.raises(
        sso2cpa_core.OAuthAuthorizationDenied
    ) as raised:
        sso2cpa_core.sso_to_token("header.payload.signature")

    assert raised.value.stage == "consent_redirect"
    assert raised.value.http_status == 303
    assert raised.value.oauth_error == "access_denied"
    assert raised.value.transport == "redirect"


def test_token_access_denied_is_classified_without_leaking_response_body(
    monkeypatch,
):
    session = _TokenDeniedSession()
    monkeypatch.setattr(sso2cpa_core, "new_session", lambda _proxy="": session)
    monkeypatch.setattr(sso2cpa_core, "_CONSENT_ACTION_ID_CACHE", "")

    with pytest.raises(
        sso2cpa_core.OAuthAuthorizationDenied
    ) as raised:
        sso2cpa_core.sso_to_token("header.payload.signature")

    assert raised.value.stage == "token_exchange"
    assert raised.value.http_status == 400
    assert raised.value.oauth_error == "invalid_grant"
    assert raised.value.transport == "json"
    assert "Access denied" in str(raised.value)


def test_missing_access_token_error_does_not_dump_token_payload(monkeypatch):
    session = _MissingAccessTokenSession()
    monkeypatch.setattr(sso2cpa_core, "new_session", lambda _proxy="": session)
    monkeypatch.setattr(sso2cpa_core, "_CONSENT_ACTION_ID_CACHE", "")

    with pytest.raises(sso2cpa_core.ConvertError) as raised:
        sso2cpa_core.sso_to_token("header.payload.signature")

    message = str(raised.value)
    assert "missing_access_token" in message
    assert "token response incomplete" in message
    assert "refresh-secret-must-not-leak" not in message
    assert "identity-secret-must-not-leak" not in message


def test_non_json_token_error_does_not_dump_response_body(monkeypatch):
    session = _NonJsonTokenSession()
    monkeypatch.setattr(sso2cpa_core, "new_session", lambda _proxy="": session)
    monkeypatch.setattr(sso2cpa_core, "_CONSENT_ACTION_ID_CACHE", "")

    with pytest.raises(sso2cpa_core.ConvertError) as raised:
        sso2cpa_core.sso_to_token("header.payload.signature")

    message = str(raised.value)
    assert session.token_body not in message
    assert "status=200" in message
    assert f"body_length={len(session.token_body)}" in message
    assert "transport=non-json" in message


def test_missing_consent_code_does_not_log_or_dump_response_body(
    tmp_path, monkeypatch
):
    secret = "secret-consent-token-must-not-leak"
    session = _MissingConsentCodeSession()
    monkeypatch.setattr(sso2cpa_core, "new_session", lambda _proxy="": session)
    monkeypatch.setattr(sso2cpa_core, "_CONSENT_ACTION_ID_CACHE", "")
    monkeypatch.setattr(sso2cpa_core, "_DEBUG_CONSENT_HTML", False)
    monkeypatch.setattr(
        sso2cpa_core.tempfile,
        "gettempdir",
        lambda: str(tmp_path),
    )

    with pytest.raises(sso2cpa_core.ConvertError) as raised:
        sso2cpa_core.sso_to_token("header.payload.signature")

    assert secret not in str(raised.value)
    assert not (tmp_path / "sso2cpa_consent_debug.html").exists()


def test_deadline_caps_every_oauth_request_timeout(monkeypatch):
    session = _Session()
    clock = _Clock(100.0)
    monkeypatch.setattr(sso2cpa_core, "new_session", lambda _proxy="": session)
    monkeypatch.setattr(sso2cpa_core, "_CONSENT_ACTION_ID_CACHE", "")
    monkeypatch.setattr(sso2cpa_core.time, "monotonic", clock.monotonic)

    token = sso2cpa_core.sso_to_token(
        "header.payload.signature",
        deadline=105.0,
    )

    assert token["access_token"] == "access-token"
    assert [kwargs["timeout"] for _url, kwargs in session.gets] == [
        5.0,
        5.0,
        5.0,
    ]
    assert [kwargs["timeout"] for _url, _data, kwargs in session.posts] == [
        5.0,
        5.0,
    ]


def test_deadline_is_checked_after_authorize_request(monkeypatch):
    clock = _Clock(100.0)
    session = _AdvancingAuthorizeSession(clock)
    monkeypatch.setattr(sso2cpa_core, "new_session", lambda _proxy="": session)
    monkeypatch.setattr(sso2cpa_core.time, "monotonic", clock.monotonic)

    with pytest.raises(
        sso2cpa_core.ConversionDeadlineExceeded
    ) as raised:
        sso2cpa_core.sso_to_token(
            "header.payload.signature",
            deadline=105.0,
        )

    assert raised.value.stage == "authorize"


def test_tiny_remaining_deadline_is_not_passed_as_zero_like_timeout(
    monkeypatch,
):
    clock = _Clock(100.0)
    session = _Session()
    monkeypatch.setattr(sso2cpa_core, "new_session", lambda _proxy="": session)
    monkeypatch.setattr(sso2cpa_core.time, "monotonic", clock.monotonic)

    with pytest.raises(
        sso2cpa_core.ConversionDeadlineExceeded
    ) as raised:
        sso2cpa_core.sso_to_token(
            "header.payload.signature",
            deadline=100.01,
        )

    assert raised.value.stage == "authorize"
    assert session.gets == []


def test_consent_action_lock_uses_remaining_deadline(monkeypatch):
    clock = _Clock(100.0)
    lock = _TimedAcquireLock()
    monkeypatch.setattr(sso2cpa_core, "_CONSENT_ACTION_ID_CACHE", "")
    monkeypatch.setattr(sso2cpa_core, "_CONSENT_ACTION_ID_LOCK", lock)
    monkeypatch.setattr(sso2cpa_core.time, "monotonic", clock.monotonic)

    with pytest.raises(
        sso2cpa_core.ConversionDeadlineExceeded
    ) as raised:
        sso2cpa_core._discover_consent_action_id(
            _Session(),
            "<html></html>",
            "https://accounts.x.ai/oauth2/consent",
            deadline=103.0,
        )

    assert raised.value.stage == "consent_action_lock"
    assert lock.acquire_timeouts == [3.0]
    assert lock.released is False


def test_script_error_does_not_hide_expired_deadline(monkeypatch):
    clock = _Clock(100.0)
    session = _DeadlineExpiringScriptSession(clock)
    monkeypatch.setattr(sso2cpa_core, "_CONSENT_ACTION_ID_CACHE", "")
    monkeypatch.setattr(sso2cpa_core.time, "monotonic", clock.monotonic)

    with pytest.raises(
        sso2cpa_core.ConversionDeadlineExceeded
    ) as raised:
        sso2cpa_core._discover_consent_action_id(
            session,
            '<script src="/_next/static/chunks/consent.js"></script>',
            "https://accounts.x.ai/oauth2/consent",
            deadline=105.0,
        )

    assert raised.value.stage == "consent_script"


def test_convert_one_propagates_keyword_only_deadline(monkeypatch):
    seen = {}

    def fake_sso_to_token(sso, proxy="", *, deadline=None):
        seen.update(sso=sso, proxy=proxy, deadline=deadline)
        return {"access_token": "access-token"}

    monkeypatch.setattr(sso2cpa_core, "sso_to_token", fake_sso_to_token)
    monkeypatch.setattr(
        sso2cpa_core,
        "token_to_cpa_entry",
        lambda token, sso, email_hint="": {
            "token": token,
            "sso": sso,
            "email": email_hint,
        },
    )

    entry = sso2cpa_core.convert_one(
        "header.payload.signature",
        email="user@example.test",
        proxy="http://proxy.test",
        deadline=123.0,
    )

    assert seen["deadline"] == 123.0
    assert entry["email"] == "user@example.test"
