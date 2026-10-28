import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tripbook import device as D
from tripbook.db import Database
from tripbook.decode import decode_log, haversine_m, split_trips
from tripbook.exchange import import_points, read_gpx, report_html, summary, write_gpx

from fake_device import FakeLogger, sample_log


class DecodeTests(unittest.TestCase):
    def test_round_trip(self):
        raw, truth = sample_log()
        pts = decode_log(raw + b"\xff" * 300)
        self.assertEqual(len(pts), len(truth))
        worst = max(haversine_m(a.lat, a.lon, b.lat, b.lon) for a, b in zip(pts, truth))
        self.assertLess(worst, 3.0)
        self.assertEqual([p.time for p in pts], [p.time for p in truth])
        self.assertAlmostEqual(pts[0].alt, 1600, delta=3)

    def test_split(self):
        raw, _ = sample_log()
        trips = split_trips(decode_log(raw), stop_interval_min=5)
        self.assertEqual(len(trips), 2)
        for t in trips:
            self.assertAlmostEqual(t.distance_km, 18.0 * (239 / 240), delta=0.3)  # 54 km/h for 20 min

    def test_garbage_ignored(self):
        self.assertEqual(decode_log(bytes(range(256)) * 20), [])


class DeviceTests(unittest.TestCase):
    def run_download(self, password=""):
        raw, truth = sample_log()
        fake = FakeLogger(raw, password=password)
        fake.start()
        try:
            port, lg, baud = D.open_logger(fake.path)
            self.assertEqual(baud, 115200)
            self.assertTrue(lg.firmware_version().startswith("1.101.101"))
            if password:
                self.assertIsNone(lg.log_config())
                self.assertFalse(lg.login("wrong"))
                self.assertTrue(lg.login(password))
            ud = lg.user_data()
            self.assertEqual(ud.registration_number, "CA 123-456")
            cfg = lg.log_config()
            self.assertEqual(cfg.used_sectors, fake.used)
            progress = []
            data = lg.download(cfg, lambda a, b: progress.append((a, b)))
            self.assertEqual(progress[-1], (fake.used, fake.used))
            self.assertEqual(len(decode_log(data)), len(truth))
            self.assertTrue(lg.clear_log())
            self.assertTrue(fake.erased)
            self.assertEqual(fake.userdata[2], "CA 123-456")
            port.close()
        finally:
            fake.stop()

    def test_download(self):
        self.run_download()

    def test_download_with_password(self):
        self.run_download("s3cret")


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.vid = self.db.x("INSERT INTO vehicle(name, odometer_start) VALUES ('Car', 10000)")
        raw, _ = sample_log()
        self.pts = decode_log(raw)

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def test_import_dedupe_places_rules(self):
        self.assertEqual(import_points(self.db, self.vid, self.pts, "device"), 2)
        self.assertEqual(import_points(self.db, self.vid, self.pts, "device"), 0)  # re-download
        t1, t2 = self.db.trips(self.vid)
        self.assertAlmostEqual(t1.odo_start, 10000)
        self.assertAlmostEqual(t2.odo_start, 10000 + t1.distance_km)
        home = self.db.x("INSERT INTO place(name, lat, lon) VALUES ('Home', ?, ?)", (t1.start_lat, t1.start_lon))
        office = self.db.x("INSERT INTO place(name, lat, lon) VALUES ('Office', ?, ?)", (t1.end_lat, t1.end_lon))
        biz = self.db.q("SELECT id FROM trip_type WHERE name='Business'")[0]["id"]
        self.db.x("INSERT INTO rule(start_place_id, end_place_id, trip_type_id, reverse, reason) "
                  "VALUES (?,?,?,1,'Commute')", (home, office, biz))
        self.db.resolve_places()
        t1, t2 = self.db.trips(self.vid)
        self.assertEqual((t1.start_desc, t1.end_desc, t2.start_desc, t2.end_desc),
                         ("Home", "Office", "Office", "Home"))
        self.assertEqual((t1.trip_type_id, t2.trip_type_id), (biz, biz))
        self.assertEqual(t1.reason, "Commute")
        s = summary(self.db, [t1, t2], [])
        self.assertAlmostEqual(s["business_pct"], 100.0)
        self.assertIn("Office", report_html(self.db, None, [t1, t2], [], "Oct 2026"))
        merged = self.db.merge_trips([t1.id, t2.id])
        (m,) = self.db.trips(self.vid)
        self.assertEqual(m.id, merged)
        self.assertEqual(m.end_desc, "Home")
        self.assertEqual(len(self.db.trip_points(m.id)), len(self.pts))

    def test_gpx_round_trip(self):
        path = Path(self.tmp.name) / "x.gpx"
        write_gpx(path, [("a", self.pts)])
        back = read_gpx(path)
        self.assertEqual(len(back), len(self.pts))
        self.assertEqual(back[5].time, self.pts[5].time)

    def test_encrypted_littlelogbook_import(self):
        import sqlite3
        from tripbook.decode import encode_long
        from tripbook.exchange import import_littlelogbook_db
        plain = Path(self.tmp.name) / "llb.db"
        c = sqlite3.connect(plain)
        c.executescript("""
            CREATE TABLE Device (DeviceId INTEGER, RegistrationNumber TEXT, Make TEXT, Year TEXT,
                                 OdometerStart INTEGER, DeviceSerial TEXT, Password TEXT, Closed BOOLEAN);
            CREATE TABLE TripType (TripTypeId INTEGER, Description TEXT, IsDeleted TEXT);
            CREATE TABLE GeoName (GeoNameId INTEGER, Description TEXT, Type TEXT, Latitude REAL,
                                  Longitude REAL, IsDeleted INTEGER);
            CREATE TABLE Trip (TripId INTEGER, DeviceId INTEGER, StartDate TEXT, EndDate TEXT,
                               StartDescription TEXT, EndDescription TEXT, StartTagId INTEGER, EndTagId INTEGER,
                               TripTypeId INTEGER, Distance REAL, TripData BLOB, TripReason TEXT,
                               IsDeleted BOOLEAN, StartLatitude REAL, StartLongitude REAL, PoiCount INTEGER);
            CREATE TABLE Odometer (OdometerId INTEGER, DeviceId INTEGER, Date TEXT, Reading INTEGER);
            INSERT INTO Device VALUES (1, 'ABC123', 'Lexus', '2014', 1000, '', '', 0);
            INSERT INTO TripType VALUES (1, 'Business', 'FALSE');
            INSERT INTO GeoName VALUES (-1, 'Office', 'Private', 0, 0, 0), (5, 'Suburb', '99', 0, 0, 0);
            INSERT INTO Odometer VALUES (1, 1, '2023-10-18 05:28:51.7509454Z', 2000);
        """)
        data = b"".join(encode_long(p) for p in self.pts[:30])
        c.execute("INSERT INTO Trip VALUES (1, 1, '2026-10-20 06:30:00Z', '2026-10-20 06:32:25Z', 'A', 'B', "
                  "0, -1, 1, 2.5, ?, 'Client visit', 0, NULL, NULL, 0)", (data,))
        c.commit()
        c.close()
        # Encrypting is the same XOR as decrypting, so the decrypter produces the encrypted file.
        enc = Path(self.tmp.name) / "llb.s3db"
        enc.write_bytes(self._encrypt(plain.read_bytes()))
        stats = import_littlelogbook_db(self.db, enc)
        self.assertEqual((stats["vehicles"], stats["places"], stats["trips"], stats["odometer readings"]), (1, 1, 1, 1))
        (t,) = self.db.trips(self.db.q("SELECT id FROM vehicle WHERE registration='ABC123'")[0]["id"])
        self.assertEqual((t.end_desc, t.reason, t.distance_km), ("Office", "Client visit", 2.5))
        self.assertEqual(t.start_time, self.pts[0].time)

    @staticmethod
    def _encrypt(plain: bytes) -> bytes:
        import hashlib
        from tripbook.exchange import _rc4_keystream
        page = int.from_bytes(plain[16:18], "big")
        ks = int.from_bytes(_rc4_keystream(hashlib.sha1(b"l1ttl3l0gb00k").digest()[:16], page), "little")
        return b"".join((int.from_bytes(plain[o:o + page], "little") ^ ks).to_bytes(page, "little")
                        for o in range(0, len(plain), page))

    def test_reminders(self):
        import_points(self.db, self.vid, self.pts, "device")
        odo = self.db.current_odometer(self.vid)
        rid = self.db.x("INSERT INTO reminder(vehicle_id, title, due_odometer, repeat_km) VALUES (?,?,?,?)",
                        (self.vid, "Service", odo - 1, 15000))
        (r, status), = self.db.due_reminders()
        self.assertEqual(status, "overdue")
        self.db.complete_reminder(rid)
        (r, status), = self.db.due_reminders()
        self.assertEqual(status, "")
        self.assertAlmostEqual(r["due_odometer"], odo + 15000)

    def test_reminder_notice(self):
        import_points(self.db, self.vid, self.pts, "device")
        odo = self.db.current_odometer(self.vid)
        self.db.x("INSERT INTO reminder(vehicle_id, title, due_odometer, notice_km) VALUES (?,?,?,?)",
                  (self.vid, "Tyres", odo + 800, 1000))
        self.db.x("INSERT INTO reminder(vehicle_id, title, due_odometer, notice_km) VALUES (?,?,?,?)",
                  (self.vid, "Oil", odo + 800, 500))
        status = {r["title"]: s for r, s in self.db.due_reminders()}
        self.assertEqual(status, {"Tyres": "due soon", "Oil": ""})


class UpdateTests(unittest.TestCase):
    """Features and fixes that follow Little LogBook Global 2.4.x."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.vid = self.db.x("INSERT INTO vehicle(name, odometer_start) VALUES ('Car', 16093.44)")
        raw, _ = sample_log()
        self.pts = decode_log(raw)
        self.biz = self.db.q("SELECT id FROM trip_type WHERE name='Business'")[0]["id"]

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def test_old_database_is_migrated(self):
        import sqlite3
        path = Path(self.tmp.name) / "old.db"
        c = sqlite3.connect(path)
        c.executescript("CREATE TABLE rule (id INTEGER PRIMARY KEY, vehicle_id INTEGER, start_place_id INTEGER, "
                        "end_place_id INTEGER, trip_type_id INTEGER, reverse INTEGER, reason TEXT);"
                        "CREATE TABLE reminder (id INTEGER PRIMARY KEY, vehicle_id INTEGER, title TEXT, "
                        "due_date TEXT, due_odometer REAL, repeat_months INTEGER, repeat_km REAL, done INTEGER);")
        c.close()
        db = Database(path)
        self.assertIn("reverse_reason", {r[1] for r in db.q("PRAGMA table_info(rule)")})
        self.assertIn("notice_km", {r[1] for r in db.q("PRAGMA table_info(reminder)")})
        db.conn.close()

    def test_miles_are_converted_once(self):
        self.db.set_setting("units", "mi")
        self.assertAlmostEqual(self.db.to_unit(16.09344), 10.0)
        self.assertAlmostEqual(self.db.from_unit(10.0), 16.09344)
        import_points(self.db, self.vid, self.pts, "device")
        trips = self.db.trips(self.vid)
        html = report_html(self.db, None, trips, [], "p", layout="both")
        self.assertIn("10,000", html)  # opening odometer 16,093.44 km shown as 10,000 mi, not 6,214
        self.assertNotIn("6,214", html)

    def test_split_trip(self):
        import_points(self.db, self.vid, self.pts, "device")
        t, _ = self.db.trips(self.vid)
        pts = self.db.trip_points(t.id)
        new = self.db.split_trip(t.id, len(pts) // 2)
        a, b = self.db.trip(t.id), self.db.trip(new)
        self.assertEqual(a.end_time, b.start_time)
        self.assertAlmostEqual(a.distance_km + b.distance_km, t.distance_km, places=2)
        self.assertEqual(len(self.db.trip_points(t.id)) + len(self.db.trip_points(new)), len(pts) + 1)
        self.assertEqual(len(self.db.trips(self.vid)), 3)
        with self.assertRaises(ValueError):
            self.db.split_trip(t.id, 0)
        self.assertEqual(self.db.merge_trips([t.id, new]), t.id)  # split and combine are inverses
        self.assertAlmostEqual(self.db.trip(t.id).distance_km, t.distance_km, places=2)

    def test_manual_trips_are_not_combined_with_recorded_ones(self):
        import_points(self.db, self.vid, self.pts, "device")
        t1, t2 = self.db.trips(self.vid)
        self.db.delete_trips([t2.id])
        mid = self.db.add_manual_trip(self.vid, t1.end_time + timedelta(minutes=1),
                                      t1.end_time + timedelta(minutes=30), "A", "B", 12, None, "")
        with self.assertRaises(ValueError):
            self.db.merge_trips([t1.id, mid])

    def test_return_trip_reason_and_reason_history(self):
        import_points(self.db, self.vid, self.pts, "device")
        t1, t2 = self.db.trips(self.vid)
        home = self.db.x("INSERT INTO place(name, lat, lon) VALUES ('Home', ?, ?)", (t1.start_lat, t1.start_lon))
        office = self.db.x("INSERT INTO place(name, lat, lon) VALUES ('Office', ?, ?)", (t1.end_lat, t1.end_lon))
        self.db.x("INSERT INTO rule(start_place_id, end_place_id, trip_type_id, reverse, reason, reverse_reason) "
                  "VALUES (?,?,?,1,'To the office','Back home')", (home, office, self.biz))
        self.db.resolve_places()
        t1, t2 = self.db.trips(self.vid)
        self.assertEqual((t1.reason, t2.reason), ("To the office", "Back home"))
        self.assertEqual(set(self.db.reasons()), {"To the office", "Back home"})

    def test_timed_odometer_reading(self):
        import_points(self.db, self.vid, self.pts, "device")
        t1, t2 = self.db.trips(self.vid)
        # A reading taken between the two trips of the same day only corrects the second one.
        from tripbook.db import to_iso
        self.db.x("INSERT INTO odometer(vehicle_id, date, reading) VALUES (?,?,?)",
                  (self.vid, to_iso(t1.end_time + timedelta(minutes=1)), 50000))
        t1, t2 = self.db.trips(self.vid)
        self.assertAlmostEqual(t1.odo_start, 16093.44)
        self.assertAlmostEqual(t2.odo_start, 50000)

    def test_opening_odometer_never_overrides_later_readings(self):
        # Imported vehicle: opening odometer 16,093 km, saved with today's date by an edit, plus a reading
        # taken before the trips. The trips must continue from the reading, not restart at the opening value.
        import_points(self.db, self.vid, self.pts, "device")
        t1, t2 = self.db.trips(self.vid)
        self.db.x("UPDATE vehicle SET odometer_start_date=? WHERE id=?",
                  (t2.start_time.date().isoformat(), self.vid))
        self.db.x("INSERT INTO odometer(vehicle_id, date, reading) VALUES (?, '2020-01-01', 85000)", (self.vid,))
        t1, t2 = self.db.trips(self.vid)
        self.assertAlmostEqual(t1.odo_start, 85000)
        self.assertAlmostEqual(t2.odo_start, t1.odo_end)
        self.assertAlmostEqual(self.db.current_odometer(self.vid), 85000 + t1.distance_km + t2.distance_km)

    def test_mileage_claim(self):
        import_points(self.db, self.vid, self.pts, "device")
        trips = self.db.trips(self.vid)
        self.db.update_trip(trips[0].id, trip_type_id=self.biz)
        day = trips[0].start_time.astimezone().date()
        self.db.x("INSERT INTO mileage_rate(vehicle_id, from_date, rate) VALUES (NULL, ?, 0.5)",
                  ((day - timedelta(days=30)).isoformat(),))
        self.db.x("INSERT INTO mileage_rate(vehicle_id, from_date, rate) VALUES (NULL, ?, 9)",
                  ((day + timedelta(days=1)).isoformat(),))  # not valid yet on the trip's date
        trips = self.db.trips(self.vid)
        s = summary(self.db, trips, [])
        self.assertAlmostEqual(s["mileage_claim"], trips[0].distance_km * 0.5)
        self.assertIn("mileage claim", report_html(self.db, None, trips, [], "p"))

    def test_report_layouts(self):
        import_points(self.db, self.vid, self.pts, "device")
        trips = self.db.trips(self.vid)
        detailed = report_html(self.db, None, trips, [], "p", layout="detailed")
        monthly = report_html(self.db, None, trips, [], "p", layout="monthly")
        self.assertIn("<h2>Trips</h2>", detailed)
        self.assertNotIn("Monthly totals", detailed)
        self.assertIn("Monthly totals", monthly)
        self.assertNotIn("<h2>Trips</h2>", monthly)

    def test_kml_export(self):
        import xml.etree.ElementTree as ET
        from tripbook.exchange import write_kml
        path = Path(self.tmp.name) / "t.kml"
        write_kml(path, [("trip", "#2a7de1", self.pts)])
        ns = {"k": "http://www.opengis.net/kml/2.2"}
        root = ET.parse(path).getroot()
        coords = root.find(".//k:LineString/k:coordinates", ns).text.split()
        self.assertEqual(len(coords), len(self.pts))
        lon, lat, _ = map(float, coords[0].split(","))
        self.assertAlmostEqual(lat, self.pts[0].lat, places=5)
        self.assertAlmostEqual(lon, self.pts[0].lon, places=5)
        self.assertEqual(root.find(".//k:LineStyle/k:color", ns).text, "ffe17d2a")

    def test_naming_a_location_names_it_for_all_trips(self):
        import_points(self.db, self.vid, self.pts, "device")
        t1, t2 = self.db.trips(self.vid)
        # The return trip ends ~250 m from where the first trip started: still the same location.
        self.db.x("UPDATE trip SET end_lat=end_lat+0.00225 WHERE id=?", (t2.id,))
        pid = self.db.name_place(t1.start_lat, t1.start_lon, "Home")
        t1, t2 = self.db.trips(self.vid)
        self.assertEqual((t1.start_desc, t2.end_desc), ("Home", "Home"))
        self.assertEqual(t2.end_place_id, pid)
        self.db.name_place(t2.end_lat, t2.end_lon, "Home sweet home", t2.end_place_id)  # rename, no duplicate
        t1, _ = self.db.trips(self.vid)
        self.assertEqual(t1.start_desc, "Home sweet home")
        self.assertEqual(len(self.db.places()), 1)

    def test_old_place_radius_is_upgraded_once(self):
        import_points(self.db, self.vid, self.pts, "device")
        t1, _ = self.db.trips(self.vid)
        self.db.x("INSERT INTO place(name, lat, lon, radius_m) VALUES ('Imported', ?, ?, 50)",
                  (t1.start_lat + 0.002, t1.start_lon))  # ~220 m away: missed with 50 m
        self.db.x("DELETE FROM setting WHERE key='place_radius_upgraded'")
        self.db.conn.close()
        self.db = Database(self.db.path)
        self.assertEqual(self.db.places()[0]["radius_m"], 400)
        self.assertEqual(self.db.trips(self.vid)[0].start_desc, "Imported")
        self.db.x("UPDATE place SET radius_m=50")  # a radius the user sets later is kept
        self.db.conn.close()
        self.db = Database(self.db.path)
        self.assertEqual(self.db.places()[0]["radius_m"], 50)

    def test_unnamed_spots_and_create_places(self):
        import_points(self.db, self.vid, self.pts, "device")
        t1, t2 = self.db.trips(self.vid)
        self.db.update_trip(t2.id, end_desc="Some suburb")
        # Trip 1 drives A -> B, trip 2 B -> A; trip 2's end already shows a suburb name.
        spots = self.db.unnamed_spots()
        self.assertEqual([sorted(s.ends) for s in spots], [sorted([(t1.id, "end"), (t2.id, "start")]),
                                                           [(t1.id, "start")]])
        self.assertEqual([len(s.ends) for s in self.db.unnamed_spots(include_described=True)], [2, 2])
        office = spots[0]
        self.db.create_places([("Office", office.lat, office.lon)])
        t1, t2 = self.db.trips(self.vid)
        self.assertEqual((t1.end_desc, t2.start_desc), ("Office", "Office"))
        self.assertEqual([s.label for s in self.db.unnamed_spots(include_described=True)], ["Some suburb"])

    def test_place_suggestions(self):
        import io
        import json
        from unittest import mock
        from tripbook import exchange
        photon = {"features": [
            {"properties": {"name": "Main Road", "osm_key": "highway"}},
            {"properties": {"name": "Corner Cafe", "osm_key": "amenity"}},
            {"properties": {"osm_key": "place", "street": "Main Road", "housenumber": "12"}},
            {"properties": {"name": "Book Shop", "osm_key": "shop"}},
            {"properties": {"name": "1234", "osm_key": "place"}},
        ]}
        nominatim = {"address": {"road": "Main Road", "suburb": "Greenside"}}
        calls = []

        def fake_urlopen(req, timeout=0):
            calls.append(req.full_url)
            return io.BytesIO(json.dumps(photon if "photon" in req.full_url else nominatim).encode())

        with mock.patch.object(exchange.urllib.request, "urlopen", fake_urlopen), \
                mock.patch.object(exchange.time, "sleep", lambda s: None):
            names = exchange.place_suggestions(self.db, -26.1, 28.0)
            again = exchange.place_suggestions(self.db, -26.1, 28.0)
        self.assertEqual(names, ["Corner Cafe", "Book Shop", "12 Main Road", "Main Road, Greenside", "Main Road"])
        self.assertEqual(again, names)
        self.assertEqual(len(calls), 2)  # the second lookup comes from the cache

    def test_backup_is_a_plain_file_copy(self):
        # SQLite never opens the destination (its locks hang on network shares); odd names must work too.
        import_points(self.db, self.vid, self.pts, "device")
        folder = Path(self.tmp.name) / "share #1?"
        folder.mkdir()
        dest = folder / "copy #2.db"
        self.db.backup_to(dest)
        self.assertEqual([p.name for p in folder.iterdir()], ["copy #2.db"])  # no .part or snapshot left
        self.assertEqual(list(Path(self.tmp.name).glob(".snapshot-*")), [])
        self.db.x("DELETE FROM trip")
        self.db.restore_from(dest)
        self.assertEqual(len(self.db.trips()), 2)

    def test_auto_backup_and_restore(self):
        folder = Path(self.tmp.name) / "backups"
        for i in range(4):
            folder.mkdir(exist_ok=True)
            (folder / f"tripbook-2020010{i}-000000.db").write_bytes(b"")
        newest = self.db.auto_backup(folder, keep=3)
        self.assertEqual(len(list(folder.glob("tripbook-*.db"))), 3)
        self.assertTrue(newest.exists())
        import_points(self.db, self.vid, self.pts, "device")
        self.assertEqual(len(self.db.trips()), 2)
        self.db.restore_from(newest)
        self.assertEqual(len(self.db.trips()), 0)
        with self.assertRaises(ValueError):
            self.db.restore_from(folder / "tripbook-20200103-000000.db")


if __name__ == "__main__":
    unittest.main()
