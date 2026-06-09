"""Update notifier (Phase 18) — pure version logic + the HA publish path."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from _fakes import FakeMqttClient, _publisher

from expense_analyzer.config import Settings
from expense_analyzer.ha.update_notify import (
    StoredStatus,
    UpdateStatus,
    load_status,
    parse_version,
    save_status,
    select_update,
)


class TestParseVersion:
    @pytest.mark.parametrize(
        ("tag", "expected"),
        [
            ("v1.2.3", (1, 2, 3)),
            ("1.2.3", (1, 2, 3)),  # leading v optional
            ("v10.0.1", (10, 0, 1)),
            ("  v1.0.0  ", (1, 0, 0)),  # whitespace tolerated
        ],
    )
    def test_parses_release_tags(self, tag, expected):
        assert parse_version(tag) == expected

    @pytest.mark.parametrize("tag", ["v1.2", "v1.2.3-rc1", "nightly", "v1.2.3.4", "", "vX.Y.Z"])
    def test_rejects_non_releases(self, tag):
        assert parse_version(tag) is None


class TestSelectUpdate:
    def test_newer_release_is_available(self):
        status = select_update("v1.2.0", ["v1.1.0", "v1.2.0", "v1.3.0"])
        assert status.latest == "v1.3.0"
        assert status.update_available is True

    def test_already_on_latest(self):
        status = select_update("v1.3.0", ["v1.2.0", "v1.3.0"])
        assert status.latest == "v1.3.0"
        assert status.update_available is False

    def test_current_ahead_of_any_tag_is_not_an_update(self):
        status = select_update("v2.0.0", ["v1.9.0"])
        assert status.update_available is False

    def test_no_release_tags_means_no_update(self):
        status = select_update("v1.0.0", ["nightly", "latest"])
        assert status.latest is None
        assert status.update_available is False

    def test_untagged_deploy_with_a_release_is_behind(self):
        status = select_update(None, ["v1.0.0"])
        assert status.latest == "v1.0.0"
        assert status.update_available is True

    def test_non_release_tags_are_ignored_when_picking_latest(self):
        status = select_update("v1.0.0", ["v1.1.0", "v2.0.0-rc1", "garbage"])
        assert status.latest == "v1.1.0"  # the rc and garbage don't win

    def test_semantic_not_lexicographic_ordering(self):
        # "v9.0.0" < "v10.0.0" numerically, but > lexicographically.
        status = select_update("v9.0.0", ["v9.0.0", "v10.0.0"])
        assert status.latest == "v10.0.0"
        assert status.update_available is True


class TestPublishUpdate:
    def test_publishes_retained_sensor_with_attributes(self):
        client = FakeMqttClient()
        _publisher(client).publish_update(current="v1.2.0", latest="v1.3.0", update_available=True)

        by_topic = {p.topic: p for p in client.published}
        # Availability online (retained) with the offline Last Will armed.
        assert by_topic["expense_analyzer/availability"].payload == "online"
        assert client.will == ("expense_analyzer/availability", "offline", True)
        # Discovery config for the update sensor, retained, reads the latest field.
        cfg = json.loads(by_topic["homeassistant/sensor/expense_analyzer/update/config"].payload)
        assert cfg["unique_id"] == "expense_analyzer_update"
        assert cfg["value_template"] == "{{ value_json.latest }}"
        assert cfg["json_attributes_topic"] == "expense_analyzer/update"
        # Retained state carries the verdict.
        state = by_topic["expense_analyzer/update"]
        assert state.retain is True and state.qos == 1
        assert json.loads(state.payload) == {
            "current": "v1.2.0",
            "latest": "v1.3.0",
            "update_available": True,
        }

    def test_up_to_date_still_publishes_a_false_flag(self):
        client = FakeMqttClient()
        _publisher(client).publish_update(current="v1.3.0", latest="v1.3.0", update_available=False)

        state = json.loads(
            next(p for p in client.published if p.topic == "expense_analyzer/update").payload
        )
        assert state["update_available"] is False

    def test_unknown_versions_render_as_unknown(self):
        client = FakeMqttClient()
        _publisher(client).publish_update(current=None, latest=None, update_available=False)

        state = json.loads(
            next(p for p in client.published if p.topic == "expense_analyzer/update").payload
        )
        assert state == {"current": "unknown", "latest": "unknown", "update_available": False}

    def test_from_settings_refuses_when_not_configured(self):
        # The CLI guards on this before publishing; confirm the guard exists.
        assert Settings(mqtt_host="").mqtt_configured is False


class TestPersistStatus:
    """The local status file the in-app Updates view reads (no network of its own)."""

    def test_save_then_load_round_trips(self, tmp_path: Path):
        status = UpdateStatus(current="v1.2.0", latest="v1.3.0", update_available=True)
        when = datetime(2026, 6, 9, 7, 30, tzinfo=UTC)
        path = tmp_path / "nested" / "update_status.json"  # parent created on save

        save_status(status, path=path, checked_at=when)

        assert load_status(path) == StoredStatus("v1.2.0", "v1.3.0", True, when)

    def test_missing_file_loads_as_none(self, tmp_path: Path):
        assert load_status(tmp_path / "absent.json") is None

    def test_corrupt_file_loads_as_none(self, tmp_path: Path):
        path = tmp_path / "bad.json"
        path.write_text("{ not valid json")
        assert load_status(path) is None
