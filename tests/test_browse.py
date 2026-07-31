"""GetDisplayableContent, the browse experience.

Note this directive answers on payloadVersion 2.0 while its errors still go out
on Alexa.Media at 1.0, and it takes an ARRAY of resolved criteria rather than
the single object GetPlayableContent receives.
"""

from __future__ import annotations

from conftest import directive


def post(client, body):
    resp = client.post("/music", json=body)
    assert resp.status_code == 200, resp.data
    return resp.get_json()


def browse(criteria, limit=25):
    return directive(
        "Alexa.Media.Search", "GetDisplayableContent",
        {
            "requestContext": {"user": {"id": "u"}},
            "filters": {"explicitLanguageAllowed": True},
            "endpoints": [],
            "maxResultLimit": limit,
            "policies": [],
            "alexaResolvedSelectionCriteria": criteria,
        },
        version="2.0",
    )


def test_response_uses_payload_version_2(client):
    out = post(client, browse([{"attributes": [{"type": "ARTIST", "value": "Gregory"}]}]))
    assert out["header"]["payloadVersion"] == "2.0"
    assert out["header"]["name"] == "GetDisplayableContent.Response"


def test_search_returns_grouped_shelves(client):
    out = post(client, browse([{"attributes": [{"type": "ARTIST", "value": "Gregory"}]}]))
    groups = out["payload"]["contentGroups"]
    labels = [g["metadata"]["name"]["display"] for g in groups]
    assert labels == ["Artists", "Albums", "Songs"]
    for g in groups:
        assert g["contentList"], g


def test_items_carry_required_fields(client):
    out = post(client, browse([{"attributes": [{"type": "ARTIST", "value": "Gregory"}]}]))
    item = out["payload"]["contentGroups"][0]["contentList"][0]
    assert item["id"].startswith("ar:")
    assert item["metadata"]["type"] == "ARTIST"
    assert item["metadata"]["name"]["speech"]["type"] == "PLAIN_TEXT"
    assert item["metadata"]["name"]["display"]
    assert "art" in item["metadata"]


def test_album_items_include_authors(client):
    """MediaMetadata requires `authors` on ALBUM."""
    out = post(client, browse([{"attributes": [{"type": "ALBUM", "value": "Appaloosa"}]}]))
    albums = [g for g in out["payload"]["contentGroups"]
              if g["metadata"]["name"]["display"] == "Albums"][0]
    assert albums["contentList"][0]["metadata"]["authors"][0]["name"]["display"]


def test_art_ladder_is_complete(client):
    """No viewport info arrives, so every size must be offered."""
    out = post(client, browse([{"attributes": [{"type": "TRACK", "value": "Light Year"}]}]))
    songs = [g for g in out["payload"]["contentGroups"]
             if g["metadata"]["name"]["display"] == "Songs"][0]
    sources = songs["contentList"][0]["metadata"]["art"]["sources"]
    assert [s["size"] for s in sources] == ["X_SMALL", "SMALL", "MEDIUM", "LARGE", "X_LARGE"]
    assert all(s["url"].startswith("https://") for s in sources)


def test_browsable_flags(client):
    """Artists drill down; tracks do not."""
    out = post(client, browse([{"attributes": [{"type": "ARTIST", "value": "Gregory"}]}]))
    by_label = {g["metadata"]["name"]["display"]: g for g in out["payload"]["contentGroups"]}
    assert by_label["Artists"]["contentList"][0]["actions"]["browsable"] is True
    assert by_label["Songs"]["contentList"][0]["actions"]["browsable"] is False
    assert by_label["Songs"]["contentList"][0]["actions"]["playable"] is True


def test_contextual_request_offers_library_shelves(client):
    """SEARCH_METHOD is undocumented; treat it as 'show me something'."""
    out = post(client, browse([{"attributes": [
        {"type": "SEARCH_METHOD", "basis": None, "value": "RECOMMENDED"}
    ]}]))
    labels = [g["metadata"]["name"]["display"] for g in out["payload"]["contentGroups"]]
    assert labels == ["Your Playlists", "Genres"]


def test_empty_criteria_offers_library_shelves(client):
    out = post(client, browse([]))
    labels = [g["metadata"]["name"]["display"] for g in out["payload"]["contentGroups"]]
    assert "Your Playlists" in labels


def test_max_result_limit_respected(client):
    out = post(client, browse([], limit=1))
    playlists = out["payload"]["contentGroups"][0]["contentList"]
    assert len(playlists) == 1


def test_browse_ids_are_initiate_compatible(client):
    """Tapping a browse tile sends Initiate with that same id."""
    out = post(client, browse([{"attributes": [{"type": "ARTIST", "value": "Gregory"}]}]))
    content_id = out["payload"]["contentGroups"][0]["contentList"][0]["id"]

    played = post(client, directive(
        "Alexa.Media.Playback", "Initiate", {"contentId": content_id},
    ))
    assert played["payload"]["playbackMethod"]["firstItem"]["stream"]["uri"]


def test_failure_errors_on_media_namespace_at_v1(client, app, monkeypatch):
    """Errors stay on Alexa.Media / 1.0 even for a 2.0 request."""
    monkeypatch.setattr(app.subsonic, "playlists", lambda: [])
    monkeypatch.setattr(app.subsonic, "genres", lambda: [])
    out = post(client, browse([]))
    assert out["header"]["namespace"] == "Alexa.Media"
    assert out["header"]["payloadVersion"] == "1.0"
    assert out["payload"]["type"] == "CONTENT_NOT_FOUND"


def test_undocumented_fields_tolerated(client):
    body = browse([{"attributes": [{"type": "ARTIST", "value": "Gregory"}]}])
    body["payload"]["policies"] = ["something-new"]
    body["payload"]["playQueuePreviewCriteria"] = {"previewItemLimit": 1}
    out = post(client, body)
    assert out["payload"]["contentGroups"]
