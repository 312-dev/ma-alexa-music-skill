"""The setup operations, called directly.

These were written against the Flask wizard's routes, because that was the only
way to reach the code: creating a skill and creating the catalogs lived inside
view functions. The behaviour they cover is unchanged and the wizard is gone,
so they now call the operations instead of posting forms at them.

Everything here is about Amazon's habit of answering 200 to a request that did
not do what it says. A skill id comes back before validation has run, a catalog
upload succeeds and silently unbinds the provider slot, an enablement reports
enabled for a skill that never receives a directive. Each test names the shape
it is holding the line on.
"""

from __future__ import annotations

import time

import pytest

from ma_provider import setup_ops
from ma_provider import setup_state as store
from ma_provider import setup_steps
from ma_provider import smapi_rest


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    from ma_provider import core

    monkeypatch.setattr(store, "STATE_DIR", tmp_path)
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "captures")
    monkeypatch.setattr(core, "PUBLIC_BASE", "https://ampere.example.test")
    monkeypatch.setenv("SKILL_ID", "")
    monkeypatch.setattr(smapi_rest, "connected", lambda: True)
    setup_steps._SKILL_CHECK.update(at=0.0, id="", exists=True)
    # No test here may reach Amazon. Anything not explicitly replaced should
    # fail loudly rather than open a socket.
    for name in ("create_skill", "delete_skill", "create_catalog",
                 "associate_catalog", "list_catalogs", "list_skills",
                 "skill_status", "upload_catalog", "delete_catalog"):
        monkeypatch.setattr(smapi_rest, name, _forbidden(name))
    monkeypatch.setattr(setup_ops, "manifest_verdict", lambda *a, **k: "")
    return tmp_path


def _forbidden(name):
    def refuse(*_args, **_kwargs):
        raise AssertionError(f"{name} reached the network")
    return refuse


def passing_endpoint():
    store.update(endpoint_ok=True, alias="ampere", cert_type="Trusted")


# --- creating the skill -----------------------------------------------------


def test_a_skill_is_created_once_the_endpoint_passes(monkeypatch):
    passing_endpoint()
    monkeypatch.setattr(smapi_rest, "create_skill",
                        lambda manifest, vendor: "amzn1.ask.skill.new")

    outcome = setup_ops.create_skill(alias="ampere",
                                     public_base="https://ampere.example.test")
    assert outcome.ok
    assert store.load()["skill_id"] == "amzn1.ask.skill.new"


def test_creation_refuses_without_a_passing_endpoint():
    """The gate the endpoint step exists for.

    A manifest pointing at an address Amazon cannot reach is accepted without
    complaint, and the failure surfaces much later as a skill that answers
    nothing. Nothing in the creation call itself can catch that.
    """
    store.update(endpoint_ok=False, alias="ampere")
    outcome = setup_ops.create_skill(alias="ampere",
                                     public_base="https://ampere.example.test")
    assert outcome.ok is False
    assert "endpoint" in outcome.detail.lower()
    assert not store.load()["skill_id"]


def test_creation_requires_being_connected(monkeypatch):
    passing_endpoint()
    monkeypatch.setattr(smapi_rest, "connected", lambda: False)
    outcome = setup_ops.create_skill(alias="ampere",
                                     public_base="https://ampere.example.test")
    assert outcome.ok is False
    assert "Amazon" in outcome.detail


def test_creation_refuses_a_plain_http_base():
    """Amazon will not call an http endpoint and does not say so."""
    passing_endpoint()
    outcome = setup_ops.create_skill(alias="ampere",
                                     public_base="http://ampere.example.test")
    assert outcome.ok is False
    assert "https" in outcome.detail


def test_a_second_press_does_not_mint_a_duplicate():
    """A resubmitted form, a stale tab, or Music Assistant re-rendering the
    settings page must not create a second skill on the account."""
    passing_endpoint()
    store.update(skill_id="amzn1.ask.skill.already")

    outcome = setup_ops.create_skill(alias="ampere",
                                     public_base="https://ampere.example.test")
    assert outcome.ok is True
    assert "already exists" in outcome.detail
    assert store.load()["skill_id"] == "amzn1.ask.skill.already"


def test_the_manifest_carries_the_derived_certificate_type(monkeypatch):
    """Getting this wrong is worse than omitting it: the manifest is accepted
    and Amazon simply never calls the endpoint."""
    passing_endpoint()
    store.update(cert_type="Wildcard")
    seen = {}
    monkeypatch.setattr(smapi_rest, "create_skill",
                        lambda manifest, vendor: seen.update(manifest=manifest)
                        or "amzn1.ask.skill.new")

    setup_ops.create_skill(alias="ampere",
                           public_base="https://ampere.example.test")
    endpoint = seen["manifest"]["manifest"]["apis"]["music"]["endpoint"]
    assert endpoint["sslCertificateType"] == "Wildcard"


def test_the_vendor_is_passed_through(monkeypatch):
    """An account with several vendors gets an ambiguous request refused by
    Amazon rather than resolved for it."""
    passing_endpoint()
    seen = {}
    monkeypatch.setattr(smapi_rest, "create_skill",
                        lambda manifest, vendor: seen.update(vendor=vendor)
                        or "amzn1.ask.skill.new")

    setup_ops.create_skill(alias="ampere", vendor="M2VENDOR",
                           public_base="https://ampere.example.test")
    assert seen["vendor"] == "M2VENDOR"
    assert store.load()["vendor_id"] == "M2VENDOR"


def test_a_skill_that_fails_validation_is_deleted_and_explained(monkeypatch):
    """Creation returning an id is not acceptance.

    Validation runs afterwards, and a skill that fails it exists in name only:
    it lists, it 404s for catalog association, and nothing ever calls its
    endpoint. Better removed now, with Amazon's own words, than discovered two
    steps later.
    """
    passing_endpoint()
    deleted = []
    monkeypatch.setattr(smapi_rest, "create_skill",
                        lambda manifest, vendor: "amzn1.ask.skill.bad")
    monkeypatch.setattr(smapi_rest, "delete_skill", deleted.append)
    monkeypatch.setattr(setup_ops, "manifest_verdict",
                        lambda *a, **k: "endpoint is not a valid URI")

    outcome = setup_ops.create_skill(alias="ampere",
                                     public_base="https://ampere.example.test")
    assert outcome.ok is False
    assert "endpoint is not a valid URI" in outcome.detail
    assert deleted == ["amzn1.ask.skill.bad"]
    assert not store.load()["skill_id"], "and no record of it is kept"


def test_recreating_reattaches_the_catalogs_that_survived(monkeypatch):
    """The catalogs live on the vendor and outlive the skill.

    A recreated skill starts with no associations even though the catalogs are
    still there, so re-binding here is what keeps the catalogs step honest
    about already being done.
    """
    passing_endpoint()
    store.update(catalogs={"artists": "cat-a", "albums": "cat-b"})
    associated = []
    monkeypatch.setattr(smapi_rest, "create_skill",
                        lambda manifest, vendor: "amzn1.ask.skill.new")
    monkeypatch.setattr(smapi_rest, "associate_catalog",
                        lambda skill, catalog: associated.append((skill, catalog)))

    setup_ops.create_skill(alias="ampere",
                           public_base="https://ampere.example.test")
    assert associated == [("amzn1.ask.skill.new", "cat-a"),
                          ("amzn1.ask.skill.new", "cat-b")]


# --- the catalogs -----------------------------------------------------------


def test_every_kind_gets_a_catalog_and_an_association(monkeypatch):
    passing_endpoint()
    store.update(skill_id="amzn1.ask.skill.x")
    created, associated = [], []
    monkeypatch.setattr(smapi_rest, "list_catalogs", lambda: [])
    monkeypatch.setattr(smapi_rest, "create_catalog",
                        lambda title, kind: created.append((title, kind))
                        or f"cat-{len(created)}")
    monkeypatch.setattr(smapi_rest, "associate_catalog",
                        lambda skill, catalog: associated.append(catalog))

    outcome = setup_ops.create_catalogs()
    assert outcome.ok
    assert [kind for _title, kind in created] == list(
        setup_ops.CATALOG_KINDS.values())
    assert len(associated) == len(setup_ops.CATALOG_KINDS)
    assert set(store.load()["catalogs"]) == set(setup_ops.CATALOG_KINDS)


def test_catalogs_need_a_skill_first():
    outcome = setup_ops.create_catalogs()
    assert outcome.ok is False
    assert "skill" in outcome.detail.lower()


def test_an_orphan_is_reused_rather_than_duplicated(monkeypatch):
    """A catalog created moments before its association failed was never
    recorded anywhere. Matching by title means a re-run heals instead of
    minting a second one on the vendor every time."""
    passing_endpoint()
    store.update(skill_id="amzn1.ask.skill.x")
    monkeypatch.setattr(smapi_rest, "list_catalogs",
                        lambda: [{"title": "Ampere artists", "id": "orphan-1"}])
    monkeypatch.setattr(smapi_rest, "create_catalog",
                        lambda title, kind: f"fresh-{title}")
    monkeypatch.setattr(smapi_rest, "associate_catalog", lambda skill, catalog: None)

    setup_ops.create_catalogs()
    assert store.load()["catalogs"]["artists"] == "orphan-1"


def test_a_catalog_is_recorded_before_it_is_associated(monkeypatch):
    """Association failing must not orphan the catalog it just made."""
    passing_endpoint()
    store.update(skill_id="amzn1.ask.skill.x")
    monkeypatch.setattr(smapi_rest, "list_catalogs", lambda: [])
    monkeypatch.setattr(smapi_rest, "create_catalog",
                        lambda title, kind: "cat-created")

    def refuse(_skill, _catalog):
        raise smapi_rest.SmapiError("nope", status=400)

    monkeypatch.setattr(smapi_rest, "associate_catalog", refuse)

    outcome = setup_ops.create_catalogs()
    assert outcome.ok is False
    assert store.load()["catalogs"]["artists"] == "cat-created"


def test_a_half_success_is_reported_per_kind(monkeypatch):
    passing_endpoint()
    store.update(skill_id="amzn1.ask.skill.x")
    monkeypatch.setattr(smapi_rest, "list_catalogs", lambda: [])
    monkeypatch.setattr(smapi_rest, "associate_catalog", lambda s, c: None)

    def sometimes(title, kind):
        if "tracks" in title:
            raise smapi_rest.SmapiError("rate limited", status=429)
        return "cat-ok"

    monkeypatch.setattr(smapi_rest, "create_catalog", sometimes)

    outcome = setup_ops.create_catalogs()
    assert outcome.ok is False
    failed = [row["kind"] for row in outcome.rows if not row["ok"]]
    assert failed == ["tracks"]


# --- connecting the Amazon account ------------------------------------------


def test_connecting_needs_both_halves():
    outcome = setup_ops.begin_amazon_link("client-id-only", "")
    assert outcome.ok is False
    assert "secret" in outcome.detail


def test_connecting_refuses_without_an_https_public_base(monkeypatch):
    """Amazon only redirects to https, so a consent round trip started from an
    http origin cannot come back."""
    from ma_provider import core

    monkeypatch.setattr(core, "PUBLIC_BASE", "http://ampere.example.test")
    outcome = setup_ops.begin_amazon_link("cid", "secret")
    assert outcome.ok is False
    assert "https" in outcome.detail


def test_a_callback_nobody_asked_for_is_refused():
    setup_ops._PENDING.clear()
    assert setup_ops.complete_amazon_link("code", "forged").ok is False


def test_a_mismatched_state_is_refused():
    setup_ops._PENDING.clear()
    setup_ops._PENDING.update({"state": "real", "verifier": "v",
                               "at": time.time(), "client_id": "c",
                               "client_secret": "s"})
    assert setup_ops.complete_amazon_link("code", "different").ok is False


def test_an_expired_consent_request_is_refused():
    setup_ops._PENDING.clear()
    setup_ops._PENDING.update({"state": "s", "verifier": "v",
                               "at": time.time() - setup_ops.CONSENT_TTL - 1,
                               "client_id": "c", "client_secret": "s"})
    assert setup_ops.complete_amazon_link("code", "s").ok is False


def test_a_consent_request_is_good_for_exactly_one_attempt(monkeypatch):
    """The pending record is cleared before anything is checked, so a replayed
    code cannot be exchanged a second time."""
    exchanged = []
    monkeypatch.setattr(smapi_rest, "complete",
                        lambda code, cid, secret, verifier:
                        exchanged.append(code) or {})
    setup_ops._PENDING.clear()
    setup_ops._PENDING.update({"state": "s", "verifier": "v", "at": time.time(),
                               "client_id": "c", "client_secret": "sec"})

    assert setup_ops.complete_amazon_link("code", "s").ok is True
    assert setup_ops.complete_amazon_link("code", "s").ok is False
    assert exchanged == ["code"]


# --- starting over ----------------------------------------------------------


def test_teardown_refuses_without_the_exact_skill_id():
    store.update(skill_id="amzn1.ask.skill.x", catalogs={"artists": "cat-a"})
    outcome = setup_ops.teardown("amzn1.ask.skill.")
    assert outcome.ok is False
    assert store.load()["skill_id"] == "amzn1.ask.skill.x"


def test_teardown_removes_the_catalogs_and_the_skill(monkeypatch):
    store.update(skill_id="amzn1.ask.skill.x",
                 catalogs={"artists": "cat-a", "albums": "cat-b"},
                 uploads={"artists": "up-1"}, enabled=True)
    removed = []
    monkeypatch.setattr(smapi_rest, "delete_catalog", removed.append)
    monkeypatch.setattr(smapi_rest, "delete_skill", removed.append)

    outcome = setup_ops.teardown("amzn1.ask.skill.x")
    assert outcome.ok
    assert removed == ["cat-a", "cat-b", "amzn1.ask.skill.x"]

    after = store.load()
    assert after["skill_id"] == "" and after["catalogs"] == {}
    assert after["uploads"] == {} and after["enabled"] is False


def test_the_record_is_cleared_even_when_amazon_refuses(monkeypatch):
    """Leaving a skill id behind for a skill that may be gone is the state the
    wizard has to detect and offer to repair; there is no reason to create it
    deliberately."""
    store.update(skill_id="amzn1.ask.skill.x", catalogs={})

    def refuse(_skill):
        raise smapi_rest.SmapiError("gone", status=404)

    monkeypatch.setattr(smapi_rest, "delete_skill", refuse)

    outcome = setup_ops.teardown("amzn1.ask.skill.x")
    assert outcome.ok is False
    assert store.load()["skill_id"] == ""


def test_removing_a_competing_skill_needs_confirmation(monkeypatch):
    monkeypatch.setattr(smapi_rest, "delete_skill",
                        _forbidden("delete_skill"))
    assert setup_ops.remove_competing_skill("amzn1.ask.skill.old", "").ok is False


def test_a_competing_skill_can_be_removed(monkeypatch):
    """Alexa routes an invocation across every enabled music skill, so a
    leftover from an earlier install fights the current one for the same
    words, and the symptom is intermittent."""
    removed = []
    monkeypatch.setattr(smapi_rest, "delete_skill", removed.append)
    outcome = setup_ops.remove_competing_skill("amzn1.ask.skill.old", "yes")
    assert outcome.ok
    assert removed == ["amzn1.ask.skill.old"]


# --- the upload -------------------------------------------------------------


def test_the_upload_needs_catalogs_first():
    outcome = setup_ops.run_upload()
    assert outcome.ok is False
    assert "catalogs" in outcome.detail.lower()


def test_an_unchanged_catalog_is_skipped(monkeypatch):
    """What makes a schedule safe: only real deltas spend one of the
    rate-limited upload slots."""
    from ma_provider import catalog_sync

    store.update(skill_id="s", catalogs={"artists": "cat-a"},
                 uploads={"artists": "up-1"}, catalog_hashes={"artists": "h1"})
    monkeypatch.setattr(catalog_sync, "collect",
                        lambda progress=None: {"artists": [{"id": "a"}]})
    monkeypatch.setattr(catalog_sync, "apply_timestamps",
                        lambda kind, entities, saved: (entities, "h1"))

    outcome = setup_ops.run_upload()
    assert outcome.ok
    assert outcome.rows == [{"kind": "artists", "ok": True,
                             "detail": "unchanged, skipped"}]


def test_a_changed_catalog_is_uploaded_and_recorded(monkeypatch):
    from ma_provider import catalog_sync

    store.update(skill_id="s", catalogs={"artists": "cat-a"},
                 catalog_hashes={"artists": "old"})
    monkeypatch.setattr(catalog_sync, "collect",
                        lambda progress=None: {"artists": [{"id": "a"}]})
    monkeypatch.setattr(catalog_sync, "apply_timestamps",
                        lambda kind, entities, saved: (entities, "new"))
    monkeypatch.setattr(smapi_rest, "upload_catalog",
                        lambda catalog_id, payload: "up-9")

    outcome = setup_ops.run_upload()
    assert outcome.ok
    after = store.load()
    assert after["uploads"]["artists"] == "up-9"
    assert after["catalog_hashes"]["artists"] == "new"


def test_a_cancelled_upload_says_it_stopped(monkeypatch):
    """Asked before the configuration check, so a caller that has already given
    up hears that it stopped rather than a complaint about setup it was never
    going to reach."""
    outcome = setup_ops.run_upload(should_stop=lambda: True)
    assert outcome.ok is False
    assert outcome.detail.startswith("Stopped")
