"""Calendar HTTP surface with real SQLite roles, revision checks and durable jobs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import tempfile
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


def dense_daily_rule():
    return "RRULE:FREQ=DAILY;BYHOUR=" + ",".join(str(hour) for hour in range(24))


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
        await self.create({"summary": "Hourly",
                           "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                           "recurrence": [dense_daily_rule()]})
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
                response = await self.instances("from=2026-01-01&to=2026-01-04")
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
                                "start": {"dateTime": "2026-02-01T09:00:00Z", "timeZone": "UTC"},
                                "end": {"dateTime": "2026-02-01T10:00:00Z", "timeZone": "UTC"}}]})
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
                           "start": {"dateTime": "2026-03-29T01:30:00+01:00", "timeZone": "Europe/Warsaw"},
                           "end": {"dateTime": "2026-03-29T01:45:00+01:00", "timeZone": "Europe/Warsaw"},
                           "recurrence": ["RRULE:FREQ=DAILY;BYHOUR=1,2;UNTIL=20260329T013000Z"]})
        response = await self.instances("from=2026-03-28&to=2026-04-01")
        instances = (await response.json())["instances"]
        by_summary = {}
        for item in instances:
            by_summary.setdefault(item["summary"], []).append(item["start"]["dateTime"])
        self.assertEqual(by_summary["Gap"], ["2026-03-28T02:30:00+01:00", "2026-03-29T03:30:00+02:00",
                                             "2026-03-30T02:30:00+02:00"])
        self.assertEqual(by_summary["Until gap"], ["2026-03-29T01:30:00+01:00", "2026-03-29T03:30:00+02:00"])
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
        response = await self.instances("from=2026-01-20&to=2026-01-22")
        instances = (await response.json())["instances"]
        self.assertEqual([item["summary"] for item in instances], ["Moved explicit", "Moved series"])
        self.assertEqual(instances[0]["recurrence_id"]["dateTime"], "2026-01-01T09:00:00+00:00")
        self.assertEqual(instances[1]["recurrence_id"]["dateTime"], "2026-01-02T09:00:00+00:00")

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
        await self.create({"summary": "Hourly",
                           "start": {"dateTime": "2026-01-01T00:00:00Z", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-01-01T01:00:00Z", "timeZone": "UTC"},
                           "recurrence": [dense_daily_rule()]})
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

    async def test_instances_date_rules_ignore_time_selectors_before_count(self):
        await self.create({"summary": "Dates", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"},
                           "recurrence": ["RRULE:FREQ=DAILY;BYHOUR=0,12;COUNT=3"]})
        response = await self.instances("from=2026-01-01&to=2026-01-05")
        instances = (await response.json())["instances"]
        self.assertEqual([item["start"]["date"] for item in instances],
                         ["2026-01-01", "2026-01-02", "2026-01-03"])
        self.assertEqual(len({item["instance_id"] for item in instances}), 3)

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
        response = await self.instances("from=0001-01-01&to=0001-01-02&tz=Europe/Warsaw")
        self.assertEqual(response.status, 200)
        response = await self.instances("from=9999-12-30&to=9999-12-31&tz=America/New_York")
        self.assertEqual(response.status, 200)
        self.assertIsNone(instance_expander._base_end({"start": {"date": "9999-12-30"}, "end": {"date": "9999-12-31"}},
                                                       {"date": "9999-12-31"}))

    async def test_instances_incompatible_rule_does_not_fail_group(self):
        incompatible = await self.create({"summary": "Leap second",
                                          "start": {"dateTime": "2026-01-01T09:00:00Z", "timeZone": "UTC"},
                                          "end": {"dateTime": "2026-01-01T10:00:00Z", "timeZone": "UTC"},
                                          "recurrence": ["RRULE:FREQ=DAILY;BYSECOND=60;COUNT=2"]})
        healthy = await self.create({"summary": "Healthy", "start": {"date": "2026-01-02"}, "end": {"date": "2026-01-03"}})
        with self.assertLogs("hestia.calendar_sync.instances", level="WARNING") as logs:
            response = await self.instances("from=2026-01-01&to=2026-01-03")
        self.assertEqual(response.status, 200)
        state = await response.json()
        self.assertEqual([(item["event_id"], item["summary"]) for item in state["instances"]],
                         [(incompatible["id"], "Leap second"), (healthy["id"], "Healthy")])
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

    def test_override_membership_guards_and_double_failure_fallback(self):
        event = {"id": "event", "uid": "event", "payload": {"start": {"timeZone": "UTC"}}}
        self.assertFalse(instance_expander._override_member(event, [], set(), datetime.max.replace(tzinfo=timezone.utc)))
        until = datetime(2020, 1, 1, tzinfo=timezone.utc)
        original = datetime(2021, 1, 1, tzinfo=timezone.utc)
        self.assertFalse(instance_expander._override_member(event, [({}, until)], set(), original))
        event["payload"] = {"start": {"date": "2026-01-01"}}
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
