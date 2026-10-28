"""Leaflet/OpenStreetMap map embedded through Qt WebEngine."""
from __future__ import annotations

import json

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView

DEFAULT_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

HTML = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body,#map{height:100%;margin:0;background:#ddd}
.lbl{background:rgba(255,255,255,.9);border:1px solid #888;border-radius:3px;padding:1px 4px;font:12px sans-serif}
#off{position:absolute;inset:0;display:none;align-items:center;justify-content:center;font:14px sans-serif;color:#555}
</style></head><body><div id="map"></div><div id="off">Map unavailable (no internet connection?)</div><script>
if (typeof L === 'undefined') { document.getElementById('off').style.display='flex'; }
var map = L.map('map', {zoomControl:true}).setView([%LAT%, %LON%], %ZOOM%);
L.tileLayer('%TILES%', {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
var layer = L.layerGroup().addTo(map), placeLayer = L.layerGroup().addTo(map);
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
</script></body></html>"""


class _Page(QWebEnginePage):
    def __init__(self, view: "MapView"):
        super().__init__(view)
        self._view = view

    def javaScriptConsoleMessage(self, level, message, line, source):
        if message.startswith("tripbook-click:"):
            lat, lon = message.split(":", 1)[1].split(",")
            self._view.clicked.emit(float(lat), float(lon))


class MapView(QWebEngineView):
    clicked = Signal(float, float)

    def __init__(self, parent=None, tiles: str = DEFAULT_TILES, center=(20.0, 0.0), zoom: int = 2):
        super().__init__(parent)
        self._ready = False
        self._pending: list[str] = []
        self.setPage(_Page(self))
        s = self.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.loadFinished.connect(self._on_loaded)
        html = (HTML.replace("%TILES%", tiles or DEFAULT_TILES).replace("%LAT%", str(center[0]))
                .replace("%LON%", str(center[1])).replace("%ZOOM%", str(zoom)))
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
