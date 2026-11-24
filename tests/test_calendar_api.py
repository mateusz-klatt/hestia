"""Calendar HTTP surface with real SQLite roles, revision checks and durable jobs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
from zoneinfo import ZoneInfo

from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from hestia import api_contract, auth, db, store_sql, web
from hestia.calendar_sync import api, instances as instance_expander
from hestia.calendar_sync.config import ConfigError
from hestia.calendar_sync.store import CalendarConflict


def content(summary="Family"):
    return {"summary": summary, "start": {"date": "2026-10-21"}, "end": {"date": "2026-10-22"}}


def create_request(summary="Family", key="calendar-create-key-0001"):
    return {"group": "family", "payload": content(summary), "idempotency_key": key}


class CalendarApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.config_path = self.path / "calendars.json"
        self.config = {"version": 1, "accounts": {"google": {"provider": "google", "client_id_env": "G_ID",
                       "client_secret_env": "G_SECRET", "refresh_token_env": "G_REFRESH"}},
                       "groups": [{"id": "family", "calendars": [{"id": "family-google", "account": "google", "calendar_id": "private@example.test"}]}]}
        self.config_path.write_text(json.dumps(self.config))
        patches = [mock.patch.dict(os.environ, {"HESTIA_DB": str(self.path / "hestia.db"),
                   "HESTIA_PERSIST": "sqlite", "HESTIA_CALENDAR_CONFIG": str(self.config_path),
                   "G_SECRET": "never-leak-secret", "G_REFRESH": "never-leak-token"}),
                   mock.patch.object(web, "_AUTH_ENABLED", True),
                   mock.patch.object(web, "_SESSION_SECRET", b"calendar-api-test")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(db.reset_engine_cache)
        engine, sessions = db.init_db()
        store_sql.cutover_users(engine, {"admin": "unused-password-hash"})
        with sessions.begin() as session:
            session.add(db.User(username="operator", password_hash="unused", role="operator", disabled=False))
            session.add(db.User(username="viewer", password_hash="unused", role="viewer", disabled=False))
        self.client = TestClient(TestServer(web.make_app(SimpleNamespace())))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    def headers(self, role="operator"):
        return {"Authorization": "Bearer " + auth.make_session(role, now=web._now(), secret=b"calendar-api-test")}

    async def post(self, body, *, deleting=False, role="operator"):
        return await self.client.post(api.DELETE_PATH if deleting else api.EVENTS_PATH,
                                      json=body, headers=self.headers(role))

    async def create(self, payload, *, group="family"):
        body = {"group": group, "payload": payload, "idempotency_key": uuid.uuid4().hex}
        response = await self.post(body)
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["event"]

    async def instances(self, query, *, role="viewer"):
        path = api.INSTANCES_PATH + ("?" + query if query else "")
        return await self.client.get(path, headers=self.headers(role))

    async def create_dense_daily_series(self, summary="Hourly"):
        for hour in range(24):
            await self.create({
                "summary": summary,
                "start": {"dateTime": f"2026-01-01T{hour:02d}:00:00Z", "timeZone": "UTC"},
                "end": {"dateTime": f"2026-01-01T{hour:02d}:30:00Z", "timeZone": "UTC"},
                "recurrence": ["RRULE:FREQ=DAILY"],
            })

    async def test_all_crud_and_revision_conflicts_are_persisted_and_serialized(self):
        response = await self.post(create_request())
        self.assertEqual(response.status, 200)
        created = await response.json()
        api_contract.CalendarSaved.model_validate(created)
        event = created["event"]
        request = {"group": "family", "id": event["id"], "revision": event["revision"], "payload": content("Updated")}
        api_contract.CalendarUpdateRequest.model_validate(request)
        response = await self.post(request, role="admin")
        self.assertEqual(response.status, 200)
        edited = (await response.json())["event"]
        self.assertEqual(edited["revision"], 2)
        self.assertEqual((await self.post(request)).status, 409)
        response = await self.client.get(api.PATH, headers=self.headers("viewer"))
        state = await response.json()
        api_contract.Calendars.model_validate(state)
        self.assertEqual(state["groups"], [{"id": "family"}])
        self.assertEqual(state["events"], [edited])
        self.assertEqual(state["status"]["pending"][0]["revision"], 2)
        delete = {"group": "family", "id": event["id"], "revision": 1}
        self.assertEqual((await self.post(delete, deleting=True)).status, 409)
        delete["revision"] = 2
        api_contract.CalendarDeleteRequest.model_validate(delete)
        response = await self.post(delete, deleting=True)
        self.assertEqual(response.status, 200)
        tombstone = (await response.json())["event"]
        self.assertTrue(tombstone["deleted"])
        self.assertEqual(tombstone["revision"], 3)
        self.assertTrue(api._store().list_events("family", include_deleted=True)[0]["deleted"])
        response = await self.client.get(api.PATH + "?group=family", headers=self.headers())
        self.assertEqual((await response.json())["events"], [])
        request["revision"] = 3
        self.assertEqual((await self.post(request)).status, 409)

    async def test_real_auth_roles_allow_read_and_gate_both_writes(self):
        query = "from=2026-01-01&to=2026-01-02"
        self.assertEqual((await self.client.get(api.PATH)).status, 401)
        self.assertEqual((await self.client.get(api.INSTANCES_PATH + "?" + query)).status, 401)
        self.assertEqual((await self.client.post(api.EVENTS_PATH, json={})).status, 401)
        for role in ("viewer", "operator", "admin"):
            self.assertEqual((await self.client.get(api.PATH, headers=self.headers(role))).status, 200)
            self.assertEqual((await self.instances(query, role=role)).status, 200)
        for deleting in (False, True):
            self.assertEqual((await self.post({"group": "family", "payload": content()}, deleting=deleting, role="viewer")).status, 403)

    async def test_invalid_request_fields_and_revisions(self):
        invalid = [[], {"group": "family", "payload": content(), "unexpected": True},
                   {"payload": content()}, {"group": 1}, {"group": "family", "id": ""},
                   {"group": "family", "id": None}, {"group": "family", "id": 1},
                   {"group": "family", "id": "event"}, {"group": "family", "id": "event", "revision": True},
                   {"group": "family", "id": "event", "revision": 0},
                   {"group": "family", "revision": 1, "payload": content()},
                   {"group": "family", "payload": None, "idempotency_key": "calendar-create-key-0001"},
                   {**create_request(), "group": "unknown"}]
        for body in invalid:
            with self.subTest(body=body):
                response = await self.post(body)
                self.assertEqual(response.status, 400)
                self.assertEqual(await response.json(), {"error": "invalid_calendar_event"})
        self.assertEqual((await self.post({"group": "family"}, deleting=True)).status, 400)
        self.assertEqual((await self.post({"group": "family", "id": "event", "revision": 1, "payload": {}}, deleting=True)).status, 400)
        self.assertEqual((await self.client.get(api.PATH + "?group=unknown", headers=self.headers())).status, 400)

    async def test_create_retry_identity_is_required_and_cannot_be_used_for_edits(self):
        for key in (None, 1, True, "", "short", "x" * 129, "calendar/key/invalid", "calendar-key-newline\n"):
            with self.subTest(key=key):
                self.assertEqual((await self.post(create_request(key=key))).status, 400)
        self.assertEqual((await self.post({"group": "family", "payload": content()})).status, 400)
        body = create_request()
        response = await self.post(body)
        original = (await response.json())["event"]
        self.assertEqual(response.status, 200)
        for attempt in range(2):
            db.reset_engine_cache()  # HTTP response loss followed by a process restart.
            retry = await self.post(body)
            self.assertEqual(retry.status, 200)
            self.assertEqual((await retry.json())["event"], original)
        self.assertEqual(len(api._store().list_events("family")), 1)
        edit = {"group": "family", "id": original["id"], "revision": 1, "payload": content("Later edit")}
        self.assertEqual((await self.post({**edit, "idempotency_key": body["idempotency_key"]})).status, 400)
        edited = (await (await self.post(edit)).json())["event"]
        self.assertEqual((await (await self.post(body)).json())["event"], edited)
        self.assertEqual((await self.post(create_request("Different retry"))).status, 409)
        deletion = {"group": "family", "id": original["id"], "revision": edited["revision"]}
        deleted = (await (await self.post(deletion, deleting=True)).json())["event"]
        db.reset_engine_cache()
        self.assertEqual((await (await self.post(body)).json())["event"], deleted)
        self.assertTrue(deleted["deleted"])
        self.assertEqual(api._store().list_events("family"), [])

    async def test_json_body_guards(self):
        response = await self.client.post(api.EVENTS_PATH, data="{}", headers=self.headers())
        self.assertEqual(response.status, 415)
        response = await self.client.post(api.EVENTS_PATH, data="{broken", headers={**self.headers(), "Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        response = await self.post({"group": "family", "payload": content("x" * 70000)})
        self.assertEqual(response.status, 413)

    async def test_disabled_empty_and_invalid_configuration_without_secret_disclosure(self):
        with mock.patch.object(api, "load_config", return_value=None):
            response = await self.client.get(api.PATH, headers=self.headers())
            self.assertEqual(await response.json(), {"enabled": False, "groups": [], "events": [], "status": {"endpoints": [], "pending": []}})
            response = await self.instances("from=2026-01-01&to=2026-01-02")
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"enabled": False, "groups": [], "truncated": False, "instances": []})
            self.assertEqual((await self.post(create_request())).status, 503)
        empty = {**self.config, "groups": []}
        self.config_path.write_text(json.dumps(empty))
        response = await self.client.get(api.PATH, headers=self.headers())
        self.assertEqual((await response.json())["events"], [])
        response = await self.instances("from=2026-01-01&to=2026-01-02")
        self.assertEqual(await response.json(), {"enabled": True, "groups": [], "truncated": False, "instances": []})
        for exception in (ConfigError("never-leak-secret"), CalendarConflict("never-leak-token")):
            with mock.patch.object(api, "_store", side_effect=exception):
                response = await self.client.get(api.PATH, headers=self.headers())
                self.assertEqual(response.status, 503)
                self.assertNotIn("never-leak", await response.text())
                response = await self.instances("from=2026-01-01&to=2026-01-02")
                self.assertEqual(response.status, 503)
                self.assertNotIn("never-leak", await response.text())
        with mock.patch.object(api, "_write", side_effect=TypeError("never-leak-token")):
            response = await self.post(create_request())
            self.assertEqual(response.status, 400)
            self.assertNotIn("never-leak", await response.text())

    async def test_config_retirement_filters_endpoints_pending_and_private_identifiers(self):
        response = await self.post(create_request())
        self.assertEqual(response.status, 200)
        new = {**self.config, "groups": [{"id": "new", "calendars": [{"id": "new-google", "account": "google", "calendar_id": "new-private@example.test"}]}]}
        self.config_path.write_text(json.dumps(new))
        response = await self.client.get(api.PATH, headers=self.headers())
        text = await response.text()
        state = json.loads(text)
        self.assertEqual([row["id"] for row in state["status"]["endpoints"]], ["new-google"])
        self.assertEqual(state["status"]["pending"], [])
        self.assertNotIn("private@example.test", text)
        self.assertNotIn("never-leak", text)
        self.assertNotIn("client_secret", text)

    async def test_instances_expand_daily_weekly_monthly_rules(self):
        await self.create({"summary": "Daily", "start": {"date": "2026-03-20"}, "end": {"date": "2026-03-21"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"]})
        await self.create({"summary": "Weekly", "start": {"date": "2026-03-02"}, "end": {"date": "2026-03-03"},
                           "recurrence": ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;COUNT=4"]})
        await self.create({"summary": "Monthly", "start": {"date": "2026-01-13"}, "end": {"date": "2026-01-14"},
                           "recurrence": ["RRULE:FREQ=MONTHLY;INTERVAL=2;BYDAY=2TU;UNTIL=20260331"]})
        response = await self.instances("from=2026-01-01&to=2026-03-31")
        self.assertEqual(response.status, 200)
        state = await response.json()
        api_contract.CalendarInstances.model_validate(state)
        by_summary = {}
        for instance in state["instances"]:
            by_summary.setdefault(instance["summary"], []).append(instance["start"]["date"])
        self.assertEqual(by_summary, {
            "Monthly": ["2026-01-13", "2026-03-10"],
            "Weekly": ["2026-03-02", "2026-03-04", "2026-03-16", "2026-03-18"],
            "Daily": ["2026-03-20", "2026-03-21", "2026-03-22"],
        })
        self.assertFalse(state["truncated"])

    async def test_instances_expand_rdate_and_drop_exdate(self):
        await self.create({"summary": "Extra dates", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=3", "RDATE;VALUE=DATE:20260110",
                                          "EXDATE;VALUE=DATE:20260102"]})
        response = await self.instances("from=2026-01-01&to=2026-01-11")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual([item["start"]["date"] for item in state["instances"]],
                         ["2026-01-01", "2026-01-03", "2026-01-10"])
        self.assertEqual([item["recurrence_id"]["date"] for item in state["instances"]],
                         ["2026-01-01", "2026-01-03", "2026-01-10"])

    async def test_instances_apply_moved_and_cancelled_exceptions(self):
        series = await self.create({"summary": "Base", "location": "Home",
                           "start": {"date": "2025-12-30"}, "end": {"date": "2025-12-31"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=4"], "exceptions": [
                               {"originalStartTime": {"date": "2025-12-31"}, "summary": "Moved", "location": "Office",
                                "start": {"date": "2026-01-05"}, "end": {"date": "2026-01-06"}},
                               {"originalStartTime": {"date": "2026-01-01"}, "status": "cancelled"},
                           ]})
        response = await self.instances("from=2026-01-01&to=2026-01-06")
        self.assertEqual(response.status, 200)
        instances = (await response.json())["instances"]
        self.assertEqual([(item["summary"], item["start"]["date"], item["recurrence_id"]["date"])
                          for item in instances], [("Base", "2026-01-02", "2026-01-02"),
                                                   ("Moved", "2026-01-05", "2025-12-31")])
        self.assertEqual(instances[1]["location"], "Office")
        self.assertEqual(instances[1]["instance_id"], series["id"] + "/2025-12-31")
        identifiers = [item["instance_id"] for item in instances]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        repeated = await self.instances("from=2026-01-01&to=2026-01-06")
        self.assertEqual([item["instance_id"] for item in (await repeated.json())["instances"]], identifiers)

    async def test_instances_exclude_cancelled_events_series_and_exceptions(self):
        await self.create({"summary": "Visible", "start": {"date": "2026-01-02"}, "end": {"date": "2026-01-03"}})
        await self.create({"summary": "Cancelled single", "status": "cancelled",
                           "start": {"date": "2026-01-03"}, "end": {"date": "2026-01-04"}})
        await self.create({"summary": "Cancelled master", "status": "cancelled",
                           "start": {"date": "2026-01-04"}, "end": {"date": "2026-01-05"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]})
        await self.create({"summary": "Exception base", "start": {"date": "2026-01-06"}, "end": {"date": "2026-01-07"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"], "exceptions": [
                               {"originalStartTime": {"date": "2026-01-07"}, "status": "cancelled"}]})
        response = await self.instances("from=2026-01-01&to=2026-01-10")
        instances = (await response.json())["instances"]
        self.assertEqual([item["summary"] for item in instances], ["Visible", "Exception base"])
        self.assertEqual([item["start"]["date"] for item in instances], ["2026-01-02", "2026-01-06"])
        self.assertTrue(all(item["status"] != "cancelled" for item in instances))

    async def test_instances_preserve_all_day_and_timed_representations(self):
        dates = await self.create({"summary": "Dates", "start": {"date": "2026-06-10"}, "end": {"date": "2026-06-12"}})
        timed = await self.create({"summary": "Timed", "location": "Warsaw", "status": "tentative",
                           "start": {"dateTime": "2026-06-10T09:00:00", "timeZone": "Europe/Warsaw"},
                           "end": {"dateTime": "2026-06-10T10:00:00", "timeZone": "Europe/Warsaw"}})
        response = await self.instances("from=2026-06-10&to=2026-06-11")
        state = await response.json()
        api_contract.CalendarInstances.model_validate(state)
        instances = {item["summary"]: item for item in state["instances"]}
        self.assertEqual(instances["Dates"]["start"], {"date": "2026-06-10"})
        self.assertEqual(instances["Dates"]["end"], {"date": "2026-06-12"})
        self.assertTrue(instances["Dates"]["all_day"])
        self.assertEqual(instances["Timed"]["start"],
                         {"dateTime": "2026-06-10T09:00:00+02:00", "timeZone": "Europe/Warsaw"})
        self.assertEqual(instances["Timed"]["end"],
                         {"dateTime": "2026-06-10T10:00:00+02:00", "timeZone": "Europe/Warsaw"})
        self.assertFalse(instances["Timed"]["all_day"])
        self.assertEqual(instances["Timed"]["status"], "tentative")
        self.assertEqual(instances["Dates"]["instance_id"], dates["id"])
        self.assertEqual(instances["Timed"]["instance_id"], timed["id"])
        self.assertTrue(all(item["recurrence_id"] is None for item in state["instances"]))

    async def test_instances_keep_warsaw_wall_time_across_dst(self):
        series = await self.create({"summary": "DST",
                           "start": {"dateTime": "2026-03-22T09:00:00+01:00", "timeZone": "Europe/Warsaw"},
                           "end": {"dateTime": "2026-03-22T10:00:00+01:00", "timeZone": "Europe/Warsaw"},
                           "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=3"]})
        response = await self.instances("from=2026-03-22&to=2026-04-06")
        instances = (await response.json())["instances"]
        self.assertEqual([(item["start"]["dateTime"], item["end"]["dateTime"]) for item in instances], [
            ("2026-03-22T09:00:00+01:00", "2026-03-22T10:00:00+01:00"),
            ("2026-03-29T09:00:00+02:00", "2026-03-29T10:00:00+02:00"),
            ("2026-04-05T09:00:00+02:00", "2026-04-05T10:00:00+02:00"),
        ])
        self.assertTrue(all(item["start"]["timeZone"] == "Europe/Warsaw" for item in instances))
        self.assertEqual([item["instance_id"] for item in instances], [
            series["id"] + "/2026-03-22T08:00:00+00:00",
            series["id"] + "/2026-03-29T07:00:00+00:00",
            series["id"] + "/2026-04-05T07:00:00+00:00",
        ])

    async def test_instances_use_overlap_and_half_open_window_boundaries(self):
        events = [
            ("Overlaps start", "2025-12-31T23:30:00Z", "2026-01-01T00:30:00Z"),
            ("Ends at start", "2025-12-31T22:30:00Z", "2026-01-01T00:00:00Z"),
            ("Point at start", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
            ("Ends at window end", "2026-01-01T23:30:00Z", "2026-01-02T00:00:00Z"),
            ("Starts at window end", "2026-01-02T00:00:00Z", "2026-01-02T01:00:00Z"),
        ]
        for summary, starting, ending in events:
            await self.create({"summary": summary,
                               "start": {"dateTime": starting, "timeZone": "UTC"},
                               "end": {"dateTime": ending, "timeZone": "UTC"}})
        await self.create({"summary": "All-day before", "start": {"date": "2025-12-31"}, "end": {"date": "2026-01-01"}})
        await self.create({"summary": "All-day within", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}})
        await self.create({"summary": "All-day at end", "start": {"date": "2026-01-02"}, "end": {"date": "2026-01-03"}})
        response = await self.instances("from=2026-01-01&to=2026-01-02")
        instances = (await response.json())["instances"]
        self.assertEqual({item["summary"] for item in instances},
                         {"Overlaps start", "Point at start", "All-day within", "Ends at window end"})

    async def test_instances_validate_dates_and_the_92_day_limit(self):
        response = await self.instances("from=2026-01-01&to=2026-04-03")
        self.assertEqual(response.status, 200)
        response = await self.instances("from=2026-01-01&to=2026-01-01")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["instances"], [])
        invalid = ["", "from=2026-01-01", "to=2026-01-01", "from=20260101&to=2026-01-02",
                   "from=2026-02-30&to=2026-03-01", "from=2026-01-03&to=2026-01-02",
                   "from=2026-01-01&to=2026-04-04"]
        for query in invalid:
            with self.subTest(query=query):
                response = await self.instances(query)
                self.assertEqual(response.status, 400)
                self.assertEqual(await response.json(), {"error": "invalid_calendar_event"})
        response = await self.instances("from=2026-01-01&to=2026-01-02&group=unknown")
        self.assertEqual(response.status, 400)
        self.assertEqual(await response.json(), {"error": "unknown_calendar_group"})

    async def test_instances_cap_and_truncated_flag(self):
        await self.create_dense_daily_series()
        response = await self.instances("from=2026-01-01&to=2026-04-01")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual(len(state["instances"]), 2000)
        self.assertTrue(state["truncated"])
        self.assertEqual(state["instances"][0]["start"]["dateTime"], "2026-01-01T00:00:00+00:00")
        stamps = [item["start"]["dateTime"] for item in state["instances"]]
        self.assertEqual(stamps, sorted(stamps))
        response = await self.instances("from=2026-01-01&to=2026-01-02")
        state = await response.json()
        self.assertEqual(len(state["instances"]), 24)
        self.assertFalse(state["truncated"])

    async def test_instances_group_filter_defaults_like_calendar_read(self):
        other = {"id": "other", "calendars": [{"id": "other-google", "account": "google",
                                               "calendar_id": "other@example.test"}]}
        self.config_path.write_text(json.dumps({**self.config, "groups": [*self.config["groups"], other]}))
        family = await self.create({"summary": "Family instance", "start": {"date": "2026-01-05"},
                                    "end": {"date": "2026-01-06"}})
        work = await self.create({"summary": "Other instance", "start": {"date": "2026-01-05"},
                                  "end": {"date": "2026-01-06"}}, group="other")
        calendar_response = await self.client.get(api.PATH, headers=self.headers("viewer"))
        calendar_groups = (await calendar_response.json())["groups"]
        self.assertEqual(calendar_groups, [{"id": "family"}, {"id": "other"}])
        for query, expected in [("from=2026-01-01&to=2026-01-10", ("family", family["id"])),
                                ("from=2026-01-01&to=2026-01-10&group=family", ("family", family["id"])),
                                ("from=2026-01-01&to=2026-01-10&group=other", ("other", work["id"]))]:
            with self.subTest(query=query):
                response = await self.instances(query)
                state = await response.json()
                self.assertTrue(state["enabled"])
                self.assertEqual(state["groups"], calendar_groups)
                instances = state["instances"]
                self.assertEqual([(item["group"], item["event_id"]) for item in instances], [expected])

    async def test_instances_old_daily_rule_uses_replay_bound(self):
        await self.create({"summary": "Ancient", "start": {"date": "0001-01-01"}, "end": {"date": "0001-01-02"},
                           "recurrence": ["RRULE:FREQ=DAILY"]})
        with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library replay")):
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
                response = await self.instances("from=9999-01-01&to=9999-01-04")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual(state["instances"], [])
        self.assertTrue(state["truncated"])
        self.assertIn("replay bound", "\n".join(logs.output))

    async def test_instances_occurrence_bound_falls_back_before_library(self):
        event = await self.create({"summary": "Dense",
                                   "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                                   "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                                   "recurrence": ["RRULE:FREQ=DAILY"]})
        with mock.patch.object(instance_expander, "OCCURRENCE_BOUND", 10):
            with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library expansion")):
                with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
                    response = await self.instances("from=2026-01-01&to=2026-01-21")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual([item["event_id"] for item in state["instances"]], [event["id"]])
        self.assertTrue(state["truncated"])
        self.assertIn("occurrence bound", "\n".join(logs.output))

    async def test_instances_hourly_uses_dtstart_fallback(self):
        event = await self.create({"summary": "Hourly",
                                   "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                                   "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                                   "recurrence": ["RRULE:FREQ=HOURLY"]})
        with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library expansion")):
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
                response = await self.instances("from=2026-01-01&to=2026-01-03")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual([item["event_id"] for item in state["instances"]], [event["id"]])
        self.assertTrue(state["truncated"])
        self.assertIn("HOURLY", "\n".join(logs.output))

    async def test_instances_fallback_handles_dtstart_exdate_and_exceptions(self):
        active = await self.create({"summary": "Master",
                                    "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                                    "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                                    "recurrence": ["RRULE:FREQ=HOURLY"], "exceptions": [
                                        {"originalStartTime": {"dateTime": "2025-12-31T00:00:00Z"}, "status": "cancelled"},
                                        {"originalStartTime": {"dateTime": "2026-01-01T00:00:00Z"},
                                         "summary": "Active", "start": {"dateTime": "2026-01-01T02:00:00Z", "timeZone": "UTC"},
                                         "end": {"dateTime": "2026-01-01T03:00:00Z", "timeZone": "UTC"}}]})
        await self.create({"summary": "Cancelled exception",
                           "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=HOURLY"], "exceptions": [
                               {"originalStartTime": {"dateTime": "2026-01-01T00:00:00Z"}, "status": "cancelled"}]})
        await self.create({"summary": "Cancelled", "status": "cancelled",
                           "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=HOURLY"]})
        await self.create({"summary": "Excluded", "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=HOURLY", "EXDATE:20260101T000000Z"]})
        with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
            response = await self.instances("from=2026-01-01&to=2026-01-02")
        instances = (await response.json())["instances"]
        self.assertEqual([(item["event_id"], item["summary"]) for item in instances], [(active["id"], "Active")])
        self.assertEqual(instances[0]["start"]["dateTime"], "2026-01-01T02:00:00+00:00")

    async def test_instances_override_moved_outside_window_is_not_returned(self):
        await self.create({"summary": "Base", "start": {"dateTime": "2026-01-05T09:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-05T10:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=1"], "exceptions": [
                               {"originalStartTime": {"dateTime": "2026-01-05T09:00:00Z"},
                                "start": {"dateTime": "2026-01-06T09:00:00Z", "timeZone": "UTC"},
                                "end": {"dateTime": "2026-01-06T10:00:00Z", "timeZone": "UTC"}}]})
        response = await self.instances("from=2026-01-05&to=2026-01-06")
        self.assertEqual((await response.json())["instances"], [])

    async def test_instances_cancelled_master_keeps_active_override(self):
        event = await self.create({"summary": "Cancelled", "status": "cancelled",
                                   "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"},
                                   "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"], "exceptions": [
                                       {"originalStartTime": {"date": "2026-01-02"}, "summary": "Active override",
                                        "status": "confirmed", "start": {"date": "2026-01-03"}, "end": {"date": "2026-01-04"}}]})
        response = await self.instances("from=2026-01-01&to=2026-01-05")
        self.assertEqual(response.status, 200)
        instances = (await response.json())["instances"]
        self.assertEqual([item["summary"] for item in instances], ["Active override"])
        self.assertEqual(instances[0]["recurrence_id"], {"date": "2026-01-02"})
        self.assertEqual(instances[0]["instance_id"], event["id"] + "/2026-01-02")

    async def test_instances_shift_spring_gaps_and_deduplicate_instants(self):
        await self.create({"summary": "Gap",
                           "start": {"dateTime": "2026-03-28T02:30:00+01:00", "timeZone": "Europe/Warsaw"},
                           "end": {"dateTime": "2026-03-28T03:30:00+01:00", "timeZone": "Europe/Warsaw"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=3", "RDATE:20260329T013000Z"]})
        await self.create({"summary": "Until gap",
                           "start": {"dateTime": "2026-03-28T02:30:00+01:00", "timeZone": "Europe/Warsaw"},
                           "end": {"dateTime": "2026-03-28T02:45:00+01:00", "timeZone": "Europe/Warsaw"},
                           "recurrence": ["RRULE:FREQ=DAILY;UNTIL=20260329T013000Z"]})
        response = await self.instances("from=2026-03-28&to=2026-04-01")
        instances = (await response.json())["instances"]
        by_summary = {}
        for item in instances:
            by_summary.setdefault(item["summary"], []).append(item["start"]["dateTime"])
        self.assertEqual(by_summary["Gap"], ["2026-03-28T02:30:00+01:00", "2026-03-29T03:30:00+02:00",
                                             "2026-03-30T02:30:00+02:00"])
        self.assertEqual(by_summary["Until gap"], ["2026-03-28T02:30:00+01:00", "2026-03-29T03:30:00+02:00"])
        identifiers = [item["instance_id"] for item in instances]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    async def test_instances_fall_back_rdates_keep_distinct_utc_identities(self):
        event = await self.create({"summary": "Fold",
                                   "start": {"dateTime": "2026-10-24T09:00:00+02:00", "timeZone": "Europe/Warsaw"},
                                   "end": {"dateTime": "2026-10-24T10:00:00+02:00", "timeZone": "Europe/Warsaw"},
                                   "recurrence": ["RDATE:20261025T003000Z", "RDATE:20261025T013000Z",
                                                  "EXDATE:20261025T003000Z"], "exceptions": [
                                       {"originalStartTime": {"dateTime": "2026-10-25T01:30:00Z"},
                                        "summary": "Fold override",
                                        "start": {"dateTime": "2026-10-25T02:30:00+01:00", "timeZone": "Europe/Warsaw"},
                                        "end": {"dateTime": "2026-10-25T03:30:00+01:00", "timeZone": "Europe/Warsaw"}}]})
        response = await self.instances("from=2026-10-25&to=2026-10-26")
        instances = (await response.json())["instances"]
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["summary"], "Fold override")
        self.assertEqual(instances[0]["start"],
                         {"dateTime": "2026-10-25T02:30:00+01:00", "timeZone": "Europe/Warsaw"})
        self.assertEqual(instances[0]["recurrence_id"],
                         {"dateTime": "2026-10-25T02:30:00+01:00", "timeZone": "Europe/Warsaw"})
        self.assertEqual(instances[0]["instance_id"], event["id"] + "/2026-10-25T01:30:00+00:00")

    async def test_instances_second_fold_cutoff_keeps_first_fold_occurrence(self):
        event = await self.create({"summary": "First fold",
                                   "start": {"dateTime": "2026-10-24T02:30:00+02:00", "timeZone": "Europe/Warsaw"},
                                   "end": {"dateTime": "2026-10-24T03:30:00+02:00", "timeZone": "Europe/Warsaw"},
                                   "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]})
        window = instance_expander._Window(date(2026, 10, 25), date(2026, 10, 26), ZoneInfo("UTC"),
                                           datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc),
                                           datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc))
        instances, incomplete = instance_expander._expand_event(event, window)
        self.assertFalse(incomplete)
        self.assertEqual([item["start"]["dateTime"] for item in instances], ["2026-10-25T02:30:00+02:00"])

    async def test_instances_second_fold_dtstart_keeps_actual_start_and_later_rules(self):
        event = await self.create({"summary": "Second fold",
                                   "start": {"dateTime": "2026-10-25T02:30:00+01:00", "timeZone": "Europe/Warsaw"},
                                   "end": {"dateTime": "2026-10-25T03:30:00+01:00", "timeZone": "Europe/Warsaw"},
                                   "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]})
        response = await self.instances("from=2026-10-25&to=2026-10-27")
        instances = (await response.json())["instances"]
        self.assertEqual(len(instances), 2)
        self.assertEqual([item["start"]["dateTime"] for item in instances],
                         ["2026-10-25T02:30:00+01:00", "2026-10-26T02:30:00+01:00"])
        self.assertEqual(instances[0]["instance_id"], event["id"] + "/2026-10-25T01:30:00+00:00")

    async def test_instances_fold_straddling_master_preserves_anchor_and_offsets(self):
        event = await self.create({"summary": "Fold duration",
                                   "start": {"dateTime": "2026-10-25T02:45:00+02:00", "timeZone": "Europe/Warsaw"},
                                   "end": {"dateTime": "2026-10-25T02:15:00+01:00", "timeZone": "Europe/Warsaw"},
                                   "recurrence": ["RRULE:FREQ=DAILY;COUNT=1"]})
        response = await self.instances("from=2026-10-25&to=2026-10-26")
        state = await response.json()
        instances = state["instances"]
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["start"],
                         {"dateTime": "2026-10-25T02:45:00+02:00", "timeZone": "Europe/Warsaw"})
        self.assertEqual(instances[0]["end"],
                         {"dateTime": "2026-10-25T02:15:00+01:00", "timeZone": "Europe/Warsaw"})
        self.assertEqual(instances[0]["recurrence_id"], instances[0]["start"])
        self.assertEqual(instances[0]["instance_id"], event["id"] + "/2026-10-25T00:45:00+00:00")
        self.assertFalse(state["truncated"])

    async def test_instances_orphan_override_is_skipped_with_debug_log(self):
        await self.create({"summary": "Monday master",
                           "start": {"dateTime": "2026-01-05T09:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-05T10:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=2"], "exceptions": [
                               {"originalStartTime": {"dateTime": "2026-01-06T09:00:00Z"},
                                "summary": "Orphan", "start": {"dateTime": "2026-01-07T09:00:00Z", "timeZone": "UTC"},
                                "end": {"dateTime": "2026-01-07T10:00:00Z", "timeZone": "UTC"}}]})
        with self.assertLogs("hestia.calendar_sync.instances", level="DEBUG") as logs:
            response = await self.instances("from=2026-01-05&to=2026-01-13")
        self.assertEqual(response.status, 200)
        instances = (await response.json())["instances"]
        self.assertEqual([item["summary"] for item in instances], ["Monday master", "Monday master"])
        self.assertIn("orphan override", "\n".join(logs.output))

    async def test_instances_moved_override_membership_is_checked_against_series(self):
        await self.create({"summary": "Base",
                           "start": {"dateTime": "2026-01-01T09:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-03T09:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=DAILY;UNTIL=20260110T000000Z"], "exceptions": [
                               {"originalStartTime": {"dateTime": "2026-01-01T09:00:00Z"}, "summary": "Moved explicit",
                                "start": {"dateTime": "2026-01-20T09:00:00Z", "timeZone": "UTC"},
                                "end": {"dateTime": "2026-01-20T10:00:00Z", "timeZone": "UTC"}},
                               {"originalStartTime": {"dateTime": "2026-01-02T09:00:00Z"}, "summary": "Moved series",
                                "start": {"dateTime": "2026-01-21T09:00:00Z", "timeZone": "UTC"},
                                "end": {"dateTime": "2026-01-21T10:00:00Z", "timeZone": "UTC"}}]})
        with mock.patch.object(instance_expander, "of", wraps=instance_expander.of) as library:
            response = await self.instances("from=2026-01-20&to=2026-01-22")
        self.assertEqual(library.call_count, 1)
        instances = (await response.json())["instances"]
        self.assertEqual([item["summary"] for item in instances], ["Moved explicit", "Moved series"])
        self.assertEqual(instances[0]["recurrence_id"]["dateTime"], "2026-01-01T09:00:00+00:00")
        self.assertEqual(instances[1]["recurrence_id"]["dateTime"], "2026-01-02T09:00:00+00:00")

    async def test_instances_yearly_weekday_expansion_falls_back_before_library(self):
        hours = ",".join(str(value) for value in range(24))
        seconds = ",".join(str(value) for value in range(60))
        await self.create({"summary": "Pathological yearly",
                           "start": {"dateTime": "1900-01-01T00:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "1900-01-01T01:00:00Z", "timeZone": "UTC"},
                           "recurrence": [f"RRULE:FREQ=YEARLY;BYDAY=MO;BYHOUR={hours};BYSECOND={seconds}"]})
        started = time.monotonic()
        with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library replay")):
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
                response = await self.instances("from=2026-01-01&to=2026-02-01")
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual(state["instances"], [])
        self.assertTrue(state["truncated"])
        self.assertIn("unsupported recurrence", "\n".join(logs.output))

    async def test_instances_impossible_month_day_falls_back_before_library(self):
        event = await self.create({"summary": "Impossible date",
                                   "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                                   "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                                   "recurrence": ["RRULE:FREQ=DAILY;COUNT=1;BYMONTH=2;BYMONTHDAY=30"]})
        started = time.monotonic()
        with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library replay")):
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
                response = await self.instances("from=2026-01-01&to=2026-02-01")
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual([item["event_id"] for item in state["instances"]], [event["id"]])
        self.assertTrue(state["truncated"])
        self.assertIn("unsupported recurrence", "\n".join(logs.output))

    async def test_instances_stale_override_does_not_widen_generation(self):
        await self.create({"summary": "Daily", "start": {"date": "2026-03-01"}, "end": {"date": "2026-03-02"},
                           "recurrence": ["RRULE:FREQ=DAILY"], "exceptions": [
                               {"originalStartTime": {"date": "2000-01-01"}, "summary": "Stale",
                                "start": {"date": "2026-04-02"}, "end": {"date": "2026-04-03"}},
                               {"originalStartTime": {"date": "2000-01-02"}, "summary": "Standalone",
                                "start": {"date": "2026-04-01"}, "end": {"date": "2026-04-02"}}]})
        with mock.patch.object(instance_expander, "of", wraps=instance_expander.of) as library:
            response = await self.instances("from=2026-04-01&to=2026-04-02")
        self.assertEqual(library.call_count, 1)
        state = await response.json()
        self.assertEqual({item["summary"] for item in state["instances"]}, {"Daily", "Standalone"})
        self.assertNotIn("Stale", [item["summary"] for item in state["instances"]])
        self.assertFalse(state["truncated"])

    async def test_instances_bysetpos_on_daily_falls_back_before_library(self):
        event = await self.create({"summary": "No second candidate",
                                   "start": {"dateTime": "2026-01-01T09:00:00Z", "timeZone": "UTC"},
                                   "end": {"dateTime": "2026-01-01T10:00:00Z", "timeZone": "UTC"},
                                   "recurrence": ["RRULE:FREQ=DAILY;COUNT=1;BYHOUR=9;BYSETPOS=2"]})
        started = time.monotonic()
        with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library replay")):
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
                response = await self.instances("from=2026-01-01&to=2026-01-02")
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual([item["event_id"] for item in state["instances"]], [event["id"]])
        self.assertTrue(state["truncated"])
        self.assertIn("unsupported recurrence", "\n".join(logs.output))

    async def test_instances_yearly_month_day_time_selectors_fall_back_before_library(self):
        hours = ",".join(str(value) for value in range(24))
        seconds = ",".join(str(value) for value in range(60))
        await self.create({"summary": "Pathological year days",
                           "start": {"dateTime": "1900-01-01T00:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "1900-01-01T01:00:00Z", "timeZone": "UTC"},
                           "recurrence": [f"RRULE:FREQ=YEARLY;BYMONTHDAY=1;BYHOUR={hours};BYSECOND={seconds}"]})
        started = time.monotonic()
        with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library replay")):
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
                response = await self.instances("from=2026-01-01&to=2026-02-01")
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual(state["instances"], [])
        self.assertTrue(state["truncated"])
        self.assertIn("unsupported recurrence", "\n".join(logs.output))

    async def test_instances_enumerated_shapes_s1_through_s7_expand(self):
        # S1: no BY*, implicit DTSTART day (including day 31 and Feb 29 at INTERVAL=1 and INTERVAL=1000)
        await self.create({"summary": "S1 daily 1000", "start": {"date": "2026-01-05"}, "end": {"date": "2026-01-06"},
                           "recurrence": ["RRULE:FREQ=DAILY;INTERVAL=1000"]})
        await self.create({"summary": "S1 monthly 31", "start": {"date": "2026-01-31"}, "end": {"date": "2026-02-01"},
                           "recurrence": ["RRULE:FREQ=MONTHLY"]})
        await self.create({"summary": "S1 yearly leap", "start": {"date": "2024-02-29"}, "end": {"date": "2024-03-01"},
                           "recurrence": ["RRULE:FREQ=YEARLY"]})
        # S2: WEEKLY plain BYDAY with INTERVAL > 1
        await self.create({"summary": "S2 weekly", "start": {"date": "2026-01-05"}, "end": {"date": "2026-01-06"},
                           "recurrence": ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE"]})
        # S3: DAILY plain BYDAY at INTERVAL=1
        await self.create({"summary": "S3 daily byday", "start": {"date": "2026-01-05"}, "end": {"date": "2026-01-06"},
                           "recurrence": ["RRULE:FREQ=DAILY;BYDAY=MO,WE,FR"]})
        # S4: MONTHLY single BYMONTHDAY (-1 and 15 with INTERVAL=2)
        await self.create({"summary": "S4 monthly -1", "start": {"date": "2026-01-31"}, "end": {"date": "2026-02-01"},
                           "recurrence": ["RRULE:FREQ=MONTHLY;BYMONTHDAY=-1"]})
        await self.create({"summary": "S4 monthly 15 int2", "start": {"date": "2026-01-15"}, "end": {"date": "2026-01-16"},
                           "recurrence": ["RRULE:FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=15"]})
        # S5(a) & S5(b): MONTHLY plain BYDAY and single ordinal BYDAY (2TU, -4FR int2, -5FR int1)
        await self.create({"summary": "S5a monthly plain", "start": {"date": "2026-01-02"}, "end": {"date": "2026-01-03"},
                           "recurrence": ["RRULE:FREQ=MONTHLY;INTERVAL=2;BYDAY=MO,FR"]})
        await self.create({"summary": "S5b monthly 2TU", "start": {"date": "2026-01-13"}, "end": {"date": "2026-01-14"},
                           "recurrence": ["RRULE:FREQ=MONTHLY;BYDAY=2TU"]})
        await self.create({"summary": "S5b monthly -5FR", "start": {"date": "2026-01-02"}, "end": {"date": "2026-01-03"},
                           "recurrence": ["RRULE:FREQ=MONTHLY;BYDAY=-5FR"]})
        await self.create({"summary": "S5b monthly -4FR int2", "start": {"date": "2026-01-09"}, "end": {"date": "2026-01-10"},
                           "recurrence": ["RRULE:FREQ=MONTHLY;INTERVAL=2;BYDAY=-4FR"]})
        # S6: MONTHLY plain BYDAY with single BYSETPOS (including Codex (c) MO-FR BYSETPOS=6)
        await self.create({"summary": "S6 sixth weekday",
                           "start": {"dateTime": "2026-01-08T09:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-08T10:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=6"]})
        await self.create({"summary": "S6 last weekday int2", "start": {"date": "2026-01-30"}, "end": {"date": "2026-01-31"},
                           "recurrence": ["RRULE:FREQ=MONTHLY;INTERVAL=2;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"]})
        # S7(a), S7(b), S7(c): YEARLY single BYMONTH with single BYMONTHDAY / single ordinal BYDAY / plain BYDAY
        await self.create({"summary": "S7a yearly leap", "start": {"date": "2024-02-29"}, "end": {"date": "2024-03-01"},
                           "recurrence": ["RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29"]})
        await self.create({"summary": "S7a yearly June 15 int2", "start": {"date": "2024-06-15"}, "end": {"date": "2024-06-16"},
                           "recurrence": ["RRULE:FREQ=YEARLY;INTERVAL=2;BYMONTH=6;BYMONTHDAY=15"]})
        await self.create({"summary": "S7b yearly 4TH Nov", "start": {"date": "2025-11-27"}, "end": {"date": "2025-11-28"},
                           "recurrence": ["RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=4TH"]})
        await self.create({"summary": "S7b yearly 5FR Jan", "start": {"date": "2026-01-30"}, "end": {"date": "2026-01-31"},
                           "recurrence": ["RRULE:FREQ=YEARLY;BYMONTH=1;BYDAY=5FR"]})
        await self.create({"summary": "S7c yearly March plain", "start": {"date": "2025-03-03"}, "end": {"date": "2025-03-04"},
                           "recurrence": ["RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=MO,FR"]})

        def by_summary(state):
            grouped = {}
            for item in state["instances"]:
                value = item["start"].get("date") or item["start"]["dateTime"][:10]
                grouped.setdefault(item["summary"], []).append(value)
            return grouped

        q1 = await (await self.instances("from=2026-01-01&to=2026-04-01")).json()
        self.assertFalse(q1["truncated"])
        s1_q1 = by_summary(q1)
        self.assertEqual(s1_q1["S1 daily 1000"], ["2026-01-05"])
        self.assertEqual(s1_q1["S1 monthly 31"], ["2026-01-31", "2026-03-31"])
        self.assertEqual(s1_q1["S2 weekly"][:4], ["2026-01-05", "2026-01-07", "2026-01-19", "2026-01-21"])
        self.assertEqual(s1_q1["S3 daily byday"][:3], ["2026-01-05", "2026-01-07", "2026-01-09"])
        self.assertEqual(s1_q1["S4 monthly -1"], ["2026-01-31", "2026-02-28", "2026-03-31"])
        self.assertEqual(s1_q1["S4 monthly 15 int2"], ["2026-01-15", "2026-03-15"])
        self.assertEqual(s1_q1["S5a monthly plain"][-3:], ["2026-03-23", "2026-03-27", "2026-03-30"])
        self.assertEqual(s1_q1["S5b monthly 2TU"], ["2026-01-13", "2026-02-10", "2026-03-10"])
        self.assertEqual(s1_q1["S5b monthly -5FR"], ["2026-01-02"])
        self.assertEqual(s1_q1["S5b monthly -4FR int2"], ["2026-01-09", "2026-03-06"])
        self.assertEqual(s1_q1["S6 sixth weekday"], ["2026-01-08", "2026-02-09", "2026-03-09"])
        self.assertEqual(s1_q1["S6 last weekday int2"], ["2026-01-30", "2026-03-31"])
        self.assertEqual(s1_q1["S7b yearly 5FR Jan"], ["2026-01-30"])
        self.assertEqual(s1_q1["S7c yearly March plain"][:3], ["2026-03-02", "2026-03-06", "2026-03-09"])

        q_jun = await (await self.instances("from=2026-06-01&to=2026-07-01")).json()
        self.assertEqual(by_summary(q_jun)["S7a yearly June 15 int2"], ["2026-06-15"])
        q_nov = await (await self.instances("from=2026-11-01&to=2026-12-01")).json()
        self.assertEqual(by_summary(q_nov)["S7b yearly 4TH Nov"], ["2026-11-26"])
        q_leap = await (await self.instances("from=2028-02-28&to=2028-03-01")).json()
        self.assertEqual(by_summary(q_leap)["S1 yearly leap"], ["2028-02-29"])
        self.assertEqual(by_summary(q_leap)["S7a yearly leap"], ["2028-02-29"])

    async def test_instances_codex_pathological_rules_fall_back_fast(self):
        cases = [
            ("Codex (1) 2TU+1st", "2026-01-01T09:00:00Z", "from=2026-01-01&to=2026-02-01",
             "RRULE:FREQ=MONTHLY;BYDAY=2TU;BYMONTHDAY=1", True),
            ("Codex (2) mixed BYDAY", "2026-01-01T09:00:00Z", "from=2026-01-01&to=2026-02-01",
             "RRULE:FREQ=MONTHLY;INTERVAL=2;BYDAY=MO,1TU", True),
            ("Codex (3) yearly 7wd pos364", "1500-01-01T09:00:00Z", "from=2026-01-01&to=2026-02-01",
             "RRULE:FREQ=YEARLY;BYDAY=MO,TU,WE,TH,FR,SA,SU;BYSETPOS=364", False),
            ("Codex (a) leap weekday pos", "2026-01-01T09:00:00Z", "from=2026-01-01&to=2026-02-01",
             "RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;BYDAY=MO;BYSETPOS=2", True),
            ("Codex (b) Jan->Jun", "2026-01-01T09:00:00Z", "from=2026-01-01&to=2026-02-01",
             "RRULE:FREQ=MONTHLY;INTERVAL=12;BYMONTH=6", True),
        ]
        for summary, start, query, rule, expect_dtstart in cases:
            with self.subTest(summary=summary):
                event = await self.create({"summary": summary,
                                           "start": {"dateTime": start, "timeZone": "UTC"},
                                           "end": {"dateTime": start.replace("09:00", "10:00"), "timeZone": "UTC"},
                                           "recurrence": [rule]})
                started = time.monotonic()
                with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library replay")):
                    with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                        response = await self.instances(query)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(response.status, 200)
                state = await response.json()
                ids = [item["event_id"] for item in state["instances"]]
                self.assertEqual(event["id"] in ids, expect_dtstart)
                self.assertTrue(state["truncated"])

    async def test_instances_event_amplification_and_duration_caps_fall_back_fast(self):
        # Issue 1: 1000 S1 RRULEs + 1300 RDATEs must fall back in <1s without calling of().
        rrules = [f"RRULE:FREQ=DAILY;COUNT=1;INTERVAL={n}" for n in range(1, 1001)]
        rdates = [f"RDATE:202601{(i % 28) + 1:02d}T{i % 24:02d}0000Z" for i in range(1300)]
        amplified = {"id": "amp", "uid": "amp", "revision": 1, "group_id": "family",
                     "payload": {"summary": "Amplified",
                                 "start": {"dateTime": "2026-01-01T09:00:00Z", "timeZone": "UTC"},
                                 "end": {"dateTime": "2026-01-01T10:00:00Z", "timeZone": "UTC"},
                                 "recurrence": [*rrules, *rdates]}}
        started = time.monotonic()
        with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library replay")):
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                direct = instance_expander.expand_events([amplified], date(2026, 1, 1), date(2026, 1, 2))
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(direct["truncated"])
        self.assertEqual([item["event_id"] for item in direct["instances"]], ["amp"])

        # HTTP route regressions for >1 RRULE, >500 RDATE, >500 EXDATE, and Issue 2 (>31-day duration).
        too_many_rdates = [f"RDATE:202601{(i // 24) + 1:02d}T{i % 24:02d}0000Z" for i in range(501)]
        too_many_exdates = [f"EXDATE:202601{(i // 24) + 1:02d}T{i % 24:02d}0000Z" for i in range(501)]
        for summary, start, end, rec, query in [
            ("Two rules", {"date": "2026-01-01"}, {"date": "2026-01-02"},
             ["RRULE:FREQ=DAILY", "RRULE:FREQ=WEEKLY"], "from=2026-01-01&to=2026-01-03"),
            ("Over 500 RDATE", {"dateTime": "2026-02-01T09:00:00Z", "timeZone": "UTC"},
             {"dateTime": "2026-02-01T10:00:00Z", "timeZone": "UTC"}, too_many_rdates, "from=2026-02-01&to=2026-02-02"),
            ("Over 500 EXDATE", {"dateTime": "2026-03-01T09:00:00Z", "timeZone": "UTC"},
             {"dateTime": "2026-03-01T10:00:00Z", "timeZone": "UTC"}, too_many_exdates, "from=2026-03-01&to=2026-03-02"),
            ("Codex long span daily", {"date": "1500-01-01"}, {"date": "2026-01-03"},
             ["RRULE:FREQ=DAILY"], "from=2026-01-01&to=2026-01-04"),
            ("Over 31d RDATE", {"date": "2026-04-01"}, {"date": "2026-05-03"},
             ["RDATE;VALUE=DATE:20260415"], "from=2026-04-01&to=2026-05-05"),
        ]:
            with self.subTest(summary=summary):
                event = await self.create({"summary": summary, "start": start, "end": end, "recurrence": rec})
                started = time.monotonic()
                with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library replay")):
                    with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                        response = await self.instances(query)
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(response.status, 200)
                state = await response.json()
                self.assertIn(event["id"], [item["event_id"] for item in state["instances"]])
                self.assertTrue(state["truncated"])

    async def test_instances_31_day_recurring_and_long_nonrecurring_expand(self):
        await self.create({"summary": "31d recurring", "start": {"date": "2026-01-01"}, "end": {"date": "2026-02-01"},
                           "recurrence": ["RRULE:FREQ=MONTHLY;COUNT=2"]})
        await self.create({"summary": "Long single", "start": {"date": "1500-01-01"}, "end": {"date": "2026-01-03"}})
        response = await self.instances("from=2026-01-01&to=2026-02-05")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertFalse(state["truncated"])
        by_summary = {}
        for item in state["instances"]:
            by_summary.setdefault(item["summary"], []).append(item["start"]["date"])
        self.assertEqual(by_summary["31d recurring"], ["2026-01-01", "2026-02-01"])
        self.assertEqual(by_summary["Long single"], ["1500-01-01"])

    async def test_instances_codex_1000_series_shares_request_replay_budget(self):
        payload = {"summary": "Shared budget",
                   "start": {"dateTime": "2000-01-01T09:00:00Z", "timeZone": "UTC"},
                   "end": {"dateTime": "2000-01-01T10:00:00Z", "timeZone": "UTC"},
                   "recurrence": ["RRULE:FREQ=DAILY"]}
        ordered_ids = self._seed_group_events(1000, "daily", payload)
        per_event_bound = instance_expander._replay_upper_bound(
            payload, datetime(2026, 1, 4, tzinfo=timezone.utc), datetime(2026, 1, 1, tzinfo=timezone.utc)
        )
        expected_expanded = instance_expander.REQUEST_REPLAY_BUDGET // per_event_bound
        self.assertEqual(expected_expanded, 63)

        started = time.monotonic()
        with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
            response = await self.instances("from=2026-01-02&to=2026-01-03")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertTrue(state["truncated"])
        self.assertEqual([item["event_id"] for item in state["instances"]], ordered_ids[:expected_expanded])
        self.assertTrue(all(
            item["start"] == {"dateTime": "2026-01-02T09:00:00+00:00", "timeZone": "UTC"}
            for item in state["instances"]
        ))
        self.assertIn("remaining request replay bound", "\n".join(logs.output))

    def _seed_group_events(self, count: int, prefix: str, payload: dict) -> list[str]:
        encoded = json.dumps(payload)
        ordered_ids = [f"{prefix}-{index:04d}" for index in range(count)]
        store = api._store()
        with store.transaction() as session:
            session.add_all([
                db.CalendarEvent(
                    id=identifier, group_id="family", uid=f"{identifier}@hestia",
                    payload_json=encoded, fingerprint=f"fp-{identifier}",
                    deleted=False, deleted_at=None, revision=1, modified_at=0.0, source="local",
                )
                for identifier in ordered_ids
            ])
        return ordered_ids

    async def test_instances_codex_1000_rdate_and_exdate_events_charge_budget_before_parsing(self):
        rdates = [f"RDATE:202601{(i % 28) + 1:02d}T{i % 24:02d}{i % 60:02d}00Z" for i in range(500)]
        rdate_payload = {"summary": "Many RDATEs",
                         "start": {"dateTime": "2025-12-01T00:00:00+00:00", "timeZone": "UTC"},
                         "end": {"dateTime": "2025-12-01T01:00:00+00:00", "timeZone": "UTC"},
                         "recurrence": rdates}
        ordered_ids = self._seed_group_events(1000, "rdate", rdate_payload)
        started = time.monotonic()
        with mock.patch.object(instance_expander, "_property_values", wraps=instance_expander._property_values) as parser:
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                response = await self.instances("from=2026-01-01&to=2026-01-29")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertTrue(state["truncated"])
        self.assertEqual(len(state["instances"]), 2000)
        self.assertEqual(sorted({item["event_id"] for item in state["instances"]}), ordered_ids[:4])
        # Only the 4 events within budget parsed RDATE+EXDATE; remaining 996 were omitted untouched.
        self.assertEqual(parser.call_count, 8)

        # Verify 10,000 events with 500 later EXDATEs each parse only within budget, omit excess events safely, and cap at 5,000 events.
        exdates = [f"EXDATE:202601{(i % 27) + 2:02d}T{i % 24:02d}{i % 60:02d}00Z" for i in range(500)]
        exdate_events = [
            {"id": f"ex{i:05d}", "uid": f"ex{i:05d}", "revision": 1, "group_id": "family",
             "payload": {"summary": "Many EXDATEs",
                         "start": {"dateTime": "2026-01-01T09:00:00+00:00", "timeZone": "UTC"},
                         "end": {"dateTime": "2026-01-01T10:00:00+00:00", "timeZone": "UTC"},
                         "recurrence": ["RRULE:FREQ=DAILY", *exdates]}}
            for i in range(10000)
        ]
        started = time.monotonic()
        with mock.patch.object(instance_expander, "_property_values", wraps=instance_expander._property_values) as parser:
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                ex_result = instance_expander.expand_events(exdate_events, date(2026, 1, 1), date(2026, 1, 2))
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(ex_result["truncated"])
        self.assertEqual([item["event_id"] for item in ex_result["instances"]], ["ex00000", "ex00001", "ex00002"])
        self.assertEqual(parser.call_count, 6)

        # Verify the hard per-request cap (MAX_REQUEST_EVENTS = 5000) stops and truncates even without budget/collector overflow.
        cap_events = [
            {"id": f"cap{i:04d}", "uid": f"cap{i:04d}", "revision": 1, "group_id": "family",
             "payload": {"summary": "Cap check",
                         "start": {"date": "2026-01-01" if i == 5001 else "2025-12-01"},
                         "end": {"date": "2026-01-02" if i == 5001 else "2025-12-02"}}}
            for i in range(5005)
        ]
        started = time.monotonic()
        with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as cap_logs:
            cap_result = instance_expander.expand_events(cap_events, date(2026, 1, 1), date(2026, 1, 2))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(cap_result, {"instances": [], "truncated": True})
        self.assertIn("processing limit", "\n".join(cap_logs.output))

    async def test_instances_sql_limit_avoids_decoding_beyond_5001_rows(self):
        from hestia.calendar_sync.store import SyncStore

        old_payload = {"summary": "Old", "start": {"date": "2025-12-01"}, "end": {"date": "2025-12-02"}}
        in_window = {"summary": "Late row", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}}
        store = api._store()
        with store.transaction() as session:
            session.add_all([
                db.CalendarEvent(
                    id=f"row-{i:04d}", group_id="family", uid=f"row-{i:04d}@hestia",
                    payload_json=json.dumps(in_window if i == 5001 else old_payload),
                    fingerprint=f"fp-row-{i:04d}", deleted=False, deleted_at=None,
                    revision=1, modified_at=0.0, source="local",
                )
                for i in range(5005)
            ])
        started = time.monotonic()
        with mock.patch.object(SyncStore, "_event_dict", wraps=SyncStore._event_dict) as decoder:
            with mock.patch.object(SyncStore, "list_events", wraps=store.list_events) as listed:
                response = await self.instances("from=2026-01-01&to=2026-01-02")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual(state["instances"], [])
        self.assertTrue(state["truncated"])
        self.assertEqual(listed.call_args.kwargs.get("limit"), instance_expander.MAX_REQUEST_EVENTS + 1)
        self.assertEqual(decoder.call_count, instance_expander.MAX_REQUEST_EVENTS + 1)

    async def test_instances_92_day_5000_daily_series_hits_constructed_instance_budget_fast(self):
        daily_payload = {"summary": "92d daily",
                         "start": {"dateTime": "2026-01-01T09:00:00+00:00", "timeZone": "UTC"},
                         "end": {"dateTime": "2026-01-01T10:00:00+00:00", "timeZone": "UTC"},
                         "recurrence": ["RRULE:FREQ=DAILY"]}
        self._seed_group_events(5000, "d92", daily_payload)
        started = time.monotonic()
        with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
            response = await self.instances("from=2026-01-01&to=2026-04-03")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertTrue(state["truncated"])
        self.assertEqual(len(state["instances"]), 2000)
        self.assertEqual(state["instances"][0]["start"],
                         {"dateTime": "2026-01-01T09:00:00+00:00", "timeZone": "UTC"})
        self.assertIn("constructed instance limit", "\n".join(logs.output))

        # Pending-override loop also stops and truncates if constructed instance budget hits zero mid-event.
        pending_event = {
            "id": "pend", "uid": "pend", "revision": 1, "group_id": "family",
            "payload": {"summary": "Pending",
                        "start": {"dateTime": "2026-01-01T09:00:00+00:00", "timeZone": "UTC"},
                        "end": {"dateTime": "2026-01-01T10:00:00+00:00", "timeZone": "UTC"},
                        "recurrence": ["RRULE:FREQ=DAILY;COUNT=1"],
                        "exceptions": [
                            {"originalStartTime": {"dateTime": "2026-01-10T09:00:00+00:00"}, "summary": "M1",
                             "start": {"dateTime": "2026-01-02T09:00:00+00:00", "timeZone": "UTC"},
                             "end": {"dateTime": "2026-01-02T10:00:00+00:00", "timeZone": "UTC"}},
                            {"originalStartTime": {"dateTime": "2026-01-11T09:00:00+00:00"}, "summary": "M2",
                             "start": {"dateTime": "2026-01-03T09:00:00+00:00", "timeZone": "UTC"},
                             "end": {"dateTime": "2026-01-03T10:00:00+00:00", "timeZone": "UTC"}},
                        ]},
        }
        budget = instance_expander._ReplayBudget(instances_remaining=2)
        window = instance_expander._make_window(date(2026, 1, 1), date(2026, 1, 5), "UTC")
        with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
            partial, truncated = instance_expander._expand_event(pending_event, window, budget)
        self.assertTrue(truncated)
        self.assertEqual(len(partial), 2)

    def test_expand_events_5000_series_with_4000_cancellations_charges_before_indexing(self):
        cancellations = [
            {"originalStartTime": {"dateTime": f"{2000 + (i // 365):04d}-{((i % 365) // 28) % 12 + 1:02d}-{(i % 28) + 1:02d}T09:00:00+00:00"},
             "status": "cancelled"}
            for i in range(4000)
        ]
        shared_payload = {"summary": "Cancelled history",
                          "start": {"dateTime": "2000-01-01T09:00:00+00:00", "timeZone": "UTC"},
                          "end": {"dateTime": "2000-01-01T10:00:00+00:00", "timeZone": "UTC"},
                          "recurrence": ["RRULE:FREQ=DAILY"],
                          "exceptions": cancellations}
        events = [
            {"id": f"ov{i:04d}", "uid": f"ov{i:04d}", "revision": 1, "group_id": "family", "payload": shared_payload}
            for i in range(5000)
        ]
        started = time.monotonic()
        with mock.patch.object(instance_expander, "_key", wraps=instance_expander._key) as keyed:
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                result = instance_expander.expand_events(events, date(2026, 1, 1), date(2026, 1, 2))
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(result["truncated"])
        self.assertEqual([item["event_id"] for item in result["instances"]], ["ov0000"])
        # Only event 0 (~400k override cost <= 600k) indexed exceptions; the other 4,999 skipped before any dict build.
        self.assertLess(keyed.call_count, 5000)

    async def test_instances_codex_1000_etc_gmt5_series_reuse_zone_without_vtimezone(self):
        from icalendar import Timezone as ICalTimezone
        from hestia.calendar_sync import events as event_codec

        gmt5_payload = {"summary": "GMT+5 daily",
                        "start": {"dateTime": "2026-01-01T09:00:00-05:00", "timeZone": "Etc/GMT+5"},
                        "end": {"dateTime": "2026-01-01T10:00:00-05:00", "timeZone": "Etc/GMT+5"},
                        "recurrence": ["RRULE:FREQ=DAILY;COUNT=1"]}
        self._seed_group_events(1000, "gmt5", gmt5_payload)
        event_codec._vtimezone.cache_clear()
        started = time.monotonic()
        with mock.patch.object(ICalTimezone, "from_tzinfo", wraps=ICalTimezone.from_tzinfo) as tz_scan:
            response = await self.instances("from=2026-01-01&to=2026-01-02")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(tz_scan.call_count, 0)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertFalse(state["truncated"])
        self.assertEqual(len(state["instances"]), 1000)
        self.assertEqual(state["instances"][0]["start"],
                         {"dateTime": "2026-01-01T09:00:00-05:00", "timeZone": "Etc/GMT+5"})
        # Serialization to_ical() also builds each TZID VTIMEZONE at most once.
        started = time.monotonic()
        with mock.patch.object(ICalTimezone, "from_tzinfo", wraps=ICalTimezone.from_tzinfo) as tz_scan:
            for index in range(1000):
                event_codec.to_ical(gmt5_payload, f"u-{index}")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(tz_scan.call_count, 1)

    async def test_instances_codex_1500_yearly_s1_series_use_day_check_budget(self):
        yearly_payload = {"summary": "Yearly 1900",
                          "start": {"dateTime": "1900-01-01T09:00:00+00:00", "timeZone": "UTC"},
                          "end": {"dateTime": "1900-01-01T10:00:00+00:00", "timeZone": "UTC"},
                          "recurrence": ["RRULE:FREQ=YEARLY"]}
        ordered_ids = self._seed_group_events(1500, "yr", yearly_payload)
        per_event = instance_expander._replay_upper_bound(
            yearly_payload, datetime(2026, 1, 4, tzinfo=timezone.utc), datetime(2025, 12, 31, tzinfo=timezone.utc)
        )
        self.assertGreater(per_event, 46_000)
        expected_expanded = instance_expander.REQUEST_REPLAY_BUDGET // per_event
        self.assertEqual(expected_expanded, 12)
        started = time.monotonic()
        with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
            response = await self.instances("from=2026-01-01&to=2026-01-03")
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertTrue(state["truncated"])
        self.assertEqual([item["event_id"] for item in state["instances"]], ordered_ids[:expected_expanded])

    async def test_instances_moved_override_at_padded_query_end_is_not_dropped(self):
        await self.create({
            "summary": "Moved boundary",
            "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
            "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
            "recurrence": ["RRULE:FREQ=DAILY"],
            "exceptions": [
                {"originalStartTime": {"dateTime": "2026-01-03T00:00:00Z"},
                 "summary": "Moved into Jan 1",
                 "start": {"dateTime": "2026-01-01T12:00:00Z", "timeZone": "UTC"},
                 "end": {"dateTime": "2026-01-01T13:00:00Z", "timeZone": "UTC"}},
                {"originalStartTime": {"dateTime": "2026-01-02T12:00:00Z"},
                 "summary": "True orphan inside padded window",
                 "start": {"dateTime": "2026-01-01T15:00:00Z", "timeZone": "UTC"},
                 "end": {"dateTime": "2026-01-01T16:00:00Z", "timeZone": "UTC"}},
            ],
        })
        response = await self.instances("from=2026-01-01&to=2026-01-02")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertFalse(state["truncated"])
        self.assertEqual(
            [(item["summary"], item["start"]["dateTime"], item["recurrence_id"]["dateTime"])
             for item in state["instances"]],
            [
                ("Moved boundary", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
                ("Moved into Jan 1", "2026-01-01T12:00:00+00:00", "2026-01-03T00:00:00+00:00"),
            ],
        )

    def test_expand_events_shared_budget_fallback_and_exact_boundary(self):
        events = [
            {"id": "e1-within", "uid": "e1", "revision": 1, "group_id": "family",
             "payload": {"summary": "First within",
                         "start": {"dateTime": "2026-01-01T09:00:00+00:00", "timeZone": "UTC"},
                         "end": {"dateTime": "2026-01-01T10:00:00+00:00", "timeZone": "UTC"},
                         "recurrence": ["RRULE:FREQ=DAILY"]}},
            {"id": "e2-over", "uid": "e2", "revision": 1, "group_id": "family",
             "payload": {"summary": "Exceeds remaining",
                         "start": {"dateTime": "2026-01-02T10:00:00+00:00", "timeZone": "UTC"},
                         "end": {"dateTime": "2026-01-02T11:00:00+00:00", "timeZone": "UTC"},
                         "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]}},
            {"id": "e3-exact", "uid": "e3", "revision": 1, "group_id": "family",
             "payload": {"summary": "Fits remaining",
                         "start": {"dateTime": "2026-01-01T11:00:00+00:00", "timeZone": "UTC"},
                         "end": {"dateTime": "2026-01-01T11:00:00+00:00", "timeZone": "UTC"},
                         "recurrence": ["RRULE:FREQ=DAILY"]}},
            {"id": "e4-single", "uid": "e4", "revision": 1, "group_id": "family",
             "payload": {"summary": "Nonrecurring",
                         "start": {"dateTime": "2026-01-03T08:00:00+00:00", "timeZone": "UTC"},
                         "end": {"dateTime": "2026-01-03T09:00:00+00:00", "timeZone": "UTC"}}},
            {"id": "e5-zero", "uid": "e5", "revision": 1, "group_id": "family",
             "payload": {"summary": "Zero remaining",
                         "start": {"dateTime": "2026-01-03T12:00:00+00:00", "timeZone": "UTC"},
                         "end": {"dateTime": "2026-01-03T13:00:00+00:00", "timeZone": "UTC"},
                         "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]}},
        ]
        with mock.patch.object(instance_expander, "REQUEST_REPLAY_BUDGET", 11):
            with mock.patch.object(instance_expander, "of", wraps=instance_expander.of) as library:
                with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                    result = instance_expander.expand_events(events, date(2026, 1, 2), date(2026, 1, 4))
        self.assertEqual(library.call_count, 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(
            [(item["event_id"], item["start"]["dateTime"]) for item in result["instances"]],
            [
                ("e1-within", "2026-01-02T09:00:00+00:00"),
                ("e2-over", "2026-01-02T10:00:00+00:00"),
                ("e3-exact", "2026-01-02T11:00:00+00:00"),
                ("e4-single", "2026-01-03T08:00:00+00:00"),
                ("e1-within", "2026-01-03T09:00:00+00:00"),
                ("e3-exact", "2026-01-03T11:00:00+00:00"),
                ("e5-zero", "2026-01-03T12:00:00+00:00"),
            ],
        )

    async def test_instances_disallowed_rule_shapes_use_dtstart_fallback(self):
        cases = [
            ("2026-01-01", "RRULE:FREQ=YEARLY;BYYEARDAY=1"),
            ("2026-01-02", "RRULE:FREQ=YEARLY;BYWEEKNO=1;BYDAY=MO"),
            ("2026-01-03", "RRULE:FREQ=DAILY;BYHOUR=9"),
            ("2026-01-04", "RRULE:FREQ=DAILY;BYMINUTE=0"),
            ("2026-01-05", "RRULE:FREQ=DAILY;BYSECOND=0"),
            ("2026-01-06", "RRULE:FREQ=MONTHLY;BYMONTHDAY=1,15"),
            ("2026-01-07", "RRULE:FREQ=MONTHLY;BYMONTHDAY=1,15;BYSETPOS=1"),
            ("2026-01-08", "RRULE:FREQ=WEEKLY;BYDAY=1MO"),
            ("2026-01-09", "RRULE:FREQ=MONTHLY;BYDAY=1MO;BYSETPOS=1"),
            ("2026-01-10", "RRULE:FREQ=MONTHLY;BYDAY=1MO,2TU"),
            ("2026-01-11", "RRULE:FREQ=MONTHLY;BYDAY=MO;BYSETPOS=5"),
            ("2026-01-12", "RRULE:FREQ=MONTHLY;BYDAY=MO,MO;BYSETPOS=5"),
            ("2026-01-13", "RRULE:FREQ=MONTHLY;BYDAY=MO;BYSETPOS=0"),
            ("2026-01-14", "RRULE:FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1,-1"),
            ("2026-01-15", "RRULE:FREQ=MONTHLY;BYDAY=MO;BYSETPOS=29"),
            ("2026-01-16", "RRULE:FREQ=DAILY;INTERVAL=2;BYDAY=MO"),
            ("2026-01-17", "RRULE:FREQ=DAILY;BYDAY=1MO"),
            ("2026-01-18", "RRULE:FREQ=MONTHLY;BYDAY=6MO"),
            ("2026-01-19", "RRULE:FREQ=YEARLY;BYMONTH=2;BYDAY=6MO"),
            ("2026-01-20", "RRULE:FREQ=YEARLY;BYMONTH=3;INTERVAL=2;BYDAY=5MO"),
            ("2026-01-21", "RRULE:FREQ=YEARLY;BYMONTH=13"),
            ("2026-01-22", "RRULE:FREQ=MONTHLY;BYMONTHDAY=32"),
            ("2026-01-23", "RRULE:FREQ=DAILY;INTERVAL=1001"),
            ("2026-01-24", "RRULE:FREQ=YEARLY;BYMONTH=1,3;BYMONTHDAY=15"),
            ("2026-01-25", "RRULE:FREQ=YEARLY;BYMONTH=1;BYMONTHDAY=1,15"),
            ("2026-01-26", "RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=30"),
            ("2026-01-27", "RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=29;INTERVAL=2"),
            ("2026-01-28", "RRULE:FREQ=YEARLY;BYDAY=MO"),
            ("2026-02-01", "RRULE:FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=29"),
            ("2026-02-02", "RRULE:FREQ=MONTHLY;INTERVAL=2;BYMONTHDAY=-29"),
            ("2026-02-03", "RRULE:FREQ=MONTHLY;INTERVAL=2;BYDAY=5MO"),
            ("2026-02-04", "RRULE:FREQ=YEARLY;BYMONTH=1"),
            ("2026-02-05", "RRULE:FREQ=YEARLY;BYMONTH=1;BYDAY=MO;BYSETPOS=1"),
            ("2026-02-06", "RRULE:FREQ=MONTHLY;BYDAY=FR;BYMONTHDAY=13"),
            ("2026-01-31", "RRULE:FREQ=MONTHLY;INTERVAL=2"),
            ("2024-02-29", "RRULE:FREQ=YEARLY;INTERVAL=2"),
        ]
        for index, (day_str, rule) in enumerate(cases):
            with self.subTest(rule=rule):
                day = date.fromisoformat(day_str)
                event = await self.create({"summary": f"Fallback {index}",
                                           "start": {"dateTime": f"{day_str}T09:00:00Z", "timeZone": "UTC"},
                                           "end": {"dateTime": f"{day_str}T10:00:00Z", "timeZone": "UTC"},
                                           "recurrence": [rule]})
                started = time.monotonic()
                next_day = (day + timedelta(days=1)).isoformat()
                with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library replay")):
                    with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                        response = await self.instances(f"from={day_str}&to={next_day}")
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(response.status, 200)
                state = await response.json()
                self.assertIn(event["id"], [item["event_id"] for item in state["instances"]])
                self.assertTrue(state["truncated"])

    async def test_instances_all_day_next_day_override_retains_membership(self):
        await self.create({"summary": "Sunday", "start": {"date": "2025-12-28"}, "end": {"date": "2025-12-29"},
                           "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=2"], "exceptions": [
                               {"originalStartTime": {"date": "2026-01-04"}, "summary": "Moved Sunday",
                                "start": {"date": "2026-01-02"}, "end": {"date": "2026-01-03"}}]})
        response = await self.instances("from=2026-01-02&to=2026-01-03&tz=Europe/Warsaw")
        state = await response.json()
        instances = state["instances"]
        self.assertEqual([item["summary"] for item in instances], ["Moved Sunday"])
        self.assertEqual(instances[0]["recurrence_id"], {"date": "2026-01-04"})
        self.assertFalse(state["truncated"])

    async def test_instances_all_day_orphan_override_is_skipped(self):
        await self.create({"summary": "Monday all-day", "start": {"date": "2026-01-05"}, "end": {"date": "2026-01-06"},
                           "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=2"], "exceptions": [
                               {"originalStartTime": {"date": "2026-01-06"}, "summary": "Orphan all-day",
                                "start": {"date": "2026-01-07"}, "end": {"date": "2026-01-08"}}]})
        with self.assertLogs("hestia.calendar_sync.instances", level="DEBUG") as logs:
            response = await self.instances("from=2026-01-05&to=2026-01-13")
        instances = (await response.json())["instances"]
        self.assertEqual([item["summary"] for item in instances], ["Monday all-day", "Monday all-day"])
        self.assertIn("orphan override", "\n".join(logs.output))

    async def test_instances_gap_day_moved_override_matches_exact_utc_slot(self):
        await self.create({"summary": "Gap base",
                           "start": {"dateTime": "2026-03-28T02:30:00+01:00", "timeZone": "Europe/Warsaw"},
                           "end": {"dateTime": "2026-03-28T03:30:00+01:00", "timeZone": "Europe/Warsaw"},
                           "recurrence": ["RRULE:FREQ=DAILY;UNTIL=20260330T000000Z"], "exceptions": [
                               {"originalStartTime": {"dateTime": "2026-03-29T01:30:00Z"}, "summary": "Moved gap",
                                "start": {"dateTime": "2026-04-01T09:00:00+02:00", "timeZone": "Europe/Warsaw"},
                                "end": {"dateTime": "2026-04-01T10:00:00+02:00", "timeZone": "Europe/Warsaw"}}]})
        response = await self.instances("from=2026-04-01&to=2026-04-02")
        instances = (await response.json())["instances"]
        self.assertEqual([item["summary"] for item in instances], ["Moved gap"])
        self.assertEqual(instances[0]["recurrence_id"],
                         {"dateTime": "2026-03-29T03:30:00+02:00", "timeZone": "Europe/Warsaw"})

    async def test_instances_all_day_master_uses_stored_timed_override_instants(self):
        await self.create({"summary": "All-day master", "start": {"date": "2026-10-24"}, "end": {"date": "2026-10-25"},
                           "recurrence": ["RDATE;VALUE=DATE:20261025"], "exceptions": [
                               {"originalStartTime": {"date": "2026-10-25"}, "summary": "Timed fold override",
                                "start": {"dateTime": "2026-10-25T02:30:00+01:00", "timeZone": "Europe/Warsaw"},
                                "end": {"dateTime": "2026-10-25T03:30:00+01:00", "timeZone": "Europe/Warsaw"}}]})
        response = await self.instances("from=2026-10-25&to=2026-10-26")
        instances = (await response.json())["instances"]
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["start"],
                         {"dateTime": "2026-10-25T02:30:00+01:00", "timeZone": "Europe/Warsaw"})
        self.assertEqual(instances[0]["end"],
                         {"dateTime": "2026-10-25T03:30:00+01:00", "timeZone": "Europe/Warsaw"})
        self.assertEqual(instances[0]["recurrence_id"], {"date": "2026-10-25"})

    async def test_instances_explicit_override_after_until_is_still_emitted(self):
        await self.create({"summary": "Base", "start": {"dateTime": "2026-01-20T09:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-20T10:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=DAILY;UNTIL=20260110T000000Z"], "exceptions": [
                               {"originalStartTime": {"dateTime": "2026-01-20T09:00:00Z"}, "summary": "Explicit override",
                                "start": {"dateTime": "2026-01-20T09:00:00Z", "timeZone": "UTC"},
                                "end": {"dateTime": "2026-01-20T10:00:00Z", "timeZone": "UTC"}}]})
        response = await self.instances("from=2026-01-20&to=2026-01-21")
        instances = (await response.json())["instances"]
        self.assertEqual([item["summary"] for item in instances], ["Explicit override"])

    async def test_instances_irrelevant_overrides_do_not_expand_replay_window(self):
        await self.create({"summary": "Base", "start": {"dateTime": "2026-01-01T09:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-01T10:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=1"], "exceptions": [
                               {"originalStartTime": {"dateTime": "2000-01-01T09:00:00Z"}, "status": "cancelled"},
                               {"originalStartTime": {"dateTime": "2000-01-02T09:00:00Z"},
                                "start": {"dateTime": "2000-01-03T09:00:00Z", "timeZone": "UTC"},
                                "end": {"dateTime": "2000-01-03T10:00:00Z", "timeZone": "UTC"}},
                               {"originalStartTime": {"dateTime": "2026-01-01T09:00:00Z"}, "summary": "Active",
                                "start": {"dateTime": "2026-01-02T09:00:00Z", "timeZone": "UTC"},
                                "end": {"dateTime": "2026-01-02T10:00:00Z", "timeZone": "UTC"}}]})
        response = await self.instances("from=2026-01-02&to=2026-01-03")
        state = await response.json()
        self.assertEqual([item["summary"] for item in state["instances"]], ["Active"])
        self.assertFalse(state["truncated"])

    async def test_instances_first_fold_rdate_survives_second_fold_dtstart(self):
        await self.create({"summary": "Both folds",
                           "start": {"dateTime": "2026-10-25T02:30:00+01:00", "timeZone": "Europe/Warsaw"},
                           "end": {"dateTime": "2026-10-25T03:30:00+01:00", "timeZone": "Europe/Warsaw"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=1", "RDATE:20261025T003000Z"]})
        response = await self.instances("from=2026-10-25&to=2026-10-26")
        instances = (await response.json())["instances"]
        self.assertEqual([item["start"]["dateTime"] for item in instances],
                         ["2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00"])
        self.assertEqual(len({item["instance_id"] for item in instances}), 2)

    async def test_instances_apia_empty_utc_window_only_excludes_timed_events(self):
        await self.create({"summary": "Spanning skipped date",
                           "start": {"dateTime": "2011-12-29T00:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2011-12-31T00:00:00Z", "timeZone": "UTC"}})
        await self.create({"summary": "All-day remains", "start": {"date": "2011-12-30"}, "end": {"date": "2011-12-31"}})
        response = await self.instances("from=2011-12-30&to=2011-12-31&tz=Pacific/Apia")
        self.assertEqual(response.status, 200)
        self.assertEqual([item["summary"] for item in (await response.json())["instances"]], ["All-day remains"])

    async def test_instances_base_end_uses_stored_elapsed_duration(self):
        await self.create({"summary": "Elapsed",
                           "start": {"dateTime": "2026-03-28T02:30:00+01:00", "timeZone": "Europe/Warsaw"},
                           "end": {"dateTime": "2026-03-28T04:30:00+01:00", "timeZone": "Europe/Warsaw"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]})
        await self.create({"summary": "Date span", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-03"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]})
        response = await self.instances("from=2026-03-29&to=2026-03-30")
        elapsed = (await response.json())["instances"]
        self.assertEqual(len(elapsed), 1)
        self.assertEqual(elapsed[0]["start"],
                         {"dateTime": "2026-03-29T03:30:00+02:00", "timeZone": "Europe/Warsaw"})
        self.assertEqual(elapsed[0]["end"],
                         {"dateTime": "2026-03-29T05:30:00+02:00", "timeZone": "Europe/Warsaw"})
        response = await self.instances("from=2026-01-02&to=2026-01-03")
        dates = (await response.json())["instances"]
        self.assertEqual([(item["start"], item["end"]) for item in dates],
                         [({"date": "2026-01-01"}, {"date": "2026-01-03"}),
                          ({"date": "2026-01-02"}, {"date": "2026-01-04"})])

    async def test_instances_cap_merges_actual_matches_by_start(self):
        await self.create_dense_daily_series()
        earliest = await self.create({"summary": "Earliest",
                                      "start": {"dateTime": "2025-12-31T23:00:00Z", "timeZone": "UTC"},
                                      "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"}})
        response = await self.instances("from=2026-01-01&to=2026-04-01")
        state = await response.json()
        self.assertEqual(len(state["instances"]), 2000)
        self.assertTrue(state["truncated"])
        self.assertEqual(state["instances"][0]["event_id"], earliest["id"])
        self.assertEqual(state["instances"][1]["summary"], "Hourly")
        self.assertEqual(state["instances"][1]["start"]["dateTime"], "2026-01-01T00:00:00+00:00")

    async def test_instances_timed_exdate_uses_utc_instant(self):
        event = await self.create({"summary": "Fold base",
                                   "start": {"dateTime": "2026-10-24T09:00:00+02:00", "timeZone": "Europe/Warsaw"},
                                   "end": {"dateTime": "2026-10-24T10:00:00+02:00", "timeZone": "Europe/Warsaw"},
                                   "recurrence": ["RDATE:20261025T003000Z", "RDATE:20261025T013000Z",
                                                  "EXDATE:20261025T003000Z"]})
        response = await self.instances("from=2026-10-25&to=2026-10-26")
        instances = (await response.json())["instances"]
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0]["start"],
                         {"dateTime": "2026-10-25T02:30:00+01:00", "timeZone": "Europe/Warsaw"})
        self.assertEqual(instances[0]["instance_id"], event["id"] + "/2026-10-25T01:30:00+00:00")

    async def test_instances_sparse_secondly_rule_uses_dtstart_without_expansion(self):
        event = await self.create({"summary": "Sparse secondly",
                                   "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                                   "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                                   "recurrence": ["RRULE:FREQ=SECONDLY;BYMONTH=12;BYMONTHDAY=31;BYHOUR=23;BYMINUTE=59;BYSECOND=59"]})
        with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library expansion")):
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                response = await self.instances("from=2026-01-01&to=2026-04-03")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual([item["event_id"] for item in state["instances"]], [event["id"]])
        self.assertTrue(state["truncated"])

    async def test_instances_date_rules_with_time_selectors_use_dtstart_fallback(self):
        event = await self.create({"summary": "Dates", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"},
                                   "recurrence": ["RRULE:FREQ=DAILY;BYHOUR=0,12;COUNT=3"]})
        with mock.patch.object(instance_expander, "of", side_effect=AssertionError("library replay")):
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                response = await self.instances("from=2026-01-01&to=2026-01-05")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual([item["event_id"] for item in state["instances"]], [event["id"]])
        self.assertTrue(state["truncated"])

    async def test_instances_empty_window_excludes_spanning_event(self):
        await self.create({"summary": "Spanning", "start": {"date": "2025-12-31"}, "end": {"date": "2026-01-02"}})
        response = await self.instances("from=2026-01-01&to=2026-01-01")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["instances"], [])

    async def test_instances_clamp_extreme_date_lookback_and_ends(self):
        await self.create({"summary": "Minimum", "start": {"date": "0001-01-01"}, "end": {"date": "0001-01-02"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=1"]})
        await self.create({"summary": "Maximum", "start": {"date": "9999-12-30"}, "end": {"date": "9999-12-31"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]})
        response = await self.instances("from=0001-01-01&to=0001-01-02")
        self.assertEqual(response.status, 200)
        self.assertEqual([item["summary"] for item in (await response.json())["instances"]], ["Minimum"])
        response = await self.instances("from=9999-12-30&to=9999-12-31")
        self.assertEqual(response.status, 200)
        self.assertEqual([item["summary"] for item in (await response.json())["instances"]], ["Maximum"])
        await self.create({"summary": "Minimum timed",
                           "start": {"dateTime": "0001-01-01T00:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "0001-01-01T01:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=1"]})
        await self.create({"summary": "Maximum timed",
                           "start": {"dateTime": "9999-12-30T00:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "9999-12-30T01:00:00Z", "timeZone": "UTC"},
                           "recurrence": ["RRULE:FREQ=DAILY;COUNT=1"]})
        response = await self.instances("from=0001-01-01&to=0001-01-02")
        self.assertEqual({item["summary"] for item in (await response.json())["instances"]}, {"Minimum", "Minimum timed"})
        response = await self.instances("from=9999-12-30&to=9999-12-31")
        self.assertEqual({item["summary"] for item in (await response.json())["instances"]}, {"Maximum", "Maximum timed"})
        response = await self.instances("from=0001-01-01&to=0001-01-02&tz=Europe/Warsaw")
        self.assertEqual(response.status, 200)
        response = await self.instances("from=9999-12-30&to=9999-12-31&tz=America/New_York")
        self.assertEqual(response.status, 200)
        win = instance_expander._make_window(date(9999, 12, 30), date(9999, 12, 31), "UTC")
        overflow_event = {"id": "max", "uid": "max", "revision": 1, "group_id": "family",
                          "payload": {"summary": "Max", "start": {"date": "9999-12-30"}, "end": {"date": "9999-12-31"}}}
        self.assertIsNone(instance_expander._occurrence_instance(overflow_event, date(9999, 12, 31), timedelta(days=1), win, None, None))

    async def test_instances_incompatible_rule_does_not_fail_group(self):
        incompatible = await self.create({"summary": "Incompatible library rule",
                                          "start": {"dateTime": "2026-01-01T09:00:00Z", "timeZone": "UTC"},
                                          "end": {"dateTime": "2026-01-01T10:00:00Z", "timeZone": "UTC"},
                                          "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"]})
        healthy = await self.create({"summary": "Healthy", "start": {"date": "2026-01-02"}, "end": {"date": "2026-01-03"}})
        with mock.patch.object(instance_expander, "of", side_effect=ValueError("incompatible")):
            with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
                response = await self.instances("from=2026-01-01&to=2026-01-03")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual([(item["event_id"], item["summary"]) for item in state["instances"]],
                         [(incompatible["id"], "Incompatible library rule"), (healthy["id"], "Healthy")])
        self.assertTrue(state["truncated"])
        self.assertTrue(any(incompatible["id"] in line for line in logs.output))

    async def test_instances_timezone_parameter_uses_local_midnights(self):
        event = await self.create({"summary": "Local midnight",
                                   "start": {"dateTime": "2026-01-02T00:30:00+01:00", "timeZone": "Europe/Warsaw"},
                                   "end": {"dateTime": "2026-01-02T00:30:00+01:00", "timeZone": "Europe/Warsaw"}})
        response = await self.instances("from=2026-01-02&to=2026-01-03")
        self.assertEqual((await response.json())["instances"], [])
        response = await self.instances("from=2026-01-02&to=2026-01-03&tz=Europe/Warsaw")
        self.assertEqual(response.status, 200)
        self.assertEqual([item["event_id"] for item in (await response.json())["instances"]], [event["id"]])
        for zone_name in ("Invalid/Unknown", "localtime", "posixrules", "right/UTC", "posix/UTC"):
            with self.subTest(zone_name=zone_name):
                response = await self.instances("from=2026-01-02&to=2026-01-03&tz=" + zone_name)
                self.assertEqual(response.status, 400)
                self.assertEqual(await response.json(), {"error": "invalid_calendar_event"})


class CalendarApiContractTests(unittest.TestCase):
    def test_contract_requests_and_optimistic_revision_shape(self):
        api_contract.CalendarCreateRequest.model_validate(create_request())
        for key in (None, True, 1, "", "short", "x" * 129, "calendar/key/invalid"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                api_contract.CalendarCreateRequest.model_validate(create_request(key=key))
        with self.assertRaises(ValidationError):
            api_contract.CalendarCreateRequest.model_validate({"group": "family", "payload": content()})
        for model in (api_contract.CalendarUpdateRequest, api_contract.CalendarDeleteRequest):
            for revision in (None, True, 0, "1"):
                body = {"group": "family", "id": "event", "revision": revision}
                if model is api_contract.CalendarUpdateRequest:
                    body["payload"] = content()
                with self.assertRaises(ValidationError):
                    model.model_validate(body)
        schemas = api_contract.build_openapi()["components"]["schemas"]
        self.assertIn("CalendarPayload", schemas)
        self.assertEqual(set(schemas["CalendarCreateRequest"]["required"]), {"group", "payload", "idempotency_key"})
        self.assertEqual(set(schemas["CalendarUpdateRequest"]["required"]), {"group", "id", "revision", "payload"})

    def test_expanded_instances_contract_and_route_parameters(self):
        result = {"enabled": True, "groups": [{"id": "family"}],
                  "instances": [{"instance_id": "event", "event_id": "event", "revision": 1, "group": "family",
                                 "summary": "Agenda", "location": "", "start": {"date": "2026-11-01"},
                                 "end": {"date": "2026-11-02"}, "all_day": True, "status": "confirmed",
                                 "recurrence_id": None}], "truncated": False}
        api_contract.CalendarInstances.model_validate(result)
        document = api_contract.build_openapi()
        operation = document["paths"][api.INSTANCES_PATH]["get"]
        self.assertEqual(operation["x-required-role"], "viewer")
        parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
        self.assertTrue(parameters["from"]["required"])
        self.assertTrue(parameters["to"]["required"])
        self.assertIn("Inclusive", parameters["from"]["description"])
        self.assertIn("Exclusive", parameters["to"]["description"])
        self.assertNotIn("required", parameters["tz"])
        self.assertEqual(parameters["tz"]["schema"]["default"], "UTC")
        self.assertIn("local midnights", parameters["tz"]["description"])
        self.assertNotIn("required", parameters["group"])
        schema = document["components"]["schemas"]["CalendarExpandedInstance"]
        self.assertEqual(set(schema["required"]), {"instance_id", "event_id", "revision", "group", "summary", "location",
                                                   "start", "end", "all_day", "status", "recurrence_id"})
        self.assertIn("stable across calls", schema["properties"]["instance_id"]["description"])
        self.assertIn("moved exceptions", schema["properties"]["instance_id"]["description"])
        self.assertEqual(document["components"]["schemas"]["CalendarInstances"]["properties"]["instances"]["maxItems"], 2000)

    def test_replay_bound_and_rule_allowlist(self):
        end = datetime(2026, 1, 3, tzinfo=timezone.utc)
        daily = {"start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                 "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                 "recurrence": ["RRULE:FREQ=DAILY", "RDATE:20260102T000000Z"]}
        self.assertEqual(instance_expander._replay_upper_bound(daily, end), 303)
        yearly_s1 = {"start": {"dateTime": "1900-01-01T09:00:00Z", "timeZone": "UTC"},
                     "end": {"dateTime": "1900-01-01T10:00:00Z", "timeZone": "UTC"},
                     "recurrence": ["RRULE:FREQ=YEARLY"]}
        self.assertEqual(
            instance_expander._replay_upper_bound(yearly_s1, datetime(2026, 1, 4, tzinfo=timezone.utc)),
            46848,
        )
        comma_rdate = {"start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"},
                       "recurrence": ["RDATE;VALUE=DATE:20260102,20260103"]}
        self.assertEqual(instance_expander._replay_upper_bound(comma_rdate, end), 600)
        self.assertEqual(
            instance_expander._property_values({"recurrence": ["RDATE;TZID=Europe/Warsaw:20260101T090000"]}, "RDATE"),
            [datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)],
        )
        self.assertEqual(
            instance_expander._property_values({"recurrence": ["RDATE:20260101T090000"]}, "RDATE"),
            [datetime(2026, 1, 1, 9, 0).astimezone(timezone.utc)],
        )
        with self.assertRaises(ValueError):
            instance_expander._property_values({"recurrence": ["RDATE:not-a-date"]}, "RDATE")
        monthly = {"start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"},
                   "recurrence": ["RRULE:FREQ=MONTHLY;BYDAY=-1FR"]}
        self.assertEqual(instance_expander._replay_upper_bound(monthly, end), 62)
        yearly = {"start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"},
                  "recurrence": ["RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU"]}
        self.assertEqual(instance_expander._replay_upper_bound(yearly, end), 732)
        long_duration = {"start": {"date": "2026-01-01"}, "end": {"date": "2550-01-01"},
                         "recurrence": ["RRULE:FREQ=DAILY"]}
        self.assertGreater(instance_expander._replay_upper_bound(
            long_duration, datetime(2026, 2, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, tzinfo=timezone.utc)
        ), 190000)
        start_day = {"start": {"date": "2026-01-01"}}
        for bad_rule in (
            "RRULE:broken",
            "RRULE:FREQ=DAILY;FREQ=WEEKLY",
            "RRULE:FREQ=DAILY;COUNT=1;UNTIL=20260102T000000Z",
            "RRULE:FREQ=DAILY;INTERVAL=0",
            "RRULE:FREQ=DAILY;INTERVAL=1,2",
            "RRULE:FREQ=DAILY;COUNT=abc",
            "RRULE:FREQ=DAILY;WKST=XX",
            "RRULE:FREQ=MONTHLY;BYDAY=XX",
            "RRULE:FREQ=MONTHLY;BYDAY=0MO",
            "RRULE:FREQ=YEARLY;BYMONTH=4;BYMONTHDAY=-31",
        ):
            with self.subTest(bad_rule=bad_rule):
                self.assertIsNotNone(instance_expander._unsupported_rule_reason({**start_day, "recurrence": [bad_rule]}))
        self.assertIsNone(instance_expander._unsupported_rule_reason({**start_day, "recurrence": ["RRULE:FREQ=YEARLY;BYMONTH=2;BYMONTHDAY=-29"]}))
        ny_jan31 = {"start": {"dateTime": "2026-02-01T04:30:00Z", "timeZone": "America/New_York"},
                    "recurrence": ["RRULE:FREQ=MONTHLY;INTERVAL=2"]}
        self.assertIsNotNone(instance_expander._unsupported_rule_reason(ny_jan31))

    def test_double_failure_fallback(self):
        event = {"id": "event", "uid": "event", "payload": {"start": {"date": "2026-01-01"}}}
        with mock.patch.object(instance_expander, "_expand_event", side_effect=ValueError("bad rule")):
            with mock.patch.object(instance_expander, "_first_instance", side_effect=ValueError("bad start")):
                with self.assertLogs("hestia.calendar_sync.instances", level="WARNING"):
                    result = instance_expander.expand_events([event], date(2026, 1, 1), date(2026, 1, 2))
        self.assertEqual(result, {"instances": [], "truncated": True})

    def test_parallel_web_and_runtime_db_initialization_has_one_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "parallel.db"
            try:
                with ThreadPoolExecutor(max_workers=4) as executor:
                    results = list(executor.map(lambda _: db.init_db(path), range(4)))
                self.assertTrue(all(result is results[0] for result in results))
            finally:
                db.reset_engine_cache()
