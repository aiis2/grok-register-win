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
