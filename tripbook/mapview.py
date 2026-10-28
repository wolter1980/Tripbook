"""Leaflet map (OpenStreetMap, satellite, hybrid and terrain views) embedded through Qt WebEngine."""
from __future__ import annotations

import json

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

DEFAULT_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
_ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/{}/MapServer/tile/{{z}}/{{y}}/{{x}}"
_OSM_ATTR = "&copy; OpenStreetMap contributors"
_ESRI_ATTR = "Imagery &copy; Esri, Maxar, Earthstar Geographics"
# Base map views, each a stack of tile layers (all usable without an API key).
MAP_VIEWS: dict[str, list[dict]] = {
    "Map": [{"url": DEFAULT_TILES, "attribution": _OSM_ATTR, "maxZoom": 19}],
    "Satellite": [{"url": _ESRI.format("World_Imagery"), "attribution": _ESRI_ATTR, "maxZoom": 19}],
    "Hybrid": [{"url": _ESRI.format("World_Imagery"), "attribution": _ESRI_ATTR, "maxZoom": 19},
               {"url": _ESRI.format("Reference/World_Transportation"), "attribution": "", "maxZoom": 19},
               {"url": _ESRI.format("Reference/World_Boundaries_and_Places"), "attribution": "", "maxZoom": 19}],
    "Terrain": [{"url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", "maxZoom": 17,
                 "attribution": _OSM_ATTR + ", SRTM | &copy; OpenTopoMap (CC-BY-SA)"}],
}


def map_views(custom_tiles: str = "") -> dict[str, list[dict]]:
    """The built-in views plus "Custom" when the user configured their own tile server."""
    views = dict(MAP_VIEWS)
    if custom_tiles and custom_tiles != DEFAULT_TILES:
        views["Custom"] = [{"url": custom_tiles, "attribution": "", "maxZoom": 19}]
    return views

HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0;background:#ddd}
.lbl{background:rgba(255,255,255,.9);border:1px solid #888;border-radius:3px;padding:1px 4px;font:12px sans-serif}
#off{position:absolute;inset:0;display:none;align-items:center;justify-content:center;font:14px sans-serif;color:#555}
</style></head><body><div id="map"></div><div id="off">Map unavailable (no internet connection?)</div><script>
if (typeof L === 'undefined') { document.getElementById('off').style.display='flex'; }
var map = L.map('map', {zoomControl:true}).setView([%LAT%, %LON%], %ZOOM%);
var views = %VIEWS%, bases = {};
Object.keys(views).forEach(function(k){
  bases[k] = L.layerGroup(views[k].map(function(t){ return L.tileLayer(t.url, {maxZoom: t.maxZoom, attribution: t.attribution}); }));
});
(bases[%VIEW%] || bases['Map']).addTo(map);
L.control.layers(bases, null, {position: 'topright'}).addTo(map);
map.on('baselayerchange', function(e){ console.log('tripbook-view:' + e.name); });
var layer = L.layerGroup().addTo(map), placeLayer = L.layerGroup().addTo(map), cursor = null;
map.on('click', function(e){ console.log('tripbook-click:' + e.latlng.lat + ',' + e.latlng.lng); });
function dot(lat, lon, color, label){
  var m = L.circleMarker([lat, lon], {radius:7, color:'#fff', weight:2, fillColor:color, fillOpacity:1});
  if (label) m.bindTooltip(label); return m;
}
function showTrips(trips, fit){
  layer.clearLayers(); var b = [];
  trips.forEach(function(t){
    if (t.coords.length > 1) { L.polyline(t.coords, {color:t.color, weight:4, opacity:.85}).bindTooltip(t.label).addTo(layer); b = b.concat(t.coords); }
    if (t.start) { dot(t.start[0], t.start[1], '#1a9850', t.startLabel).addTo(layer); b.push(t.start); }
    if (t.end) { dot(t.end[0], t.end[1], '#d73027', t.endLabel).addTo(layer); b.push(t.end); }
    (t.pois||[]).forEach(function(p){ dot(p[0], p[1], '#fdae61', 'Button press').addTo(layer); });
  });
  if (fit && b.length) map.fitBounds(L.latLngBounds(b).pad(0.15), {maxZoom: 16});
}
function showPlaces(places, fit){
  placeLayer.clearLayers(); var b = [];
  places.forEach(function(p){
    L.circle([p.lat, p.lon], {radius:p.radius, color:'#6a3d9a', weight:1, fillOpacity:.12}).addTo(placeLayer);
    L.marker([p.lat, p.lon]).bindTooltip(p.name, {permanent: p.permanent, direction:'top', className:'lbl'}).addTo(placeLayer);
    b.push([p.lat, p.lon]);
  });
  if (fit && b.length) map.fitBounds(L.latLngBounds(b).pad(0.2), {maxZoom: 15});
}
function focusOn(lat, lon, z){ map.setView([lat, lon], z || 16); }
function showCursor(lat, lon, pan){
  if (!cursor) cursor = L.circleMarker([lat, lon], {radius:8, color:'#333', weight:2, fillColor:'#ffd700', fillOpacity:1}).addTo(map);
  cursor.setLatLng([lat, lon]);
  if (pan && !map.getBounds().contains([lat, lon])) map.panTo([lat, lon]);
}
</script></body></html>"""


class _Page(QWebEnginePage):
    def __init__(self, view: "MapView"):
        super().__init__(view)
        self._view = view

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message.startswith("tripbook-click:"):
            lat, lon = message.split(":", 1)[1].split(",")
            self._view.clicked.emit(float(lat), float(lon))
        elif message.startswith("tripbook-view:"):
            self._view.view_changed.emit(message.split(":", 1)[1])


class MapView(QWebEngineView):
    clicked = Signal(float, float)
    view_changed = Signal(str)  # the user picked another base map in the layer switcher

    def __init__(self, parent=None, tiles: str = DEFAULT_TILES, view: str = "Map", center=(20.0, 0.0),
                 zoom: int = 2):
        super().__init__(parent)
        self._ready = False
        self._pending: list[str] = []
        self.setPage(_Page(self))
        s = self.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.loadFinished.connect(self._on_loaded)
        html = (HTML.replace("%VIEWS%", json.dumps(map_views(tiles))).replace("%VIEW%", json.dumps(view))
                .replace("%LAT%", str(center[0])).replace("%LON%", str(center[1])).replace("%ZOOM%", str(zoom)))
        # A http base URL gives the tile server a proper Referer, as the OSM tile policy requires.
        self.setHtml(html, QUrl("http://localhost/tripbook/"))

    def _on_loaded(self, ok: bool) -> None:
        self._ready = True
        for js in self._pending:
            self.page().runJavaScript(js)
        self._pending.clear()

    def _js(self, js: str) -> None:
        if self._ready:
            self.page().runJavaScript(js)
        else:
            self._pending.append(js)

    def show_trips(self, trips: list[dict], fit: bool = True) -> None:
        self._js(f"showTrips({json.dumps(trips)}, {str(fit).lower()});")

    def show_places(self, places: list[dict], fit: bool = False) -> None:
        self._js(f"showPlaces({json.dumps(places)}, {str(fit).lower()});")

    def focus(self, lat: float, lon: float, zoom: int = 16) -> None:
        self._js(f"focusOn({lat}, {lon}, {zoom});")

    def show_cursor(self, lat: float, lon: float, pan: bool = True) -> None:
        self._js(f"showCursor({lat}, {lon}, {str(pan).lower()});")
