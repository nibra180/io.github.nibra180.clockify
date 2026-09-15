#!/usr/bin/env python3
"""Offline tests for clockify.py's pure logic.

    python3 -m unittest discover -s test

No network and no API key: everything here works on synthetic entries, which is
exactly where the date arithmetic and the task grouping tend to go wrong.
"""

import io
import os
import socket
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clockify  # noqa: E402


def entry(start, end, description="", project_id="p1", project_name="Project"):
    return {
        "id": "e-" + start,
        "description": description,
        "timeInterval": {"start": start, "end": end},
        "projectId": project_id,
        "project": {"id": project_id, "name": project_name, "color": "#fff", "clientName": ""},
    }


class TimeHelpers(unittest.TestCase):
    def test_parse_stamp_accepts_zulu_and_fractions(self):
        self.assertEqual(
            clockify.parse_stamp("2026-08-17T11:21:10Z"),
            datetime(2026, 8, 17, 11, 21, 10, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(clockify.parse_stamp("2026-08-17T11:21:10.482Z"))
        self.assertIsNone(clockify.parse_stamp(""))
        self.assertIsNone(clockify.parse_stamp("not a date"))

    def test_week_start_lands_on_the_configured_first_day(self):
        # 2026-08-19 is a Wednesday.
        wednesday = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
        monday_week = clockify.week_start(wednesday, "monday")
        sunday_week = clockify.week_start(wednesday, "sunday")
        self.assertEqual(monday_week.weekday(), 0)
        self.assertEqual(sunday_week.weekday(), 6)
        self.assertEqual((monday_week - sunday_week).days, 1)
        self.assertEqual((monday_week.hour, monday_week.minute), (0, 0))

    def test_week_start_on_the_first_day_is_that_midnight(self):
        monday = datetime(2026, 8, 17, 9, 30, tzinfo=timezone.utc)
        start = clockify.week_start(monday, "monday")
        self.assertEqual(start.date(), clockify.local_midnight(monday).date())

    def test_utc_stamps_are_comparable_as_strings(self):
        # Totals are computed by comparing stamps lexicographically, which only
        # holds while every stamp is fixed-width UTC.
        earlier = clockify.utc_stamp(datetime(2026, 8, 16, 21, 0, tzinfo=timezone.utc))
        later = clockify.utc_stamp(datetime(2026, 8, 17, 6, 0, tzinfo=timezone.utc))
        self.assertLess(earlier, later)
        self.assertEqual(earlier, "2026-08-16T21:00:00Z")


class EntryView(unittest.TestCase):
    def test_closed_entry_reports_its_duration(self):
        view = clockify.entry_view(
            entry("2026-08-17T09:00:00Z", "2026-08-17T10:30:00Z", "Writing docs"), {}
        )
        self.assertEqual(view["seconds"], 5400)
        self.assertFalse(view["running"])
        self.assertEqual(view["description"], "Writing docs")
        self.assertEqual(view["projectName"], "Project")

    def test_running_entry_has_no_duration_of_its_own(self):
        view = clockify.entry_view(entry("2026-08-17T09:00:00Z", None), {})
        self.assertTrue(view["running"])
        self.assertEqual(view["seconds"], 0)

    def test_project_name_falls_back_to_the_projects_list(self):
        raw = entry("2026-08-17T09:00:00Z", "2026-08-17T09:10:00Z")
        del raw["project"]
        view = clockify.entry_view(raw, {"p1": {"name": "From list", "color": "", "clientName": "ACME"}})
        self.assertEqual(view["projectName"], "From list")
        self.assertEqual(view["clientName"], "ACME")

    def test_recent_ticket_entries_are_deduplicated_and_newest_first(self):
        entries = [
            entry("2026-08-17T09:00:00Z", "2026-08-17T10:00:00Z", "#42 remove footer", "old"),
            entry("2026-08-19T09:00:00Z", "2026-08-19T10:00:00Z", "#42 remove footer", "new"),
            entry("2026-08-18T09:00:00Z", "2026-08-18T10:00:00Z", "#7 fix checkout", "p7"),
            entry("2026-08-20T09:00:00Z", "2026-08-20T10:00:00Z", "Daily meeting", "meeting"),
        ]
        recent = clockify.recent_ticket_entries(entries, {})
        self.assertEqual([item["description"] for item in recent], [
            "#42 remove footer",
            "#7 fix checkout",
        ])
        self.assertEqual(recent[0]["projectId"], "new")

    def test_ticket_number_requires_a_leading_hash_and_digits(self):
        self.assertEqual(clockify.ticket_number("#42 remove footer"), "42")
        self.assertEqual(clockify.ticket_number("  #7 fix checkout"), "7")
        self.assertEqual(clockify.ticket_number("Ticket #42"), "")
        self.assertEqual(clockify.ticket_number("# no number"), "")

    def test_ticket_history_reads_later_pages(self):
        class ApiStub:
            def __init__(self):
                self.pages = []

            def call(self, method, path, params=None):
                page = (params or {})["page"]
                self.pages.append(page)
                if page == 1:
                    return [
                        entry("2026-08-20T09:00:00Z", "2026-08-20T09:01:00Z", "Daily meeting")
                        for _ in range(clockify.PAGE_SIZE)
                    ]
                return [entry(
                    "2026-08-19T09:00:00Z",
                    "2026-08-19T10:00:00Z",
                    "#42 remove footer",
                )]

        api = ApiStub()
        ident = clockify.Identity("user", "workspace", "User")
        recent = clockify.fetch_recent_ticket_entries(ident, api, {})
        self.assertEqual(api.pages, [1, 2])
        self.assertEqual(recent[0]["description"], "#42 remove footer")


class ConfigStorage(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": self.temporary.name},
            clear=True,
        )
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_save_uses_private_files_and_leaves_no_temporary_key_file(self):
        clockify.save_config({"apiKey": "secret"})
        path = clockify.config_path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(clockify.load_config()["apiKey"], "secret")
        self.assertEqual(list(path.parent.glob(".clockify-*.tmp")), [])

    def test_host_refresh_does_not_restore_a_removed_key(self):
        clockify.save_config({"apiKey": "old-key", "weekStart": "sunday"})
        stale = clockify.load_config()
        clockify.update_config(removals=("apiKey", "workspaceId"))
        clockify.remember_host(stale, type("ApiStub", (), {"good_host": "203.0.113.8"})())
        current = clockify.load_config()
        self.assertNotIn("apiKey", current)
        self.assertEqual(current["apiHost"], "203.0.113.8")
        self.assertEqual(current["weekStart"], "sunday")

    def test_environment_key_takes_precedence(self):
        os.environ["CLOCKIFY_API_KEY"] = "environment"
        config = {"apiKey": "stored", "workspaceId": "stored-workspace", "defaultProjectId": "old"}
        self.assertEqual(clockify.api_key(config), "environment")
        self.assertEqual(clockify.identity_config(config), {})
        self.assertEqual(clockify.default_project_id(config), "")

    def test_set_key_rejects_a_key_shadowed_by_the_environment(self):
        os.environ["CLOCKIFY_API_KEY"] = "environment"
        with mock.patch("sys.stdin", io.StringIO("pasted\n")):
            payload = clockify.cmd_set_key(None)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["configured"])
        self.assertIn("CLOCKIFY_API_KEY", payload["error"])

    def test_workspace_change_drops_the_previous_default_project(self):
        clockify.save_config({
            "workspaceId": "old-workspace",
            "defaultProjectId": "old-project",
        })
        args = type("Args", (), {
            "project": None,
            "workspace": "new-workspace",
            "week_start": None,
        })()
        clockify.cmd_set_config(args)
        self.assertEqual(clockify.load_config()["workspaceId"], "new-workspace")
        self.assertNotIn("defaultProjectId", clockify.load_config())

    def test_new_account_drops_the_previous_default_project(self):
        clockify.save_config({
            "apiKey": "old-key",
            "workspaceId": "old-workspace",
            "defaultProjectId": "old-project",
        })
        ident = clockify.Identity("user", "new-workspace", "User")
        refreshed = clockify.base_payload()
        refreshed["configured"] = True
        with (
            mock.patch("sys.stdin", io.StringIO("new-key\n")),
            mock.patch.object(clockify, "Api", return_value=object()),
            mock.patch.object(clockify, "identity", return_value=ident),
            mock.patch.object(clockify, "snapshot", return_value=refreshed),
            mock.patch.object(clockify, "remember_host"),
        ):
            payload = clockify.cmd_set_key(None)
        self.assertTrue(payload["ok"])
        self.assertNotIn("defaultProjectId", clockify.load_config())

    def test_clear_key_refreshes_environment_account_projects(self):
        os.environ["CLOCKIFY_API_KEY"] = "environment"
        clockify.save_config({
            "apiKey": "stored",
            "workspaceId": "old-workspace",
            "defaultProjectId": "old-project",
        })
        api = object()
        ident = clockify.Identity("user", "environment-workspace", "User")
        refreshed = clockify.base_payload()
        refreshed.update({"configured": True, "projectsLoaded": True, "projects": [{"id": "new"}]})
        with (
            mock.patch.object(clockify, "open_api", return_value=api),
            mock.patch.object(clockify, "identity", return_value=ident),
            mock.patch.object(clockify, "snapshot", return_value=refreshed),
            mock.patch.object(clockify, "remember_host"),
        ):
            payload = clockify.cmd_clear_key(None)
        self.assertTrue(payload["projectsLoaded"])
        self.assertEqual(payload["projects"], [{"id": "new"}])
        self.assertNotIn("apiKey", clockify.load_config())
        self.assertNotIn("workspaceId", clockify.load_config())
        self.assertNotIn("defaultProjectId", clockify.load_config())


class ApiRetries(unittest.TestCase):
    def test_mutation_is_not_replayed_after_ambiguous_timeout(self):
        class Connection:
            def __init__(self):
                self.requests = 0

            def request(self, method, target, payload, headers):
                self.requests += 1

            def getresponse(self):
                raise socket.timeout("lost response")

            def close(self):
                pass

        connection = Connection()
        api = object.__new__(clockify.Api)
        api.key = "secret"
        api.addresses = [
            (socket.AF_INET, ("192.0.2.1", 443)),
            (socket.AF_INET, ("192.0.2.2", 443)),
        ]
        api.connection = None
        api.address = None
        api.preferred = ""
        api.good_host = ""

        with mock.patch.object(clockify.Api, "_open", return_value=connection):
            with self.assertRaisesRegex(clockify.ApiError, "may have applied"):
                api.call("POST", "/workspaces/workspace/time-entries", body={"start": "now"})
        self.assertEqual(connection.requests, 1)


class Payloads(unittest.TestCase):
    def test_base_payload_carries_every_key_the_panel_binds_to(self):
        payload = clockify.base_payload()
        for key in ("ok", "configured", "error", "note", "running", "todaySeconds",
                    "weekSeconds", "projects", "projectsLoaded", "recentEntries",
                    "historyLoaded", "fetchedAt", "defaultProjectId", "weekStart",
                    "userName", "workspaceName"):
            self.assertIn(key, payload)

    def test_unconfigured_keeps_preferences_and_stays_ok(self):
        payload = clockify.unconfigured({"weekStart": "sunday", "defaultProjectId": "p9"})
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["weekStart"], "sunday")
        self.assertEqual(payload["defaultProjectId"], "p9")

    def test_status_message_explains_the_common_refusals(self):
        self.assertIn("API key", clockify.status_message(401, ""))
        self.assertIn("rate limit", clockify.status_message(429, ""))
        self.assertEqual(clockify.status_message(404, '{"message": "not found"}'), "not found")
        self.assertIn("HTTP 500", clockify.status_message(500, ""))


if __name__ == "__main__":
    unittest.main()
