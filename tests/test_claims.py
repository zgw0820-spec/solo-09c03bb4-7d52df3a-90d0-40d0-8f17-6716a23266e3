"""Ownership claims: primary registration, backup takeover, and confirmation gating."""

from __future__ import annotations

import asyncio
import time

from tests.conftest import ApiHarness
from tests.helpers import _get, _status


async def _submit_and_accept(api: ApiHarness, alarm_no: str, deadline: int = 10) -> str:
    submit = await api.client.post("/alarms", json=api.alarm_payload(alarm_no, deadline=deadline))
    assert submit.status_code == 201, submit.text
    primary_no = submit.json()["notifications"][0]["notification_no"]
    assert await api.wait_until(lambda: _status(api, alarm_no, "awaiting_confirmation"), timeout=8)
    return primary_no


async def _escalate(api: ApiHarness, alarm_no: str) -> str:
    """Wait for the single backup notification to be accepted and return its number."""

    async def backup_no() -> str | None:
        view = (await api.client.get(f"/alarms/{alarm_no}")).json()
        for n in view["notifications"]:
            if n["role"] == "backup":
                return n["notification_no"]
        return None

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        no = await backup_no()
        if no is not None:
            assert await api.wait_until(
                lambda: _backup_state(api, alarm_no, "accepted"), timeout=8
            ), await _get(api, alarm_no)
            return no
        await asyncio.sleep(0.03)
    raise AssertionError(f"alarm {alarm_no} never escalated: {await _get(api, alarm_no)}")


async def _backup_state(api: ApiHarness, alarm_no: str, state: str) -> bool:
    response = await api.client.get(f"/alarms/{alarm_no}")
    if response.status_code != 200:
        return False
    return any(
        n["role"] == "backup" and n["state"] == state
        for n in response.json()["notifications"]
    )


async def _claim(api: ApiHarness, alarm_no: str, notification_no: str, by: str):
    return await api.client.post(
        f"/alarms/{alarm_no}/notifications/{notification_no}/claim",
        json={"claimed_by": by},
    )


async def test_primary_can_claim_only_after_gateway_acceptance(api: ApiHarness):
    api.fake.drop_receipt = True
    api.fake.drop_hold_seconds = 0.4
    api.fake.lookup_down = True
    await api.client.post("/alarms", json=api.alarm_payload("CLM-UNK", deadline=10))
    assert await api.wait_until(lambda: _status(api, "CLM-UNK", "primary_unconfirmed"), timeout=6)
    view = (await api.client.get("/alarms/CLM-UNK")).json()
    notification_no = view["notifications"][0]["notification_no"]

    # An ambiguous receipt is not proof of delivery: no claim is allowed.
    denied = await _claim(api, "CLM-UNK", notification_no, "张工")
    assert denied.status_code == 409, denied.text
    assert (await api.client.get("/alarms/CLM-UNK")).json()["claimed"] is False

    # Once the receipt is confirmed, the primary may register.
    api.fake.drop_receipt = False
    api.fake.lookup_down = False
    assert await api.wait_until(
        lambda: _status(api, "CLM-UNK", "awaiting_confirmation"), timeout=8
    )
    claimed = await _claim(api, "CLM-UNK", notification_no, "张工")
    assert claimed.status_code == 201, claimed.text
    body = claimed.json()
    assert body["role"] == "primary" and body["reused"] is False
    assert body["claim_token"]


async def test_claim_requires_current_credential_and_shows_owner_without_token(api: ApiHarness):
    primary_no = await _submit_and_accept(api, "CLM-2")
    claimed = (await _claim(api, "CLM-2", primary_no, "张工")).json()
    token = claimed["claim_token"]

    # Unclaimed-compatible confirmation now requires the token.
    missing = await api.client.post("/alarms/CLM-2/confirm", json={"confirmed_by": "张工"})
    assert missing.status_code == 403, missing.text
    wrong = await api.client.post(
        "/alarms/CLM-2/confirm", json={"confirmed_by": "张工", "claim_token": "nope"}
    )
    assert wrong.status_code == 403, wrong.text

    # Status exposes the handler but never the credential.
    view = (await api.client.get("/alarms/CLM-2")).json()
    assert view["claimed"] is True
    assert view["claimed_by"] == "张工"
    assert view["claim_role"] == "primary"
    assert view["taken_over"] is False
    assert token not in (await api.client.get("/alarms/CLM-2")).text
    assert token not in (await api.client.get("/alarms/CLM-2/notifications")).text
    assert all("claim_token" not in c for c in view["claims"])

    ok = await api.client.post(
        "/alarms/CLM-2/confirm", json={"confirmed_by": "张工", "claim_token": token}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "confirmed"


async def test_repeating_the_same_claim_reuses_the_credential(api: ApiHarness):
    primary_no = await _submit_and_accept(api, "CLM-3")
    first = await _claim(api, "CLM-3", primary_no, "张工")
    second = await _claim(api, "CLM-3", primary_no, "张工")
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["reused"] is True
    assert second.json()["claim_token"] == first.json()["claim_token"]
    assert len((await api.client.get("/alarms/CLM-3")).json()["claims"]) == 1


async def test_backup_takeover_invalidates_primary_credential(api: ApiHarness):
    primary_no = await _submit_and_accept(api, "CLM-4", deadline=2)
    primary_claim = (await _claim(api, "CLM-4", primary_no, "主班 张工")).json()
    backup_no = await _escalate(api, "CLM-4")

    takeover = await _claim(api, "CLM-4", backup_no, "备班 李工")
    assert takeover.status_code == 201, takeover.text
    backup_token = takeover.json()["claim_token"]
    assert takeover.json()["role"] == "backup"

    view = (await api.client.get("/alarms/CLM-4")).json()
    assert view["taken_over"] is True
    assert view["claimed_by"] == "备班 李工"
    assert view["claim_role"] == "backup"
    assert [c["role"] for c in view["claims"]] == ["primary", "backup"]
    assert view["claims"][0]["active"] is False
    assert view["claims"][1]["active"] is True

    # The old credential is immediately dead.
    stale = await api.client.post(
        "/alarms/CLM-4/confirm",
        json={"confirmed_by": "主班 张工", "claim_token": primary_claim["claim_token"]},
    )
    assert stale.status_code == 403, stale.text
    assert (await api.client.get("/alarms/CLM-4")).json()["confirmed"] is False

    # The primary cannot claim again after the takeover.
    re_claim = await _claim(api, "CLM-4", primary_no, "主班 张工")
    assert re_claim.status_code == 409, re_claim.text

    # The backup holder can confirm.
    confirmed = await api.client.post(
        "/alarms/CLM-4/confirm",
        json={"confirmed_by": "备班 李工", "claim_token": backup_token},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["confirmed"] is True


async def test_claim_by_notification_of_another_alarm_is_not_found(api: ApiHarness):
    primary_no = await _submit_and_accept(api, "CLM-5")
    await _submit_and_accept(api, "CLM-6")
    response = await _claim(api, "CLM-6", primary_no, "张工")
    assert response.status_code == 404, response.text
    assert (await _claim(api, "NOPE", primary_no, "张工")).status_code == 404


async def test_claimed_alarm_cannot_be_taken_over_after_confirmation(api: ApiHarness):
    primary_no = await _submit_and_accept(api, "CLM-7", deadline=2)
    backup_no = await _escalate(api, "CLM-7")
    token = (await _claim(api, "CLM-7", primary_no, "主班")).json()["claim_token"]
    assert (
        await api.client.post(
            "/alarms/CLM-7/confirm", json={"confirmed_by": "主班", "claim_token": token}
        )
    ).status_code == 200

    late = await _claim(api, "CLM-7", backup_no, "备班")
    assert late.status_code == 409, late.text
    view = (await api.client.get("/alarms/CLM-7")).json()
    assert view["confirmed"] is True
    assert view["taken_over"] is False


async def test_unclaimed_alarm_still_confirms_without_credential(api: ApiHarness):
    """Regression: the original confirmation path is unchanged for unclaimed alarms."""
    await _submit_and_accept(api, "CLM-8")
    response = await api.client.post("/alarms/CLM-8/confirm", json={"confirmed_by": "张工"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "confirmed"
    assert response.json()["claimed"] is False


async def test_backup_cannot_be_claimed_before_it_is_accepted(api: ApiHarness):
    api.fake.slow_seconds = 3.0
    await api.client.post("/alarms", json=api.alarm_payload("CLM-9", deadline=2))
    assert await api.wait_until(
        lambda: _status(api, "CLM-9", "awaiting_confirmation"), timeout=10
    )
    # The backup row exists (escalated) but its POST is still on the wire.
    assert await api.wait_until(
        lambda: _has_backup_state(api, "CLM-9", {"pending", "in_flight"}), timeout=10
    ), await _get(api, "CLM-9")
    view = (await api.client.get("/alarms/CLM-9")).json()
    backup_no = next(n["notification_no"] for n in view["notifications"] if n["role"] == "backup")
    denied = await _claim(api, "CLM-9", backup_no, "备班")
    assert denied.status_code == 409, denied.text
    api.fake.slow_seconds = 0.0


async def test_takeover_and_confirmation_are_arbitrated_on_one_committed_state(api: ApiHarness):
    """Race a backup takeover against a confirmation using the (still current)
    primary credential. Exactly one of them may win; both cannot."""
    service = api.app.state.service
    for index in range(6):
        alarm_no = f"CLM-RACE-{index}"
        primary_no = await _submit_and_accept(api, alarm_no, deadline=2)
        token = (await _claim(api, alarm_no, primary_no, "主班")).json()["claim_token"]
        backup_no = await _escalate(api, alarm_no)

        loop = asyncio.get_running_loop()
        results = await asyncio.gather(
            loop.run_in_executor(
                None,
                lambda name=alarm_no, t=token: _try(
                    service.confirm, name, "主班", None, t
                ),
            ),
            loop.run_in_executor(
                None,
                lambda name=alarm_no, no=backup_no: _try(
                    service.claim, name, no, "备班", None
                ),
            ),
        )
        confirm_ok, claim_ok = results
        assert confirm_ok != claim_ok, (
            f"both confirmation and takeover succeeded for {alarm_no}"
        )
        view = (await api.client.get(f"/alarms/{alarm_no}")).json()
        if confirm_ok:
            assert view["confirmed"] is True and view["taken_over"] is False
            assert not any(c["active"] and c["role"] == "backup" for c in view["claims"])
        else:
            assert view["taken_over"] is True and view["confirmed"] is False
            # The invalidated primary token must not confirm afterwards.
            denied = await api.client.post(
                f"/alarms/{alarm_no}/confirm",
                json={"confirmed_by": "主班", "claim_token": token},
            )
            assert denied.status_code == 403, denied.text


def _try(func, *args):
    try:
        func(*args)
        return True
    except Exception:  # noqa: BLE001 - the race loser raises by design
        return False


async def test_claim_survives_restart(tmp_path):
    """A claim is persisted; after a restart the same credential still owns the
    alarm and the backup takeover still holds."""
    import httpx

    from app.config import Settings
    from app.main import create_app
    from tests.fake_gateway import FakeGateway, build_fake_gateway_app

    fake = FakeGateway()
    gateway_url = "http://gw.test"
    gateway_app = build_fake_gateway_app(fake)
    db_path = str(tmp_path / "claims.db")

    class Restart:
        async def start(self):
            settings = Settings(
                database_path=db_path,
                gateway_url=gateway_url,
                post_timeout_seconds=1.0,
                lookup_timeout_seconds=0.5,
                worker_interval_seconds=0.02,
                delivery_lease_seconds=1.0,
                max_backoff_seconds=0.05,
                reconcile_on_read=True,
            )
            self.app = create_app(settings)
            self._lifespan = self.app.router.lifespan_context(self.app)
            await self._lifespan.__aenter__()
            self.app.state.gateway._client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway_app), base_url=gateway_url
            )
            self.client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://api.test"
            )
            return self

        async def stop(self):
            await self.client.aclose()
            await self._lifespan.__aexit__(None, None, None)

    first = await Restart().start()
    try:
        submit = await first.client.post(
            "/alarms",
            json={
                "alarm_no": "CLM-RST",
                "content": "供电柜 C01 温度 79°C",
                "primary_contact": {"name": "主班"},
                "backup_contact": {"name": "备班"},
                "ack_deadline_seconds": 10,
            },
        )
        primary_no = submit.json()["notifications"][0]["notification_no"]
        for _ in range(250):
            view = (await first.client.get("/alarms/CLM-RST")).json()
            if view["primary_accepted_at"] is not None:
                break
            await asyncio.sleep(0.02)
        claim = await first.client.post(
            f"/alarms/CLM-RST/notifications/{primary_no}/claim",
            json={"claimed_by": "主班 张工"},
        )
        assert claim.status_code == 201, claim.text
        token = claim.json()["claim_token"]
    finally:
        await first.stop()

    second = await Restart().start()
    try:
        view = (await second.client.get("/alarms/CLM-RST")).json()
        assert view["claimed"] is True
        assert view["claimed_by"] == "主班 张工"
        assert token not in (await second.client.get("/alarms/CLM-RST")).text

        denied = await second.client.post(
            "/alarms/CLM-RST/confirm", json={"confirmed_by": "主班 张工"}
        )
        assert denied.status_code == 403
        ok = await second.client.post(
            "/alarms/CLM-RST/confirm",
            json={"confirmed_by": "主班 张工", "claim_token": token},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["confirmed"] is True
    finally:
        await second.stop()


async def test_late_acceptance_after_backup_claimed_does_not_revoke_claim(api: ApiHarness):
    """The backup notification was posted late (original overdue delivery in
    flight), the receipt only resolves afterwards. This must not disturb the
    mutually exclusive confirmed / taken-over outcome."""
    api.fake.slow_seconds = 0.5
    await api.client.post("/alarms", json=api.alarm_payload("CLM-LATE", deadline=2))
    assert await api.wait_until(
        lambda: _status(api, "CLM-LATE", "awaiting_confirmation"), timeout=8
    )
    primary_no = next(
        n["notification_no"]
        for n in (await api.client.get("/alarms/CLM-LATE")).json()["notifications"]
        if n["role"] == "primary"
    )
    await _claim(api, "CLM-LATE", primary_no, "主班")
    backup_no = await _escalate(api, "CLM-LATE")
    takeover = await _claim(api, "CLM-LATE", backup_no, "备班")
    assert takeover.status_code == 201, takeover.text
    # The claim remains the current one; no later state change revokes it.
    view = (await api.client.get("/alarms/CLM-LATE")).json()
    assert view["taken_over"] is True and view["confirmed"] is False
    assert [c["active"] for c in view["claims"]] == [False, True]


async def test_backup_can_take_over_without_a_prior_primary_claim(api: ApiHarness):
    """If the primary never claimed, the backup may still take ownership of an
    accepted backup notification; the primary is then locked out."""
    primary_no = await _submit_and_accept(api, "CLM-10", deadline=2)
    backup_no = await _escalate(api, "CLM-10")
    takeover = await _claim(api, "CLM-10", backup_no, "备班")
    assert takeover.status_code == 201, takeover.text
    assert takeover.json()["role"] == "backup"

    primary_attempt = await _claim(api, "CLM-10", primary_no, "主班")
    assert primary_attempt.status_code == 409, primary_attempt.text
    view = (await api.client.get("/alarms/CLM-10")).json()
    assert view["taken_over"] is True
    assert [c["active"] for c in view["claims"]] == [True]


async def _has_backup_state(api: ApiHarness, alarm_no: str, states: set[str]) -> bool:
    response = await api.client.get(f"/alarms/{alarm_no}")
    if response.status_code != 200:
        return False
    return any(
        n["role"] == "backup" and n["state"] in states
        for n in response.json()["notifications"]
    )
