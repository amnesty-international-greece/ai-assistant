"""Brevo client: campaign audience, explicit-only live sends, readiness check."""
from __future__ import annotations

import httpx
import pytest

import src.integrations.brevo as brevo_mod
from src.integrations.brevo import BrevoClient, build_recipients


class _FakeClient:
    """Minimal httpx.AsyncClient stand-in routing (METHOD, path) to responses."""

    def __init__(self, routes, calls):
        self.routes, self.calls = routes, calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _do(self, method, url, **kwargs):
        path = url.replace(brevo_mod._BREVO_API_BASE, "")
        self.calls.append((method, path, kwargs.get("json"), kwargs.get("params")))
        status, payload = self.routes.get((method, path), (404, {"message": "not found"}))
        return httpx.Response(status, json=payload, request=httpx.Request(method, url))

    async def get(self, url, **kw):
        return await self._do("GET", url, **kw)

    async def post(self, url, **kw):
        return await self._do("POST", url, **kw)

    async def put(self, url, **kw):
        return await self._do("PUT", url, **kw)

    async def delete(self, url, **kw):
        return await self._do("DELETE", url, **kw)


@pytest.fixture
def fake_http(monkeypatch):
    routes, calls = {}, []
    monkeypatch.setattr(brevo_mod.httpx, "AsyncClient",
                        lambda *a, **k: _FakeClient(routes, calls))
    monkeypatch.setattr(brevo_mod, "log_action", lambda **kw: None)
    return routes, calls


# -- audience ------------------------------------------------------------------


def test_build_recipients_lists_segments_and_both():
    assert build_recipients([82], []) == {"listIds": [82]}
    assert build_recipients([], [1]) == {"segmentIds": [1]}
    assert build_recipients([82], [1]) == {"listIds": [82], "segmentIds": [1]}
    with pytest.raises(ValueError):
        build_recipients([], [])


async def test_campaign_targets_a_segment_and_sends_only_a_test(fake_http):
    routes, calls = fake_http
    routes[("GET", "/smtp/templates/5")] = (200, {"htmlContent": "<p>[X]</p>"})
    routes[("POST", "/emailCampaigns")] = (201, {"id": 7})
    routes[("POST", "/emailCampaigns/7/sendTest")] = (204, None)

    result = await BrevoClient().send_campaign(
        template_id=5, list_ids=[], segment_ids=[1], subject="s",
        params={"[X]": "y"}, test_emails=["t@example.org"],
    )

    assert result == {"campaign_id": 7, "test": True}
    create = next(c for c in calls if c[:2] == ("POST", "/emailCampaigns"))
    assert create[2]["recipients"] == {"segmentIds": [1]}
    assert create[2]["htmlContent"] == "<p>y</p>"
    assert not any(c[1].endswith("/sendNow") for c in calls)


async def test_campaign_without_test_address_is_only_a_draft(fake_http):
    """Regression: an empty test address used to trigger a LIVE send."""
    routes, calls = fake_http
    routes[("GET", "/smtp/templates/5")] = (200, {"htmlContent": "<p/>"})
    routes[("POST", "/emailCampaigns")] = (201, {"id": 8})

    result = await BrevoClient().send_campaign(template_id=5, list_ids=[82], subject="s")

    assert result == {"campaign_id": 8, "test": False}
    assert not any(c[1].endswith(("/sendNow", "/sendTest")) for c in calls)


async def test_campaign_with_no_audience_fails_before_any_request(fake_http):
    _, calls = fake_http
    with pytest.raises(ValueError):
        await BrevoClient().send_campaign(template_id=5, list_ids=[], subject="s")
    assert calls == []


# -- preflight -----------------------------------------------------------------


def _healthy(routes):
    routes[("GET", "/account")] = (200, {"email": "x"})
    routes[("GET", "/smtp/templates/234")] = (200, {"id": 234})
    routes[("GET", "/senders")] = (
        200, {"senders": [{"email": "members@example.org", "active": True}]}
    )
    routes[("GET", "/contacts/segments")] = (200, {"segments": [
        {"id": 1, "segmentName": "Regular members"},
        {"id": 3, "segmentName": "Candidates"},
    ]})
    routes[("GET", "/contacts/lists/82")] = (
        200, {"id": 82, "name": "Register", "uniqueSubscribers": 484}
    )


async def _check(**kw):
    args = dict(template_id=234, list_ids=[], segment_ids=[1],
                sender_email="members@example.org")
    args.update(kw)
    return await BrevoClient().preflight(**args)


async def test_preflight_passes_and_names_the_audience(fake_http):
    routes, _ = fake_http
    _healthy(routes)
    report = await _check()
    assert report["ok"] and report["problems"] == []
    assert any("Regular members" in a for a in report["audience"])


async def test_preflight_reports_unauthorised_ip_and_stops(fake_http):
    routes, calls = fake_http
    routes[("GET", "/account")] = (401, {
        "code": "unauthorized",
        "message": "We have detected you are using an unrecognised IP address 192.0.2.1",
    })
    report = await _check()
    assert not report["ok"]
    assert len(report["problems"]) == 1
    assert "authorised_ips" in report["problems"][0]
    assert [c[1] for c in calls] == ["/account"]


async def test_preflight_catches_a_segment_id_configured_as_a_list(fake_http):
    """The invitation bug: segment 1 configured under newsletter_list_ids."""
    routes, _ = fake_http
    _healthy(routes)
    report = await _check(list_ids=[1], segment_ids=[])
    assert not report["ok"]
    assert any("List 1" in p and "segment" in p for p in report["problems"])


async def test_preflight_catches_missing_segment_template_and_unverified_sender(fake_http):
    routes, _ = fake_http
    _healthy(routes)
    routes[("GET", "/smtp/templates/234")] = (404, {"message": "Template not found"})
    routes[("GET", "/senders")] = (
        200, {"senders": [{"email": "members@example.org", "active": False}]}
    )
    report = await _check(segment_ids=[99])
    problems = " | ".join(report["problems"])
    assert "Template 234" in problems
    assert "Segment 99" in problems
    assert "not verified" in problems


async def test_preflight_flags_a_missing_audience(fake_http):
    routes, _ = fake_http
    _healthy(routes)
    report = await _check(segment_ids=[])
    assert not report["ok"]
    assert any("audience" in p for p in report["problems"])
