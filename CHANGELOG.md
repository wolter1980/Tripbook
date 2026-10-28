# Changelog

## 1.1.0 – 2026-10-28

Brought in line with **Little LogBook Global 2.4.002** (Windows installer of 28 May 2024; release notes
of 2.4.001) and its user guide.

From the Little LogBook 2.4.001 release notes:

- **Map views** (Little LogBook removed its broken Yandex and Google China maps): every map now has a
  layer switcher with Map (OpenStreetMap), Satellite, Hybrid and Terrain views, all usable without an API
  key, plus your own tile server as "Custom". The last view you picked is remembered.
- **Replacement tracker**: "Use this tracker for the vehicle…" in the download dialog pairs the connected
  tracker with the selected vehicle on purpose (writes the registration, stores the tracker serial), instead
  of relying on matching details.
- **Kilometres/miles**: Tripbook's counterpart of the "Km to Miles to Miles" bug is fixed. With miles
  selected, distance fields (opening odometer, odometer readings, manual trip distance, reminders, expense
  odometer) used to expect kilometres while the trip list showed miles. They now use the chosen unit, and so
  does the odometer list.
- **Return trip reasons on auto-mark rules**: a rule can have a different reason for the return trip, both
  reason fields offer previously used reasons, and rules can now be edited.
- **Report options are saved**: the report layout and the expenses option survive a restart.

From the Little LogBook user guide:

- Split a trip at any point of its track (right-click → Split trip…); the longest stop is proposed.
- Suggest names for unnamed places (Places tab, or right-click a trip): trip starts/ends without a place
  are grouped into spots, most visited first, with names from OpenStreetMap – nearby businesses and
  buildings (Photon) and the street address (Nominatim). Creating them names every trip at those spots.
- Reports: choose every trip, monthly totals (with opening/closing odometer per month), or both.
- Mileage rates per km/mile, valid from a date (per vehicle or for all); reports show the business
  mileage claim.
- KML (Google Earth) export of trips and of the report period.
- Odometer readings have a time, so a reading between two trips on the same day corrects only the later one.
- Reminder kinds (service interval, licence renewal, custom) and a per-reminder advance notice in days
  and distance.
- Automatic database backup when Tripbook closes, keeping the newest 5 (configurable), into a backup folder
  of your choice (e.g. a USB drive or network share; when it is unavailable the backup goes to the data
  folder with a warning), and File → Restore from backup….
- Warning when the tracker memory is 80 % full.
- Manual trips can't be combined with recorded trips (a manual trip has no GPS track).
- The status bar also shows total driving time and the number of auto-marked trips.
- Little LogBook import keeps the time of odometer readings.

Fixes:

- The tracker could not be opened right after being added to the `dialout` group, until the next login.
  The `tripbook` launcher now picks up the group by itself, and the error message says what is wrong.
- Naming a place now names it for every trip at that location, as in Little LogBook: places match trip
  ends within 400 m (Little LogBook's range; places imported with 50 m and older 150 m places are upgraded
  once), and typing a name into a recorded trip's From/To cell names that location instead of only that trip.
- The odometer of new trips could restart at the opening odometer: editing a vehicle saved today's date as
  the opening odometer date, which then overrode all later odometer readings. The opening odometer now
  always applies from the first trip, and the vehicle window no longer has that date field.
- Backing up onto a network share (SMB/CIFS) froze Tripbook: SQLite's file locks hang there. Backups are
  now made locally and copied over as a plain file in the background, with a 60-second limit; restoring
  or importing from a share reads without locks. Importing a Tripbook backup as a Little LogBook
  database now explains to use Restore instead.
- Tables keep their selection when they refresh: e.g. the odometer readings of the selected vehicle no
  longer disappear after adding a reading.

Existing databases are upgraded automatically when Tripbook starts.

## 1.0.0

First release.
