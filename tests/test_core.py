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


if __name__ == "__main__":
    unittest.main()
