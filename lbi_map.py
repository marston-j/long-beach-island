#!/usr/bin/env python3
"""
Long Beach Island Geospatial Map Builder

Builds a self-contained interactive Leaflet.js map centered on Long Beach
Island, New Jersey, with a search radius wide enough to take in Barnegat Bay,
the Great Bay / Mullica estuary and the New Jersey Pine Barrens.

Ports every layer from the Gulf Islands `trail_map.py` build and adds the
New Jersey specific sources: NJDEP, NJ Pinelands Commission, NJ HPO,
USFWS (refuges + National Wetlands Inventory), USGS PAD-US, NOAA MPA
Inventory, FEMA NFHL, and NOAA ENC nautical charts as selectable basemaps.

Usage:
  python3 lbi_map.py \\
    --bbox lbi-region \\
    --ebird-key YOUR_KEY \\
    --out output/lbi/index.html

  # Island only, fast build
  python3 lbi_map.py --bbox lbi --out output/lbi/index.html

  # Re-render HTML from cache with no network calls
  python3 lbi_map.py --bbox lbi-region --out output/lbi/index.html --render-only

  # Inject as a "Map" tab into an existing field-checklist page instead
  python3 lbi_map.py --bbox lbi-region --target output/lbi/checklist.html
"""

import argparse
import json
import logging
import math
import os
import re
import time
from pathlib import Path

import requests

try:
    import osm2geojson
except ImportError:
    print("Install osm2geojson:  pip install osm2geojson")
    raise SystemExit(1)

log = logging.getLogger("lbi_map")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
)

# The public Overpass instance returns 504 freely on a bbox this size — even
# for trivial queries when it is busy — and this build makes ~20 OSM queries.
# Rotate through the mirrors rather than burning the whole backoff on one host.
#
# Only add a mirror after checking it actually serves this region. Several
# public endpoints are country-scoped: overpass.osm.ch answers 200 in under a
# second for a New Jersey bbox and returns zero elements, which looks exactly
# like "there is nothing there" and silently emptied 13 layers before it was
# caught. Both mirrors below were verified against a query with known results.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
EBIRD_API = "https://api.ebird.org/v2"
INAT_API_URL = "https://api.inaturalist.org/v1"
HEADERS = {"User-Agent": "LBIMapBuilder/1.0 (geospatial-field-map)"}

# Set by --render-only. Every network path checks the cache first and returns
# empty rather than making a request when this is on.
OFFLINE = False

# ─── Service endpoints ────────────────────────────────────────────

NPS_NRHP_API = (
    "https://mapservices.nps.gov/arcgis/rest/services/"
    "cultural_resources/nrhp_locations/MapServer"
)

# NJDEP Bureau of GIS — https://mapsdep.nj.gov/arcgis/rest/services
NJDEP = "https://mapsdep.nj.gov/arcgis/rest/services/Features"
NJDEP_LAND = f"{NJDEP}/Land/MapServer"
NJDEP_LANDLU = f"{NJDEP}/Land_lu/MapServer"
NJDEP_HABITAT = f"{NJDEP}/Environmental_habitat/MapServer"
NJDEP_ENVADMIN = f"{NJDEP}/Environmental_admin/MapServer"
NJDEP_ENV = f"{NJDEP}/Environmental/MapServer"
NJDEP_HYDRO = f"{NJDEP}/Hydrography/MapServer"
NJDEP_CAFRA = f"{NJDEP}/Land_CAFRA_coast/MapServer"

# NJ Pinelands Commission
PINELANDS = "https://services1.arcgis.com/nCm6SZaiGMuGX35l/arcgis/rest/services"

# NJDEP Landscape Project v3.4 — species-based habitat, ranked 1-5. This is New
# Jersey's operative mapping of significant habitat, and the spatial stand-in
# for the USFWS report "Significant Habitats and Habitat Complexes of the New
# York Bight Watershed" (1997), which was published as narrative and figures
# with no accompanying GIS dataset.
NJ_LANDSCAPE = "https://services1.arcgis.com/QWdNfRs7lkPq4g4Q/arcgis/rest/services"
LANDSCAPE_HABITAT = [
    ("Marine", f"{NJ_LANDSCAPE}/Landscape_Project_Species_Based_Habitat_Marine/FeatureServer/24"),
    ("Atlantic Coastal", f"{NJ_LANDSCAPE}/Atlantic_Coastal_Habitat_Landscape_v3_4/FeatureServer/26"),
    ("Pinelands", f"{NJ_LANDSCAPE}/Landscape_Project_Species_Based_Habitat_Pinelands/FeatureServer/22"),
    ("Delaware Bay", f"{NJ_LANDSCAPE}/Landscape_Project_Species_Based_Habitat_Delaware_Bay/FeatureServer/25"),
]
LANDSCAPE_STREAM = f"{NJ_LANDSCAPE}/Stream_Habitat_Landscape_v3_4/FeatureServer/20"
LANDSCAPE_VERNAL = f"{NJ_LANDSCAPE}/Landscape_Project_Vernal_Pools/FeatureServer/30"
LANDSCAPE_VERNAL_HAB = f"{NJ_LANDSCAPE}/Landscape_Project_Vernal_Pools/FeatureServer/13"

# Designated critical habitat under the ESA
FWS_CRITICAL_HABITAT = ("https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/"
                        "rest/services/Critical_Habitat/FeatureServer/1")
NMFS_CRITICAL_HABITAT = ("https://maps.fisheries.noaa.gov/server/rest/services/"
                         "All_NMFS_Critical_Habitat/MapServer/2")

# USGS Protected Areas Database of the United States (PAD-US 4.1)
PADUS_MANAGER = (
    "https://services.arcgis.com/v01gqwM5QqNysAAi/arcgis/rest/services/"
    "Manager_Type_PADUS/FeatureServer/0"
)

# USFWS
FWS_NWRS = (
    "https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/services/"
    "National_Wildlife_Refuge_System_Boundaries/FeatureServer/0"
)
FWS_WILDERNESS = (
    "https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/services/"
    "FWSWilderness/FeatureServer/0"
)
NWI_RASTER = (
    "https://fwsprimary.wim.usgs.gov/server/rest/services/"
    "Wetlands_Raster/ImageServer/exportImage"
)

# NOAA
NOAA_MPA = (
    "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/"
    "NOAA_MPA_Inventory_2023/FeatureServer/0"
)
NOAA_COOPS_STATIONS = (
    "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations.json"
)
NOAA_HMS_SMOKE = (
    "https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/"
    "NOAA_Satellite_Smoke_Detection_(v1)/FeatureServer/0/query"
)
NIFC_FIRE_PERIMETERS = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services/"
    "WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
)

# ─── Geography ────────────────────────────────────────────────────

# Long Beach Island runs ~18 mi NNE-SSW from Barnegat Light down to Holgate.
# Default center is the Manahawkin Bay causeway landing at Ship Bottom.
LBI_CENTER = (39.6444, -74.1800)

BBOX_PRESETS = {
    # The island plus its back bay — fast build, everything on foot/bike.
    "lbi": "39.48,-74.40,39.80,-74.02",
    # Default. Island + Barnegat Bay + Great Bay + the full Pinelands
    # National Reserve out to its western edge near Hammonton.
    "lbi-region": "39.25,-75.05,40.10,-73.90",
    # Barnegat Bay watershed, Island Beach down to Little Egg Harbor.
    "barnegat-bay": "39.45,-74.45,40.05,-73.98",
    # The Pine Barrens / Pinelands National Reserve only.
    "pinelands": "39.30,-75.05,40.05,-74.10",
    # Jacques Cousteau NERR — Mullica River / Great Bay estuary.
    "mullica-great-bay": "39.45,-74.60,39.65,-74.25",
}

# ─── Layer catalogue ──────────────────────────────────────────────
# group   — sidebar section
# color   — swatch + default vector colour
# on      — visible on first load
# kind    — "vector" (GeoJSON) or "raster" (Esri dynamic tile overlay)

LAYER_GROUPS = [
    ("island", "Island & Shore"),
    ("treats", "Treats & Amusements"),
    ("trails", "Trails & Routes"),
    ("historic", "Historic"),
    ("protected", "Protected Lands"),
    ("pinelands", "Pine Barrens"),
    ("habitat", "Significant Habitat"),
    ("marine", "Marine & Estuarine"),
    ("wetlands", "Wetlands & Water"),
    ("wildlife", "Wildlife"),
    ("live", "Live Conditions"),
    ("charts", "Charts & Terrain"),
]

LAYER_DEFS = {
    # ── Island & Shore ──
    "beaches_public": {"group": "island", "label": "Public Beaches",
                       "color": "#27AE60", "on": True},
    "beaches_private": {"group": "island", "label": "Private / Restricted",
                        "color": "#E74C3C", "on": False},
    "public_access": {"group": "island", "label": "NJ Shore Access Points",
                      "color": "#0E8A6E", "on": True},
    "lighthouses": {"group": "island", "label": "Lighthouses",
                    "color": "#C0392B", "on": True},
    "boat_access": {"group": "island", "label": "Boat Ramps & Fishing Access",
                    "color": "#1F6FB2", "on": False},

    # ── Treats & Amusements ──
    "ice_cream": {"group": "treats", "label": "Ice Cream Stands",
                  "color": "#E8739C", "on": True},
    "mini_golf": {"group": "treats", "label": "Mini Golf",
                  "color": "#7B3FBF", "on": True},
    "amusements": {"group": "treats", "label": "Arcades & Water Parks",
                   "color": "#F39C12", "on": False},

    # ── Trails & Routes ──
    "hiking": {"group": "trails", "label": "Hiking Trails",
               "color": "#D4820F", "on": True},
    "bike": {"group": "trails", "label": "Bike Routes",
             "color": "#2E6B94", "on": False},
    "nj_trails": {"group": "trails", "label": "NJ Statewide Trails",
                  "color": "#A0522D", "on": False},
    "park_trails": {"group": "trails", "label": "State Park Trails",
                    "color": "#8B6914", "on": False},

    # ── Historic ──
    "heritage": {"group": "historic", "label": "Historic Architecture",
                 "color": "#7A5230", "on": True},
    "nj_historic": {"group": "historic", "label": "NJ Historic Properties",
                    "color": "#9B2335", "on": False},
    "nj_historic_dist": {"group": "historic", "label": "NJ Historic Districts",
                         "color": "#B8860B", "on": False},
    "kings_roads": {"group": "historic", "label": "Old King's Roads",
                    "color": "#6B3410", "on": True},
    "orig_highways": {"group": "historic", "label": "Original Highways",
                      "color": "#8E5A2B", "on": False},
    "old_rail": {"group": "historic", "label": "Abandoned Rail Grades",
                 "color": "#555555", "on": False},
    "hist_shoreline": {"group": "historic", "label": "Historical Shorelines",
                       "color": "#C77B3F", "on": False},

    # ── Protected Lands ──
    "federal_lands": {"group": "protected", "label": "Federal Protected Lands",
                      "color": "#1B5E20", "on": True},
    "refuges_fws": {"group": "protected", "label": "Nat'l Wildlife Refuges",
                    "color": "#2A7A7A", "on": True},
    "fws_wilderness": {"group": "protected", "label": "Federal Wilderness",
                       "color": "#4A6A3A", "on": False},
    "state_lands": {"group": "protected", "label": "NJ State Lands",
                    "color": "#3A7D50", "on": True},
    "natural_areas": {"group": "protected", "label": "State Natural Areas",
                      "color": "#256D45", "on": False},
    "nhp_sites": {"group": "protected", "label": "Natural Heritage Priority",
                  "color": "#8E44AD", "on": False},
    "focal_areas": {"group": "protected", "label": "Conservation Focal Areas",
                    "color": "#5B8C5A", "on": False},
    "state_parks": {"group": "protected", "label": "State Parks (OSM)",
                    "color": "#48896B", "on": False},
    "refuges": {"group": "protected", "label": "Protected Areas (OSM)",
                "color": "#2D7A5F", "on": False},
    "forests": {"group": "protected", "label": "Forests (OSM)",
                "color": "#2D5A1E", "on": False},
    "open_space_all": {"group": "protected", "label": "All Open Space (NJDEP)",
                       "color": "#6FA96F", "on": False, "kind": "raster"},

    # ── Pine Barrens ──
    "pnr": {"group": "pinelands", "label": "Pinelands National Reserve",
            "color": "#1E5631", "on": True},
    "pinelands_mgmt": {"group": "pinelands", "label": "Pinelands Mgmt Areas",
                       "color": "#7A9E3F", "on": False},
    "chanj": {"group": "pinelands", "label": "CHANJ Habitat Cores",
              "color": "#4E8C3C", "on": False, "kind": "raster"},

    # ── Significant Habitat ──
    "sig_habitat": {"group": "habitat", "label": "Significant Habitat",
                    "color": "#7D3C98", "on": False},
    "vernal_pools": {"group": "habitat", "label": "Vernal Pools & Habitat",
                     "color": "#2980B9", "on": False},
    "stream_habitat": {"group": "habitat", "label": "Stream Habitat",
                       "color": "#1ABC9C", "on": False},
    "critical_habitat": {"group": "habitat", "label": "ESA Critical Habitat",
                         "color": "#CB4335", "on": True},

    # ── Marine & Estuarine ──
    "mpa": {"group": "marine", "label": "Marine Protected Areas",
            "color": "#00688B", "on": True},
    "nerrs": {"group": "marine", "label": "Estuarine Reserves (NERR)",
              "color": "#005F73", "on": True},
    "shellfish": {"group": "marine", "label": "Shellfish Classification",
                  "color": "#8B7355", "on": False},
    "reefs": {"group": "marine", "label": "Artificial Reefs",
              "color": "#B03A2E", "on": False},
    "tide_stations": {"group": "marine", "label": "NOAA Tide Stations",
                      "color": "#154360", "on": False},

    # ── Wetlands & Water ──
    "wetlands_osm": {"group": "wetlands", "label": "Wetlands (mapped)",
                     "color": "#3D8B7D", "on": True},
    "nwi": {"group": "wetlands", "label": "USFWS Wetlands Inventory",
            "color": "#007C88", "on": False, "kind": "raster"},
    "njdep_wetlands": {"group": "wetlands", "label": "NJDEP Wetlands 2012",
                       "color": "#2E8B77", "on": False, "kind": "raster"},
    "tidelands": {"group": "wetlands", "label": "Tidelands Claims",
                  "color": "#5DADE2", "on": False, "kind": "raster"},
    "flood": {"group": "wetlands", "label": "FEMA Flood Zones",
              "color": "#00B5E2", "on": False, "kind": "raster"},

    # ── Wildlife ──
    "hotspots": {"group": "wildlife", "label": "Birding Hotspots",
                 "color": "#8B4513", "on": True},
    "ebird_obs": {"group": "wildlife", "label": "eBird Obs (30 d)",
                  "color": "#1A6B3A", "on": False},
    "inat_rare": {"group": "wildlife", "label": "Rare Species (iNat)",
                  "color": "#D4380D", "on": False},

    # ── Live Conditions ──
    "wildfires": {"group": "live", "label": "Active Wildfires",
                  "color": "#FF4500", "on": True},
    "smoke": {"group": "live", "label": "Smoke Plumes (HMS)",
              "color": "#8B6914", "on": False},

    # ── Charts & Terrain ──
    "noaa_charts": {"group": "charts", "label": "NOAA Nautical Chart",
                    "color": "#1B4F72", "on": False, "kind": "raster"},
    "bathymetry": {"group": "charts", "label": "Bathymetry (NCEI)",
                   "color": "#1A5276", "on": False, "kind": "raster"},
}


# Stacking order, low to high. Large regional polygons sit at the bottom so the
# small specific features drawn inside them stay clickable; lines sit above all
# areas, and point markers above everything.
STACK_ORDER = [
    # broad regional extents
    "pnr", "pinelands_mgmt", "focal_areas", "mpa", "nerrs",
    # public land ownership
    "federal_lands", "refuges_fws", "fws_wilderness", "forests", "refuges",
    "state_parks", "state_lands",
    # habitat and water
    "shellfish", "wetlands_osm", "sig_habitat", "critical_habitat",
    "natural_areas", "nhp_sites",
    # small, specific areas
    "nj_historic_dist", "beaches_private", "beaches_public", "reefs",
    "mini_golf", "amusements", "smoke", "wildfires",
    # lines
    "stream_habitat",
    "hist_shoreline", "old_rail", "orig_highways", "kings_roads",
    "nj_trails", "park_trails", "bike", "hiking",
    # points
    "heritage", "nj_historic", "boat_access", "public_access",
    "vernal_pools", "tide_stations", "lighthouses", "hotspots", "ebird_obs",
    "ice_cream",
]


def stack_order() -> list:
    """STACK_ORDER, with any layer it forgets appended so nothing is dropped."""
    known = set(STACK_ORDER)
    missing = [k for k in LAYER_DEFS
               if k not in known and LAYER_DEFS[k].get("kind") != "raster"]
    return [k for k in STACK_ORDER if k in LAYER_DEFS] + missing


def vector_keys() -> list:
    return [k for k, v in LAYER_DEFS.items() if v.get("kind", "vector") == "vector"]


# ─── Caching ───────────────────────────────────────────────────────

def load_cache(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("Cache at %s is corrupt — starting fresh", path)
    return {}


def save_cache(path: Path, data: dict):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


# ─── Bounding box helpers ─────────────────────────────────────────

def parse_bbox(s: str) -> tuple:
    parts = [float(x.strip()) for x in s.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox must be S,W,N,E — got {s!r}")
    s_, w_, n_, e_ = parts
    if s_ >= n_ or w_ >= e_:
        raise ValueError(f"bbox must be S,W,N,E with S<N and W<E — got {s!r}")
    return (s_, w_, n_, e_)


def bbox_ql(bbox: tuple) -> str:
    """Overpass bbox order: south,west,north,east."""
    return f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"


def bbox_esri(bbox: tuple) -> str:
    """ArcGIS envelope order: xmin,ymin,xmax,ymax."""
    s, w, n, e = bbox
    return f"{w},{s},{e},{n}"


def grid_points(bbox: tuple, step_km: float = 45) -> list:
    s, w, n, e = bbox
    mid_lat = (s + n) / 2
    lat_step = step_km / 111.0
    lng_step = step_km / (111.0 * math.cos(math.radians(mid_lat)))
    pts = []
    lat = s + lat_step / 2
    while lat < n + lat_step / 2:
        lng = w + lng_step / 2
        while lng < e + lng_step / 2:
            pts.append((round(lat, 3), round(lng, 3)))
            lng += lng_step
        lat += lat_step
    return pts


# ─── GeoJSON helpers ──────────────────────────────────────────────

def _round_coords(coords, nd=5):
    if isinstance(coords, (int, float)):
        return round(coords, nd)
    return [_round_coords(c, nd) for c in coords]


# Agency salt-marsh and refuge boundaries are digitised at survey resolution —
# Edwin B. Forsythe NWR alone is 74k vertices, which is meaningless detail for
# a field map and dominates the page weight. Generalizing to ~6 m is invisible
# below zoom 16 and cuts those layers roughly eight-fold.
# Overridable with --simplify, since a regional bbox carries far more geometry
# than the island alone. FINE tracks it at 40%.
SIMPLIFY_DEG = 0.00005        # ≈ 6 m at this latitude — areas
SIMPLIFY_DEG_FINE = 0.00002   # ≈ 2 m — shorelines and trails, where shape matters
# Planning-scale boundaries: Conservation Focal Areas arrive as 19 polygons
# carrying 233k vertices, and nobody reads a regional conservation boundary to
# the metre. ~22 m keeps them legible and cuts several megabytes off the page.
SIMPLIFY_DEG_COARSE = 0.0002  # ≈ 22 m — regional planning and habitat polygons

# Sentinel so callers can say "whatever the current default is" and pick up a
# --simplify override, which a signature default bound at import time would miss.
DEFAULT_TOL = object()


def _dp_simplify(points: list, tol: float) -> list:
    """Douglas-Peucker on a [lng, lat] ring or line. Iterative, so long marsh
    boundaries cannot blow the recursion limit."""
    if len(points) < 3:
        return points

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        if last <= first + 1:
            continue
        x1, y1 = points[first][0], points[first][1]
        x2, y2 = points[last][0], points[last][1]
        dx, dy = x2 - x1, y2 - y1
        norm = dx * dx + dy * dy
        far_i, far_d = -1, -1.0
        for i in range(first + 1, last):
            px, py = points[i][0], points[i][1]
            if norm == 0:
                d = (px - x1) ** 2 + (py - y1) ** 2
            else:
                # Squared distance from the point to the segment.
                t = ((px - x1) * dx + (py - y1) * dy) / norm
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                ex, ey = x1 + t * dx - px, y1 + t * dy - py
                d = ex * ex + ey * ey
            if d > far_d:
                far_i, far_d = i, d
        if far_d > tol * tol:
            keep[far_i] = True
            stack.append((first, far_i))
            stack.append((far_i, last))
    return [p for p, k in zip(points, keep) if k]


def _simplify_coords(coords, tol: float, depth: int = 0):
    """Walk a GeoJSON coordinate array and thin every ring or line inside it."""
    if not coords or isinstance(coords[0], (int, float)):
        return coords
    if isinstance(coords[0], list) and coords[0] and \
            isinstance(coords[0][0], (int, float)):
        closed = len(coords) > 3 and coords[0] == coords[-1]
        out = _dp_simplify(coords, tol)
        # A ring thinned below 4 points is no longer a polygon; keep the
        # original rather than emit invalid geometry.
        if closed and len(out) < 4:
            return coords
        if closed and out[0] != out[-1]:
            out.append(out[0])
        return out
    return [_simplify_coords(c, tol, depth + 1) for c in coords]


def simplify_feature_geometry(geojson: dict, tol: float) -> dict:
    """Thin every LineString / Polygon in a collection; points pass through."""
    if not tol:
        return geojson
    for f in geojson.get("features", []):
        geom = f.get("geometry") or {}
        gtype = geom.get("type", "")
        if gtype in ("Point", "MultiPoint") or not geom.get("coordinates"):
            continue
        geom["coordinates"] = _simplify_coords(geom["coordinates"], tol)
    return geojson


def simplify_geojson(geojson: dict, keep_tags: list | None = None) -> dict:
    features = []
    for f in geojson.get("features", []):
        props = f.get("properties", {})
        tags = props.get("tags", {})
        flat: dict = {}
        if keep_tags:
            for k in keep_tags:
                val = tags.get(k) or props.get(k)
                if val:
                    flat[k] = val
        else:
            flat = dict(tags)
        geom = f.get("geometry")
        if not geom:
            continue
        if geom.get("coordinates"):
            geom = {**geom, "coordinates": _round_coords(geom["coordinates"])}
        features.append({"type": "Feature", "properties": flat, "geometry": geom})
    return {"type": "FeatureCollection", "features": features}


def fc(features: list) -> dict:
    return {"type": "FeatureCollection", "features": features}


EMPTY_FC = fc([])


def dedupe_by_geometry(collections: list) -> dict:
    """Merge feature collections, dropping features with identical geometry."""
    seen, merged = set(), []
    for coll in collections:
        for f in coll.get("features", []):
            key = json.dumps(f.get("geometry", {}).get("coordinates", []))
            if key not in seen:
                seen.add(key)
                merged.append(f)
    return fc(merged)


# ─── Overpass API ─────────────────────────────────────────────────

def _overpass(query: str, cache: dict, key: str) -> dict:
    if key in cache:
        log.info("    [cached] %s", key)
        return cache[key]
    if OFFLINE:
        return {"elements": []}
    log.info("    Querying Overpass: %s", key)
    rounds = 3
    for attempt in range(rounds * len(OVERPASS_MIRRORS)):
        url = OVERPASS_MIRRORS[attempt % len(OVERPASS_MIRRORS)]
        host = url.split("/")[2]
        last = attempt == rounds * len(OVERPASS_MIRRORS) - 1
        try:
            r = requests.post(url, data={"data": query},
                              headers=HEADERS, timeout=300)
            if r.status_code in (429, 504):
                log.warning("    %s busy (%d) — trying next mirror",
                            host, r.status_code)
                # Only pause once a full pass over the mirrors has failed.
                if not last and (attempt + 1) % len(OVERPASS_MIRRORS) == 0:
                    wait = 30 * ((attempt + 1) // len(OVERPASS_MIRRORS))
                    log.warning("    all mirrors busy — waiting %d s", wait)
                    time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            # Overpass reports a server-side timeout or runtime error as HTTP
            # 200 with an empty element list and a `remark`. Caching that would
            # persist a failure as though the area were genuinely empty.
            remark = data.get("remark", "")
            if remark and not data.get("elements"):
                log.warning("    %s returned a remark, not data: %s",
                            host, remark[:120])
                continue
            cache[key] = data
            return data
        except Exception as exc:
            log.warning("    %s failed: %s", host, exc)
            if not last and (attempt + 1) % len(OVERPASS_MIRRORS) == 0:
                time.sleep(20)
    log.error("    Giving up on Overpass query %s — layer will be empty", key)
    return {"elements": []}


def _osm_geojson(query: str, cache: dict, key: str,
                 keep: list | None = None, *, tol: float = 0.0,
                 require_tags: bool = True) -> dict:
    """Overpass query to trimmed GeoJSON.

    Overpass answers `out body; >; out skel qt;` with the child nodes and ways
    of every match so geometry can be reconstructed. osm2geojson turns those
    into features too, which would otherwise pad a layer with hundreds of
    untagged points — 554 of the 591 "amusements" on a first pass. Any feature
    that kept none of `keep` carries no information for this layer's popup, so
    drop it.
    """
    raw = _overpass(query, cache, key)
    try:
        gj = osm2geojson.json2geojson(raw, log_level="ERROR")
    except Exception as exc:
        log.warning("    osm2geojson failed for %s: %s", key, exc)
        gj = EMPTY_FC
    gj = simplify_geojson(gj, keep)
    if require_tags and keep:
        before = len(gj["features"])
        gj["features"] = [f for f in gj["features"] if f["properties"]]
        dropped = before - len(gj["features"])
        if dropped:
            log.info("    %s: dropped %d untagged member geometries",
                     key, dropped)
    return simplify_feature_geometry(gj, tol)


# ─── ArcGIS REST helper ───────────────────────────────────────────

def _arcgis_geojson(layer_url: str, bbox: tuple, cache: dict, cache_key: str,
                    *, out_fields: str = "*", where: str = "1=1",
                    keep: dict | None = None, page: int = 1000,
                    max_features: int = 12000, precision: int = 5,
                    tol=DEFAULT_TOL) -> dict:
    """Page an ArcGIS Map/FeatureServer layer as GeoJSON inside a bbox.

    `keep` maps source attribute names to output property names; when given,
    every other attribute is dropped so the embedded payload stays small.
    `tol` is passed to the service as `maxAllowableOffset` so the generalizing
    happens server-side where possible, and is then applied again client-side.
    Support is uneven — NJDEP honours it at a coarser scale than documented and
    hosted ArcGIS Online services ignore it outright — so the second pass is
    what actually guarantees the vertex budget.
    """
    if tol is DEFAULT_TOL:
        tol = SIMPLIFY_DEG
    # The tolerance is baked into the cache key: change it and the layer
    # refetches rather than silently serving geometry at the old resolution.
    if tol:
        cache_key = f"{cache_key}_g{tol:g}"
    if cache_key in cache:
        log.info("    [cached] %s", cache_key)
        return cache[cache_key]
    if OFFLINE:
        return EMPTY_FC

    features: list = []
    offset = 0
    while offset < max_features:
        params = {
            "where": where,
            "geometry": bbox_esri(bbox),
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": out_fields,
            "returnGeometry": "true",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": page,
        }
        if tol:
            params["maxAllowableOffset"] = tol
        try:
            r = requests.get(f"{layer_url}/query", params=params,
                             headers=HEADERS, timeout=180)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.warning("    ArcGIS query failed (%s offset=%d): %s",
                        cache_key, offset, exc)
            break
        if isinstance(data, dict) and data.get("error"):
            log.warning("    ArcGIS error (%s): %s", cache_key,
                        data["error"].get("message"))
            break
        batch = data.get("features") or []
        for f in batch:
            geom = f.get("geometry")
            if not geom or not geom.get("coordinates"):
                continue
            src = f.get("properties") or {}
            if keep:
                props = {}
                for src_key, out_key in keep.items():
                    val = src.get(src_key)
                    if val not in (None, "", " "):
                        props[out_key] = val
            else:
                props = {k: v for k, v in src.items() if v not in (None, "")}
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": {**geom,
                             "coordinates": _round_coords(geom["coordinates"],
                                                          precision)},
            })
        log.info("    %s  offset=%d  +%d (total %d)",
                 cache_key, offset, len(batch), len(features))
        if len(batch) < page:
            break
        offset += len(batch)
        time.sleep(0.2)

    if len(features) >= max_features:
        log.warning("    %s hit the %d-feature cap — results truncated",
                    cache_key, max_features)

    gj = simplify_feature_geometry(fc(features), tol)
    cache[cache_key] = gj
    return gj


# ══════════════════════════════════════════════════════════════════
#  ISLAND & SHORE
# ══════════════════════════════════════════════════════════════════

def fetch_beaches(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:180];\n(\n"
        f'  way["natural"="beach"]({bb});\n'
        f'  node["natural"="beach"]({bb});\n'
        f'  relation["natural"="beach"]({bb});\n'
        f'  way["leisure"="beach_resort"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "beaches", [
        "natural", "leisure", "name", "surface", "access", "vehicles", "dog",
        "operator", "fee", "lifeguard", "wheelchair", "website",
    ], tol=SIMPLIFY_DEG_FINE)


def fetch_public_access(bbox, cache):
    """NJDEP public shore-access points: badge requirements, restrooms, parking."""
    return _arcgis_geojson(
        f"{NJDEP_ENVADMIN}/7", bbox, cache, "nj_public_access",
        keep={
            "ID_SIGN": "name", "STREET": "street", "CROSS_ST": "cross",
            "MUN_LABEL": "muni", "COUNTY_LAB": "county",
            "ACCESS_TYP": "accessType", "BADGE": "badge",
            "PARKING": "parking", "SWIMMING": "swimming",
            "SURFING": "surfing", "FISHING": "fishing", "PIER": "pier",
            "BOATLNCH": "boatLaunch", "MARINA": "marina",
            "PLAYGRD": "playground", "RESTRM": "restroom",
            "FOOD_DRINK": "food", "H_C": "accessible",
            "SHORELINE": "shoreline", "COMMENTS": "notes",
        },
        max_features=4000, tol=0,
    )


def fetch_boat_access(bbox, cache):
    """NJDEP saltwater fishing access sites merged with OSM slipways/marinas."""
    nj = _arcgis_geojson(
        f"{NJDEP_ENVADMIN}/31", bbox, cache, "nj_saltwater_access",
        keep={"SITE_NAME": "name", "SITE_ADDRESS": "street",
              "SITE_CITY": "muni", "COUNTY": "county",
              "SHORE_MODE": "shoreMode"},
        max_features=2000, tol=0,
    )
    for f in nj.get("features", []):
        f["properties"]["_source"] = "njdep"

    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:180];\n(\n"
        f'  nwr["leisure"="slipway"]({bb});\n'
        f'  nwr["leisure"="marina"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    osm = _osm_geojson(q, cache, "boat_access_osm",
                       ["leisure", "name", "operator", "fee", "website"])
    for f in osm.get("features", []):
        f["properties"]["_source"] = "osm"
    return fc(nj.get("features", []) + osm.get("features", []))


def fetch_lighthouses(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:120];\n(\n"
        f'  node["man_made"="lighthouse"]({bb});\n'
        f'  way["man_made"="lighthouse"]({bb});\n'
        f'  node["seamark:type"="light_major"]({bb});\n'
        f'  node["man_made"="beacon"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "lighthouses", [
        "man_made", "seamark:type", "name", "start_date", "operator",
        "website", "height",
        "seamark:light:character", "seamark:light:range", "wikipedia",
    ])


# ══════════════════════════════════════════════════════════════════
#  TREATS & AMUSEMENTS
# ══════════════════════════════════════════════════════════════════

def fetch_ice_cream(bbox, cache):
    """Ice cream stands, frozen custard, water ice and gelato."""
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:180];\n(\n"
        f'  nwr["amenity"="ice_cream"]({bb});\n'
        f'  nwr["shop"="ice_cream"]({bb});\n'
        f'  nwr["cuisine"~"ice_cream|gelato|frozen_yogurt"]({bb});\n'
        f'  nwr["shop"="frozen_yogurt"]({bb});\n'
        f");\nout center body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "ice_cream", [
        "name", "brand", "cuisine", "opening_hours", "website", "phone",
        "outdoor_seating", "takeaway", "addr:street", "addr:city",
        "amenity", "shop",
    ])


def fetch_mini_golf(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:180];\n(\n"
        f'  nwr["leisure"="miniature_golf"]({bb});\n'
        f'  nwr["sport"="miniature_golf"]({bb});\n'
        f'  nwr["golf"="miniature"]({bb});\n'
        f");\nout center body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "mini_golf", [
        "leisure", "sport", "golf", "name", "holes", "opening_hours",
        "website", "phone", "fee", "operator", "addr:street", "addr:city",
        "lit",
    ])


def fetch_amusements(bbox, cache):
    """Arcades, water parks, amusement rides — the rest of the boardwalk stack."""
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:180];\n(\n"
        f'  nwr["leisure"="amusement_arcade"]({bb});\n'
        f'  nwr["leisure"="water_park"]({bb});\n'
        f'  nwr["tourism"="theme_park"]({bb});\n'
        f'  nwr["attraction"~"^(amusement_ride|carousel|water_slide|maze|'
        f'roller_coaster|bumper_car|big_wheel)$"]({bb});\n'
        f'  nwr["leisure"="bowling_alley"]({bb});\n'
        f");\nout center body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "amusements", [
        "name", "leisure", "tourism", "attraction", "opening_hours",
        "website", "fee", "operator", "addr:city",
    ])


# ══════════════════════════════════════════════════════════════════
#  TRAILS & ROUTES
# ══════════════════════════════════════════════════════════════════

# Named trail systems and conservation lands worth sweeping specifically —
# OSM's generic path tagging misses a lot of these.
NJ_PARK_AREA_REGEX = (
    "Edwin B\\\\.? Forsythe National Wildlife Refuge"
    "|Brigantine Division"
    "|Barnegat Division"
    "|Holgate"
    "|Barnegat Lighthouse State Park"
    "|Island Beach State Park"
    "|Bass River State Forest"
    "|Wharton State Forest"
    "|Brendan T\\\\.? Byrne State Forest"
    "|Lebanon State Forest"
    "|Penn State Forest"
    "|Stafford Forge"
    "|Manahawkin Wildlife Management Area"
    "|Great Bay Boulevard"
    "|Swan Bay"
    "|Port Republic"
    "|Absecon"
    "|Cattus Island"
    "|Double Trouble State Park"
    "|Colliers Mills"
    "|Greenwood Forest"
    "|Pasadena"
    "|Whiting Wildlife Management Area"
    "|Batona"
    "|Wells Mills"
    "|Jakes Branch"
    "|Cloverdale Farm"
    "|Tuckerton Seaport"
    "|Jacques Cousteau"
)


def fetch_hiking(bbox, cache):
    bb = bbox_ql(bbox)
    broad = _osm_geojson(
        f"[out:json][timeout:300];\n(\n"
        f'  relation["route"~"^(hiking|foot|walking)$"]({bb});\n'
        f'  way["highway"~"^(path|footway)$"]["name"]({bb});\n'
        f'  way["highway"="track"]["sac_scale"]({bb});\n'
        f'  way["highway"="footway"]["footway"="boardwalk"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;",
        cache, "hiking_broad",
        ["name", "route", "highway", "sac_scale", "surface", "footway"])

    park = _osm_geojson(
        f"[out:json][timeout:300];\n"
        f'area["name"~"{NJ_PARK_AREA_REGEX}",i]->.parks;\n(\n'
        f'  way["highway"~"^(path|footway|track|bridleway)$"](area.parks)({bb});\n'
        f'  relation["route"~"^(hiking|foot|walking)$"](area.parks)({bb});\n'
        f");\nout body;\n>;\nout skel qt;",
        cache, "hiking_parks",
        ["name", "route", "highway", "sac_scale", "surface"])

    named = _osm_geojson(
        f"[out:json][timeout:180];\n(\n"
        f'  way["highway"~"^(path|footway|track|bridleway)$"]'
        f'["name"~"Batona|Forsythe|Wildlife Drive|Akers|Barnegat|Holgate|'
        f"Wells Mills|Jakes Branch|Cattus|Double Trouble|Bass River|"
        f'Wharton|Penn Swamp|Absegami|Lake Nescochague|Mullica",i]({bb});\n'
        f'  relation["name"~"Batona Trail|East Coast Greenway",i]({bb});\n'
        f");\nout body;\n>;\nout skel qt;",
        cache, "hiking_named",
        ["name", "route", "highway", "sac_scale", "surface"])

    return simplify_feature_geometry(
        dedupe_by_geometry([broad, park, named]), SIMPLIFY_DEG_FINE)


def fetch_bike(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:180];\n(\n"
        f'  relation["route"="bicycle"]({bb});\n'
        f'  way["highway"="cycleway"]({bb});\n'
        f'  way["cycleway"~"^(lane|track|opposite_lane|shared_lane)$"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "bike",
                        ["highway", "route", "cycleway", "name", "surface",
                         "network", "ref"], tol=SIMPLIFY_DEG_FINE)


def fetch_nj_trails(bbox, cache):
    return _arcgis_geojson(
        f"{NJDEP_LANDLU}/121", bbox, cache, "nj_statewide_trails",
        keep={"TRAIL_NAME_LONG": "name", "TRAIL_NAME_SEGMENT": "segment",
              "PARK_NAME": "park", "BLAZE_COLOR": "blaze",
              "SURFACE": "surface", "TRAIL_DIFFICULTY": "difficulty",
              "GIS_SEGMENT_LENGTH_MI": "miles",
              "MANAGING_AGENCY": "operator", "PARK_WEBSITE": "website",
              "HIKING": "hiking", "BIKING": "biking",
              "EQUESTRIAN": "equestrian", "WATER_TRAIL": "waterTrail",
              "ADA_COMPLIANT": "ada", "COUNTY": "county"},
        max_features=4000, tol=SIMPLIFY_DEG_FINE,
    )


def fetch_park_trails(bbox, cache):
    return _arcgis_geojson(
        f"{NJDEP_LAND}/63", bbox, cache, "nj_park_trails",
        keep={"TRAIL_NAME": "name", "PARK_NAME": "park",
              "SITE_NAME": "site", "TRL_TYPE_S": "type",
              "TRL_SRFACE": "surface", "TRL_LENGTH": "length",
              "TRL_COLOR": "blaze", "BLAZE_DESC": "blazeDesc",
              "TRL_DIFF": "difficulty", "HIKING_TRL": "hiking",
              "BIKING_TRL": "biking", "PARK_WEBSITE": "website"},
        max_features=3000, tol=SIMPLIFY_DEG_FINE,
    )


# ══════════════════════════════════════════════════════════════════
#  HISTORIC
# ══════════════════════════════════════════════════════════════════

def fetch_historic_osm(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:180];\n(\n"
        f'  nwr["historic"~"^(monument|memorial|building|house|fort|ruins|'
        f'castle|manor|farm|church|archaeological_site|battlefield|heritage|'
        f'wreck|ship|railway_station|lighthouse|tower|mine|bridge)$"]({bb});\n'
        f'  nwr["heritage"]({bb});\n'
        f'  node["tourism"="museum"]({bb});\n'
        f'  nwr["building"="chapel"]["start_date"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "historic_osm", [
        "name", "historic", "tourism", "heritage", "heritage:operator",
        "start_date", "building", "description", "wikipedia", "wikidata",
        "website", "operator", "inscription", "ref:nrhp",
    ], tol=SIMPLIFY_DEG_FINE)


def _nrhp_query(layer_id: int, bbox: tuple, cache: dict, cache_key: str) -> list:
    """Query one NPS National Register ArcGIS layer, paging until exhausted."""
    if cache_key in cache:
        log.info("    [cached] %s", cache_key)
        return cache[cache_key]
    if OFFLINE:
        return []

    url = f"{NPS_NRHP_API}/{layer_id}/query"
    fields = ("RESNAME,ResType,Address,City,County,State,CertDate,Is_NHL,"
              "NRIS_Refnum,NARA_URL")
    offset, all_features = 0, []
    while True:
        params = {
            "where": "1=1",
            "geometry": bbox_esri(bbox),
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": fields,
            "returnGeometry": "true",
            "f": "geojson",
            "resultOffset": offset,
            "resultRecordCount": 2000,
        }
        log.info("    NPS NRHP layer %d  offset=%d ...", layer_id, offset)
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=120)
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            log.warning("    NPS query failed: %s", exc)
            break
        batch = data.get("features") or []
        all_features.extend(batch)
        if len(batch) < 2000:
            break
        offset += len(batch)

    cache[cache_key] = all_features
    return all_features


def fetch_nrhp(bbox, cache):
    """National Register points (layer 0) and historic-district polygons (1)."""
    features = []
    for f in (_nrhp_query(0, bbox, cache, "nrhp_points")
              + _nrhp_query(1, bbox, cache, "nrhp_polygons")):
        geom = f.get("geometry")
        if not geom or not geom.get("coordinates"):
            continue
        p = f.get("properties") or {}
        slim = {
            "name": p.get("RESNAME") or "Unknown",
            "type": p.get("ResType") or "",
            "address": p.get("Address") or "",
            "city": p.get("City") or "",
            "county": p.get("County") or "",
            "state": p.get("State") or "",
            "listed": p.get("CertDate") or "",
            "refnum": p.get("NRIS_Refnum") or "",
        }
        if p.get("Is_NHL"):
            slim["nhl"] = True
        if p.get("NARA_URL"):
            slim["nara"] = p["NARA_URL"]
        features.append({
            "type": "Feature",
            "properties": {k: v for k, v in slim.items() if v != ""},
            "geometry": {**geom,
                         "coordinates": _round_coords(geom["coordinates"])},
        })
    return fc(features)


def fetch_heritage(bbox, cache):
    """OSM historic structures merged with the NPS National Register."""
    osm = fetch_historic_osm(bbox, cache)
    nrhp = fetch_nrhp(bbox, cache)
    for f in osm.get("features", []):
        f["properties"]["_source"] = "osm"
    for f in nrhp.get("features", []):
        f["properties"]["_source"] = "nrhp"
    return fc(osm.get("features", []) + nrhp.get("features", []))


# NJ HPO tracks ~38k survey records region-wide. Restrict to individually
# designated resources — the buildings themselves rather than every parcel
# swept up inside a historic district.
NJ_HISTORIC_STATUS = (
    "STATUS IN ('LISTED_INDV','NHL_INDV','ELIGIBLE_INDV','LOCAL_LANDMARK',"
    "'LOCALLY_DESIGNATED_HD','DELISTED_INDV')"
)

NJ_HPO_FIELDS = {
    "NAME": "name", "ALT_NAME": "altName", "ADDRESS": "address",
    "STATUS": "status", "NRDATE": "nrDate", "SRDATE": "srDate",
    "NHL": "nhl", "NHLDATE": "nhlDate", "DOEDATE": "doeDate",
    "LOCALDATE": "localDate", "POS_BEG": "periodBegin",
    "POS_END": "periodEnd", "NR_CRIT": "criteria",
    "DEMOLISHED": "demolished", "NOTES": "notes",
}


def fetch_nj_historic(bbox, cache):
    """NJ Historic Preservation Office individually designated properties."""
    return _arcgis_geojson(
        f"{NJDEP_LAND}/55", bbox, cache, "nj_historic_props",
        where=NJ_HISTORIC_STATUS, keep=NJ_HPO_FIELDS,
        max_features=6000, precision=6, tol=SIMPLIFY_DEG_FINE,
    )


def fetch_nj_historic_districts(bbox, cache):
    return _arcgis_geojson(
        f"{NJDEP_LAND}/57", bbox, cache, "nj_historic_districts",
        keep=NJ_HPO_FIELDS, max_features=2000,
    )


# ─── Historic roads ───────────────────────────────────────────────

# The King's Highway system (chartered 1650s-1700s) plus the colonial stage
# and shore roads that carried traffic to the Barnegat / Little Egg Harbor
# coast before the 1914 causeway. Matched on OSM `name`.
KINGS_ROAD_REGEX = (
    r"King'?s\s+(High\s?way|Hwy|Road|Rd)"
    r"|Old\s+King'?s"
    r"|Old\s+Shore\s+Road"
    r"|Shore\s+Road"
    r"|Old\s+New\s+York\s+Road"
    r"|Stage\s+Road"
    r"|Tuckerton\s+Stage"
    r"|Half\s*Way\s+Road"
    r"|Cedar\s+Bridge\s+Road"
    r"|Quaker\s+Bridge"
    r"|Long.?a.?Coming"
    r"|Old\s+Cape\s+May"
    r"|Blue\s+Anchor\s+Road"
    r"|Egg\s+Harbor\s+Road"
    r"|Old\s+Egg\s+Harbor"
    r"|Mays\s+Landing\s+Road"
    r"|Weymouth\s+Road"
    r"|Old\s+Indian\s+Mills"
    r"|Indian\s+Mills\s+Road"
    r"|Sooy\s+Place"
    r"|Speedwell"
    r"|Friendship\s+Road"
    r"|Bishops\s+Bridge"
    r"|Retreat\s+Road"
    r"|Old\s+Mill\s+Road"
    r"|Old\s+Post\s+Road"
    r"|Post\s+Road"
    r"|Old\s+Stage"
    r"|Colonial\s+Road"
    r"|Old\s+Half\s*Way"
    r"|Carranza\s+Road"
    r"|Batsto"
    r"|Pleasant\s+Mills"
    r"|Chatsworth\s+Road"
    r"|Barnegat\s+Road"
    r"|Old\s+Barnegat"
    r"|Manahawkin\s+Road"
    r"|Lacey\s+Road"
    r"|Bay\s+Shore\s+Road"
)

# Pre-1927 alignments and the named turnpikes / plank roads that preceded the
# numbered state highway system.
ORIG_HIGHWAY_NAME_REGEX = (
    r"White\s+Horse\s+Pike"
    r"|Black\s+Horse\s+Pike"
    r"|Marlton\s+Pike"
    r"|Berlin.?Cross\s+Keys"
    r"|Turnpike"
    r"|Plank\s+Road"
    r"|Old\s+Route"
    r"|Old\s+Highway"
    r"|Old\s+US"
    r"|Old\s+Trail"
    r"|Lakehurst\s+Road"
    r"|Central\s+Avenue"
    r"|Long\s+Beach\s+Boulevard"
    r"|Ocean\s+Boulevard"
    r"|Bay\s+Avenue"
)

# Routes whose current alignment is the historic through-route for this coast:
# US 9 is the old shore King's Highway, NJ 72 the Manahawkin causeway road,
# NJ 166 the pre-1953 US 9, CR 539/563/542/532 the Pine Barrens stage roads.
ORIG_HIGHWAY_REF_REGEX = (
    r"^(US\s?9|NJ\s?9|NJ\s?72|NJ\s?166|NJ\s?70|US\s?30|US\s?40|US\s?322"
    r"|NJ\s?47|NJ\s?49|NJ\s?50|NJ\s?52|NJ\s?54|CR\s?539|CR\s?563|CR\s?542"
    r"|CR\s?532|CR\s?curve|CR\s?624|CR\s?614|CR\s?607)$"
)

HIST_ROAD_TAGS = [
    "name", "old_name", "old_ref", "ref", "highway", "historic",
    "surface", "description", "wikipedia", "wikidata", "note",
    "abandoned:highway", "was:highway", "network",
]


def fetch_kings_roads(bbox, cache):
    """Colonial King's Highway / stage-road alignments still on the ground."""
    bb = bbox_ql(bbox)
    named = _osm_geojson(
        f"[out:json][timeout:300];\n(\n"
        f'  way["highway"]["name"~"{KINGS_ROAD_REGEX}",i]({bb});\n'
        f'  way["name"~"{KINGS_ROAD_REGEX}",i]["highway"!~"."]'
        f'["route"!~"."]({bb});\n'
        f'  relation["name"~"{KINGS_ROAD_REGEX}",i]["type"="route"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;",
        cache, "kings_roads_named", HIST_ROAD_TAGS)

    tagged = _osm_geojson(
        f"[out:json][timeout:300];\n(\n"
        f'  way["historic"="road"]({bb});\n'
        f'  way["abandoned:highway"]({bb});\n'
        f'  way["was:highway"]({bb});\n'
        f'  way["highway"]["historic"~"^(highway|road|track|trail)$"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;",
        cache, "kings_roads_tagged", HIST_ROAD_TAGS)

    merged = simplify_feature_geometry(
        dedupe_by_geometry([named, tagged]), SIMPLIFY_DEG_FINE)
    for f in merged["features"]:
        p = f["properties"]
        p["_class"] = "kings" if re.search(
            r"king|stage|post|colonial|old", p.get("name", ""), re.I
        ) else "historic"
    return merged


def fetch_orig_highways(bbox, cache):
    """Original numbered-highway alignments, named pikes, and renamed roads."""
    bb = bbox_ql(bbox)
    pikes = _osm_geojson(
        f"[out:json][timeout:300];\n(\n"
        f'  way["highway"]["name"~"{ORIG_HIGHWAY_NAME_REGEX}",i]({bb});\n'
        f");\nout body;\n>;\nout skel qt;",
        cache, "orig_hwy_pikes", HIST_ROAD_TAGS)

    renamed = _osm_geojson(
        f"[out:json][timeout:300];\n(\n"
        f'  way["highway"]["old_ref"]({bb});\n'
        f'  way["highway"]["old_name"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;",
        cache, "orig_hwy_renamed", HIST_ROAD_TAGS)

    trunk = _osm_geojson(
        f"[out:json][timeout:300];\n(\n"
        f'  way["highway"~"^(trunk|primary|secondary)$"]'
        f'["ref"~"{ORIG_HIGHWAY_REF_REGEX}"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;",
        cache, "orig_hwy_trunk", HIST_ROAD_TAGS)

    merged = simplify_feature_geometry(
        dedupe_by_geometry([pikes, renamed, trunk]), SIMPLIFY_DEG_FINE)
    for f in merged["features"]:
        p = f["properties"]
        if p.get("old_ref") or p.get("old_name"):
            p["_class"] = "renamed"
        elif re.search(r"pike|turnpike|plank", p.get("name", ""), re.I):
            p["_class"] = "pike"
        else:
            p["_class"] = "route"
    return merged


def fetch_old_rail(bbox, cache):
    """Abandoned rail grades — the Tuckerton Railroad reached the bay in 1871
    and the Long Beach Railroad crossed to the island until the 1935 hurricane."""
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:300];\n(\n"
        f'  way["railway"~"^(abandoned|dismantled|razed|disused|historic)$"]({bb});\n'
        f'  way["abandoned:railway"]({bb});\n'
        f'  way["was:railway"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "old_rail", [
        "name", "railway", "abandoned:railway", "was:railway", "operator",
        "old_name", "start_date", "end_date", "description", "wikipedia",
    ], tol=SIMPLIFY_DEG_FINE)


def fetch_hist_shoreline(bbox, cache):
    """NJDEP historical shoreline traces — barrier-island migration over time."""
    return _arcgis_geojson(
        f"{NJDEP_CAFRA}/11", bbox, cache, "hist_shoreline",
        keep={"YEAR": "year", "SYMBOL": "symbol", "LENGTH": "length"},
        max_features=4000, precision=6, tol=SIMPLIFY_DEG_FINE,
    )


# ══════════════════════════════════════════════════════════════════
#  PROTECTED LANDS
# ══════════════════════════════════════════════════════════════════

PADUS_FIELDS = {
    "Unit_Nm": "name", "Des_Tp": "designation", "Loc_Ds": "localDesignation",
    "Mang_Name": "manager", "Mang_Type": "managerType", "Own_Name": "owner",
    "Own_Type": "ownerType", "Pub_Access": "access", "GAP_Sts": "gap",
    "IUCN_Cat": "iucn", "GIS_Acres": "acres", "Date_Est": "established",
    "State_Nm": "state",
}


def fetch_federal_lands(bbox, cache):
    """PAD-US polygons managed by a federal agency (NPS, FWS, USFS, DOD, ...)."""
    return _arcgis_geojson(
        PADUS_MANAGER, bbox, cache, "padus_federal",
        where="Mang_Type = 'FED'", keep=PADUS_FIELDS,
        max_features=2000, precision=5, tol=SIMPLIFY_DEG_COARSE,
    )


def fetch_state_lands(bbox, cache):
    """State-owned open space from NJDEP, generalized boundaries."""
    return _arcgis_geojson(
        f"{NJDEP_LAND}/67", bbox, cache, "nj_state_lands",
        keep={"FEATURE_NAME": "name", "FEATURE_CLASS": "class",
              "OWNERSHIP": "ownership", "USE_DESIGNATION": "use",
              "OWNERSHIP_USE": "ownershipUse", "LAND_MANAGER": "manager",
              "LAND_MANAGER_AGENCY": "agency", "AGENCY_URL": "agencyUrl",
              "FACILITY_URL": "website",
              "FACILITY_TRAILMAP_URL": "trailMap",
              "PUBLIC_ACCESS": "access", "PARKING": "parking",
              "HUNTING_PERMITTED": "hunting", "MUNICIPALITY": "muni",
              "COUNTY": "county", "TELEPHONE": "phone"},
        max_features=1500,
    )


def fetch_natural_areas(bbox, cache):
    """NJ State Natural Areas — the strictest state protection class."""
    return _arcgis_geojson(
        f"{NJDEP_LAND}/80", bbox, cache, "nj_natural_areas",
        keep={"FEATURE_NAME": "name", "ACRES": "acres",
              "FACILITY_LABEL": "facility", "MUNICIPALITY": "muni",
              "COUNTY": "county"},
        max_features=500,
    )


def fetch_nhp_sites(bbox, cache):
    """Natural Heritage Priority Sites — rare-species and community hotspots."""
    return _arcgis_geojson(
        f"{NJDEP_HABITAT}/93", bbox, cache, "nj_nhp_sites", tol=SIMPLIFY_DEG_COARSE,
        keep={"SITENAME": "name", "SITECODE": "code", "COUNTY": "county",
              "MUNICIPALI": "muni", "DESCRIPTIO": "description",
              "BIODIVRANK": "biodivRank", "SITECLASS": "siteClass",
              "BIODIVCOMM": "biodivComment", "QUADNAME": "quad"},
        max_features=600,
    )


# NJDEP splits Conservation Focal Areas into one layer per landscape region.
FOCAL_AREA_LAYERS = {
    100: "Marine",
    101: "Delaware Bay",
    102: "Coastal",
    103: "Pinelands",
    104: "Piedmont",
    105: "Skylands",
}


def fetch_focal_areas(bbox, cache):
    """Conservation Focal Areas across every NJDEP landscape region."""
    features = []
    for layer_id, region in FOCAL_AREA_LAYERS.items():
        gj = _arcgis_geojson(
            f"{NJDEP_HABITAT}/{layer_id}", bbox, cache,
            f"nj_focal_{layer_id}", tol=SIMPLIFY_DEG_COARSE,
            keep={"CFA_NAME": "name", "REGION": "region", "ACRES": "acres",
                  "DESCRIPTION": "description", "CFA_ID": "code"},
            max_features=400,
        )
        for f in gj.get("features", []):
            f["properties"].setdefault("region", region)
            features.append(f)
    return fc(features)


def fetch_refuges_fws(bbox, cache):
    """USFWS National Wildlife Refuge System boundaries."""
    return _arcgis_geojson(
        FWS_NWRS, bbox, cache, "fws_refuges", tol=SIMPLIFY_DEG_COARSE,
        keep={"ORGNAME": "name", "RSL_TYPE": "type", "LIT": "unit",
              "FWSREGION": "region", "CostCenter": "costCenter"},
        max_features=200, precision=5,
    )


def fetch_fws_wilderness(bbox, cache):
    """Congressionally designated wilderness inside FWS refuges."""
    return _arcgis_geojson(
        FWS_WILDERNESS, bbox, cache, "fws_wilderness", tol=SIMPLIFY_DEG_COARSE,
        keep={"WILDNAME": "name", "ORGNAME": "refuge", "ACRES": "acres",
              "YEARDESIG": "designated", "PUBLICLAW": "publicLaw"},
        max_features=200,
    )


# ─── OSM protected-area fallbacks (parity with the Gulf Islands build) ──

def fetch_state_parks(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:180];\n(\n"
        f'  nwr["boundary"="protected_area"]["protection_title"~"State Park",i]({bb});\n'
        f'  nwr["leisure"="park"]["name"~"State Park",i]({bb});\n'
        f'  nwr["boundary"="national_park"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "state_parks",
                        ["boundary", "leisure", "name", "protection_title",
                         "operator", "website"],
                        tol=SIMPLIFY_DEG)


def fetch_refuges(bbox, cache):
    """OSM IUCN-classed protected areas — catches locally held preserves that
    are absent from PAD-US and the NJDEP open-space inventory."""
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:300];\n(\n"
        f'  nwr["boundary"="protected_area"]["protect_class"~"^(1|1a|1b|2|3|4|5|6)$"]({bb});\n'
        f'  nwr["boundary"="protected_area"]["protection_title"~'
        f'"Wildlife Refuge|Wildlife Management|Preserve|Conservation|NWR",i]({bb});\n'
        f'  nwr["leisure"="nature_reserve"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "refuges_osm", [
        "boundary", "name", "protect_class", "protection_title", "operator",
        "designation", "ownership", "website", "wikipedia", "opening_hours",
        "leisure",
    ], tol=SIMPLIFY_DEG_COARSE)


def fetch_forests(bbox, cache):
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:180];\n(\n"
        f'  nwr["boundary"="protected_area"]["protection_title"~'
        f'"State Forest|National Forest",i]({bb});\n'
        f'  nwr["landuse"="forest"]["name"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "forests",
                        ["boundary", "landuse", "name", "protection_title",
                         "operator", "leaf_type"],
                        tol=SIMPLIFY_DEG)


# ══════════════════════════════════════════════════════════════════
#  PINE BARRENS
# ══════════════════════════════════════════════════════════════════

def fetch_pnr(bbox, cache):
    """Pinelands National Reserve — 1.1 M acres, the first US National Reserve
    (1978). Falls back to the OSM boundary relation if the Commission's
    service is unavailable."""
    gj = _arcgis_geojson(
        f"{PINELANDS}/Pinelands_National_Reserve/FeatureServer/0",
        bbox, cache, "pinelands_nr", keep={}, max_features=50, precision=5,
        tol=SIMPLIFY_DEG_COARSE,
    )
    for f in gj.get("features", []):
        f["properties"].update({
            "name": "Pinelands National Reserve",
            "designation": "National Reserve (est. 1978)",
            "operator": "New Jersey Pinelands Commission",
            "website": "https://www.nj.gov/pinelands/",
        })
    if gj.get("features"):
        return gj

    log.warning("    Pinelands Commission service empty — trying OSM boundary")
    bb = bbox_ql(bbox)
    return _osm_geojson(
        f"[out:json][timeout:300];\n(\n"
        f'  relation["name"~"Pinelands National Reserve",i]({bb});\n'
        f");\nout body;\n>;\nout skel qt;",
        cache, "pinelands_nr_osm",
        ["name", "boundary", "protect_class", "protection_title", "operator",
         "website", "wikipedia"])


def fetch_pinelands_mgmt(bbox, cache):
    """Pinelands management areas — Preservation Area District, Forest Area,
    Agricultural Production Area, Regional Growth Area and the rest."""
    return _arcgis_geojson(
        f"{PINELANDS}/Pinelands_ManagementAreas/FeatureServer/0",
        bbox, cache, "pinelands_mgmt", tol=SIMPLIFY_DEG_COARSE,
        keep={"MGT_NAME": "name", "MGT_CODE": "code", "NAME": "altName",
              "ACRES": "acres"},
        max_features=800, precision=5,
    )


# ══════════════════════════════════════════════════════════════════
#  SIGNIFICANT HABITAT
# ══════════════════════════════════════════════════════════════════

# Landscape Project rank, from the NJDEP methodology. Rank is assigned by the
# highest-status species documented using the patch.
LANDSCAPE_RANK = {
    5: "Rank 5 — habitat for a federally listed species",
    4: "Rank 4 — habitat for a State endangered species",
    3: "Rank 3 — habitat for a State threatened species",
    2: "Rank 2 — habitat for a species of special concern",
    1: "Rank 1 — species occurrence area",
}

LANDSCAPE_FIELDS = {
    "LNDR": "rank", "LABEL20": "landCover", "TYPE20": "coverType",
    "REGION": "region", "ACRES": "acres", "RIPARIAN": "riparian",
    "FOR_CORE": "forestCore", "VERSION": "version",
}


HABITAT_MIN_RANK = 4   # overridable with --habitat-rank


def fetch_sig_habitat(bbox, cache, *, min_rank: int | None = None):
    """Significant habitat from the NJDEP Landscape Project v3.4.

    The full dataset is enormous — 152k polygons for the Pinelands region alone
    across a regional bbox — so it is filtered to the ranks that carry
    regulatory weight (4: State endangered, 5: federally listed) and capped.
    On the island preset the result is complete; on the full regional preset it
    is truncated, and the build log says so.
    """
    if min_rank is None:
        min_rank = HABITAT_MIN_RANK
    features = []
    for region, url in LANDSCAPE_HABITAT:
        gj = _arcgis_geojson(
            url, bbox, cache,
            f"landscape_{region.lower().replace(' ', '_')}_r{min_rank}",
            where=f"LNDR >= {min_rank}", keep=LANDSCAPE_FIELDS,
            max_features=12000, tol=SIMPLIFY_DEG_COARSE,
        )
        for f in gj.get("features", []):
            f["properties"].setdefault("region", region)
            features.append(f)
    return fc(features)


def fetch_stream_habitat(bbox, cache):
    """Stream reaches documented as habitat for listed species."""
    return _arcgis_geojson(
        LANDSCAPE_STREAM, bbox, cache, "landscape_stream",
        keep={"LNDR": "rank", "GNIS_NAME": "name", "REGION": "region",
              "VERSION": "version"},
        max_features=6000, tol=SIMPLIFY_DEG_FINE,
    )


VERNAL_STATUS = {
    "C": "Confirmed vernal pool",
    "P": "Potential vernal pool",
    "D": "Documented, not field verified",
}


def fetch_vernal_pools(bbox, cache):
    """Vernal pools and their surrounding vernal habitat — a defining feature of
    Pine Barrens amphibian breeding."""
    pools = _arcgis_geojson(
        LANDSCAPE_VERNAL, bbox, cache, "landscape_vernal_pools",
        keep={"VP_ID": "code", "VP_STATUS": "status", "REGION": "region",
              "VERSION": "version"},
        max_features=8000, tol=0,
    )
    for f in pools.get("features", []):
        f["properties"]["_kind"] = "pool"

    habitat = _arcgis_geojson(
        LANDSCAPE_VERNAL_HAB, bbox, cache, "landscape_vernal_habitat",
        keep={"VPH_ID": "code", "LNDR": "rank", "REGION": "region",
              "ACRES": "acres"},
        max_features=4000, tol=SIMPLIFY_DEG,
    )
    for f in habitat.get("features", []):
        f["properties"]["_kind"] = "habitat"

    return fc(pools.get("features", []) + habitat.get("features", []))


def fetch_critical_habitat(bbox, cache):
    """Designated critical habitat under the Endangered Species Act — USFWS
    polygons plus the NMFS lines that carry Atlantic sturgeon (New York Bight
    distinct population segment) river habitat."""
    fws = _arcgis_geojson(
        FWS_CRITICAL_HABITAT, bbox, cache, "fws_critical_habitat",
        keep={"comname": "name", "sciname": "sciName", "status": "status",
              "listing_status": "listing", "unitname": "unit",
              "COMNAME": "name", "SCINAME": "sciName", "STATUS": "status",
              "UNIT": "unit", "LISTING_STATUS": "listing"},
        max_features=500, tol=SIMPLIFY_DEG_COARSE,
    )
    for f in fws.get("features", []):
        f["properties"]["_source"] = "fws"

    nmfs = _arcgis_geojson(
        NMFS_CRITICAL_HABITAT, bbox, cache, "nmfs_critical_habitat",
        keep={"COMNAME": "name", "SCIENAME": "sciName", "LISTENTITY": "entity",
              "LISTSTATUS": "status", "CHSTATUS": "chStatus", "UNIT": "unit",
              "HABTYPE": "habitatType", "EFFECTDATE": "effective",
              "INPORTURL": "url"},
        max_features=500, tol=SIMPLIFY_DEG_FINE,
    )
    for f in nmfs.get("features", []):
        f["properties"]["_source"] = "nmfs"

    return fc(fws.get("features", []) + nmfs.get("features", []))


# ══════════════════════════════════════════════════════════════════
#  MARINE & ESTUARINE
# ══════════════════════════════════════════════════════════════════

def fetch_mpa(bbox, cache):
    """NOAA Marine Protected Areas Inventory."""
    return _arcgis_geojson(
        NOAA_MPA, bbox, cache, "noaa_mpa", tol=SIMPLIFY_DEG_COARSE,
        keep={"Site_Name": "name", "Gov_Level": "govLevel",
              "Prot_Lvl": "protLevel", "Mgmt_Agen": "agency",
              "Fish_Rstr": "fishing", "Prot_Focus": "focus",
              "Cons_Focus": "consFocus", "Permanence": "permanence",
              "Constancy": "constancy", "Estab_Yr": "established",
              "IUCNcat": "iucn", "Design": "designation",
              "AreaKm": "areaKm2", "Anchor": "anchoring",
              "Vessel": "vessel", "URL": "url", "State": "state"},
        max_features=400, precision=5,
    )


def fetch_nerrs(bbox, cache):
    """National Estuarine Research Reserves. The Jacques Cousteau NERR covers
    the Mullica River / Great Bay estuary immediately south-west of LBI; it is
    carried in the NOAA MPA inventory, so pull it from there and fall back to
    OSM for anything else tagged as a reserve."""
    mpa = fetch_mpa(bbox, cache)
    features = []
    for f in mpa.get("features", []):
        name = (f["properties"].get("name") or "").lower()
        if "estuarine research reserve" in name or "nerr" in name.split():
            g = json.loads(json.dumps(f))
            g["properties"]["_source"] = "noaa"
            features.append(g)

    bb = bbox_ql(bbox)
    osm = _osm_geojson(
        f"[out:json][timeout:300];\n(\n"
        f'  nwr["boundary"="protected_area"]'
        f'["name"~"Estuarine Research Reserve|NERR",i]({bb});\n'
        f'  relation["operator"~"NOAA",i]["boundary"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;",
        cache, "nerrs_osm",
        ["boundary", "name", "protect_class", "protection_title", "operator",
         "website"])
    known = {(f["properties"].get("name") or "").lower() for f in features}
    for f in osm.get("features", []):
        if (f["properties"].get("name") or "").lower() not in known:
            f["properties"]["_source"] = "osm"
            features.append(f)
    return fc(features)


def fetch_shellfish(bbox, cache):
    """NJDEP shellfish-harvest water classification (Approved, Prohibited, ...)."""
    return _arcgis_geojson(
        f"{NJDEP_ENVADMIN}/2", bbox, cache, "nj_shellfish",
        keep={"STATUS": "status", "ACRES": "acres"},
        max_features=1500, precision=5, tol=SIMPLIFY_DEG_COARSE,
    )


def fetch_reefs(bbox, cache):
    """NJ artificial reef sites off the Barnegat / Little Egg Harbor coast."""
    return _arcgis_geojson(
        f"{NJDEP_ENVADMIN}/9", bbox, cache, "nj_reefs",
        keep={"REEF_NAME": "name", "REEF_URL": "url", "ID": "code"},
        max_features=200,
    )


def fetch_tide_stations(bbox, cache):
    """NOAA CO-OPS tide-prediction stations inside the bbox."""
    ck = "noaa_tide_stations"
    if ck in cache:
        log.info("    [cached] %s", ck)
        return cache[ck]
    if OFFLINE:
        return EMPTY_FC

    s, w, n, e = bbox
    features = []
    try:
        r = requests.get(NOAA_COOPS_STATIONS,
                         params={"type": "tidepredictions"},
                         headers=HEADERS, timeout=120)
        r.raise_for_status()
        for st in r.json().get("stations", []):
            lat, lng = st.get("lat"), st.get("lng")
            if lat is None or lng is None:
                continue
            if not (s <= lat <= n and w <= lng <= e):
                continue
            features.append({
                "type": "Feature",
                "properties": {
                    "name": st.get("name", ""),
                    "stationId": st.get("id", ""),
                    "state": st.get("state", ""),
                    "type": st.get("type", ""),
                },
                "geometry": {"type": "Point",
                             "coordinates": [round(lng, 5), round(lat, 5)]},
            })
    except Exception as exc:
        log.warning("    NOAA CO-OPS station list failed: %s", exc)

    gj = fc(features)
    cache[ck] = gj
    return gj


# ══════════════════════════════════════════════════════════════════
#  WETLANDS
# ══════════════════════════════════════════════════════════════════

def fetch_wetlands_osm(bbox, cache):
    """OSM wetland polygons — salt marsh, tidal flat, swamp, bog. Clickable,
    unlike the NWI and NJDEP rasters, and the back-bay marsh behind LBI is
    mapped in detail."""
    bb = bbox_ql(bbox)
    q = (
        f"[out:json][timeout:300];\n(\n"
        f'  way["natural"="wetland"]({bb});\n'
        f'  relation["natural"="wetland"]({bb});\n'
        f'  way["natural"="mud"]({bb});\n'
        f'  way["natural"="saltmarsh"]({bb});\n'
        f");\nout body;\n>;\nout skel qt;"
    )
    return _osm_geojson(q, cache, "wetlands_osm", [
        "name", "wetland", "natural", "tidal", "salt", "operator",
        "protect_class", "description",
    ], tol=SIMPLIFY_DEG_COARSE)


# ══════════════════════════════════════════════════════════════════
#  WILDLIFE
# ══════════════════════════════════════════════════════════════════

def fetch_inat_rare(bbox, cache):
    """Threatened / endangered observations from iNaturalist, research grade."""
    ck = "inat_rare"
    if ck in cache:
        log.info("    [cached] %s", ck)
        return cache[ck]
    if OFFLINE:
        return EMPTY_FC

    s, w, n, e = bbox
    features = []
    for page in range(1, 4):
        try:
            r = requests.get(
                f"{INAT_API_URL}/observations",
                params={"nelat": n, "nelng": e, "swlat": s, "swlng": w,
                        "threatened": "true", "quality_grade": "research",
                        "per_page": 200, "page": page,
                        "order": "desc", "order_by": "observed_on"},
                headers=HEADERS, timeout=120)
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as exc:
            log.warning("    iNat rare species page %d failed: %s", page, exc)
            break
        if not results:
            break
        for obs in results:
            taxon = obs.get("taxon") or {}
            loc = obs.get("location")
            if not loc or not taxon.get("name"):
                continue
            lat_s, lng_s = loc.split(",")
            cs = taxon.get("conservation_status") or {}
            features.append({
                "type": "Feature",
                "properties": {
                    "name": taxon.get("preferred_common_name") or taxon["name"],
                    "sciName": taxon["name"],
                    "rank": taxon.get("rank", ""),
                    "status": cs.get("status_name", ""),
                    "iucn": cs.get("iucn", ""),
                    "observedOn": obs.get("observed_on", ""),
                    "uri": obs.get("uri", ""),
                },
                "geometry": {"type": "Point",
                             "coordinates": [round(float(lng_s), 5),
                                             round(float(lat_s), 5)]},
            })
        if len(results) < 200:
            break
        time.sleep(1.0)

    gj = fc(features)
    cache[ck] = gj
    return gj


def fetch_hotspots(bbox, api_key, cache):
    if "hotspots" in cache:
        log.info("    [cached] hotspots")
        raw = cache["hotspots"]
    elif OFFLINE:
        raw = []
    else:
        pts = grid_points(bbox, step_km=45)
        seen, raw = set(), []
        log.info("    Querying eBird hotspots (%d grid pts)...", len(pts))
        for lat, lng in pts:
            try:
                r = requests.get(f"{EBIRD_API}/ref/hotspot/geo",
                                 params={"lat": lat, "lng": lng,
                                         "dist": 40, "fmt": "json"},
                                 headers={"X-eBirdApiToken": api_key, **HEADERS},
                                 timeout=45)
                r.raise_for_status()
                for h in r.json():
                    lid = h.get("locId", "")
                    if lid and lid not in seen:
                        seen.add(lid)
                        raw.append(h)
                time.sleep(0.3)
            except Exception as exc:
                log.warning("    Hotspot query at %.3f,%.3f: %s", lat, lng, exc)
        cache["hotspots"] = raw

    s, w, n, e = bbox
    features = []
    for h in raw:
        lat, lng = h.get("lat"), h.get("lng")
        if lat is None or lng is None or not (s <= lat <= n and w <= lng <= e):
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "name": h.get("locName", ""),
                "locId": h.get("locId", ""),
                "numSpecies": h.get("numSpeciesAllTime", 0),
                "latestObs": h.get("latestObsDt", ""),
            },
            "geometry": {"type": "Point",
                         "coordinates": [round(lng, 5), round(lat, 5)]},
        })
    return fc(features)


def fetch_ebird_obs(bbox, api_key, back, cache):
    ck = f"ebird_obs_{back}"
    if ck in cache:
        log.info("    [cached] ebird observations")
        raw = cache[ck]
    elif OFFLINE:
        raw = []
    else:
        pts = grid_points(bbox, step_km=45)
        seen, raw = set(), []
        log.info("    Querying eBird obs (%d grid pts, back=%d)...",
                 len(pts), back)
        for lat, lng in pts:
            try:
                r = requests.get(f"{EBIRD_API}/data/obs/geo/recent",
                                 params={"lat": lat, "lng": lng, "dist": 40,
                                         "back": back,
                                         "includeProvisional": "true",
                                         "maxResults": 10000},
                                 headers={"X-eBirdApiToken": api_key, **HEADERS},
                                 timeout=90)
                r.raise_for_status()
                for obs in r.json():
                    key = (obs.get("speciesCode", ""), obs.get("locId", ""))
                    if key not in seen:
                        seen.add(key)
                        raw.append(obs)
                time.sleep(0.5)
            except Exception as exc:
                log.warning("    Obs query at %.3f,%.3f: %s", lat, lng, exc)
        cache[ck] = raw

    s, w, n, e = bbox
    loc_agg: dict[str, dict] = {}
    for obs in raw:
        loc = obs.get("locId", "")
        lat, lng = obs.get("lat"), obs.get("lng")
        if not loc or lat is None or lng is None:
            continue
        if not (s <= lat <= n and w <= lng <= e):
            continue
        entry = loc_agg.setdefault(loc, {
            "locName": obs.get("locName", ""), "lat": lat, "lng": lng,
            "latestDate": obs.get("obsDt", ""), "species": {},
        })
        if obs.get("obsDt", "") > entry["latestDate"]:
            entry["latestDate"] = obs["obsDt"]
        sp = obs.get("comName", "Unknown")
        sp_key = obs.get("speciesCode", sp)
        prev = entry["species"].get(sp_key)
        if prev is None:
            entry["species"][sp_key] = {
                "species": sp, "sciName": obs.get("sciName", ""),
                "howMany": obs.get("howMany") or 1,
                "obsDt": obs.get("obsDt", ""), "subId": obs.get("subId", ""),
            }
        else:
            prev["howMany"] = max(prev["howMany"], obs.get("howMany") or 1)
            if obs.get("obsDt", "") > prev.get("obsDt", ""):
                prev["obsDt"] = obs["obsDt"]
                if obs.get("subId"):
                    prev["subId"] = obs["subId"]

    features = []
    for loc_id, rec in loc_agg.items():
        features.append({
            "type": "Feature",
            "properties": {
                "locName": rec["locName"],
                "latestDate": rec["latestDate"],
                "locId": loc_id,
                "species_list": sorted(rec["species"].values(),
                                       key=lambda x: x["species"]),
            },
            "geometry": {"type": "Point",
                         "coordinates": [round(rec["lng"], 5),
                                         round(rec["lat"], 5)]},
        })
    return fc(features)


# ══════════════════════════════════════════════════════════════════
#  LIVE CONDITIONS
# ══════════════════════════════════════════════════════════════════

def fetch_wildfires(bbox, cache, *, wide_bbox=None):
    """Active wildfire perimeters from NIFC WFIGS. Always live, never cached —
    the Pine Barrens is the most fire-prone landscape in the Northeast."""
    if OFFLINE:
        return EMPTY_FC
    s, w, n, e = wide_bbox or bbox
    features = []
    try:
        r = requests.get(NIFC_FIRE_PERIMETERS, params={
            "where": "1=1",
            "geometry": f"{w},{s},{e},{n}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "poly_IncidentName,attr_IncidentSize,"
                         "attr_FireBehaviorGeneral,attr_ContainmentPercent,"
                         "attr_FireDiscoveryDateTime",
            "returnGeometry": "true", "f": "geojson",
            "resultRecordCount": 500,
        }, headers=HEADERS, timeout=90)
        r.raise_for_status()
        for f in r.json().get("features", []):
            geom = f.get("geometry")
            if not geom or not geom.get("coordinates"):
                continue
            p = f.get("properties") or {}
            slim = {"name": p.get("poly_IncidentName") or "Unknown Fire",
                    "acres": p.get("attr_IncidentSize") or 0}
            if p.get("attr_FireBehaviorGeneral"):
                slim["behavior"] = p["attr_FireBehaviorGeneral"]
            if p.get("attr_ContainmentPercent") is not None:
                slim["containment"] = p["attr_ContainmentPercent"]
            features.append({
                "type": "Feature", "properties": slim,
                "geometry": {**geom,
                             "coordinates": _round_coords(geom["coordinates"])},
            })
    except Exception as exc:
        log.warning("    NIFC wildfire query failed: %s", exc)
    return fc(features)


def fetch_smoke(bbox, cache):
    """NOAA HMS analyst-reviewed satellite smoke plumes. Always live."""
    if OFFLINE:
        return EMPTY_FC
    s, w, n, e = bbox
    features = []
    try:
        r = requests.get(NOAA_HMS_SMOKE, params={
            "where": "1=1",
            "geometry": f"{w},{s},{e},{n}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "outSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "Density", "returnGeometry": "true",
            "f": "geojson", "resultRecordCount": 500,
        }, headers=HEADERS, timeout=90)
        r.raise_for_status()
        for f in r.json().get("features", []):
            geom = f.get("geometry")
            if not geom or not geom.get("coordinates"):
                continue
            features.append({
                "type": "Feature",
                "properties": {"density": (f.get("properties") or {}).get(
                    "Density") or "Unknown"},
                "geometry": {**geom,
                             "coordinates": _round_coords(geom["coordinates"])},
            })
    except Exception as exc:
        log.warning("    NOAA HMS smoke query failed: %s", exc)
    return fc(features)


# ══════════════════════════════════════════════════════════════════
#  RASTER OVERLAYS
# ══════════════════════════════════════════════════════════════════
# Served live from the agency as Esri dynamic-export or WMTS tiles, so no
# geometry is embedded in the page. `service` is "esri" (MapServer /export or
# ImageServer /exportImage) or "noaa_wmts".
#
# Several agency services carry scale-visibility rules that blank the image
# outside a zoom window; minNativeZoom / maxNativeZoom pin requests to a range
# that actually renders. NJDEP's Wetlands (2012) layer only draws around
# 1:68k, so it is pinned to z13 and upscaled — use the USFWS NWI overlay when
# you need detail at higher zoom.

RASTER_DEFS = {
    "noaa_charts": {
        "service": "noaa_wmts",
        "url": ("https://gis.charttools.noaa.gov/arcgis/rest/services/"
                "MarineChart_Services/NOAACharts/MapServer/WMTS/tile/1.0.0/"
                "MarineChart_Services_NOAACharts/default/GoogleMapsCompatible"),
        "opacity": 0.75,
        "maxNativeZoom": 14,
        "attribution": "NOAA Office of Coast Survey — ENC",
    },
    "bathymetry": {
        "service": "esri",
        "url": ("https://gis.ngdc.noaa.gov/arcgis/rest/services/DEM_mosaics/"
                "DEM_all/ImageServer/exportImage"),
        "opacity": 0.5,
        "maxNativeZoom": 13,
        "attribution": "NOAA NCEI DEM mosaic",
    },
    "nwi": {
        "service": "esri",
        "url": NWI_RASTER,
        "opacity": 0.6,
        "attribution": "USFWS National Wetlands Inventory",
    },
    "njdep_wetlands": {
        "service": "esri",
        "url": f"{NJDEP_LANDLU}/export",
        "params": {"layers": "show:2"},
        "opacity": 0.55,
        "minNativeZoom": 13,
        "maxNativeZoom": 13,
        "attribution": "NJDEP Wetlands 2012 (LULC)",
    },
    "tidelands": {
        "service": "esri",
        "url": f"{NJDEP_HYDRO}/export",
        "params": {"layers": "show:30"},
        "opacity": 0.55,
        "minNativeZoom": 15,
        "attribution": "NJDEP Tidelands",
    },
    "flood": {
        "service": "esri",
        "url": "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/"
               "MapServer/export",
        "params": {"layers": "show:28"},
        "opacity": 0.5,
        "minNativeZoom": 14,
        "attribution": "FEMA National Flood Hazard Layer",
    },
    "open_space_all": {
        "service": "esri",
        "url": f"{NJDEP_LAND}/export",
        "params": {"layers": "show:65"},
        "opacity": 0.5,
        "minNativeZoom": 12,
        "attribution": "NJDEP Open Space",
    },
    "chanj": {
        "service": "esri",
        "url": f"{NJDEP_ENV}/export",
        "params": {"layers": "show:107"},
        "opacity": 0.5,
        "minNativeZoom": 11,
        "attribution": "NJDEP Connecting Habitat Across New Jersey",
    },
}


# ══════════════════════════════════════════════════════════════════
#  LOCAL TILE CACHE
# ══════════════════════════════════════════════════════════════════
# Vector data is embedded in the page, but basemaps and raster overlays are
# fetched from the agency on every view. Pre-downloading them into
# `<output>/tiles/<key>/<z>/<x>/<y>.png` makes the map work offline and stops
# hammering the services on every reload.

NOAA_CHART_WMTS = (
    "https://gis.charttools.noaa.gov/arcgis/rest/services/MarineChart_Services/"
    "NOAACharts/MapServer/WMTS/tile/1.0.0/MarineChart_Services_NOAACharts/"
    "default/GoogleMapsCompatible"
)

# `policy: "cdn"` marks community/commercial tile CDNs whose terms of use
# prohibit bulk downloading. Those are never included in a group selection —
# name them explicitly if you have permission.
BASEMAP_TILES = {
    "carto_voyager": {
        "kind": "xyz", "policy": "cdn", "subdomains": "abcd",
        "url": "https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png",
    },
    "carto_light": {
        "kind": "xyz", "policy": "cdn", "subdomains": "abcd",
        "url": "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    },
    "osm": {
        "kind": "xyz", "policy": "cdn", "subdomains": "abc",
        "url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    },
    "opentopo": {
        "kind": "xyz", "policy": "cdn", "subdomains": "abc", "maxNativeZoom": 17,
        "url": "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
    },
    "esri_imagery": {
        "kind": "xyz",
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "World_Imagery/MapServer/tile/{z}/{y}/{x}",
    },
    "esri_ocean": {
        "kind": "xyz", "maxNativeZoom": 13,
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/"
               "Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}",
    },
    "noaa_chart": {
        "kind": "noaa_wmts", "maxNativeZoom": 14, "url": NOAA_CHART_WMTS,
    },
}

# The chart overlay draws the same tiles as the chart basemap, so they share a
# cache directory rather than downloading the pyramid twice.
RASTER_CACHE_ALIAS = {"noaa_charts": "noaa_chart"}


def tile_sources() -> dict:
    """Every cacheable tile pyramid, keyed by cache directory name."""
    src = dict(BASEMAP_TILES)
    for key, rd in RASTER_DEFS.items():
        ck = RASTER_CACHE_ALIAS.get(key, key)
        if ck in src:
            continue
        src[ck] = {
            "kind": rd["service"],
            "url": rd["url"],
            "params": rd.get("params", {}),
            "minNativeZoom": rd.get("minNativeZoom"),
            "maxNativeZoom": rd.get("maxNativeZoom"),
        }
    return src


WEB_MERCATOR_R = 20037508.342789244


def _lon2x(lon: float, z: int) -> int:
    return int((lon + 180.0) / 360.0 * (2 ** z))


def _lat2y(lat: float, z: int) -> int:
    lat = max(-85.05112878, min(85.05112878, lat))
    return int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi)
               / 2.0 * (2 ** z))


def tile_range(bbox: tuple, z: int) -> tuple:
    """(x0, x1, y0, y1) inclusive tile bounds covering bbox at zoom z."""
    s, w, n, e = bbox
    lim = 2 ** z - 1
    x0, x1 = _lon2x(w, z), _lon2x(e, z)
    y0, y1 = _lat2y(n, z), _lat2y(s, z)
    return (max(0, x0), min(lim, x1), max(0, y0), min(lim, y1))


# A typical desktop map pane is wider than it is tall. Leaflet's fitBounds
# fits the tighter axis, so a tall narrow bbox like Long Beach Island ends up
# showing far more longitude than was asked for — and a tile cache cut exactly
# to the bbox leaves a blank band on screen. Cache out to this on-screen aspect
# ratio (width:height) before applying the margin.
TILE_ASPECT = 2.0


def pad_bbox(bbox: tuple, frac: float, aspect: float = TILE_ASPECT) -> tuple:
    """Grow a bbox to at least `aspect` on screen, then by `frac` on each side."""
    s, w, n, e = bbox
    mid_lat = (s + n) / 2
    scale = math.cos(math.radians(mid_lat))   # degrees lon -> screen distance

    if aspect and (n - s) > 0:
        on_screen = ((e - w) * scale) / (n - s)
        if on_screen < aspect:
            want_lon = aspect * (n - s) / scale
            grow = (want_lon - (e - w)) / 2
            w, e = w - grow, e + grow
        else:
            # Wide and short instead: grow latitude to match.
            want_lat = ((e - w) * scale) / aspect
            grow = (want_lat - (n - s)) / 2
            s, n = s - grow, n + grow

    if frac > 0:
        dy, dx = (n - s) * frac, (e - w) * frac
        s, w, n, e = s - dy, w - dx, n + dy, e + dx

    return (max(-85.0, s), max(-180.0, w), min(85.0, n), min(180.0, e))


def count_tiles(bbox: tuple, zooms: range) -> int:
    total = 0
    for z in zooms:
        x0, x1, y0, y1 = tile_range(bbox, z)
        total += (x1 - x0 + 1) * (y1 - y0 + 1)
    return total


def _esri_tile_url(src: dict, z: int, x: int, y: int) -> str:
    """Mirror of the EsriDynamic.getTileUrl bbox math in the page JS."""
    span = 2 * WEB_MERCATOR_R / (2 ** z)
    xmin = -WEB_MERCATOR_R + x * span
    ymax = WEB_MERCATOR_R - y * span
    params = {
        "bbox": f"{xmin},{ymax - span},{xmin + span},{ymax}",
        "bboxSR": "3857", "imageSR": "3857", "size": "256,256",
        "format": "png32", "transparent": "true", "f": "image",
    }
    params.update(src.get("params") or {})
    return src["url"] + "?" + "&".join(
        f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())


def source_tile_url(src: dict, z: int, x: int, y: int) -> str:
    kind = src["kind"]
    if kind == "esri":
        return _esri_tile_url(src, z, x, y)
    if kind == "noaa_wmts":
        return f"{src['url']}/{max(0, z - 2)}/{y}/{x}.png"
    url = src["url"].replace("{z}", str(z)).replace("{x}", str(x)) \
                    .replace("{y}", str(y))
    subs = src.get("subdomains")
    if subs and "{s}" in url:
        url = url.replace("{s}", subs[(x + y) % len(subs)])
    return url


def clamp_zoom(src: dict, z: int) -> int | None:
    """The zoom a source can actually serve for a request at zoom z, or None
    if it cannot serve it at all. Mirrors Leaflet's native-zoom clamping."""
    lo, hi = src.get("minNativeZoom"), src.get("maxNativeZoom")
    if hi is not None and z > hi:
        return None       # Leaflet upscales the hi tile; no need to store more
    if lo is not None and z < lo:
        return None
    return z


def download_tiles(keys: list, bbox: tuple, zooms: range, dest: Path, *,
                   max_tiles: int, workers: int = 5,
                   overwrite: bool = False) -> dict:
    """Fetch tile pyramids to `dest/<key>/<z>/<x>/<y>.png`. Returns a manifest
    of {key: {minZoom, maxZoom}} describing what is available locally."""
    import concurrent.futures as cf

    sources = tile_sources()
    dest.mkdir(parents=True, exist_ok=True)
    manifest_path = dest / "manifest.json"
    manifest = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
        except json.JSONDecodeError:
            manifest = {}

    session = requests.Session()
    session.headers.update(HEADERS)

    for key in keys:
        src = sources.get(key)
        if not src:
            log.warning("  Unknown tile source %r — skipping", key)
            continue

        jobs = []
        for z in zooms:
            if clamp_zoom(src, z) is None:
                continue
            x0, x1, y0, y1 = tile_range(bbox, z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    path = dest / key / str(z) / str(x) / f"{y}.png"
                    if path.exists() and path.stat().st_size > 0 and not overwrite:
                        continue
                    jobs.append((z, x, y, path))

        served = [z for z in zooms if clamp_zoom(src, z) is not None]
        if not served:
            log.info("  %-16s serves no zoom in %d-%d — skipping",
                     key, zooms.start, zooms.stop - 1)
            continue
        if not jobs:
            log.info("  %-16s already complete for z%d-%d",
                     key, min(served), max(served))
            manifest[key] = {"minZoom": min(served), "maxZoom": max(served)}
            continue
        if len(jobs) > max_tiles:
            log.warning("  %-16s needs %s tiles, over the --max-tiles cap of "
                        "%s — skipping. Narrow --tile-zooms or the bbox, or "
                        "raise the cap.", key, f"{len(jobs):,}", f"{max_tiles:,}")
            continue

        log.info("  %-16s %s tiles for z%d-%d ...",
                 key, f"{len(jobs):,}", min(served), max(served))
        ok = fail = 0

        def fetch(job):
            z, x, y, path = job
            url = source_tile_url(src, z, x, y)
            for attempt in range(3):
                try:
                    r = session.get(url, timeout=90)
                    if r.status_code == 200 and r.content:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(r.content)
                        return True
                    if r.status_code in (404, 400):
                        return False   # genuinely absent, do not retry
                except Exception:
                    pass
                time.sleep(1.5 * (attempt + 1))
            return False

        with cf.ThreadPoolExecutor(workers) as ex:
            for got in ex.map(fetch, jobs):
                if got:
                    ok += 1
                else:
                    fail += 1
                if (ok + fail) % 500 == 0:
                    log.info("      %s/%s (%d missing)",
                             f"{ok + fail:,}", f"{len(jobs):,}", fail)

        log.info("  %-16s done — %s saved, %d unavailable",
                 key, f"{ok:,}", fail)
        if ok or fail < len(jobs):
            manifest[key] = {"minZoom": min(served), "maxZoom": max(served)}

    manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    total_bytes = sum(f.stat().st_size for f in dest.rglob("*.png"))
    log.info("  Tile cache: %s files, %.1f MB at %s",
             f"{sum(1 for _ in dest.rglob('*.png')):,}",
             total_bytes / 1024 / 1024, dest)
    return manifest


# Leaflet, its plugin and the webfont are loaded from CDNs by default. Vendoring
# them alongside the tile cache is what makes the page work with no network at
# all, rather than merely without the map services.
VENDOR_ASSETS = [
    ("leaflet.css", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"),
    ("leaflet.js", "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"),
    ("MarkerCluster.css",
     "https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"),
    ("MarkerCluster.Default.css",
     "https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"),
    ("leaflet.markercluster.js",
     "https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"),
]

# Referenced by url() inside leaflet.css, so they must sit in lib/images/.
VENDOR_IMAGES = [
    "layers.png", "layers-2x.png",
    "marker-icon.png", "marker-icon-2x.png", "marker-shadow.png",
]

GOOGLE_FONT_CSS = ("https://fonts.googleapis.com/css2?"
                   "family=IBM+Plex+Sans:wght@400;500;600&display=swap")


def vendor_libs(dest: Path, *, overwrite: bool = False) -> bool:
    """Download Leaflet, markercluster and the webfont into `dest`.

    Returns True if everything needed is present afterwards.
    """
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "images").mkdir(exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)

    def grab(path: Path, url: str, binary=True) -> bool:
        if path.exists() and path.stat().st_size > 0 and not overwrite:
            return True
        try:
            r = session.get(url, timeout=120)
            r.raise_for_status()
            path.write_bytes(r.content) if binary else \
                path.write_text(r.text, encoding="utf-8")
            return True
        except Exception as exc:
            log.warning("  vendor %s failed: %s", path.name, exc)
            return False

    ok = True
    for name, url in VENDOR_ASSETS:
        ok &= grab(dest / name, url)
    for img in VENDOR_IMAGES:
        grab(dest / "images" / img,
             f"https://unpkg.com/leaflet@1.9.4/dist/images/{img}")

    # The webfont: fetch Google's stylesheet, pull the woff2 files it points at
    # and rewrite the url()s to the local copies.
    css_path = dest / "fonts.css"
    if overwrite or not css_path.exists():
        try:
            r = session.get(GOOGLE_FONT_CSS, timeout=60, headers={
                # Without a modern UA Google serves legacy formats.
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/126.0 Safari/537.36"})
            r.raise_for_status()
            css = r.text
            for i, font_url in enumerate(dict.fromkeys(
                    re.findall(r'url\((https://fonts\.gstatic\.com/[^)]+)\)', css))):
                fname = f"font{i}.woff2"
                if grab(dest / fname, font_url):
                    css = css.replace(font_url, fname)
            css_path.write_text(css, encoding="utf-8")
        except Exception as exc:
            log.warning("  vendor webfont failed: %s", exc)

    missing = [n for n, _ in VENDOR_ASSETS if not (dest / n).exists()]
    if missing:
        log.warning("  vendored assets incomplete, still missing: %s",
                    ", ".join(missing))
        return False
    size = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    log.info("  Vendored Leaflet + webfont: %.1f KB at %s", size / 1024, dest)
    return True


# Local equivalents of CSP / FONT_LINK / LEAFLET_CDN, used once libraries are
# vendored. 'self' only — no external origin is needed to render the page.
def local_head(tile_root: str = "tiles/") -> tuple:
    csp = (
        '<meta http-equiv="Content-Security-Policy" content="'
        "default-src 'none';"
        "script-src 'self' 'unsafe-inline';"
        "style-src 'self' 'unsafe-inline';"
        "img-src 'self' data: blob: "
        # Kept so zooms outside the cached range still fall back to the service.
        "https://*.basemaps.cartocdn.com https://*.tile.openstreetmap.org "
        "https://*.tile.opentopomap.org https://server.arcgisonline.com "
        "https://services.arcgisonline.com https://tiles.arcgis.com "
        "https://gis.charttools.noaa.gov https://gis.ngdc.noaa.gov "
        "https://mapsdep.nj.gov https://fwsprimary.wim.usgs.gov "
        "https://hazards.fema.gov;"
        "font-src 'self';"
        "connect-src 'self';"
        '"/>'
    )
    fonts = '<link rel="stylesheet" href="lib/fonts.css"/>'
    libs = (
        '<link rel="stylesheet" href="lib/leaflet.css"/>\n'
        '<link rel="stylesheet" href="lib/MarkerCluster.css"/>\n'
        '<link rel="stylesheet" href="lib/MarkerCluster.Default.css"/>\n'
        '<script src="lib/leaflet.js"></' + 'script>\n'
        '<script src="lib/leaflet.markercluster.js"></' + 'script>'
    )
    return csp, fonts, libs


def resolve_tile_keys(spec: str) -> list:
    """Expand a --cache-tiles value into concrete tile source keys.

    Groups deliberately exclude community and commercial tile CDNs, whose
    terms prohibit bulk downloading; name those explicitly to include them.
    """
    sources = tile_sources()
    open_keys = [k for k, v in sources.items() if v.get("policy") != "cdn"]
    overlay_keys = [RASTER_CACHE_ALIAS.get(k, k) for k in RASTER_DEFS]
    groups = {
        "default": ["noaa_chart", "esri_imagery"] +
                   [k for k in overlay_keys if k != "noaa_chart"],
        "overlays": list(dict.fromkeys(overlay_keys)),
        "basemaps": [k for k in BASEMAP_TILES if k in open_keys],
        "open": open_keys,
        "all": list(sources),
    }
    out: list = []
    for part in (spec or "default").split(","):
        part = part.strip()
        if not part:
            continue
        if part in groups:
            out.extend(groups[part])
        elif part in sources:
            out.append(part)
        else:
            raise ValueError(
                f"unknown tile source {part!r}. Groups: "
                f"{', '.join(sorted(groups))}. Sources: "
                f"{', '.join(sorted(sources))}")
    return list(dict.fromkeys(out))


# ══════════════════════════════════════════════════════════════════
#  HTML / CSS / JS
# ══════════════════════════════════════════════════════════════════

MAP_CSS = """
*{box-sizing:border-box}
body{margin:0;font-family:'IBM Plex Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  color:#1a1a1a;background:#f7f6f3;--accent:#1A6B9A;--muted:#707070}
.shell{display:flex;height:100vh;overflow:hidden}
.sidebar{width:290px;flex:0 0 290px;background:#fff;border-right:1px solid #e2ded6;
  display:flex;flex-direction:column;overflow:hidden}
.sidebar header{padding:14px 16px 10px;border-bottom:1px solid #eee9e0}
.sidebar h1{margin:0;font-size:15px;font-weight:600;letter-spacing:-.01em}
.sidebar .sub{margin:3px 0 0;font-size:11px;color:var(--muted);line-height:1.45}
.layer-scroll{flex:1;overflow-y:auto;padding-bottom:24px}
.page-links{display:flex;flex-wrap:wrap;gap:8px;margin-top:7px}
.page-links a{font-size:11px;color:var(--accent);text-decoration:none;
  border:1px solid #d8e3ea;border-radius:4px;padding:2px 7px;background:#f4f9fc}
.page-links a:hover{background:#e8f2f8}
.layer-tools{display:flex;gap:6px;padding:8px 16px;border-bottom:1px solid #f0ece4}
.layer-tools button{flex:1;font:inherit;font-size:11px;padding:4px 6px;cursor:pointer;
  background:#f7f6f3;border:1px solid #e2ded6;border-radius:4px;color:#444}
.layer-tools button:hover{background:#efece6}
.layer-group{border-bottom:1px solid #f4f1ea}
.layer-group > summary{padding:8px 16px;font-size:11px;font-weight:600;text-transform:uppercase;
  letter-spacing:.06em;color:#6a6a6a;cursor:pointer;list-style:none;display:flex;align-items:center;gap:6px}
.layer-group > summary::-webkit-details-marker{display:none}
.layer-group > summary::before{content:'\\25B8';font-size:9px;color:#aaa;transition:transform .15s}
.layer-group[open] > summary::before{transform:rotate(90deg)}
.layer-group > summary:hover{background:#faf9f6}
.group-count{color:#b0aaa0;font-size:10px;font-variant-numeric:tabular-nums}
.group-acts{display:flex;gap:3px;margin-left:auto}
.group-acts button{font:inherit;font-size:9px;font-weight:600;letter-spacing:.04em;
  padding:1px 5px;cursor:pointer;background:#fff;border:1px solid #dcd7ce;
  border-radius:3px;color:#777;line-height:1.5}
.group-acts button:hover{background:var(--accent);border-color:var(--accent);color:#fff}
.map-layer-toggle{display:flex;align-items:center;gap:8px;padding:5px 16px 5px 26px;
  font-size:12px;cursor:pointer;user-select:none}
.map-layer-toggle:hover{background:rgba(0,0,0,.04)}
.map-layer-toggle input[type=checkbox]{margin:0;accent-color:var(--accent);flex-shrink:0}
.map-layer-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.map-layer-dot.raster{border-radius:2px;border:1px solid rgba(0,0,0,.25)}
.map-layer-label{flex:1;line-height:1.3}
.map-layer-count{color:var(--muted);font-size:11px;min-width:30px;text-align:right;
  font-variant-numeric:tabular-nums}
.zoom-note{font-size:9px;color:#b08968;padding:0 16px 4px 26px;line-height:1.3}
.credits{padding:10px 16px;font-size:9px;color:#a09a90;line-height:1.6;border-top:1px solid #f0ece4}
.map-wrap{flex:1;position:relative;min-width:0}
#leaflet-map{position:absolute;inset:0;z-index:1}
.leaflet-popup-content{font-family:'IBM Plex Sans',sans-serif;font-size:13px;line-height:1.45;margin:10px 12px}
.leaflet-popup-content b{font-weight:600}
.popup-kicker{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#8a8a8a;
  display:block;margin-bottom:2px}
.popup-species{color:#1A6B3A;font-weight:500}
.popup-meta{color:#707070;font-size:11px}
.popup-row{font-size:11px;color:#444;margin-top:2px}
.popup-chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:5px}
.chip{font-size:10px;padding:1px 6px;border-radius:9px;background:#f0ede6;color:#555;white-space:nowrap}
.chip.yes{background:#e4f2e8;color:#1d6b3f}
.chip.no{background:#f7e6e4;color:#9b3227}
.chip.warn{background:#fdf1dc;color:#8a5a10}
.popup-links{margin-top:6px;font-size:11px;display:flex;gap:10px;flex-wrap:wrap}
.multi-head{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:#8a8a8a;
  padding-bottom:5px;border-bottom:1px solid #e8e4dc;margin-bottom:6px}
.multi-item{padding:7px 0;border-bottom:1px solid #f0ece4}
.multi-item:last-child{border-bottom:none;padding-bottom:0}
.multi-layer{font-size:9px;text-transform:uppercase;letter-spacing:.05em;color:#a89f92;
  display:block;margin-bottom:2px}
.leaflet-control-layers-toggle{background-image:none!important;background-size:0!important;
  display:flex!important;align-items:center!important;justify-content:center!important;
  width:36px!important;height:36px!important;padding:0!important;margin:0!important}
.leaflet-control-layers-toggle svg{width:20px;height:20px;display:block}
.leaflet-control-layers{border-radius:6px!important;box-shadow:0 2px 8px rgba(0,0,0,.18)!important}
.leaflet-control-layers-list{font-size:12px}
.sheet-toggle{display:none}
.sheet-scrim{display:none}

/* Phones: the map takes the whole screen and the layer list becomes a sheet
   that slides up over it. Stacking the two vertically left the scroll area
   with almost no height — one group header and then the credits — which made
   the layers unusable. */
@media(max-width:760px){
  .shell{flex-direction:column;position:relative}
  .map-wrap{position:absolute;inset:0}
  .sidebar{position:absolute;left:0;right:0;bottom:0;z-index:1200;width:auto;
    max-height:86vh;height:86vh;border-right:none;border-top:1px solid #d9d3c9;
    border-radius:14px 14px 0 0;box-shadow:0 -4px 24px rgba(0,0,0,.22);
    transform:translateY(100%);transition:transform .22s ease-out;
    padding-bottom:env(safe-area-inset-bottom)}
  .sidebar.open{transform:translateY(0)}
  .sidebar header{padding:8px 16px 8px;position:relative}
  .sidebar header::before{content:'';position:absolute;top:5px;left:50%;
    transform:translateX(-50%);width:38px;height:4px;border-radius:2px;background:#d8d2c8}
  .sidebar h1{font-size:14px;margin-top:6px}
  .sidebar .sub{font-size:10px}
  .layer-scroll{-webkit-overflow-scrolling:touch;overscroll-behavior:contain}

  /* Touch targets: 13px checkboxes and 5px rows are unusable with a thumb. */
  .map-layer-toggle{padding:11px 16px 11px 24px;font-size:13.5px;gap:11px;
    border-bottom:1px solid #f6f3ee}
  .map-layer-toggle input[type=checkbox]{width:20px;height:20px}
  .map-layer-dot{width:12px;height:12px}
  .map-layer-count{font-size:12px}
  .layer-group > summary{padding:13px 16px;font-size:12px}
  .group-acts button{padding:6px 11px;font-size:10px}
  .layer-tools{padding:10px 16px;gap:8px}
  .layer-tools button{padding:10px 6px;font-size:12.5px}
  .page-links a{padding:6px 11px;font-size:12px}

  /* The source list is long; keep it out of the way until asked for. */
  .credits{max-height:2.9em;overflow:hidden;position:relative;cursor:pointer}
  .credits.expanded{max-height:none}
  .credits::after{content:'sources \25BE';position:absolute;right:0;bottom:0;
    padding:0 10px 0 34px;color:var(--accent);font-weight:600;
    background:linear-gradient(90deg,rgba(255,255,255,0),#fff 30px)}
  .credits.expanded::after{content:''}

  /* Floating button that opens the sheet. */
  .sheet-toggle{display:flex;align-items:center;gap:7px;position:absolute;
    left:10px;bottom:calc(14px + env(safe-area-inset-bottom));z-index:1100;
    background:#fff;border:1px solid #d9d3c9;border-radius:22px;
    box-shadow:0 2px 10px rgba(0,0,0,.22);padding:10px 16px;font:inherit;
    font-size:13.5px;font-weight:600;color:#333;cursor:pointer}
  .sheet-toggle .n{color:var(--accent)}
  .sidebar.open ~ .sheet-toggle{display:none}
  .sheet-scrim{display:block;position:absolute;inset:0;z-index:1150;
    background:rgba(0,0,0,.28);opacity:0;pointer-events:none;transition:opacity .2s}
  .sheet-scrim.show{opacity:1;pointer-events:auto}
  /* Keep Leaflet's scale bar and attribution clear of the Layers button. */
  .leaflet-bottom.leaflet-left{bottom:54px}
  .leaflet-control-attribution{font-size:9px}
  .sheet-close{position:absolute;top:8px;right:10px;width:34px;height:34px;
    border:none;background:transparent;font-size:22px;line-height:1;color:#888;
    cursor:pointer;z-index:2}
}
@media(min-width:761px){ .sheet-close{display:none} }
"""

# Injection mode reuses the host page's own layout; keep the map tall and let
# the checklist's nav column carry the toggles.
INJECT_CSS = """
#leaflet-map{height:calc(100vh - 20px);width:100%;z-index:1;min-width:0;position:relative}
.layout{max-width:none!important}
@media(min-width:1400px){.main{max-width:none;padding-right:60px}}
.panel:not(.active){display:none!important;overflow:hidden;height:0}
"""

CSP = (
    '<meta http-equiv="Content-Security-Policy" content="'
    "default-src 'none';"
    "script-src 'self' 'unsafe-inline' https://unpkg.com https://cdn.jsdelivr.net;"
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com "
    "https://unpkg.com https://cdn.jsdelivr.net;"
    "img-src 'self' data: blob: "
    "https://*.basemaps.cartocdn.com https://*.tile.openstreetmap.org "
    "https://*.tile.opentopomap.org https://server.arcgisonline.com "
    "https://services.arcgisonline.com https://tiles.arcgis.com "
    "https://gis.charttools.noaa.gov https://gis.ngdc.noaa.gov "
    "https://mapsdep.nj.gov https://fwsprimary.wim.usgs.gov "
    "https://hazards.fema.gov https://unpkg.com "
    "https://cdn.download.ams.birds.cornell.edu "
    "https://inaturalist-open-data.s3.amazonaws.com "
    "https://static.inaturalist.org https://upload.wikimedia.org;"
    "font-src https://fonts.gstatic.com;"
    "connect-src 'self';"
    '"/>'
)

FONT_LINK = (
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    'family=IBM+Plex+Sans:wght@400;500;600&display=swap"/>'
)

LEAFLET_CDN = (
    '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"'
    ' integrity="sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H"'
    ' crossorigin="anonymous"/>\n'
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/'
    'leaflet.markercluster@1.5.3/dist/MarkerCluster.css"'
    ' integrity="sha384-pmjIAcz2bAn0xukfxADbZIb3t8oRT9Sv0rvO+BR5Csr6Dhqq+nZs59P0pPKQJkEV"'
    ' crossorigin="anonymous"/>\n'
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/'
    'leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"'
    ' integrity="sha384-wgw+aLYNQ7dlhK47ZPK7FRACiq7ROZwgFNg0m04avm4CaXS+Z9Y7nMu8yNjBKYC+"'
    ' crossorigin="anonymous"/>\n'
    '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"'
    ' integrity="sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH"'
    ' crossorigin="anonymous"></' + 'script>\n'
    '<script src="https://cdn.jsdelivr.net/npm/leaflet.markercluster@1.5.3/'
    'dist/leaflet.markercluster.js"'
    ' integrity="sha384-eXVCORTRlv4FUUgS/xmOyr66XBVraen8ATNLMESp92FKXLAMiKkerixTiBvXriZr"'
    ' crossorigin="anonymous"></' + 'script>'
)

MAP_JS_TEMPLATE = r"""
var _map=null,_mapLayers={},_rasterDefs=__RASTER_DEFS__,_colors=__COLORS__;
var _tileCache=__TILE_CACHE__,_tileRoot=__TILE_ROOT__,_zorder=__ZORDER__;
var _deferred=__DEFERRED__,_deferredState={};
var _layerLabels=__LABELS__;

/* Tiles pre-downloaded by --cache-tiles live at tiles/<key>/<z>/<x>/<y>.png.
   Inside the cached zoom range the page never touches the network; outside it
   the layer falls back to the live service, so a partial cache still works. */
function _cachedTile(key,coords){
  var c=key&&_tileCache[key];
  if(!c||coords.z<c.minZoom||coords.z>c.maxZoom)return null;
  return _tileRoot+key+'/'+coords.z+'/'+coords.x+'/'+coords.y+'.png';
}
var CachedTile=L.TileLayer.extend({
  initialize:function(url,options){
    this._cacheKey=(options||{}).cacheKey;
    L.TileLayer.prototype.initialize.call(this,url,options);
  },
  remoteUrl:function(coords){
    return L.TileLayer.prototype.getTileUrl.call(this,coords);
  },
  getTileUrl:function(coords){
    return _cachedTile(this._cacheKey,coords)||this.remoteUrl(coords);
  }
});

/* The manifest records which sources and zooms were cached, but coverage is
   per bounding box — a page built for one extent can believe a local tile
   exists just outside it. Rather than leave a hole, fall back to the live
   service for any local tile that fails. */
function _tileFallback(layer){
  layer.on('tileerror',function(e){
    var t=e.tile;
    if(!t||t.__lbiRetried||typeof layer.remoteUrl!=='function')return;
    if(t.src.indexOf(_tileRoot)!==0)return;      // already a remote URL
    t.__lbiRetried=true;
    t.src=layer.remoteUrl(e.coords);
  });
  return layer;
}

function xyz(cacheKey,url,opts){
  opts=opts||{}; opts.cacheKey=cacheKey;
  return _tileFallback(new CachedTile(url,opts));
}

/* Esri MapServer /export and ImageServer /exportImage rendered as a tile grid.
   Leaflet clamps coords.z to min/maxNativeZoom before calling getTileUrl, so
   the bbox we compute always matches the tile actually being placed. */
var EsriDynamic=L.TileLayer.extend({
  initialize:function(url,options){
    this._exportUrl=url;
    L.TileLayer.prototype.initialize.call(this,'',options);
  },
  remoteUrl:function(coords){
    var R=20037508.342789244,span=2*R/Math.pow(2,coords.z);
    var xmin=-R+coords.x*span,ymax=R-coords.y*span;
    var q=['bbox='+[xmin,ymax-span,xmin+span,ymax].join('%2C'),
           'bboxSR=3857','imageSR=3857','size=256%2C256',
           'format=png32','transparent=true','f=image'];
    var extra=this.options.exportParams||{};
    for(var k in extra)q.push(encodeURIComponent(k)+'='+encodeURIComponent(extra[k]));
    return this._exportUrl+'?'+q.join('&');
  },
  getTileUrl:function(coords){
    return _cachedTile(this.options.cacheKey,coords)||this.remoteUrl(coords);
  }
});

/* NOAA's ENC WMTS pyramid is offset two levels from the standard Web Mercator
   scale set: its TileMatrix 12 is the 1:34k level that Leaflet calls z14. */
var NoaaChart=L.TileLayer.extend({
  initialize:function(url,options){
    this._base=url;
    L.TileLayer.prototype.initialize.call(this,'',options);
  },
  remoteUrl:function(coords){
    return this._base+'/'+Math.max(0,coords.z-2)+'/'+coords.y+'/'+coords.x+'.png';
  },
  getTileUrl:function(coords){
    return _cachedTile(this.options.cacheKey||'noaa_chart',coords)
        || this.remoteUrl(coords);
  }
});

function initMap(){
  if(_map){_map.invalidateSize();return;}
  /* Canvas rendering, with every vector layer sharing ONE canvas.
     Leaflet creates a separate canvas per pane, and the highest-z canvas
     swallows clicks even where it has drawn nothing — which is what made
     polygons in the lower panes unselectable. A single pane means a single
     canvas, so Leaflet hit-tests all vector layers together and the topmost
     shape under the cursor wins. SVG would also fix the hit-testing, but not
     at nine thousand habitat polygons. Draw order (and therefore both paint
     order and click priority) is set by _restack(). */
  _map=L.map('leaflet-map',{zoomControl:true,preferCanvas:true})
        .setView([__CENTER_LAT__,__CENTER_LNG__],__ZOOM__);

  /* ── Basemaps ── */
  var voyager=xyz('carto_voyager','https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}.png',
    {attribution:'&copy; <a href="https://carto.com/">CARTO</a>, OpenStreetMap',maxZoom:19,subdomains:'abcd'});
  var minimal=xyz('carto_light','https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png',
    {attribution:'CARTO, OpenStreetMap',maxZoom:19,subdomains:'abcd'});
  var osm=xyz('osm','https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {attribution:'&copy; OpenStreetMap contributors',maxZoom:19});
  var topo=xyz('opentopo','https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    {attribution:'OpenTopoMap (CC-BY-SA)',maxZoom:17});
  var sat=xyz('esri_imagery','https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {attribution:'Esri World Imagery',maxZoom:19});
  var ocean=xyz('esri_ocean','https://server.arcgisonline.com/ArcGIS/rest/services/Ocean/World_Ocean_Base/MapServer/tile/{z}/{y}/{x}',
    {attribution:'Esri Ocean Basemap — GEBCO, NOAA',maxZoom:13,maxNativeZoom:13});

  /* NOAA electronic navigational charts as full basemaps. The chart alone is
     the working nautical view; the hybrid pair keeps satellite imagery under
     the chart so marsh and beach detail stays readable. */
  var chartUrl='https://gis.charttools.noaa.gov/arcgis/rest/services/MarineChart_Services/NOAACharts/MapServer/WMTS/tile/1.0.0/MarineChart_Services_NOAACharts/default/GoogleMapsCompatible';
  var chartOpts={attribution:'NOAA Office of Coast Survey — ENC',cacheKey:'noaa_chart',
                 maxNativeZoom:14,maxZoom:19};
  var noaaChartBase=_tileFallback(new NoaaChart(chartUrl,chartOpts));
  var noaaChartHybrid=L.layerGroup([
    xyz('esri_imagery','https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
      {attribution:'Esri World Imagery',maxZoom:19}),
    _tileFallback(new NoaaChart(chartUrl,{attribution:'NOAA ENC',cacheKey:'noaa_chart',
      maxNativeZoom:14,maxZoom:19,opacity:0.72}))
  ]);
  var noaaChartLight=L.layerGroup([
    xyz('carto_light','https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png',
      {attribution:'CARTO',maxZoom:19,subdomains:'abcd'}),
    _tileFallback(new NoaaChart(chartUrl,{attribution:'NOAA ENC',cacheKey:'noaa_chart',
      maxNativeZoom:14,maxZoom:19,opacity:0.85}))
  ]);

  var basemaps={
    'NOAA Chart':noaaChartBase,
    'NOAA Chart + Satellite':noaaChartHybrid,
    'NOAA Chart + Street':noaaChartLight,
    'Voyager':voyager,
    'Minimal':minimal,
    'Street':osm,
    'Topo':topo,
    'Satellite':sat,
    'Ocean / Bathymetric':ocean
  };
  basemaps[__DEFAULT_BASEMAP__].addTo(_map);
  L.control.layers(basemaps,null,{collapsed:true}).addTo(_map);
  var tog=document.querySelector('.leaflet-control-layers-toggle');
  if(tog){tog.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="#555" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 22 8.5 12 15 2 8.5"/><polyline points="2 12 12 18.5 22 12"/><polyline points="2 15.5 12 22 22 15.5"/></svg>';}
  L.control.scale({imperial:true,metric:true}).addTo(_map);

  /* ── Panes ──
     One pane for all canvas-drawn vector layers so they share a renderer.
     Leaflet's own markerPane (zIndex 600) still carries the divIcon markers,
     which keeps the labelled pins above everything. */
  _map.createPane('tileOverlays').style.zIndex=250;
  _map.createPane('vectors').style.zIndex=400;

  /* ── Style + popup helpers ── */
  function ps(c,d,fill){return function(){return{color:c,weight:2,opacity:.85,
    fillColor:c,fillOpacity:(fill===undefined?.15:fill),dashArray:d||'',pane:'vectors'};};}
  function ls(c,w,d){return function(){return{color:c,weight:w||3,opacity:.9,
    dashArray:d||'',fill:false,pane:'vectors',lineCap:'round'};};}
  function cm(c,r,ring){return function(f,ll){return L.circleMarker(ll,{radius:r||5,
    fillColor:c,color:ring||'#333',weight:1,fillOpacity:.85,pane:'vectors'});};}
  function glyph(c,svg,size){return function(f,ll){return L.marker(ll,{pane:'vectors',
    icon:L.divIcon({className:'',iconSize:[size||22,size||22],iconAnchor:[(size||22)/2,(size||22)/2],
      html:'<svg width="'+(size||22)+'" height="'+(size||22)+'" viewBox="0 0 24 24">'+
        '<circle cx="12" cy="12" r="11" fill="'+c+'" stroke="#fff" stroke-width="1.6"/>'+svg+'</svg>'})});};}
  function bp(ly,fn){ly.eachLayer(function(l){
    var p=l.feature&&l.feature.properties;if(p)l.bindPopup(fn(p),{maxWidth:330});});}
  function nm(p){return '<b>'+(p.name||'Unnamed')+'</b>';}
  function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
  function kicker(t){return '<span class="popup-kicker">'+t+'</span>';}
  function row(label,val){return val?'<div class="popup-row">'+label+': '+esc(val)+'</div>':'';}
  function link(url,label){
    if(!url)return '';
    var u=String(url);
    if(!/^https?:\/\//i.test(u))u='https://'+u.replace(/^\/+/,'');
    return '<a href="'+esc(u)+'" target="_blank" rel="noopener">'+label+'</a>';
  }
  function links(){
    var out=[];for(var i=0;i<arguments.length;i++){if(arguments[i])out.push(arguments[i]);}
    return out.length?'<div class="popup-links">'+out.join('')+'</div>':'';
  }
  /* NJDEP amenity flags come through as 'Y'/'N'/'Yes'/'No'. */
  function flag(v){
    if(v===undefined||v===null||v==='')return null;
    var s=String(v).trim().toLowerCase();
    if(s==='y'||s==='yes'||s==='true'||s==='1')return true;
    if(s==='n'||s==='no'||s==='false'||s==='0')return false;
    return null;
  }
  /* Most agency amenity fields are Yes/No, but some are enumerations —
     NJDEP's PARKING is Street / Lot / No — so a value that is not boolean and
     not a null marker is shown as-is rather than silently dropped. */
  function chips(pairs){
    var out=[],skip={na:1,'n/a':1,unknown:1,unk:1,none:1};
    for(var i=0;i<pairs.length;i++){
      var label=pairs[i][0],raw=pairs[i][1],v=flag(raw);
      if(v===true)out.push('<span class="chip yes">'+label+'</span>');
      else if(v===false)out.push('<span class="chip no">no '+label.toLowerCase()+'</span>');
      else if(raw!==undefined&&raw!==null&&raw!==''&&!skip[String(raw).trim().toLowerCase()])
        out.push('<span class="chip">'+esc(label)+': '+esc(raw)+'</span>');
    }
    return out.length?'<div class="popup-chips">'+out.join('')+'</div>':'';
  }
  function tags(vals){
    var out=[];
    for(var i=0;i<vals.length;i++){if(vals[i])out.push('<span class="chip">'+esc(vals[i])+'</span>');}
    return out.length?'<div class="popup-chips">'+out.join('')+'</div>':'';
  }
  function addr(p){
    var bits=[];
    if(p['addr:street'])bits.push(p['addr:street']);
    if(p['addr:city'])bits.push(p['addr:city']);
    return bits.length?'<div class="popup-row">'+esc(bits.join(', '))+'</div>':'';
  }
  function gj(key,opts){
    var data=window['mapData_'+key];
    return L.geoJSON(data||{type:'FeatureCollection',features:[]},opts);
  }
  function clusterIcon(c){
    var n=c.getChildCount(),sz=n<20?'small':n<100?'medium':'large';
    return L.divIcon({html:'<div><span>'+n+'</span></div>',
      className:'marker-cluster marker-cluster-'+sz,iconSize:L.point(40,40)});
  }
  /* Layers with hundreds of points — every street-end beach crossing on the
     island, say — bury the map when drawn flat. Cluster them, and stop
     clustering once you are zoomed in far enough to want the individual pins. */
  function gjCluster(key,opts,atZoom){
    var cg=L.markerClusterGroup({maxClusterRadius:50,showCoverageOnHover:false,
      spiderfyOnMaxZoom:true,disableClusteringAtZoom:atZoom||15,
      iconCreateFunction:clusterIcon});
    cg.addLayer(gj(key,opts));
    /* A cluster group has no addData(); deferred data arrives as a fresh
       GeoJSON layer built with the same options and handed to the cluster. */
    cg._lbiFill=function(data){cg.addLayer(L.geoJSON(data,opts));};
    return cg;
  }
  /* Bind popups through onEachFeature so a layer can be swapped between a
     plain GeoJSON layer and a cluster group without losing them. */
  function popupOn(fn){return function(f,layer){layer.bindPopup(fn(f.properties),{maxWidth:330});};}

  /* ═══ Island & Shore ═══ */
  function beachStyle(c){return function(){return{color:c,weight:3,opacity:.85,
    fillColor:c,fillOpacity:.22,pane:'vectors'};};}
  function beachPopup(f,layer){
    var p=f.properties,a=p.access;
    var s=kicker('Beach')+'<b>'+esc(p.name||'Beach')+'</b>';
    s+=row('Access',a?(a==='yes'||a==='public'?'Public':a):'Not signed in OSM');
    s+=row('Surface',p.surface);
    s+=row('Operator',p.operator);
    s+=chips([['lifeguard',p.lifeguard],['vehicles',p.vehicles],
              ['dogs',p.dog],['fee',p.fee],['wheelchair',p.wheelchair]]);
    s+=links(link(p.website,'Website'));
    layer.bindPopup(s,{maxWidth:320});
  }
  _mapLayers.beaches_public=gj('beaches',{
    filter:function(f){var a=f.properties.access;return !a||a==='yes'||a==='public'||a==='permissive';},
    style:beachStyle(_colors.beaches_public),onEachFeature:beachPopup,
    pointToLayer:cm(_colors.beaches_public,6)});
  _mapLayers.beaches_private=gj('beaches',{
    filter:function(f){var a=f.properties.access;return a==='private'||a==='no'||a==='customers';},
    style:beachStyle(_colors.beaches_private),onEachFeature:beachPopup,
    pointToLayer:cm(_colors.beaches_private,6)});

  function accessPopup(p){
    var s=kicker('NJ public shore access')+'<b>'+esc(p.name||p.street||'Access point')+'</b>';
    var where=[];
    if(p.street)where.push(p.street);
    if(p.cross)where.push('at '+p.cross);
    if(where.length)s+='<div class="popup-row">'+esc(where.join(' '))+'</div>';
    var muni=[];
    if(p.muni)muni.push(p.muni);
    if(p.county)muni.push(p.county);
    if(muni.length)s+='<div class="popup-row">'+esc(muni.join(', '))+'</div>';
    s+=row('Type',p.accessType);
    s+=row('Shoreline',p.shoreline);
    s+=chips([['badge required',p.badge],['parking',p.parking],['swimming',p.swimming],
              ['surfing',p.surfing],['fishing',p.fishing],['pier',p.pier],
              ['boat launch',p.boatLaunch],['marina',p.marina],
              ['restrooms',p.restroom],['food',p.food],
              ['playground',p.playground],['accessible',p.accessible]]);
    if(p.notes)s+='<div class="popup-row" style="margin-top:4px;color:#666">'+esc(p.notes)+'</div>';
    return s;
  }
  _mapLayers.public_access=gjCluster('public_access',{
    pointToLayer:glyph(_colors.public_access,
      '<path d="M7 15h10M12 7v8M9 10l3-3 3 3" stroke="#fff" stroke-width="1.7" fill="none" stroke-linecap="round"/>',20),
    onEachFeature:popupOn(accessPopup)},16);

  _mapLayers.lighthouses=gj('lighthouses',{
    pointToLayer:function(f,ll){return L.marker(ll,{pane:'vectors',
      icon:L.divIcon({className:'',iconSize:[20,22],iconAnchor:[10,20],
        html:'<svg width="20" height="22" viewBox="0 0 20 22">'+
          '<polygon points="10,1 13.5,8 10,6 6.5,8" fill="'+_colors.lighthouses+'"/>'+
          '<rect x="7.6" y="8" width="4.8" height="12" fill="'+_colors.lighthouses+'" stroke="#fff" stroke-width=".6"/></svg>'})});},
    style:ps(_colors.lighthouses)});
  bp(_mapLayers.lighthouses,function(p){
    var s=kicker('Light')+'<b>'+esc(p.name||'Lighthouse')+'</b>';
    s+=row('Built',p.start_date);
    s+=row('Height',p.height);
    s+=row('Character',p['seamark:light:character']);
    s+=row('Range',p['seamark:light:range']);
    s+=row('Operator',p.operator);
    s+=links(link(p.website,'Website'),
      p.wikipedia?link('https://en.wikipedia.org/wiki/'+encodeURIComponent(p.wikipedia),'Wikipedia'):'');
    return s;
  });

  _mapLayers.boat_access=gj('boat_access',{
    pointToLayer:glyph(_colors.boat_access,
      '<path d="M5 14h14l-2 4H7zM12 5v9M12 5l5 6H7z" fill="#fff" opacity=".92"/>',20),
    style:ps(_colors.boat_access)});
  bp(_mapLayers.boat_access,function(p){
    var s=kicker(p._source==='njdep'?'NJ saltwater fishing access':'Boat access')+
      '<b>'+esc(p.name||(p.leisure==='marina'?'Marina':'Boat ramp'))+'</b>';
    s+=row('Address',p.street);
    var muni=[];if(p.muni)muni.push(p.muni);if(p.county)muni.push(p.county);
    if(muni.length)s+='<div class="popup-row">'+esc(muni.join(', '))+'</div>';
    s+=row('Shore access',p.shoreMode);
    s+=row('Type',p.leisure);
    s+=chips([['fee',p.fee]]);
    s+=links(link(p.website,'Website'));
    return s;
  });

  /* ═══ Treats & Amusements ═══ */
  _mapLayers.ice_cream=gj('ice_cream',{
    pointToLayer:glyph(_colors.ice_cream,
      '<path d="M12 20l-3.2-8h6.4zM12 4a4 4 0 0 1 4 4H8a4 4 0 0 1 4-4z" fill="#fff"/>',22)});
  bp(_mapLayers.ice_cream,function(p){
    var s=kicker('Ice cream')+'<b>'+esc(p.name||p.brand||'Ice cream')+'</b>';
    s+=addr(p);
    s+=tags([p.cuisine,p.shop==='frozen_yogurt'?'frozen yogurt':null]);
    s+=row('Hours',p.opening_hours);
    s+=chips([['outdoor seating',p.outdoor_seating],['takeaway',p.takeaway]]);
    s+=links(link(p.website,'Website'),p.phone?'<span class="popup-meta">'+esc(p.phone)+'</span>':'');
    return s;
  });

  _mapLayers.mini_golf=gj('mini_golf',{
    pointToLayer:glyph(_colors.mini_golf,
      '<path d="M11 19V6l6 3-6 3" fill="#fff"/><circle cx="8" cy="18" r="2" fill="#fff"/>',22),
    style:ps(_colors.mini_golf,'',.3)});
  bp(_mapLayers.mini_golf,function(p){
    var s=kicker('Mini golf')+'<b>'+esc(p.name||'Miniature golf')+'</b>';
    s+=addr(p);
    s+=row('Holes',p.holes);
    s+=row('Hours',p.opening_hours);
    s+=row('Operator',p.operator);
    s+=chips([['lit at night',p.lit]]);
    if(p.fee)s+=row('Fee',p.fee);
    s+=links(link(p.website,'Website'),p.phone?'<span class="popup-meta">'+esc(p.phone)+'</span>':'');
    return s;
  });

  _mapLayers.amusements=gj('amusements',{
    pointToLayer:glyph(_colors.amusements,
      '<circle cx="12" cy="12" r="5.5" fill="none" stroke="#fff" stroke-width="1.6"/>'+
      '<path d="M12 6.5v11M6.5 12h11" stroke="#fff" stroke-width="1.4"/>',22),
    style:ps(_colors.amusements,'',.3)});
  bp(_mapLayers.amusements,function(p){
    var kind=p.leisure||p.tourism||p.attraction||'';
    var s=kicker(String(kind).replace(/_/g,' ')||'Attraction')+
      '<b>'+esc(p.name||'Attraction')+'</b>';
    s+=row('Hours',p.opening_hours);
    s+=row('Operator',p.operator);
    if(p.fee)s+=row('Fee',p.fee);
    s+=links(link(p.website,'Website'));
    return s;
  });

  /* ═══ Trails & Routes ═══ */
  _mapLayers.hiking=gj('hiking',{style:ls(_colors.hiking,3,'7 4'),
    pointToLayer:cm(_colors.hiking,4)});
  bp(_mapLayers.hiking,function(p){
    var s=kicker('Trail')+'<b>'+esc(p.name||'Unnamed path')+'</b>';
    s+=row('Surface',p.surface);
    s+=row('Type',p.footway==='boardwalk'?'boardwalk':(p.highway||p.route));
    return s;
  });

  _mapLayers.bike=gj('bike',{style:ls(_colors.bike,3),pointToLayer:cm(_colors.bike,4)});
  bp(_mapLayers.bike,function(p){
    var s=kicker('Bike route')+'<b>'+esc(p.name||p.ref||'Cycleway')+'</b>';
    s+=row('Network',p.network);
    s+=row('Surface',p.surface);
    return s;
  });

  _mapLayers.nj_trails=gj('nj_trails',{style:ls(_colors.nj_trails,3)});
  bp(_mapLayers.nj_trails,function(p){
    var s=kicker('NJ statewide trail')+'<b>'+esc(p.name||p.segment||'Trail segment')+'</b>';
    s+=row('Park',p.park);
    s+=row('Blaze',p.blaze);
    s+=row('Surface',p.surface);
    s+=row('Difficulty',p.difficulty);
    if(p.miles)s+=row('Segment',(Math.round(p.miles*100)/100)+' mi');
    s+=chips([['hiking',p.hiking],['biking',p.biking],
              ['equestrian',p.equestrian],['water trail',p.waterTrail],['ADA',p.ada]]);
    s+=row('Managed by',p.operator);
    s+=links(link(p.website,'Park website'));
    return s;
  });

  _mapLayers.park_trails=gj('park_trails',{style:ls(_colors.park_trails,3,'2 3')});
  bp(_mapLayers.park_trails,function(p){
    var s=kicker('State park trail')+'<b>'+esc(p.name||'Trail')+'</b>';
    s+=row('Park',p.park||p.site);
    s+=row('Blaze',p.blazeDesc||p.blaze);
    s+=row('Surface',p.surface);
    s+=row('Difficulty',p.difficulty);
    if(p.length)s+=row('Length',p.length);
    s+=chips([['hiking',p.hiking],['biking',p.biking]]);
    s+=links(link(p.website,'Park website'));
    return s;
  });

  /* ═══ Historic ═══ */
  _mapLayers.heritage=gj('heritage',{
    pointToLayer:function(f,ll){
      var p=f.properties;
      if(p._source==='nrhp'){
        return L.circleMarker(ll,{radius:p.nhl?8:5,
          fillColor:p.nhl?'#FFD700':'#9B2335',color:p.nhl?'#8B6914':'#333',
          weight:p.nhl?2:1,fillOpacity:.9,pane:'vectors'});
      }
      return L.circleMarker(ll,{radius:5,fillColor:_colors.heritage,
        color:'#333',weight:1,fillOpacity:.85,pane:'vectors'});
    },
    style:function(f){
      return f.properties._source==='nrhp'
        ? {color:'#9B2335',weight:2,opacity:.85,fillColor:'#9B2335',fillOpacity:.12,dashArray:'4 4',pane:'vectors'}
        : {color:_colors.heritage,weight:2,opacity:.85,fillColor:_colors.heritage,fillOpacity:.15,pane:'vectors'};
    },
    onEachFeature:function(f,layer){
      var p=f.properties,s='';
      if(p._source==='nrhp'){
        s+=kicker('National Register')+'<b>'+esc(p.name||'NRHP site')+'</b>';
        if(p.nhl)s+=' <span style="color:#B8860B;font-weight:700">★ National Historic Landmark</span>';
        if(p.type)s+='<div class="popup-row" style="text-transform:capitalize">'+esc(p.type)+'</div>';
        s+=row('',p.address).replace(': ','');
        var loc=[];
        if(p.city)loc.push(p.city);
        if(p.county)loc.push(p.county+' Co.');
        if(p.state)loc.push(p.state);
        if(loc.length)s+='<div class="popup-row">'+esc(loc.join(', '))+'</div>';
        if(p.listed)s+='<div class="popup-chips"><span class="chip">Listed '+esc(p.listed)+'</span></div>';
        s+=links(link(p.nara,'NARA record'));
        if(p.refnum)s+='<div class="popup-meta" style="font-size:9px;margin-top:3px">NRIS #'+esc(p.refnum)+'</div>';
      } else {
        s+=kicker(String(p.historic||p.tourism||'Historic').replace(/_/g,' '))+
           '<b>'+esc(p.name||'Historic site')+'</b>';
        if(p.description)s+='<div class="popup-row">'+esc(p.description)+'</div>';
        if(p.inscription)s+='<div class="popup-row" style="font-style:italic">'+esc(p.inscription)+'</div>';
        if(p.start_date)s+='<div class="popup-chips"><span class="chip">Est. '+esc(p.start_date)+'</span></div>';
        s+=row('Operator',p.operator);
        s+=links(link(p.website,'Website'),
          p.wikipedia?link('https://en.wikipedia.org/wiki/'+encodeURIComponent(p.wikipedia),'Wikipedia'):'');
      }
      layer.bindPopup(s,{maxWidth:330});
    }});

  var NJ_STATUS={LISTED_INDV:'Listed on the NJ & National Registers',
    NHL_INDV:'National Historic Landmark',ELIGIBLE_INDV:'Determined eligible',
    LOCAL_LANDMARK:'Local landmark',LOCALLY_DESIGNATED_HD:'Locally designated',
    DELISTED_INDV:'Delisted'};
  function njHistPopup(p){
    var s=kicker('NJ Historic Preservation Office')+'<b>'+esc(p.name||'Historic property')+'</b>';
    if(p.altName)s+='<div class="popup-row" style="font-style:italic">'+esc(p.altName)+'</div>';
    s+=row('',p.address).replace(': ','');
    if(p.status)s+='<div class="popup-chips"><span class="chip'+
      (p.status==='NHL_INDV'?' warn':'')+'">'+esc(NJ_STATUS[p.status]||p.status)+'</span></div>';
    var period=[p.periodBegin,p.periodEnd].filter(Boolean).join('–');
    s+=row('Period of significance',period);
    s+=row('National Register',p.nrDate);
    s+=row('State Register',p.srDate);
    s+=row('Determined eligible',p.doeDate);
    s+=row('Criteria',p.criteria);
    if(flag(p.demolished)===true)s+='<div class="popup-chips"><span class="chip no">demolished</span></div>';
    if(p.notes)s+='<div class="popup-row" style="color:#666">'+esc(p.notes)+'</div>';
    return s;
  }
  _mapLayers.nj_historic=gj('nj_historic',{
    style:ps(_colors.nj_historic,'',.2),pointToLayer:cm(_colors.nj_historic,5)});
  bp(_mapLayers.nj_historic,njHistPopup);

  _mapLayers.nj_historic_dist=gj('nj_historic_dist',{
    style:ps(_colors.nj_historic_dist,'5 3',.12),
    pointToLayer:cm(_colors.nj_historic_dist,5)});
  bp(_mapLayers.nj_historic_dist,njHistPopup);

  /* Old roads. Solid casing for the colonial alignments, dashes for routes
     that only carry a historic name or a superseded number. */
  var ROAD_CLASS={
    kings:{w:5,dash:'',label:"King's road / colonial alignment"},
    historic:{w:4,dash:'6 4',label:'Historic road'},
    pike:{w:5,dash:'',label:'Named turnpike / plank road'},
    renamed:{w:4,dash:'9 5',label:'Renamed or renumbered'},
    route:{w:4,dash:'',label:'Original through route'}
  };
  function roadStyle(color){
    return function(f){
      var c=ROAD_CLASS[f.properties._class]||ROAD_CLASS.historic;
      return {color:color,weight:c.w,opacity:.85,dashArray:c.dash,fill:false,
        pane:'vectors',lineCap:'round'};
    };
  }
  function roadPopup(p){
    var c=ROAD_CLASS[p._class]||ROAD_CLASS.historic;
    var s=kicker(c.label)+'<b>'+esc(p.name||p.ref||'Unnamed way')+'</b>';
    s+=row('Former name',p.old_name);
    s+=row('Former route',p.old_ref);
    s+=row('Current route',p.ref);
    s+=row('Class',p.highway||p['abandoned:highway']||p['was:highway']);
    s+=row('Surface',p.surface);
    if(p.description)s+='<div class="popup-row">'+esc(p.description)+'</div>';
    if(p.note)s+='<div class="popup-row" style="color:#666">'+esc(p.note)+'</div>';
    s+=links(p.wikipedia?link('https://en.wikipedia.org/wiki/'+encodeURIComponent(p.wikipedia),'Wikipedia'):'');
    return s;
  }
  _mapLayers.kings_roads=gj('kings_roads',{style:roadStyle(_colors.kings_roads),
    pointToLayer:cm(_colors.kings_roads,4)});
  bp(_mapLayers.kings_roads,roadPopup);

  _mapLayers.orig_highways=gj('orig_highways',{style:roadStyle(_colors.orig_highways),
    pointToLayer:cm(_colors.orig_highways,4)});
  bp(_mapLayers.orig_highways,roadPopup);

  _mapLayers.old_rail=gj('old_rail',{style:ls(_colors.old_rail,3,'10 4 2 4')});
  bp(_mapLayers.old_rail,function(p){
    var state=p.railway||p['abandoned:railway']||p['was:railway']||'abandoned';
    var s=kicker('Rail grade — '+esc(state))+
      '<b>'+esc(p.name||p.old_name||'Former railway')+'</b>';
    s+=row('Operator',p.operator);
    s+=row('Opened',p.start_date);
    s+=row('Closed',p.end_date);
    if(p.description)s+='<div class="popup-row">'+esc(p.description)+'</div>';
    s+=links(p.wikipedia?link('https://en.wikipedia.org/wiki/'+encodeURIComponent(p.wikipedia),'Wikipedia'):'');
    return s;
  });

  /* Shorelines shade oldest to newest so island migration reads at a glance. */
  var SHORE_ERA=[[1840,'#6B3410'],[1875,'#8C4A18'],[1900,'#A85E22'],
                 [1930,'#C77B3F'],[1970,'#D9A15E'],[2100,'#E8C48C']];
  function shoreColor(y){
    var n=parseInt(y,10);
    if(isNaN(n))return '#C77B3F';
    for(var i=0;i<SHORE_ERA.length;i++){if(n<=SHORE_ERA[i][0])return SHORE_ERA[i][1];}
    return '#E8C48C';
  }
  _mapLayers.hist_shoreline=gj('hist_shoreline',{
    style:function(f){return{color:shoreColor(f.properties.year),weight:2.5,
      opacity:.9,fill:false,pane:'vectors'};}});
  bp(_mapLayers.hist_shoreline,function(p){
    return kicker('Historical shoreline')+'<b>'+esc(p.year||'Undated')+'</b>'+
      '<div class="popup-meta">NJDEP shoreline change series</div>';
  });

  /* ═══ Protected Lands ═══ */
  var PADUS_ACCESS={OA:'Open access',RA:'Restricted access',XA:'Closed',UK:'Unknown'};
  var PADUS_GAP={'1':'GAP 1 — permanent, natural disturbance intact',
    '2':'GAP 2 — permanent, managed disturbance','3':'GAP 3 — multiple use',
    '4':'GAP 4 — no known mandate'};
  _mapLayers.federal_lands=gj('federal_lands',{style:ps(_colors.federal_lands,'',.18),
    pointToLayer:cm(_colors.federal_lands,6)});
  bp(_mapLayers.federal_lands,function(p){
    var s=kicker('Federal protected land — PAD-US')+
      '<b>'+esc(p.name||'Federal land')+'</b>';
    s+=row('Designation',p.designation||p.localDesignation);
    s+=row('Managed by',p.manager);
    s+=row('Owner',p.owner);
    if(p.acres)s+=row('Area',Math.round(p.acres).toLocaleString()+' acres');
    s+=row('Established',p.established);
    s+=row('Public access',PADUS_ACCESS[p.access]||p.access);
    s+=row('Protection',PADUS_GAP[String(p.gap)]||p.gap);
    s+=row('IUCN category',p.iucn);
    return s;
  });

  _mapLayers.refuges_fws=gj('refuges_fws',{style:ps(_colors.refuges_fws,'',.16),
    pointToLayer:cm(_colors.refuges_fws,6)});
  bp(_mapLayers.refuges_fws,function(p){
    var s=kicker('National Wildlife Refuge System')+
      '<b>'+esc(p.name||'Refuge')+'</b>';
    s+=row('Interest type',p.type);
    s+=row('Unit code',p.unit);
    s+=row('FWS region',p.region);
    return s;
  });

  _mapLayers.fws_wilderness=gj('fws_wilderness',{style:ps(_colors.fws_wilderness,'3 4',.2)});
  bp(_mapLayers.fws_wilderness,function(p){
    var s=kicker('Designated wilderness')+'<b>'+esc(p.name||'Wilderness')+'</b>';
    s+=row('Refuge',p.refuge);
    if(p.acres)s+=row('Area',Math.round(p.acres).toLocaleString()+' acres');
    s+=row('Designated',p.designated);
    s+=row('Public law',p.publicLaw);
    return s;
  });

  _mapLayers.state_lands=gj('state_lands',{style:ps(_colors.state_lands,'',.18),
    pointToLayer:cm(_colors.state_lands,6)});
  bp(_mapLayers.state_lands,function(p){
    var s=kicker('New Jersey state land')+'<b>'+esc(p.name||'State land')+'</b>';
    s+=row('Use',p.use||p.ownershipUse);
    s+=row('Managed by',p.manager);
    s+=row('Agency',p.agency);
    var muni=[];if(p.muni)muni.push(p.muni);if(p.county)muni.push(p.county);
    if(muni.length)s+='<div class="popup-row">'+esc(muni.join(', '))+'</div>';
    s+=chips([['public access',p.access],['parking',p.parking],['hunting',p.hunting]]);
    s+=links(link(p.website,'Facility'),link(p.trailMap,'Trail map'),
             link(p.agencyUrl,'Agency'));
    if(p.phone)s+='<div class="popup-meta">'+esc(p.phone)+'</div>';
    return s;
  });

  _mapLayers.natural_areas=gj('natural_areas',{style:ps(_colors.natural_areas,'2 3',.22)});
  bp(_mapLayers.natural_areas,function(p){
    var s=kicker('NJ State Natural Area')+'<b>'+esc(p.name||'Natural area')+'</b>';
    if(p.acres)s+=row('Area',Math.round(p.acres).toLocaleString()+' acres');
    s+=row('Facility',p.facility);
    var muni=[];if(p.muni)muni.push(p.muni);if(p.county)muni.push(p.county);
    if(muni.length)s+='<div class="popup-row">'+esc(muni.join(', '))+'</div>';
    return s;
  });

  var BIODIV={B1:'B1 — outstanding significance',B2:'B2 — very high',
    B3:'B3 — high',B4:'B4 — moderate',B5:'B5 — of general significance'};
  _mapLayers.nhp_sites=gj('nhp_sites',{style:ps(_colors.nhp_sites,'4 3',.2)});
  bp(_mapLayers.nhp_sites,function(p){
    var s=kicker('Natural Heritage Priority Site')+'<b>'+esc(p.name||'Priority site')+'</b>';
    if(p.biodivRank)s+='<div class="popup-chips"><span class="chip warn">'+
      esc(BIODIV[p.biodivRank]||p.biodivRank)+'</span></div>';
    s+=row('Class',p.siteClass);
    var muni=[];if(p.muni)muni.push(p.muni);if(p.county)muni.push(p.county);
    if(muni.length)s+='<div class="popup-row">'+esc(muni.join(', '))+'</div>';
    if(p.description)s+='<div class="popup-row">'+esc(p.description)+'</div>';
    if(p.biodivComment)s+='<div class="popup-row" style="color:#666">'+esc(p.biodivComment)+'</div>';
    return s;
  });

  _mapLayers.focal_areas=gj('focal_areas',{style:ps(_colors.focal_areas,'6 4',.14)});
  bp(_mapLayers.focal_areas,function(p){
    var s=kicker('Conservation Focal Area — '+esc(p.region||''))+
      '<b>'+esc(p.name||'Focal area')+'</b>';
    if(p.acres)s+=row('Area',Math.round(p.acres).toLocaleString()+' acres');
    if(p.description)s+='<div class="popup-row">'+esc(p.description)+'</div>';
    return s;
  });

  _mapLayers.state_parks=gj('state_parks',{style:ps(_colors.state_parks),
    pointToLayer:cm(_colors.state_parks,6)});
  bp(_mapLayers.state_parks,function(p){
    var s=kicker('State park')+'<b>'+esc(p.name||'Park')+'</b>';
    s+=row('Designation',p.protection_title);
    s+=row('Operator',p.operator);
    s+=links(link(p.website,'Website'));
    return s;
  });

  var PROTECT_CLASS={'1':'Strict nature reserve','1a':'Strict nature reserve',
    '1b':'Wilderness area','2':'National park','3':'Natural monument',
    '4':'Habitat / species management','5':'Protected landscape',
    '6':'Sustainable use of natural resources'};
  _mapLayers.refuges=gj('refuges',{style:ps(_colors.refuges,'',.14),
    pointToLayer:cm(_colors.refuges,6)});
  bp(_mapLayers.refuges,function(p){
    var s=kicker('Protected area — OSM')+'<b>'+esc(p.name||'Protected area')+'</b>';
    s+=row('Designation',p.protection_title||p.designation);
    s+=row('IUCN class',PROTECT_CLASS[String(p.protect_class)]||p.protect_class);
    s+=row('Operator',p.operator);
    s+=row('Ownership',p.ownership);
    s+=row('Hours',p.opening_hours);
    s+=links(link(p.website,'Website'),
      p.wikipedia?link('https://en.wikipedia.org/wiki/'+encodeURIComponent(p.wikipedia),'Wikipedia'):'');
    return s;
  });

  _mapLayers.forests=gj('forests',{style:ps(_colors.forests,'',.14),
    pointToLayer:cm(_colors.forests,6)});
  bp(_mapLayers.forests,function(p){
    var s=kicker('Forest')+'<b>'+esc(p.name||'Forest')+'</b>';
    s+=row('Designation',p.protection_title);
    s+=row('Operator',p.operator);
    s+=row('Leaf type',p.leaf_type);
    return s;
  });

  /* ═══ Pine Barrens ═══ */
  _mapLayers.pnr=gj('pnr',{style:function(){return{color:_colors.pnr,weight:3,
    opacity:.9,fillColor:_colors.pnr,fillOpacity:.07,dashArray:'12 5',pane:'vectors'};}});
  bp(_mapLayers.pnr,function(p){
    var s=kicker('Pinelands National Reserve')+
      '<b>'+esc(p.name||'Pinelands National Reserve')+'</b>';
    s+=row('Designation',p.designation||p.protection_title);
    s+=row('Administered by',p.operator);
    s+='<div class="popup-row" style="margin-top:4px;color:#555">The first National '+
       'Reserve in the United States, designated 1978 — roughly 1.1 million acres '+
       'over the Kirkwood-Cohansey aquifer.</div>';
    s+=links(link(p.website||'https://www.nj.gov/pinelands/','Pinelands Commission'));
    return s;
  });

  var MGMT_TINT={'Preservation Area District':'#14401F','Forest Area':'#256B34',
    'Agricultural Production Area':'#B8A13A','Special Agricultural Production Area':'#C9B457',
    'Rural Development Area':'#8FA85A','Regional Growth Area':'#C4703A',
    'Pinelands Village':'#9B5B8A','Pinelands Town':'#7D4A6E',
    'Military and Federal Installation Area':'#5A6B7D'};
  _mapLayers.pinelands_mgmt=gj('pinelands_mgmt',{
    style:function(f){
      var c=MGMT_TINT[f.properties.name]||_colors.pinelands_mgmt;
      return{color:c,weight:1.5,opacity:.85,fillColor:c,fillOpacity:.25,pane:'vectors'};
    }});
  bp(_mapLayers.pinelands_mgmt,function(p){
    var s=kicker('Pinelands management area')+
      '<b>'+esc(p.name||p.altName||'Management area')+'</b>';
    s+=row('Code',p.code);
    if(p.acres)s+=row('Area',Math.round(p.acres).toLocaleString()+' acres');
    return s;
  });

  /* ═══ Significant Habitat ═══ */
  var LNDR={5:'Rank 5 \u2014 federally listed species',
            4:'Rank 4 \u2014 State endangered',
            3:'Rank 3 \u2014 State threatened',
            2:'Rank 2 \u2014 special concern',
            1:'Rank 1 \u2014 species occurrence'};
  var RANK_TONE={5:'#5B2C6F',4:'#7D3C98',3:'#A569BD',2:'#C39BD3',1:'#D7BDE2'};
  _mapLayers.sig_habitat=gj('sig_habitat',{
    style:function(f){
      var c=RANK_TONE[f.properties.rank]||_colors.sig_habitat;
      return{color:c,weight:.8,opacity:.6,fillColor:c,fillOpacity:.18,pane:'vectors'};
    }});
  bp(_mapLayers.sig_habitat,function(p){
    var s=kicker('Significant habitat \u2014 NJDEP Landscape Project v'+(p.version||'3.4'))+
      '<b>'+esc(LNDR[p.rank]||('Rank '+p.rank))+'</b>';
    s+=row('Region',p.region);
    s+=row('Land cover',p.landCover||p.coverType);
    if(p.acres)s+=row('Patch',Math.round(p.acres).toLocaleString()+' acres');
    s+=chips([['riparian',p.riparian],['forest core',p.forestCore]]);
    s+='<div class="popup-meta" style="margin-top:4px">Rank reflects the '+
       'highest-status species documented using the patch.</div>';
    return s;
  });

  _mapLayers.stream_habitat=gj('stream_habitat',{
    style:function(f){
      var c=RANK_TONE[f.properties.rank]||_colors.stream_habitat;
      return{color:c,weight:3,opacity:.9,fill:false,pane:'vectors',lineCap:'round'};
    }});
  bp(_mapLayers.stream_habitat,function(p){
    var s=kicker('Stream habitat \u2014 Landscape Project')+
      '<b>'+esc(p.name||'Unnamed reach')+'</b>';
    s+=row('Rank',LNDR[p.rank]||p.rank);
    s+=row('Region',p.region);
    return s;
  });

  var VP_STATUS={C:'Confirmed vernal pool',P:'Potential vernal pool',
                 D:'Documented, not field verified'};
  _mapLayers.vernal_pools=gjCluster('vernal_pools',{
    pointToLayer:glyph(_colors.vernal_pools,
      '<ellipse cx="12" cy="13" rx="7" ry="4.5" fill="none" stroke="#fff" stroke-width="1.6"/>'+
      '<path d="M12 4v5" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/>',20),
    style:function(){return{color:_colors.vernal_pools,weight:1.2,opacity:.8,
      fillColor:_colors.vernal_pools,fillOpacity:.25,pane:'vectors'};},
    onEachFeature:popupOn(function(p){
      var pool=p._kind==='pool';
      var s=kicker(pool?'Vernal pool':'Vernal habitat')+
        '<b>'+esc(pool?(VP_STATUS[p.status]||p.status||'Vernal pool')
                      :(LNDR[p.rank]||'Vernal habitat'))+'</b>';
      s+=row('Region',p.region);
      if(p.acres)s+=row('Area',Math.round(p.acres).toLocaleString()+' acres');
      s+=row('ID',p.code);
      return s;
    })},14);

  _mapLayers.critical_habitat=gj('critical_habitat',{
    style:function(f){
      var line=f.geometry&&f.geometry.type&&f.geometry.type.indexOf('Line')>=0;
      return line
        ? {color:_colors.critical_habitat,weight:4,opacity:.9,fill:false,pane:'vectors',lineCap:'round'}
        : {color:_colors.critical_habitat,weight:2,opacity:.9,
           fillColor:_colors.critical_habitat,fillOpacity:.18,dashArray:'6 3',pane:'vectors'};
    },
    pointToLayer:cm(_colors.critical_habitat,6)});
  bp(_mapLayers.critical_habitat,function(p){
    var s=kicker('ESA designated critical habitat \u2014 '+
      (p._source==='nmfs'?'NOAA Fisheries':'USFWS'))+
      '<b>'+esc(p.name||'Critical habitat')+'</b>';
    if(p.sciName)s+='<div class="popup-row" style="font-style:italic;color:#666">'+esc(p.sciName)+'</div>';
    s+=row('Listing',p.listing||p.status);
    s+=row('Population',p.entity);
    s+=row('Unit',p.unit);
    s+=row('Habitat type',p.habitatType);
    s+=row('Effective',p.effective);
    s+=links(link(p.url,'InPort record'));
    return s;
  });

  /* ═══ Marine & Estuarine ═══ */
  var MPA_TONE={Federal:'#00527A',State:'#00857A',Partnership:'#6A4C93',Local:'#8B7355'};
  _mapLayers.mpa=gj('mpa',{
    style:function(f){
      var c=MPA_TONE[f.properties.govLevel]||_colors.mpa;
      return{color:c,weight:2,opacity:.9,fillColor:c,fillOpacity:.16,pane:'vectors'};
    }});
  bp(_mapLayers.mpa,function(p){
    var s=kicker('Marine protected area — NOAA inventory')+
      '<b>'+esc(p.name||'MPA')+'</b>';
    s+=tags([p.govLevel,p.designation,p.iucn?'IUCN '+p.iucn:null]);
    s+=row('Protection level',p.protLevel);
    s+=row('Managing agency',p.agency);
    s+=row('Fishing restrictions',p.fishing);
    s+=row('Protection focus',p.focus);
    s+=row('Conservation focus',p.consFocus);
    s+=row('Permanence',p.permanence);
    s+=row('Constancy',p.constancy);
    s+=row('Anchoring',p.anchoring);
    s+=row('Vessel restrictions',p.vessel);
    s+=row('Established',p.established);
    if(p.areaKm2)s+=row('Area',(Math.round(p.areaKm2*10)/10).toLocaleString()+' km²');
    s+=links(link(p.url,'Site page'));
    return s;
  });

  _mapLayers.nerrs=gj('nerrs',{style:ps(_colors.nerrs,'4 6',.12)});
  bp(_mapLayers.nerrs,function(p){
    var s=kicker('National Estuarine Research Reserve')+
      '<b>'+esc(p.name||'Estuarine reserve')+'</b>';
    s+=row('Protection level',p.protLevel||p.protection_title);
    s+=row('Managing agency',p.agency||p.operator);
    s+=row('Established',p.established);
    s+=links(link(p.url||p.website,'Reserve site'));
    return s;
  });

  var SHELLFISH_TONE={Approved:'#2E8B57','Seasonal':'#B8860B',
    'Special Restricted':'#CC6600','Restricted':'#CC6600',Prohibited:'#A93226',
    'Condemned':'#A93226'};
  function shellfishTone(status){
    var s=String(status||'');
    for(var k in SHELLFISH_TONE){if(s.indexOf(k)>=0)return SHELLFISH_TONE[k];}
    return _colors.shellfish;
  }
  _mapLayers.shellfish=gj('shellfish',{
    style:function(f){
      var c=shellfishTone(f.properties.status);
      return{color:c,weight:1,opacity:.7,fillColor:c,fillOpacity:.22,pane:'vectors'};
    }});
  bp(_mapLayers.shellfish,function(p){
    var s=kicker('Shellfish harvest classification')+
      '<b>'+esc(p.status||'Unclassified')+'</b>';
    if(p.acres)s+=row('Area',Math.round(p.acres).toLocaleString()+' acres');
    s+='<div class="popup-meta" style="margin-top:4px">NJDEP Bureau of Marine '+
       'Water Monitoring. Check the current notice before harvesting.</div>';
    return s;
  });

  _mapLayers.reefs=gj('reefs',{style:ps(_colors.reefs,'3 3',.25),
    pointToLayer:cm(_colors.reefs,6)});
  bp(_mapLayers.reefs,function(p){
    var s=kicker('Artificial reef site')+'<b>'+esc(p.name||'Reef')+'</b>';
    s+=links(link(p.url,'NJDEP reef page'));
    return s;
  });

  _mapLayers.tide_stations=gj('tide_stations',{
    pointToLayer:glyph(_colors.tide_stations,
      '<path d="M4 14c2.5-3 5.5-3 8 0s5.5 3 8 0" stroke="#fff" stroke-width="1.7" fill="none" stroke-linecap="round"/>'+
      '<path d="M4 18c2.5-3 5.5-3 8 0s5.5 3 8 0" stroke="#fff" stroke-width="1.4" fill="none" stroke-linecap="round" opacity=".7"/>',20)});
  bp(_mapLayers.tide_stations,function(p){
    var s=kicker('NOAA tide predictions')+'<b>'+esc(p.name||'Station')+'</b>';
    s+=row('Station',p.stationId);
    if(p.stationId){
      s+=links(link('https://tidesandcurrents.noaa.gov/noaatidepredictions.html?id='+
        encodeURIComponent(p.stationId),'Predictions'),
        link('https://tidesandcurrents.noaa.gov/stationhome.html?id='+
        encodeURIComponent(p.stationId),'Station home'));
    }
    return s;
  });

  /* ═══ Wetlands ═══ */
  var WETLAND_TONE={saltmarsh:'#2E8B77',tidalflat:'#4FA3A0',marsh:'#3D8B7D',
    swamp:'#2F6B4F',bog:'#6B7A3A',wet_meadow:'#5D9C6B',mangrove:'#1F6B54',
    reedbed:'#7A9E6B',fen:'#4F8C6B'};
  _mapLayers.wetlands_osm=gj('wetlands_osm',{
    style:function(f){
      var c=WETLAND_TONE[f.properties.wetland]||_colors.wetlands_osm;
      return{color:c,weight:1,opacity:.7,fillColor:c,fillOpacity:.3,pane:'vectors'};
    },
    pointToLayer:cm(_colors.wetlands_osm,4)});
  bp(_mapLayers.wetlands_osm,function(p){
    var kind=String(p.wetland||p.natural||'wetland').replace(/_/g,' ');
    var s=kicker(kind)+'<b>'+esc(p.name||'Wetland')+'</b>';
    s+=chips([['tidal',p.tidal],['saline',p.salt]]);
    s+=row('Operator',p.operator);
    if(p.description)s+='<div class="popup-row">'+esc(p.description)+'</div>';
    return s;
  });

  /* ═══ Wildlife ═══ */
  _mapLayers.hotspots=gj('hotspots',{
    pointToLayer:function(f,ll){return L.circleMarker(ll,{radius:7,
      fillColor:_colors.hotspots,color:'#fff',weight:2,fillOpacity:.9,pane:'vectors'});}});
  bp(_mapLayers.hotspots,function(p){
    var s=kicker('eBird hotspot')+'<b>'+esc(p.name||'Hotspot')+'</b>';
    s+='<div class="popup-meta">'+p.numSpecies+' species all-time'+
       (p.latestObs?'<br>Latest: '+esc(p.latestObs):'')+'</div>';
    if(p.locId)s+=links(link('https://ebird.org/hotspot/'+encodeURIComponent(p.locId),'View on eBird'));
    return s;
  });

  _mapLayers.inat_rare=gjCluster('inat_rare',{
    pointToLayer:function(f,ll){return L.circleMarker(ll,{radius:6,
      fillColor:_colors.inat_rare,color:'#fff',weight:2,fillOpacity:.9,pane:'vectors'});},
    onEachFeature:popupOn(function(p){
      var s=kicker('Threatened species observation')+'<b>'+esc(p.name||p.sciName)+'</b>';
      if(p.sciName)s+='<div class="popup-row" style="font-style:italic;color:#666">'+esc(p.sciName)+'</div>';
      if(p.status)s+='<div class="popup-chips"><span class="chip no">'+esc(p.status)+'</span></div>';
      s+=row('Observed',p.observedOn);
      s+=links(link(p.uri,'View on iNaturalist'));
      return s;
    })},15);

  var obsCluster=L.markerClusterGroup({maxClusterRadius:45,
    showCoverageOnHover:false,iconCreateFunction:clusterIcon});
  gj('ebird_obs',{
    pointToLayer:function(f,ll){return L.circleMarker(ll,{radius:4,
      fillColor:_colors.ebird_obs,color:'#fff',weight:1,fillOpacity:.8,pane:'vectors'});},
    onEachFeature:function(f,layer){
      var p=f.properties,list=p.species_list||[];
      var s='<div style="max-width:320px">'+kicker('eBird observations')+
        '<b>'+esc(p.locName||'Observation')+'</b><div class="popup-meta">'+
        list.length+' species · latest '+esc(p.latestDate||'')+'</div>';
      if(p.locId)s+=links(link('https://ebird.org/hotspot/'+encodeURIComponent(p.locId),'View on eBird'));
      s+='<div style="max-height:220px;overflow-y:auto;margin-top:5px">';
      var show=Math.min(list.length,12);
      for(var i=0;i<show;i++){
        var sp=list[i];
        var nameHtml=sp.subId
          ? '<a href="https://ebird.org/checklist/'+encodeURIComponent(sp.subId)+
            '" target="_blank" rel="noopener" class="popup-species" style="text-decoration:none">'+esc(sp.species)+'</a>'
          : '<span class="popup-species">'+esc(sp.species)+'</span>';
        s+='<div style="font-size:11px;padding:2px 0;border-bottom:1px solid #eee">'+nameHtml+
           ' <i style="color:#888">'+esc(sp.sciName||'')+'</i>'+
           (sp.howMany>1?' ('+sp.howMany+')':'')+
           (sp.obsDt?'<span style="float:right;color:#999;font-size:10px">'+
             esc(String(sp.obsDt).split(' ')[0])+'</span>':'')+'</div>';
      }
      if(list.length>12)s+='<div class="popup-meta" style="padding:3px 0">+ '+
        (list.length-12)+' more species</div>';
      s+='</div></div>';
      layer.bindPopup(s,{maxWidth:340,maxHeight:330});
    }}).addTo(obsCluster);
  _mapLayers.ebird_obs=obsCluster;

  /* ═══ Live conditions ═══ */
  _mapLayers.wildfires=gj('wildfires',{
    style:function(){return{color:_colors.wildfires,weight:2,opacity:.9,
      fillColor:_colors.wildfires,fillOpacity:.35,pane:'vectors'};},
    pointToLayer:cm(_colors.wildfires,7,'#fff')});
  bp(_mapLayers.wildfires,function(p){
    var s=kicker('Active wildfire — NIFC WFIGS')+
      '<b style="color:#C0392B">'+esc(p.name||'Active fire')+'</b>';
    if(p.acres)s+='<div class="popup-row" style="font-weight:600">'+
      Number(p.acres).toLocaleString()+' acres</div>';
    s+=row('Behavior',p.behavior);
    if(p.containment!=null)s+=row('Containment',p.containment+'%');
    return s;
  });

  var SMOKE_TONE={Heavy:'#8B0000',Medium:'#CC6600',Light:'#DAA520'};
  _mapLayers.smoke=gj('smoke',{
    style:function(f){
      var c=SMOKE_TONE[f.properties.density]||'#DAA520';
      return{color:c,weight:1,opacity:.6,fillColor:c,fillOpacity:.2,
        dashArray:'4 4',pane:'vectors'};
    }});
  bp(_mapLayers.smoke,function(p){
    var c=SMOKE_TONE[p.density]||'#999';
    return kicker('NOAA HMS satellite smoke')+
      '<b style="color:'+c+'">Smoke plume</b>'+
      '<div class="popup-row">Density: <b>'+esc(p.density||'Unknown')+'</b></div>';
  });

  /* ═══ Raster overlays ═══ */
  for(var rk in _rasterDefs){
    var d=_rasterDefs[rk];
    var opts={opacity:d.opacity||0.6,pane:'tileOverlays',
      attribution:d.attribution||'',maxZoom:19,crossOrigin:false,
      cacheKey:d.cacheKey||rk};
    if(d.minNativeZoom!=null)opts.minNativeZoom=d.minNativeZoom;
    if(d.maxNativeZoom!=null)opts.maxNativeZoom=d.maxNativeZoom;
    if(d.service==='noaa_wmts'){
      _mapLayers[rk]=_tileFallback(new NoaaChart(d.url,opts));
    } else {
      opts.exportParams=d.params||{};
      _mapLayers[rk]=_tileFallback(new EsriDynamic(d.url,opts));
    }
  }

  /* ── Turn on defaults, frame the search area, drop the base marker ── */
  var defaults=__DEFAULTS_OBJ__;
  for(var zi=0;zi<_zorder.length;zi++){
    var dk=_zorder[zi];
    if(defaults[dk]&&_mapLayers[dk])_mapLayers[dk].addTo(_map);
  }
  for(var k in _mapLayers){                      // rasters and anything unranked
    if(defaults[k]&&!_map.hasLayer(_mapLayers[k]))_mapLayers[k].addTo(_map);
  }
  _restack();
  _map.fitBounds([[__SOUTH__,__WEST__],[__NORTH__,__EAST__]]);

  /* Layers that are on by default but held back from the HTML: fetch them now
     so the map paints immediately and the geometry streams in behind it. */
  for(var dk in _deferred){if(defaults[dk])_loadDeferred(dk);}

  L.marker([__BASE_LAT__,__BASE_LNG__],{zIndexOffset:1000,
    icon:L.divIcon({className:'base-star',iconSize:[60,30],iconAnchor:[30,15],
      html:'<div style="text-align:center"><span style="font-size:15px;color:#1A6B9A;'+
        'text-shadow:0 0 3px #fff,0 0 3px #fff">&#9733;</span>'+
        '<div style="font-size:8px;font-weight:600;color:#333;white-space:nowrap;'+
        'margin-top:-3px;text-shadow:0 0 3px #fff,0 0 3px #fff">__BASE_LABEL__</div></div>'})
  }).addTo(_map);

  /* Overlapping conservation polygons are the norm here — a vernal pool sits
     inside significant habitat inside a refuge inside the Pinelands Reserve.
     Leaflet's canvas reports only the topmost shape, so whichever layer is
     drawn last wins the click and the rest are unreachable. Gather everything
     under the cursor and show it all.

     This is bound to the layers rather than the map: Leaflet's own popup
     handler calls DomEvent.stop(), so the map's own click never fires once a
     feature popup opens. Binding on the layers means we run just after that
     popup opens and can replace it. */
  function _multiClick(e){
    var oe=e.originalEvent;
    if(oe){ if(oe.__lbiMulti)return; oe.__lbiMulti=true; }
    var pt=_map.latLngToLayerPoint(e.latlng),hits=[],cap=12;
    for(var i=_zorder.length-1;i>=0&&hits.length<cap;i--){
      var key=_zorder[i],ly=_mapLayers[key];
      if(!ly||!_map.hasLayer(ly)||!ly.eachLayer)continue;
      (function(k){
        ly.eachLayer(function(l){
          if(hits.length>=cap)return;
          if(!l._containsPoint||!l.getPopup||!l.getPopup())return;
          /* A shape only has projected geometry once it has been drawn. Markers
             collapsed inside a cluster have none, and _containsPoint throws on
             them — which previously killed this whole handler. */
          if(!l._point&&!(l._parts&&l._parts.length)&&!(l._rings&&l._rings.length))return;
          var inside=false;
          try{inside=l._containsPoint(pt);}catch(err){return;}
          if(inside)hits.push({key:k,html:l.getPopup().getContent()});
        });
      })(key);
    }
    if(hits.length<2)return;                 // one hit: its own popup is fine
    var s='<div class="multi-head">'+hits.length+
          (hits.length>=cap?'+':'')+' features here</div>';
    for(var h=0;h<hits.length;h++){
      s+='<div class="multi-item"><span class="multi-layer">'+
         (_layerLabels[hits[h].key]||hits[h].key)+'</span>'+hits[h].html+'</div>';
    }
    _map.closePopup();
    L.popup({maxWidth:350,maxHeight:420,autoPanPadding:[20,20]})
      .setLatLng(e.latlng).setContent(s).openOn(_map);
  }
  for(var mk in _mapLayers){
    if(_mapLayers[mk]&&typeof _mapLayers[mk].on==='function')
      _mapLayers[mk].on('click',_multiClick);
  }

  setTimeout(function(){_map.invalidateSize();},250);
}

/* Leaflet stacks by insertion order within a pane, so a layer switched on later
   lands on top of everything and steals clicks from the smaller features under
   it. Re-assert the intended order after any change. */
function _restack(){
  if(!_map)return;
  for(var i=0;i<_zorder.length;i++){
    var l=_mapLayers[_zorder[i]];
    if(l&&_map.hasLayer(l)&&typeof l.bringToFront==='function')l.bringToFront();
  }
}

/* Layers held back from the page load are fetched the first time they are
   switched on. The layer object already exists with its styling and popups
   wired, so the data is simply poured into it. */
function _loadDeferred(key,cb){
  if(_deferredState[key]==='ready'){cb&&cb();return;}
  var lbl=document.querySelector('.map-layer-toggle input[data-layer="'+key+'"]');
  var row=lbl&&lbl.parentNode,cnt=row&&row.querySelector('.map-layer-count');
  if(_deferredState[key]==='loading')return;
  _deferredState[key]='loading';
  if(cnt){cnt.dataset.n=cnt.textContent;cnt.textContent='…';}
  fetch(_deferred[key])
    .then(function(r){if(!r.ok)throw new Error(r.status);return r.json();})
    .then(function(gjson){
      var ly=_mapLayers[key];
      if(typeof ly._lbiFill==='function')ly._lbiFill(gjson);
      else ly.addData(gjson);
      _deferredState[key]='ready';
      if(_map&&_map.hasLayer(ly))_restack();
      if(cnt)cnt.textContent=cnt.dataset.n||cnt.textContent;
      cb&&cb();
    })
    .catch(function(err){
      _deferredState[key]=null;
      if(cnt)cnt.textContent=cnt.dataset.n||'!';
      if(lbl)lbl.checked=false;
      /* Opening the page straight off disk blocks fetch; it needs a server. */
      console.warn('Could not load '+key+': '+err.message);
    });
}

function toggleMapLayer(key,on){
  if(!_map||!_mapLayers[key])return;
  if(on&&_deferred[key]&&_deferredState[key]!=='ready'){
    _loadDeferred(key,function(){
      _mapLayers[key].addTo(_map);
      _restack();
      if(typeof updateSheetCount==='function')updateSheetCount();
    });
    return;
  }
  if(on)_mapLayers[key].addTo(_map);else _map.removeLayer(_mapLayers[key]);
  _restack();
}

/* Turn every layer in one sidebar category on or off together. */
function setGroupLayers(group,on){
  var boxes=document.querySelectorAll(
    '.map-layer-toggle[data-group="'+group+'"] input[type=checkbox]');
  for(var i=0;i<boxes.length;i++){
    boxes[i].checked=on;
    var key=boxes[i].getAttribute('data-layer');
    if(_mapLayers[key]){
      if(on)_mapLayers[key].addTo(_map);else _map.removeLayer(_mapLayers[key]);
    }
  }
  _restack();
  var d=document.querySelector('.map-layer-toggle[data-group="'+group+'"]');
  if(on&&d&&d.closest('details'))d.closest('details').open=true;
}

function setAllMapLayers(on){
  var boxes=document.querySelectorAll('.map-layer-toggle input[type=checkbox]');
  for(var i=0;i<boxes.length;i++){
    boxes[i].checked=on;
    toggleMapLayer(boxes[i].getAttribute('data-layer'),on);
  }  _restack();
}

function resetMapLayers(){
  var defaults=__DEFAULTS_OBJ__;
  var boxes=document.querySelectorAll('.map-layer-toggle input[type=checkbox]');
  for(var i=0;i<boxes.length;i++){
    var key=boxes[i].getAttribute('data-layer'),want=!!defaults[key];
    boxes[i].checked=want;
    toggleMapLayer(key,want);
  }  _restack();
}
"""

CREDITS = (
    "Data: OpenStreetMap contributors · NJDEP Bureau of GIS · "
    "NJ Pinelands Commission · NJ Historic Preservation Office · "
    "NPS National Register of Historic Places · USGS PAD-US 4.1 · "
    "USFWS National Wildlife Refuge System &amp; National Wetlands Inventory · "
    "NOAA Marine Protected Areas Inventory · NOAA Office of Coast Survey (ENC) · "
    "NOAA CO-OPS · NOAA NCEI · NOAA HMS · NIFC WFIGS · FEMA NFHL · "
    "eBird (Cornell Lab of Ornithology) · iNaturalist. "
    "Basemap tiles: CARTO, OpenStreetMap, OpenTopoMap, Esri."
)


def _count_for(key: str, layers: dict) -> int:
    """Sidebar counts. Beaches share one payload split by access tag."""
    if key.startswith("beaches_"):
        feats = layers.get("beaches", EMPTY_FC).get("features", [])
        public = ("", "yes", "public", "permissive")
        private = ("private", "no", "customers")
        want = public if key == "beaches_public" else private
        return sum(1 for f in feats
                   if f.get("properties", {}).get("access", "") in want)
    return len(layers.get(key, EMPTY_FC).get("features", []))


def build_nav(layers: dict, skip: set | None = None) -> str:
    """Grouped, collapsible layer toggles."""
    skip = skip or set()
    by_group: dict[str, list] = {g: [] for g, _ in LAYER_GROUPS}
    for key, ld in LAYER_DEFS.items():
        by_group.setdefault(ld["group"], []).append((key, ld))

    sections = []
    for group_key, group_label in LAYER_GROUPS:
        items = []
        for key, ld in by_group.get(group_key, []):
            is_raster = ld.get("kind") == "raster"
            if is_raster:
                count_str = "live"
            elif key in skip:
                continue           # deliberately excluded from this build
            else:
                count = _count_for(key, layers)
                # Hide empty layers unless they default on, so the absence is
                # visible rather than silently dropped.
                if count == 0 and not ld["on"]:
                    continue
                count_str = f"{count:,}"
            checked = " checked" if ld["on"] else ""
            dot_cls = "map-layer-dot raster" if is_raster else "map-layer-dot"
            items.append(
                f'<label class="map-layer-toggle" data-group="{group_key}">'
                f'<input type="checkbox" data-layer="{key}"{checked}'
                f' onchange="toggleMapLayer(\'{key}\',this.checked)">'
                f'<span class="{dot_cls}" style="background:{ld["color"]}"></span>'
                f'<span class="map-layer-label">{ld["label"]}</span>'
                f'<span class="map-layer-count">{count_str}</span>'
                f'</label>'
            )
            rd = RASTER_DEFS.get(key, {})
            min_z, max_z = rd.get("minNativeZoom"), rd.get("maxNativeZoom")
            if min_z and max_z and min_z == max_z:
                items.append(f'<div class="zoom-note">Renders at zoom {min_z}; '
                             f'upscaled beyond</div>')
            elif min_z:
                items.append(f'<div class="zoom-note">Zoom in to {min_z}+</div>')
        if not items:
            continue
        open_attr = " open" if any(
            LAYER_DEFS[k]["on"] for k, _ in by_group.get(group_key, [])
        ) else ""
        n_layers = sum(1 for i in items if "map-layer-toggle" in i)
        # Clicking a button inside <summary> would otherwise also toggle the
        # <details> open/closed, so stop the event there.
        acts = (
            f'<span class="group-count">{n_layers}</span>'
            f'<span class="group-acts">'
            f'<button type="button" title="Turn on every layer in {group_label}" '
            f'onclick="event.preventDefault();event.stopPropagation();'
            f'setGroupLayers(\'{group_key}\',true)">ALL</button>'
            f'<button type="button" title="Turn off every layer in {group_label}" '
            f'onclick="event.preventDefault();event.stopPropagation();'
            f'setGroupLayers(\'{group_key}\',false)">NONE</button>'
            f'</span>'
        )
        sections.append(
            f'<details class="layer-group"{open_attr}>'
            f'<summary>{group_label}{acts}</summary>\n'
            + "\n".join(items) + "\n</details>"
        )
    return "\n".join(sections)


def build_init_js(bbox: tuple, center: tuple, base_label: str,
                  zoom: int, default_basemap: str,
                  tile_manifest: dict | None = None,
                  tile_root: str = "tiles/",
                  deferred: dict | None = None) -> str:
    s, w, n, e = bbox
    clat, clng = (s + n) / 2, (w + e) / 2
    colors = {k: v["color"] for k, v in LAYER_DEFS.items()}
    defaults = {k: 1 for k, v in LAYER_DEFS.items() if v["on"]}
    rasters = {k: {**v, "cacheKey": RASTER_CACHE_ALIAS.get(k, k)}
               for k, v in RASTER_DEFS.items()}
    return (
        MAP_JS_TEMPLATE
        .replace("__TILE_CACHE__", json.dumps(tile_manifest or {},
                                              separators=(",", ":")))
        .replace("__TILE_ROOT__", json.dumps(tile_root))
        .replace("__DEFERRED__", json.dumps(deferred or {}, separators=(",", ":")))
        .replace("__ZORDER__", json.dumps(stack_order(), separators=(",", ":")))
        .replace("__LABELS__", json.dumps(
            {k: v["label"] for k, v in LAYER_DEFS.items()},
            separators=(",", ":")))
        .replace("__RASTER_DEFS__", json.dumps(rasters, separators=(",", ":")))
        .replace("__COLORS__", json.dumps(colors, separators=(",", ":")))
        .replace("__DEFAULTS_OBJ__", json.dumps(defaults, separators=(",", ":")))
        .replace("__DEFAULT_BASEMAP__", json.dumps(default_basemap))
        .replace("__CENTER_LAT__", str(round(clat, 4)))
        .replace("__CENTER_LNG__", str(round(clng, 4)))
        .replace("__BASE_LAT__", str(round(center[0], 5)))
        .replace("__BASE_LNG__", str(round(center[1], 5)))
        # Lands inside a single-quoted JS string that is itself divIcon markup.
        .replace("__BASE_LABEL__", (base_label.replace("&", "&amp;")
                                              .replace("<", "&lt;")
                                              .replace(">", "&gt;")
                                              .replace('"', "&quot;")
                                              .replace("'", "&#39;")))
        .replace("__ZOOM__", str(zoom))
        .replace("__SOUTH__", str(s))
        .replace("__WEST__", str(w))
        .replace("__NORTH__", str(n))
        .replace("__EAST__", str(e))
    )


def build_data_script(layers: dict, *, defer_bytes: int = 0,
                      data_dir: Path | None = None) -> tuple:
    """Emit one `mapData_<key>` global per vector layer.

    Layers that are off by default and larger than `defer_bytes` are written to
    `data/<key>.json` instead and loaded the first time they are switched on.
    On a phone this is the difference between an 8.6 MB page and a 3.4 MB one,
    since the geometry nobody has asked for yet dominates the payload.

    Returns (script, deferred) where deferred maps layer key -> relative URL.
    """
    keys = set()
    for key in vector_keys():
        keys.add("beaches" if key.startswith("beaches_") else key)

    lines, deferred = [], {}
    for key in sorted(keys):
        gj = layers.get(key, EMPTY_FC)
        blob = json.dumps(gj, separators=(",", ":"))
        # `beaches` backs two toggles from one payload, so leave it inline.
        can_defer = (defer_bytes and data_dir is not None
                     and key != "beaches"
                     and len(blob) > defer_bytes
                     and gj.get("features"))
        if can_defer:
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / f"{key}.json").write_text(blob, encoding="utf-8")
            deferred[key] = f"data/{key}.json"
            lines.append(f'var mapData_{key}={{"type":"FeatureCollection",'
                         f'"features":[]}};')
            log.info("    deferred %-18s %6.0f KB -> data/%s.json",
                     key, len(blob) / 1024, key)
        else:
            lines.append(f"var mapData_{key}={blob};")
    return "\n".join(lines), deferred


# ─── Standalone page ──────────────────────────────────────────────

STANDALONE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<meta name="color-scheme" content="light"/>
<title>__TITLE__</title>
<link rel="icon" href="data:,"/>
__CSP__
__FONTS__
__LEAFLET__
<style>__CSS__</style>
</head>
<body>
<div class="shell">
  <aside class="sidebar" id="sidebar">
    <button class="sheet-close" type="button" aria-label="Close layer list"
            onclick="toggleSheet(false)">&times;</button>
    <header>
      <h1>__TITLE__</h1>
      <p class="sub">__SUBTITLE__</p>
__PAGELINKS__
    </header>
    <div class="layer-tools">
      <button type="button" onclick="resetMapLayers()">Reset</button>
      <button type="button" onclick="setAllMapLayers(true)">All on</button>
      <button type="button" onclick="setAllMapLayers(false)">All off</button>
    </div>
    <div class="layer-scroll">
__NAV__
    </div>
    <div class="credits" onclick="this.classList.toggle('expanded')">__CREDITS__</div>
  </aside>
  <div class="sheet-scrim" id="sheet-scrim" onclick="toggleSheet(false)"></div>
  <button class="sheet-toggle" type="button" onclick="toggleSheet(true)">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="12 2 22 8.5 12 15 2 8.5"/><polyline points="2 12 12 18.5 22 12"/>
    </svg>Layers <span class="n" id="sheet-count"></span></button>
  <div class="map-wrap"><div id="leaflet-map"></div></div>
</div>
<script>
__DATA__
__INIT__
/* Bottom-sheet layer list for phones. The sheet is CSS-hidden on desktop, so
   these handlers are harmless there. */
function toggleSheet(open){
  var sb=document.getElementById('sidebar'),sc=document.getElementById('sheet-scrim');
  if(!sb)return;
  if(open===undefined)open=!sb.classList.contains('open');
  sb.classList.toggle('open',open);
  if(sc)sc.classList.toggle('show',open);
  if(!open&&_map)setTimeout(function(){_map.invalidateSize();},220);
}
function updateSheetCount(){
  var el=document.getElementById('sheet-count');
  if(!el)return;
  el.textContent=document.querySelectorAll('.map-layer-toggle input:checked').length;
}
document.addEventListener('DOMContentLoaded',function(){
  initMap();
  updateSheetCount();
  document.addEventListener('change',function(e){
    if(e.target&&e.target.matches&&e.target.matches('.map-layer-toggle input'))
      updateSheetCount();
  });
  /* The bulk buttons set checkboxes directly and fire no change event. */
  ['setGroupLayers','setAllMapLayers','resetMapLayers'].forEach(function(fn){
    var orig=window[fn];
    if(typeof orig!=='function')return;
    window[fn]=function(){var r=orig.apply(this,arguments);updateSheetCount();return r;};
  });
});
</script>
</body>
</html>
"""


def build_standalone(layers: dict, bbox: tuple, center: tuple, *,
                     title: str, base_label: str, zoom: int,
                     default_basemap: str,
                     tile_manifest: dict | None = None,
                     vendored: bool = False,
                     page_links: list | None = None,
                     skip: set | None = None,
                     defer_bytes: int = 0,
                     data_dir: Path | None = None) -> str:
    s, w, n, e = bbox
    subtitle = (
        f"Search area {s:.2f},{w:.2f} to {n:.2f},{e:.2f} &middot; "
        f"{sum(_count_for(k, layers) for k in vector_keys()):,} mapped features "
        f"across {len(LAYER_DEFS)} layers"
    )
    if tile_manifest:
        zs = [v["minZoom"] for v in tile_manifest.values()]
        ze = [v["maxZoom"] for v in tile_manifest.values()]
        subtitle += (f" &middot; {len(tile_manifest)} tile layers cached "
                     f"locally (z{min(zs)}\u2013{max(ze)})")
        if vendored:
            subtitle += " &middot; works offline"
    links_html = ""
    if page_links:
        items = "".join(
            f'<a href="{href}">{label}</a>'
            for href, label in page_links)
        links_html = f'      <div class="page-links">{items}</div>'

    data_script, deferred = build_data_script(
        layers, defer_bytes=defer_bytes, data_dir=data_dir)
    csp, fonts, libs = local_head() if vendored else (CSP, FONT_LINK, LEAFLET_CDN)
    return (
        STANDALONE_TEMPLATE
        .replace("__CSP__", csp)
        .replace("__FONTS__", fonts)
        .replace("__LEAFLET__", libs)
        .replace("__CSS__", MAP_CSS)
        .replace("__NAV__", build_nav(layers, skip))
        .replace("__CREDITS__", CREDITS)
        .replace("__DATA__", data_script)
        .replace("__INIT__", build_init_js(bbox, center, base_label, zoom,
                                           default_basemap, tile_manifest,
                                           deferred=deferred))
        .replace("__PAGELINKS__", links_html)
        .replace("__SUBTITLE__", subtitle)
        .replace("__TITLE__", title)
    )


# ─── Injection into an existing checklist page ────────────────────

def inject_map_tab(target: Path, layers: dict, bbox: tuple, center: tuple, *,
                   base_label: str, zoom: int, default_basemap: str,
                   tile_manifest: dict | None = None,
                   vendored: bool = False, skip: set | None = None):
    """Add a Map tab to a field-checklist page built the Gulf Islands way."""
    backup = target.with_name(".index_pre_map.html")

    if backup.exists() and target.stat().st_mtime > backup.stat().st_mtime:
        log.info("Target is newer than backup — refreshing clean backup")
        html = target.read_text(encoding="utf-8")
        backup.write_text(html, encoding="utf-8")
    elif backup.exists():
        log.info("Restoring clean backup before re-injection")
        html = backup.read_text(encoding="utf-8")
    else:
        html = target.read_text(encoding="utf-8")
        backup.write_text(html, encoding="utf-8")
        log.info("Saved clean backup to %s", backup.name)

    csp, fonts, libs = local_head() if vendored else (CSP, FONT_LINK, LEAFLET_CDN)
    html = html.replace("</style>", MAP_CSS + INJECT_CSS + "</style>", 1)
    html = html.replace("</head>", csp + "\n" + libs + "\n</head>", 1)

    if 'id="btn-map"' not in html:
        map_btn = ('<button class="mode-btn" id="btn-map" '
                   'onclick="switchMode(\'map\')">Map</button>')
        html = re.sub(
            r'(class="mode-toggle">[^<]*(?:<button[^>]*>.*?</button>\s*)+)(</div>)',
            lambda m: m.group(1) + map_btn + m.group(2),
            html, count=1, flags=re.DOTALL,
        )

    nav_html = ('<div class="nav-links" id="nav-map" style="display:none">\n'
                + build_nav(layers, skip) + "\n</div>")
    html = html.replace("</nav>", nav_html + "\n</nav>", 1)

    panel_html = ('<div class="panel" id="panel-map"><div id="leaflet-map"></div>'
                  f'<div class="credits">{CREDITS}</div></div>')
    html = html.replace("</main>", panel_html + "\n</main>", 1)

    markers = [
        "document.addEventListener('DOMContentLoaded',initAprMay)",
        "document.addEventListener('DOMContentLoaded', initAprMay)",
        "document.addEventListener('DOMContentLoaded',function(){",
        "document.addEventListener('DOMContentLoaded',function (){",
    ]
    idx = -1
    for marker in markers:
        idx = html.find(marker)
        if idx >= 0:
            break
    # The host page's switchMode() may not know to build the map, and Leaflet
    # cannot size a map inside a hidden panel. Wire the tab button directly so
    # the first click initialises it either way; initMap() is idempotent.
    boot_js = ("\nvar _b=document.getElementById('btn-map');\n"
               "if(_b)_b.addEventListener('click',function(){setTimeout(initMap,0);});\n")

    if idx < 0:
        log.warning("No DOMContentLoaded hook found in %s — appending map "
                    "script before </body> instead", target.name)
        block = ("<script>\n" + build_data_script(layers)[0] + "\n"
                 + build_init_js(bbox, center, base_label, zoom,
                                 default_basemap, tile_manifest)
                 + boot_js + "</" + "script>\n")
        html = html.replace("</body>", block + "</body>", 1)
    else:
        block = (build_data_script(layers)[0] + "\n"
                 + build_init_js(bbox, center, base_label, zoom,
                                 default_basemap, tile_manifest)
                 + boot_js)
        html = html[:idx] + block + "\n" + html[idx:]

    target.write_text(html, encoding="utf-8")
    log.info("Wrote %s (%d KB)", target, len(html) // 1024)


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

# (layer key, fetch function, human label). eBird layers are handled
# separately because they need an API key.
FETCHERS = [
    ("beaches",           fetch_beaches,               "Beaches (OSM)"),
    ("public_access",     fetch_public_access,         "NJ shore access points"),
    ("boat_access",       fetch_boat_access,           "Boat ramps & fishing access"),
    ("lighthouses",       fetch_lighthouses,           "Lighthouses"),
    ("ice_cream",         fetch_ice_cream,             "Ice cream stands"),
    ("mini_golf",         fetch_mini_golf,             "Mini golf"),
    ("amusements",        fetch_amusements,            "Arcades & water parks"),
    ("hiking",            fetch_hiking,                "Hiking trails"),
    ("bike",              fetch_bike,                  "Bike routes"),
    ("nj_trails",         fetch_nj_trails,             "NJ statewide trails"),
    ("park_trails",       fetch_park_trails,           "State park trails"),
    ("heritage",          fetch_heritage,              "Historic architecture + NRHP"),
    ("nj_historic",       fetch_nj_historic,           "NJ historic properties"),
    ("nj_historic_dist",  fetch_nj_historic_districts, "NJ historic districts"),
    ("kings_roads",       fetch_kings_roads,           "Old King's roads"),
    ("orig_highways",     fetch_orig_highways,         "Original highways"),
    ("old_rail",          fetch_old_rail,              "Abandoned rail grades"),
    ("hist_shoreline",    fetch_hist_shoreline,        "Historical shorelines"),
    ("federal_lands",     fetch_federal_lands,         "Federal protected lands (PAD-US)"),
    ("refuges_fws",       fetch_refuges_fws,           "National wildlife refuges"),
    ("fws_wilderness",    fetch_fws_wilderness,        "Federal wilderness"),
    ("state_lands",       fetch_state_lands,           "NJ state lands"),
    ("natural_areas",     fetch_natural_areas,         "State natural areas"),
    ("nhp_sites",         fetch_nhp_sites,             "Natural Heritage priority sites"),
    ("focal_areas",       fetch_focal_areas,           "Conservation focal areas"),
    ("state_parks",       fetch_state_parks,           "State parks (OSM)"),
    ("refuges",           fetch_refuges,               "Protected areas (OSM)"),
    ("forests",           fetch_forests,               "Forests (OSM)"),
    ("pnr",               fetch_pnr,                   "Pinelands National Reserve"),
    ("pinelands_mgmt",    fetch_pinelands_mgmt,        "Pinelands management areas"),
    ("sig_habitat",       fetch_sig_habitat,           "Significant habitat (Landscape Project)"),
    ("stream_habitat",    fetch_stream_habitat,        "Stream habitat"),
    ("vernal_pools",      fetch_vernal_pools,          "Vernal pools & habitat"),
    ("critical_habitat",  fetch_critical_habitat,      "ESA critical habitat"),
    ("mpa",               fetch_mpa,                   "Marine protected areas"),
    ("nerrs",             fetch_nerrs,                 "Estuarine research reserves"),
    ("shellfish",         fetch_shellfish,             "Shellfish classification"),
    ("reefs",             fetch_reefs,                 "Artificial reefs"),
    ("tide_stations",     fetch_tide_stations,         "NOAA tide stations"),
    ("wetlands_osm",      fetch_wetlands_osm,          "Wetlands (OSM)"),
    ("inat_rare",         fetch_inat_rare,             "Rare species (iNaturalist)"),
    ("wildfires",         fetch_wildfires,             "Active wildfires (NIFC)"),
    ("smoke",             fetch_smoke,                 "Smoke plumes (NOAA HMS)"),
]

# Basemap label -> the tile sources it draws from, so the builder can tell
# whether a chosen basemap will work from the local cache.
BASEMAP_TILE_KEYS = {
    "NOAA Chart": ["noaa_chart"],
    "NOAA Chart + Satellite": ["noaa_chart", "esri_imagery"],
    "NOAA Chart + Street": ["noaa_chart", "carto_light"],
    "Voyager": ["carto_voyager"],
    "Minimal": ["carto_light"],
    "Street": ["osm"],
    "Topo": ["opentopo"],
    "Satellite": ["esri_imagery"],
    "Ocean / Bathymetric": ["esri_ocean"],
}
BASEMAP_CHOICES = list(BASEMAP_TILE_KEYS)


def pick_basemap(requested: str | None, tile_manifest: dict) -> str:
    """Resolve the load-time basemap.

    With a tile cache present, defaulting to CARTO would put the page straight
    back on the network — so prefer a cached basemap unless one was named.
    """
    if requested:
        missing = [k for k in BASEMAP_TILE_KEYS[requested]
                   if k not in tile_manifest]
        if tile_manifest and missing:
            log.warning("  Basemap %r needs uncached tiles (%s) and will load "
                        "them from the network. Cached basemaps: %s",
                        requested, ", ".join(missing),
                        ", ".join(cached_basemaps(tile_manifest)) or "none")
        return requested
    for name in ("NOAA Chart + Satellite", "NOAA Chart", "Satellite",
                 "Ocean / Bathymetric"):
        if all(k in tile_manifest for k in BASEMAP_TILE_KEYS[name]):
            log.info("  Default basemap set to %r — it is in the tile cache",
                     name)
            return name
    return "Voyager"


def cached_basemaps(tile_manifest: dict) -> list:
    return [n for n, keys in BASEMAP_TILE_KEYS.items()
            if keys and all(k in tile_manifest for k in keys)]


def main():
    global SIMPLIFY_DEG, SIMPLIFY_DEG_FINE, SIMPLIFY_DEG_COARSE
    global HABITAT_MIN_RANK

    parser = argparse.ArgumentParser(
        description="Build an interactive geospatial map centered on Long "
                    "Beach Island, New Jersey.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Bbox presets: " + ", ".join(sorted(BBOX_PRESETS)),
    )
    parser.add_argument("--bbox", default="lbi-region",
                        help="Bounding box S,W,N,E or a preset name "
                             "(default: lbi-region)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Write a standalone HTML map here "
                             "(default: output/lbi/index.html)")
    parser.add_argument("--target", type=Path, default=None,
                        help="Instead of --out, inject a Map tab into this "
                             "existing checklist page")
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="Cache directory (default: output HTML's parent)")
    parser.add_argument("--center", default=None,
                        help="Base marker coordinate 'lat,lng' "
                             f"(default: {LBI_CENTER[0]},{LBI_CENTER[1]})")
    parser.add_argument("--center-label", default="Ship Bottom",
                        help="Label under the base marker")
    parser.add_argument("--title", default="Long Beach Island — Field Map",
                        help="Page title")
    parser.add_argument("--zoom", type=int, default=10,
                        help="Initial zoom before the search area is framed")
    parser.add_argument("--basemap", default=None, choices=BASEMAP_CHOICES,
                        help="Basemap shown on load (default: Voyager, or a "
                             "cached basemap when tiles have been cached)")
    parser.add_argument("--ebird-key", default=os.environ.get("EBIRD_API_KEY", ""),
                        help="eBird API key (or set EBIRD_API_KEY)")
    parser.add_argument("--back", type=int, default=30,
                        help="eBird lookback days (default 30)")
    parser.add_argument("--fire-bbox", default=None,
                        help="Wider bbox S,W,N,E for the wildfire query")
    parser.add_argument("--only", default=None,
                        help="Comma-separated layer keys to fetch; everything "
                             "else comes from cache")
    parser.add_argument("--defer-large", type=int, default=0, metavar="KB",
                        help="Write layers that are off by default and larger "
                             "than this to data/<key>.json, loaded when first "
                             "switched on. Cuts the page weight substantially "
                             "for phones. Requires serving over http, not "
                             "file://. 0 disables (default)")
    parser.add_argument("--skip", default=None,
                        help="Comma-separated layer keys to leave out entirely. "
                             "Useful for layers whose service is too slow or "
                             "whose detail is meaningless at a regional bbox")
    parser.add_argument("--cache-tiles", nargs="?", const="default",
                        default=None, metavar="LAYERS",
                        help="Download basemap and overlay tiles into "
                             "<output>/tiles so the map works offline and stops "
                             "re-pulling from the services. Accepts a group "
                             "(default, overlays, basemaps, open, all) or a "
                             "comma-separated list of tile source keys")
    parser.add_argument("--tile-zooms", default="8-14", metavar="MIN-MAX",
                        help="Zoom range to cache (default 8-14)")
    parser.add_argument("--tile-margin", type=float, default=0.1,
                        metavar="FRAC",
                        help="Extra margin around the cached area, as a "
                             "fraction of the bbox per side. The area is first "
                             "widened to a 2:1 on-screen aspect so a wide map "
                             "pane has no blank band (default 0.1)")
    parser.add_argument("--max-tiles", type=int, default=20000,
                        help="Per-layer tile cap, a guard against an "
                             "accidentally huge download (default 20000)")
    parser.add_argument("--refresh-tiles", action="store_true",
                        help="Re-download tiles that are already cached")
    parser.add_argument("--page-link", action="append", default=[],
                        metavar="HREF|LABEL",
                        help="Add a link in the sidebar header, e.g. "
                             "'region.html|Full Pine Barrens region'. Repeatable")
    parser.add_argument("--vendor-libs", action="store_true",
                        help="Download Leaflet and the webfont into "
                             "<output>/lib and reference them locally, so the "
                             "page needs no network at all. Implied by "
                             "--cache-tiles")
    parser.add_argument("--list-tile-sources", action="store_true",
                        help="Print the cacheable tile sources and exit")
    parser.add_argument("--habitat-rank", type=int, default=HABITAT_MIN_RANK,
                        choices=[1, 2, 3, 4, 5], metavar="1-5",
                        help="Minimum NJDEP Landscape Project rank for the "
                             f"significant-habitat layer (default {HABITAT_MIN_RANK}: "
                             "State endangered and above). Lower values add a "
                             "great many polygons")
    parser.add_argument("--simplify", type=float, default=SIMPLIFY_DEG,
                        metavar="DEG",
                        help="Geometry generalization tolerance in degrees "
                             f"(default {SIMPLIFY_DEG:g}, about 6 m). Raise it "
                             "to shrink the page on a large bbox, set 0 to keep "
                             "full-resolution geometry")
    parser.add_argument("--render-only", action="store_true",
                        help="Rebuild HTML from cache with no network calls")
    args = parser.parse_args()

    if args.list_tile_sources:
        print("Cacheable tile sources (--cache-tiles):\n")
        for key, src in sorted(tile_sources().items()):
            note = ""
            if src.get("policy") == "cdn":
                note = "  [tile CDN — bulk download against its terms; " \
                       "excluded from groups]"
            zr = []
            if src.get("minNativeZoom"):
                zr.append(f"min z{src['minNativeZoom']}")
            if src.get("maxNativeZoom"):
                zr.append(f"max z{src['maxNativeZoom']}")
            print(f"  {key:16} {src['kind']:11}"
                  f"{('(' + ', '.join(zr) + ')') if zr else '':22}{note}")
        print("\nGroups: default, overlays, basemaps, open, all")
        return

    bbox = parse_bbox(BBOX_PRESETS.get(args.bbox, args.bbox))
    fire_bbox = parse_bbox(args.fire_bbox) if args.fire_bbox else None
    HABITAT_MIN_RANK = args.habitat_rank

    try:
        zlo, _, zhi = args.tile_zooms.partition("-")
        tile_zooms = range(int(zlo), int(zhi or zlo) + 1)
        if tile_zooms.start < 0 or tile_zooms.stop > 20 or not tile_zooms:
            raise ValueError
    except ValueError:
        parser.error(f"--tile-zooms must be MIN-MAX, got {args.tile_zooms!r}")

    page_links = []
    for spec in args.page_link:
        href, _, label = spec.partition("|")
        if not href or not label:
            parser.error(f"--page-link must be 'HREF|LABEL', got {spec!r}")
        page_links.append((href.strip(), label.strip()))

    tile_keys = []
    if args.cache_tiles:
        try:
            tile_keys = resolve_tile_keys(args.cache_tiles)
        except ValueError as exc:
            parser.error(str(exc))

    if args.simplify != SIMPLIFY_DEG:
        SIMPLIFY_DEG = args.simplify
        SIMPLIFY_DEG_FINE = args.simplify * 0.4
        SIMPLIFY_DEG_COARSE = args.simplify * 4
        log.info("Generalization tolerance set to %g deg (~%.1f m)",
                 SIMPLIFY_DEG, SIMPLIFY_DEG * 111000)

    if args.center:
        lat_s, lng_s = args.center.split(",")
        center = (float(lat_s), float(lng_s))
    else:
        center = LBI_CENTER

    if args.target and args.out:
        parser.error("Use either --out (standalone) or --target (inject), not both")
    out_path = None if args.target else (args.out or Path("output/lbi/index.html"))
    if args.target and not args.target.exists():
        parser.error(f"Injection target not found: {args.target}")

    anchor = (args.target or out_path).resolve()
    cache_dir = (args.cache_dir or anchor.parent).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / ".map_cache.json"
    cache = load_cache(cache_path)

    only = ({k.strip() for k in args.only.split(",") if k.strip()}
            if args.only else None)
    skip = {k.strip() for k in (args.skip or "").split(",") if k.strip()}
    unknown = skip - set(LAYER_DEFS)
    if unknown:
        parser.error(f"--skip names unknown layers: {', '.join(sorted(unknown))}")

    log.info("=" * 64)
    log.info("Long Beach Island Geospatial Map Builder")
    log.info("  bbox    S=%.3f  W=%.3f  N=%.3f  E=%.3f", *bbox)
    log.info("  center  %.5f, %.5f  (%s)", center[0], center[1], args.center_label)
    log.info("  output  %s", anchor)
    log.info("  cache   %s", cache_path)
    if args.render_only:
        log.info("  mode    render-only (no network calls)")
    log.info("=" * 64)

    layers: dict[str, dict] = {}

    log.info("\nStep 1/3  Vector layers")
    total = len(FETCHERS)
    for i, (key, fn, label) in enumerate(FETCHERS, 1):
        if key in skip:
            log.info("  [%2d/%d] %s — skipped", i, total, label)
            layers[key] = EMPTY_FC
            continue
        if args.render_only:
            layers[key] = _from_cache_only(key, fn, bbox, cache, label)
            continue
        if only is not None and key not in only:
            layers[key] = _from_cache_only(key, fn, bbox, cache, label,
                                           quiet=True)
            continue
        log.info("  [%2d/%d] %s", i, total, label)
        try:
            if key == "wildfires" and fire_bbox:
                layers[key] = fn(bbox, cache, wide_bbox=fire_bbox)
            else:
                layers[key] = fn(bbox, cache)
        except Exception as exc:
            log.error("  %s failed: %s — continuing with an empty layer",
                      label, exc)
            layers[key] = EMPTY_FC
        save_cache(cache_path, cache)

    log.info("\nStep 2/3  eBird layers")
    have_cached_ebird = "hotspots" in cache
    if args.render_only:
        layers["hotspots"] = _from_cache_only("hotspots", None, bbox, cache,
                                              "Birding hotspots")
        layers["ebird_obs"] = _from_cache_only("ebird_obs", None, bbox, cache,
                                               "eBird observations")
    elif args.ebird_key or have_cached_ebird:
        if not args.ebird_key:
            log.info("  No eBird key — reusing cached bird data")
        log.info("  Birding hotspots")
        layers["hotspots"] = fetch_hotspots(bbox, args.ebird_key, cache)
        save_cache(cache_path, cache)
        log.info("  Recent observations (%d days)", args.back)
        layers["ebird_obs"] = fetch_ebird_obs(bbox, args.ebird_key,
                                              args.back, cache)
        save_cache(cache_path, cache)
    else:
        log.warning("  No eBird key and nothing cached — skipping bird layers. "
                    "Get a free key at https://ebird.org/api/keygen")
        layers["hotspots"] = EMPTY_FC
        layers["ebird_obs"] = EMPTY_FC

    log.info("\nLayer summary:")
    grand = 0
    for group_key, group_label in LAYER_GROUPS:
        log.info("  %s", group_label)
        for key, ld in LAYER_DEFS.items():
            if ld["group"] != group_key:
                continue
            if ld.get("kind") == "raster":
                log.info("    %-30s  %s", ld["label"], "live raster")
                continue
            count = _count_for(key, layers)
            grand += count
            log.info("    %-30s  %6s features", ld["label"], f"{count:,}")
    log.info("  %s", "-" * 46)
    log.info("    %-30s  %6s features", "TOTAL", f"{grand:,}")

    tile_dir = anchor.parent / "tiles"
    tile_manifest = {}
    if tile_keys:
        tile_bbox = pad_bbox(bbox, args.tile_margin)
        per_layer = count_tiles(tile_bbox, tile_zooms)
        log.info("\nStep 3/4  Caching tiles  (%d sources, z%d-%d, "
                 "up to %s tiles each, %.0f%% margin)",
                 len(tile_keys), tile_zooms.start, tile_zooms.stop - 1,
                 f"{per_layer:,}", args.tile_margin * 100)
        tile_manifest = download_tiles(
            tile_keys, tile_bbox, tile_zooms, tile_dir,
            max_tiles=args.max_tiles, overwrite=args.refresh_tiles)
    elif (tile_dir / "manifest.json").exists():
        # Reuse a cache from an earlier run so the page keeps pointing at it.
        try:
            tile_manifest = json.loads((tile_dir / "manifest.json").read_text())
            log.info("\nReusing existing tile cache: %d sources",
                     len(tile_manifest))
        except json.JSONDecodeError:
            log.warning("Tile manifest at %s is unreadable — ignoring", tile_dir)

    # Caching tiles is a request for an offline-capable page, so vendor the
    # libraries too unless the page is being injected into someone else's HTML.
    want_vendor = args.vendor_libs or bool(tile_keys)
    vendored = False
    lib_dir = anchor.parent / "lib"
    if want_vendor:
        log.info("  Vendoring Leaflet and the webfont ...")
        vendored = vendor_libs(lib_dir, overwrite=args.refresh_tiles)
    elif (lib_dir / "leaflet.js").exists():
        vendored = True
        log.info("  Reusing vendored libraries at %s", lib_dir)

    basemap = pick_basemap(args.basemap, tile_manifest)

    step = "4/4" if tile_keys else "3/3"
    log.info("\nStep %s  Building HTML", step)
    if args.target:
        inject_map_tab(args.target.resolve(), layers, bbox, center,
                       base_label=args.center_label, zoom=args.zoom,
                       default_basemap=basemap,
                       tile_manifest=tile_manifest, vendored=vendored,
                       skip=skip)
        final = args.target.resolve()
    else:
        html = build_standalone(layers, bbox, center, title=args.title,
                                base_label=args.center_label, zoom=args.zoom,
                                default_basemap=basemap,
                                tile_manifest=tile_manifest,
                                vendored=vendored, page_links=page_links,
                                skip=skip,
                                defer_bytes=args.defer_large * 1024,
                                data_dir=out_path.parent / "data")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html, encoding="utf-8")
        final = out_path.resolve()
        log.info("Wrote %s (%d KB)", final, len(html) // 1024)

    log.info("\n" + "=" * 64)
    log.info("Done. Open %s", final)
    log.info("=" * 64)


def _from_cache_only(key, fn, bbox, cache, label, *, quiet=False):
    """Rebuild one layer purely from cache. Every fetcher checks the cache
    first and OFFLINE makes the fallback path a no-op, so this never touches
    the network."""
    global OFFLINE
    was = OFFLINE
    OFFLINE = True
    try:
        if fn is None:
            # eBird layers keep raw API payloads under their own keys.
            if key == "hotspots":
                return (fetch_hotspots(bbox, "", cache)
                        if "hotspots" in cache else EMPTY_FC)
            for ck in cache:
                if ck.startswith("ebird_obs_"):
                    return fetch_ebird_obs(bbox, "",
                                           int(ck.rsplit("_", 1)[1]), cache)
            return EMPTY_FC
        gj = fn(bbox, cache)
        if not gj.get("features") and not quiet:
            # Could be an empty cached result or no cache entry at all; the
            # always-live layers (wildfires, smoke) are never cached by design.
            log.info("  %s — no features from cache", label)
        return gj
    except Exception as exc:
        if not quiet:
            log.warning("  %s could not be rebuilt from cache: %s", label, exc)
        return EMPTY_FC
    finally:
        OFFLINE = was


if __name__ == "__main__":
    main()
