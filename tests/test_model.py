"""Tests for the core record types."""

import pytest

from atomic_l0g.model import Comment, Item, Release


def make_item(**overrides) -> Item:
    kwargs = {
        "id": "github:flatcar/Flatcar:pr:1234",
        "distro": "flatcar",
        "provider": "github",
        "item_kind": "pr",
        "title": "Add sysext support",
    }
    kwargs.update(overrides)
    return Item(**kwargs)


def test_item_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown item_kind"):
        make_item(item_kind="pull-request")


def test_item_requires_id():
    with pytest.raises(ValueError, match="Item.id"):
        make_item(id="")


def test_has_triage_text_accepts_title_or_summary():
    assert make_item().has_triage_text
    assert make_item(title=None, summary="kernel bump to 6.12.5").has_triage_text


def test_has_triage_text_rejects_body_only():
    """A body alone is not triageable -- sync reports these."""

    assert not make_item(title=None, summary=None, body="a long explanation").has_triage_text
    assert not make_item(title="   ").has_triage_text


def test_to_dict_prunes_empty_values():
    data = make_item().to_dict()

    assert data["title"] == "Add sysext support"
    assert "body" not in data
    assert "labels" not in data
    assert "signal" not in data
    assert "fetch_ref" not in data


def test_content_hash_ignores_collection_time():
    item = make_item()
    before = item.compute_content_hash()

    item.observed_at = "2026-09-15T09:00:00Z"
    item.first_seen = "2026-09-10T09:00:00Z"
    item.last_changed = "2026-09-15T09:00:00Z"

    assert item.compute_content_hash() == before


def test_content_hash_tracks_identity():
    item = make_item()
    before = item.compute_content_hash()

    item.body = "now with details"

    assert item.compute_content_hash() != before


def test_item_round_trip():
    item = make_item(
        labels=["kind/bug"],
        signal={"comments": 3, "reactions": 1},
        fetch_ref={"diff": "repos/flatcar/Flatcar/pulls/1234"},
        version="3815.2.4",
    )

    assert Item.from_dict(item.to_dict()) == item


def test_item_from_dict_ignores_unknown_fields():
    restored = Item.from_dict(
        {
            "id": "github:a/b:issue:1",
            "distro": "x",
            "provider": "github",
            "item_kind": "issue",
            "title": "t",
            "something_we_removed_later": True,
        }
    )

    assert restored.title == "t"


def test_comment_round_trip():
    comment = Comment(
        id="github:flatcar/Flatcar:pr:1234:comment:998877",
        parent_id="github:flatcar/Flatcar:pr:1234",
        provider="github",
        body="+1",
        author="dustymabe",
        is_review_comment=True,
    )

    assert Comment.from_dict(comment.to_dict()) == comment


def test_comment_requires_parent():
    with pytest.raises(ValueError, match="parent_id"):
        Comment(id="c1", parent_id="", provider="github")


def test_release_round_trip():
    release = Release(
        id="flatcar:release:3815.2.4",
        distro="flatcar",
        version="3815.2.4",
        channel="stable",
        release_date="2026-09-12",
        components={"kernel": "6.12.5", "systemd": "257"},
        url="https://flatcar.org",
    )

    assert Release.from_dict(release.to_dict()) == release


def test_release_requires_version():
    with pytest.raises(ValueError, match="version"):
        Release(id="x", distro="flatcar", version="")
