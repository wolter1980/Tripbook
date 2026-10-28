# Tripbook user guide

This guide covers everyday use: setting up a vehicle, downloading the tracker, cleaning up trips and
producing a logbook. For installing, see the [README](../README.md).

## 1. First start: add your vehicle

Tripbook opens on the **Vehicles** tab when there is no vehicle yet. Click **Add vehicle…** and enter:

- **Name** and **Registration** – the registration is also written to the tracker, so Tripbook knows which
  vehicle a tracker belongs to.
- **Opening odometer** – the odometer at the first trip in the logbook. Correct it later with odometer
  readings (see below), not by changing this value.
- **Tracker serial** / **Tracker password** – only needed if your tracker is password protected
  (the Little LogBook software sets a password when you register a device).

Coming from the Little LogBook Windows software? Use **File → Import Little LogBook database…** instead:
it brings in your vehicles, trips with their GPS tracks, trip types, named places, auto-mark rules,
expenses and odometer readings. Copy the database file (`llb.s3db`, in the Little LogBook folder,
typically `C:\Little LogBook Global`) from the Windows PC.

Then check **File → Settings**: distance unit (km/miles), currency, the month your financial year starts,
the stop time that splits trips, the default map, and backups.

## 2. Download trips from the tracker

1. Unplug the tracker from the car and plug it into the computer.
2. Click **Download from tracker** (Ctrl+D). The port is detected automatically; press **Refresh** if you
   plugged it in after opening the window.
3. Choose the **Vehicle** and click **Download trips**. Tick **Erase tracker memory after a successful
   download** to empty the tracker afterwards (you are asked to confirm).

**Device info** shows the registration on the tracker, its memory use and how long its satellite aiding
data is valid. A tracker more than 80 % full gets a warning: a full tracker stops recording.

New trips that overlap trips already in the logbook are skipped, so downloading the same data twice is
harmless. Every download is also saved as a `.bin` file in the `downloads` folder; **File → Import tracker
dump (.bin)…** imports it again (useful on a computer that cannot talk to the tracker, such as Windows).

**New tracker for the same car?** Select the vehicle, connect the new tracker and click **Use this tracker
for the vehicle…**. **Write registration to tracker** only writes the selected vehicle's registration.

**"No permission to open /dev/ttyACM0"** on Linux: install the udev rule (`./install.sh --udev`) or add
yourself to the `dialout` group; see the README.

## 3. The Trips tab

The toolbar filters what you see (and what goes into reports): **Vehicle**, **Period** (this/last month,
this/last financial year, last 12 months, all time or custom dates), trip **Type**, and a search box for
places and reasons. The status bar sums up the trips shown: count, distance, driving time, business share,
and how many are unmarked or were marked by rules.

The map below the table shows the selected trips (green = start, red = end, orange = a button press on
the tracker). Switch between map, satellite, hybrid and terrain in the map's top-right corner.

### Marking trips business or private

- Select one or more trips (Ctrl+click, Shift+click, or **Select all**), choose a **Type** and/or a
  **Reason** in the bar above the table, and click **Apply**. Reasons you used before are offered.
- Or double-click a trip's **Type** or **Reason** cell, or right-click → **Mark as** / **Set reason…**.

Trips nobody marked count as unmarked in reports.

### Fixing trips

Right-click a trip for:

- **Split trip…** – when two trips were recorded as one (the stop was shorter than the split time).
  Drag the slider to where the first trip ended; the longest stop is proposed.
- **Combine into one trip** – for two or more consecutive trips that are really one journey (manual trips
  can't be combined with recorded ones).
- **Delete** – e.g. "trips" recorded while the tracker stayed powered in a parked car.
- **Export as GPX… / KML…** – the track for GPS software or Google Earth.

**＋ Add trip** adds a manual trip for a journey the tracker missed: times, from/to, distance, type and
reason.

## 4. Places: name a location once

A place is a named spot with a radius (400 m by default). Every trip that starts or ends within the radius
shows the place name instead of coordinates or a suburb.

- Right-click a trip → **Name start location…** / **Name end location…**, or simply type a name in a
  trip's **From** or **To** cell: that names the location for every trip there.
- On the **Places** tab, click the map to add a place, or edit a place's name, address, position or radius
  in the table.
- **Suggest names for unnamed places…** groups the spots where trips start or end without a place, most
  visited first, and looks up names on OpenStreetMap (nearby businesses and buildings, and the street
  address). Pick or type a name, untick spots you don't want, and click **Create places**. Tick **Also
  spots that only show a suburb or street name** to include trips imported with a generic area name.

## 5. Auto-mark rules

On the **Auto-mark rules** tab, **Add rule…** marks every trip between two places automatically, e.g.
Home → Office is Business with reason "Commute". With **Also apply to the return trip** the rule also
covers Office → Home, with its own **Return trip reason** if you like. Rules apply to new downloads and
whenever places change; **Re-apply rules** runs them again. Trips you marked by hand are never changed by
a rule.

## 6. Odometer, expenses, reminders and mileage rates

**Odometer** (Vehicles tab): Tripbook computes the odometer from the opening odometer plus the GPS
distance of every trip. GPS distance drifts slowly from the car's odometer, so now and then add an
**odometer reading** with the date and time you read it: trips after that moment continue from your
reading.

**Expenses** (Expenses tab): fuel (with litres), maintenance, tyres, insurance, tolls and so on, per
vehicle. They appear in reports.

**Reminders** (Reminders tab): **Service interval** (every 12 months or 15,000 km, whichever comes first),
**Licence renewal** (yearly), or **Custom**, each with how early to remind you (days and/or distance).
Due reminders show in the status bar when Tripbook starts; **Mark done** moves a repeating reminder to its
next due date.

**Mileage rates** (Vehicles tab): a rate per km or mile, valid from a date, for one vehicle or all.
Reports then show the business mileage claim: each business trip at the rate valid on its date.

## 7. Reports

The **Report** tab shows the logbook for the vehicle, period and type chosen in the toolbar:

- a summary with total, business and per-type distance, the business-use percentage, the mileage claim
  and expenses;
- with **Layout**, every trip (date, time, from, to, opening and closing odometer, distance, type, reason),
  monthly totals with odometers, or both.

**Save as PDF…**, **Print…**, **Export CSV…** (for spreadsheets) and **Export KML…** (the period's
tracks for Google Earth). For a tax year, choose **Period → Last financial year** (set when your
financial year starts under **File → Settings**).

## 8. Backups

With **Back up the database automatically when Tripbook closes** (on by default), every close writes
`tripbook-<date>-<time>.db` to the backup folder and keeps the newest 5. Choose the **Backup folder** in
**File → Settings**: a USB drive, a network share or a synced cloud folder is a good idea. If that folder
isn't available when Tripbook closes, the backup goes to the data folder and you get a message.

**File → Back up database…** makes a backup anywhere, any time. **File → Restore from backup…** replaces
the logbook with a backup; your current data is backed up first.

## 9. Moving to another computer

Copy the data folder (see "Where your data lives" in the README) or restore a backup on the new computer.
Raw tracker dumps (`.bin`) and GPX files can be imported on any computer.
