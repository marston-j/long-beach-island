# Long Beach Island

Interactive geospatial field map centered on Long Beach Island, New Jersey.
Generates a self-contained HTML page with 50 layers pulled live from public
APIs — public beaches and shore access, ice cream stands, mini golf, wetlands,
state and federal protected lands, the Pinelands National Reserve, historic
buildings, colonial King's roads, marine protected areas, and NOAA electronic
navigational charts as selectable basemaps.

Companion to the [gulf-islands](https://github.com/marston-j/gulf-islands)
build; every layer from that project's `trail_map.py` is carried over here,
plus the New Jersey specific sources.

## Prerequisites

- Python 3.9+
- Optional: a free [eBird API key](https://ebird.org/api/keygen) for the two
  bird layers. Everything else works without any credentials.

## Quick Start

```bash
pip install -r requirements.txt

# Full regional build — island, Barnegat Bay, Great Bay, and the Pine Barrens
export EBIRD_API_KEY="your_key_here"
python3 lbi_map.py --bbox lbi-region --out output/lbi/index.html

# Island only — much faster, everything within walking or biking distance
python3 lbi_map.py --bbox lbi --out output/lbi-island/index.html
```

Then open the generated `index.html` in a browser.

A full regional build takes 15–25 minutes, most of it waiting on the public
Overpass API. Everything is cached, so re-runs are fast and interrupted builds
resume where they left off.

## Bbox Presets

| Preset | Area | Bbox (S,W,N,E) |
|--------|------|----------------|
| `lbi-region` | **Default.** Island + Barnegat Bay + Great Bay + the full Pinelands National Reserve | `39.25,-75.05,40.10,-73.90` |
| `lbi` | The island and its back bay | `39.48,-74.40,39.80,-74.02` |
| `barnegat-bay` | Barnegat Bay watershed, Island Beach to Little Egg Harbor | `39.45,-74.45,40.05,-73.98` |
| `pinelands` | Pine Barrens / Pinelands National Reserve only | `39.30,-75.05,40.05,-74.10` |
| `mullica-great-bay` | Jacques Cousteau NERR — Mullica River / Great Bay estuary | `39.45,-74.60,39.65,-74.25` |

Any explicit `S,W,N,E` string works in place of a preset.

## Basemaps

Nine basemaps are selectable from the layers control. NOAA's electronic
navigational charts are available as full basemaps, not just overlays:

| Basemap | Source |
|---------|--------|
| **NOAA Chart** | NOAA Office of Coast Survey ENC — soundings, buoys, light characteristics, channels |
| **NOAA Chart + Satellite** | ENC over Esri World Imagery, so marsh and beach detail stays readable |
| **NOAA Chart + Street** | ENC over a light street map, for finding a landing from the road |
| Voyager / Minimal | CARTO |
| Street | OpenStreetMap |
| Topo | OpenTopoMap |
| Satellite | Esri World Imagery |
| Ocean / Bathymetric | Esri Ocean Basemap (GEBCO, NOAA) |

NOAA retired its raster nautical charts (RNC) in January 2025; this build uses
the ENC-derived chart service, whose WMTS pyramid is offset two levels from the
standard Web Mercator scale set (its TileMatrix 12 is the 1:34k level Leaflet
calls zoom 14). `lbi_map.py` handles that offset.

## Layers

Grouped as they appear in the sidebar. "Raster" layers are drawn live from the
agency's own map service, so no geometry is embedded in the page.

### Island & Shore

| Layer | Source | Notes |
|---|---|---|
| Public Beaches | OSM `natural=beach` | Split by `access` tag |
| Private / Restricted | OSM `natural=beach` | |
| NJ Shore Access Points | NJDEP Environmental_admin/7 | Badge requirement, parking, restrooms, swimming, surfing, pier, boat launch, accessibility — clustered |
| Lighthouses | OSM + seamark tags | Barnegat Light, with light character and range |
| Boat Ramps & Fishing Access | NJDEP Environmental_admin/31 + OSM slipways and marinas | |

### Treats & Amusements

| Layer | Source |
|---|---|
| Ice Cream Stands | OSM `amenity=ice_cream`, `shop=ice_cream`, frozen custard / water ice / gelato |
| Mini Golf | OSM `leisure=miniature_golf` |
| Arcades & Water Parks | OSM arcades, water parks, theme parks, amusement rides, bowling |

### Trails & Routes

| Layer | Source |
|---|---|
| Hiking Trails | OSM paths, footways, boardwalks + named sweeps of Forsythe, Bass River, Wharton, Batona and friends |
| Bike Routes | OSM cycleways and bicycle routes |
| NJ Statewide Trails | NJDEP Land_lu/121 — blaze colour, difficulty, permitted uses, ADA |
| State Park Trails | NJDEP Land/63 |

### Historic

| Layer | Source | Notes |
|---|---|---|
| Historic Architecture | OSM `historic=*` + NPS National Register of Historic Places | National Historic Landmarks flagged in gold |
| NJ Historic Properties | NJ HPO via NJDEP Land/55 | Individually designated resources only — see note below |
| NJ Historic Districts | NJ HPO via NJDEP Land/57 | |
| **Old King's Roads** | OSM name and tag match | The King's Highway system plus the colonial stage, post and shore roads — Old Shore Road, Old New York Road, Tuckerton Stage, Quaker Bridge, Batsto, Chatsworth, Sooy Place, Carranza |
| **Original Highways** | OSM `old_ref` / `old_name` + named pikes + historic through-routes | White Horse and Black Horse Pikes, turnpikes and plank roads, superseded route numbers, and the alignments that are the historic through-route for this coast (US 9 as the old shore King's Highway, NJ 72 as the Manahawkin causeway road, NJ 166 as pre-1953 US 9) |
| Abandoned Rail Grades | OSM `railway=abandoned/dismantled` | The Tuckerton Railroad reached the bay in 1871; the Long Beach Railroad crossed to the island until the 1935 hurricane |
| Historical Shorelines | NJDEP Land_CAFRA_coast/11 | Shaded oldest to newest so barrier-island migration reads at a glance |

The NJ Historic Preservation Office tracks ~38,000 survey records across this
region, most of them individual parcels swept up inside a historic district.
The layer filters to `LISTED_INDV`, `NHL_INDV`, `ELIGIBLE_INDV`,
`LOCAL_LANDMARK`, `LOCALLY_DESIGNATED_HD` and `DELISTED_INDV` — the designated
buildings themselves. Districts are a separate layer.

### Protected Lands

| Layer | Source | Notes |
|---|---|---|
| Federal Protected Lands | USGS PAD-US 4.1, `Mang_Type='FED'` | GAP status, IUCN category, public access |
| Nat'l Wildlife Refuges | USFWS NWRS boundaries | Edwin B. Forsythe NWR |
| Federal Wilderness | USFWS wilderness boundaries | Brigantine Wilderness |
| NJ State Lands | NJDEP Land/67 (state-owned open space) | Manager, public access, parking, hunting, trail maps |
| State Natural Areas | NJDEP Land/80 | Strictest state protection class |
| Natural Heritage Priority | NJDEP Environmental_habitat/93 | Biodiversity rank B1–B5 |
| Conservation Focal Areas | NJDEP Environmental_habitat/100–105 | All six landscape regions — Marine, Coastal, Delaware Bay, Pinelands, Piedmont, Skylands |
| State Parks (OSM) | OSM | |
| Protected Areas (OSM) | OSM `protect_class`, `leisure=nature_reserve` | Catches locally held preserves absent from PAD-US |
| Forests (OSM) | OSM | |
| All Open Space (NJDEP) | NJDEP Land/65 — **raster** | 46k parcels region-wide including municipal; drawn as a raster rather than embedded |

### Pine Barrens

| Layer | Source | Notes |
|---|---|---|
| Pinelands National Reserve | NJ Pinelands Commission | The first National Reserve in the US, 1978, ~1.1 M acres over the Kirkwood-Cohansey aquifer. Falls back to the OSM boundary relation if the Commission service is down |
| Pinelands Mgmt Areas | NJ Pinelands Commission | Preservation Area District, Forest Area, Agricultural Production, Regional Growth, Villages — tinted by class |
| CHANJ Habitat Cores | NJDEP Environmental/107 — **raster** | Connecting Habitat Across New Jersey |

### Significant Habitat

| Layer | Source | Notes |
|---|---|---|
| Significant Habitat | NJDEP Landscape Project v3.4 — Marine, Atlantic Coastal, Pinelands and Delaware Bay regions | Species-based habitat, ranked 1–5 by the highest-status species documented using the patch. Filtered to rank 4+ and tinted by rank |
| Vernal Pools & Habitat | NJDEP Landscape Project v3.4 | Confirmed and potential vernal pools plus the surrounding vernal habitat — clustered |
| Stream Habitat | NJDEP Landscape Project v3.4 | Stream reaches documented as habitat for listed species |
| ESA Critical Habitat | USFWS + NOAA Fisheries designated critical habitat | Includes the Atlantic sturgeon New York Bight distinct population segment |

**On the USFWS New York Bight report.** *Significant Habitats and Habitat
Complexes of the New York Bight Watershed* (USFWS, 1997) covers the watershed
from Cape May to Montauk, so Long Beach Island sits squarely inside it — but it
was published as narrative descriptions and figures, with **no accompanying GIS
dataset**; nothing is registered on ScienceBase or ArcGIS Online. The
authoritative spatial equivalents for this region, all included above, are the
NJDEP Landscape Project ranking, ESA designated critical habitat, Natural
Heritage Priority Sites, and the Conservation Focal Areas.

The Landscape Project is large — 152,000 polygons for the Pinelands region
alone across the regional bbox — so the layer is filtered to the ranks carrying
regulatory weight. `--habitat-rank` sets the floor:

| Rank | Meaning |
|---|---|
| 5 | Habitat for a federally listed species |
| 4 | Habitat for a State endangered species (**default**) |
| 3 | Habitat for a State threatened species |
| 2 | Habitat for a species of special concern |
| 1 | Species occurrence area |

The island build is complete at rank 4. The regional build omits this layer via
`--skip sig_habitat`: the hosted service paginates a regional bbox at roughly a
thousand polygons per several minutes, and individual habitat patches are not
legible at that extent anyway. Use the island map for habitat work.

### Marine & Estuarine

| Layer | Source | Notes |
|---|---|---|
| Marine Protected Areas | NOAA MPA Inventory 2024 | Protection level, fishing restrictions, anchoring, IUCN category; tinted by governance level |
| Estuarine Reserves (NERR) | NOAA MPA Inventory + OSM | Jacques Cousteau NERR on the Mullica / Great Bay estuary |
| Shellfish Classification | NJDEP Environmental_admin/2 | Approved through Prohibited, colour-ramped |
| Artificial Reefs | NJDEP Environmental_admin/9 | |
| NOAA Tide Stations | NOAA CO-OPS station metadata | Links straight to live predictions |

### Wetlands & Water

| Layer | Source | Notes |
|---|---|---|
| Wetlands (mapped) | OSM `natural=wetland` | Clickable, tinted by wetland type. The back-bay salt marsh is mapped in detail |
| USFWS Wetlands Inventory | USFWS NWI raster — **raster** | National Wetlands Inventory, renders at every zoom |
| NJDEP Wetlands 2012 | NJDEP Land_lu/2 — **raster** | See the scale-window note below |
| Tidelands Claims | NJDEP Hydrography/30 — **raster** | Zoom 15+ |
| FEMA Flood Zones | FEMA NFHL layer 28 — **raster** | Zoom 14+ |

### Wildlife

| Layer | Source |
|---|---|
| Birding Hotspots | eBird `/ref/hotspot/geo` |
| eBird Obs (30 d) | eBird `/data/obs/geo/recent`, clustered with per-species checklists |
| Rare Species (iNat) | iNaturalist threatened / research-grade observations, clustered |

### Live Conditions

| Layer | Source | Notes |
|---|---|---|
| Active Wildfires | NIFC WFIGS current perimeters | Never cached. The Pine Barrens is the most fire-prone landscape in the Northeast |
| Smoke Plumes | NOAA HMS satellite smoke detection | Never cached |

### Charts & Terrain

| Layer | Source |
|---|---|
| NOAA Nautical Chart | NOAA ENC, also available as a basemap |
| Bathymetry | NOAA NCEI DEM mosaic |

## Reading the Map

**Clicking overlapping features.** Conservation polygons nest heavily here — a
vernal pool inside significant habitat inside a refuge inside the Pinelands
National Reserve. A click gathers *every* feature under the cursor and lists
them together, most specific first, rather than showing only whichever polygon
happens to be drawn on top.

**Layer stacking.** Layers are drawn in a fixed order — broad regional extents
at the bottom, specific features above, lines above those, point markers on top
— and that order is reasserted whenever you toggle something, so enabling a
large area layer never buries the small ones underneath it.

**Sidebar controls.** Each category has its own ALL / NONE buttons next to its
layer count, plus global Reset / All on / All off at the top.

## Working Offline (Local Tile Cache)

Vector data is embedded straight into the page, but basemaps and raster
overlays are normally fetched from the agency on every view. `--cache-tiles`
downloads those tile pyramids into `<output>/tiles/` and points the page at
them, so the map stops re-pulling from NOAA, NJDEP, FEMA and USFWS:

```bash
python3 lbi_map.py --bbox lbi --out output/lbi/index.html \
  --cache-tiles default --tile-zooms 9-14
```

That also vendors Leaflet and the webfont into `<output>/lib/`, so the finished
page needs **no network at all** — verified by loading it with every external
host blocked. Outside the cached zoom range a layer falls back to the live
service, so a partial cache still works.

The island preset at z9–14 is about 10,000 tiles / 39 MB across 9 sources.

| Flag | Description |
|---|---|
| `--cache-tiles [LAYERS]` | Download tiles. Accepts a group (`default`, `overlays`, `basemaps`, `open`, `all`) or a comma-separated list of source keys |
| `--tile-zooms MIN-MAX` | Zoom range to cache (default `8-14`) |
| `--tile-margin FRAC` | Extra margin per side (default `0.1`). The area is first widened to a 2:1 on-screen aspect — see below |
| `--max-tiles N` | Per-layer cap guarding against a runaway download (default 20000) |
| `--refresh-tiles` | Re-download tiles already present |
| `--vendor-libs` | Vendor Leaflet and the webfont without caching tiles |
| `--list-tile-sources` | Print the cacheable sources and exit |

Run `--list-tile-sources` to see what is available.

**Groups deliberately exclude community and commercial tile CDNs.** CARTO,
OpenStreetMap and OpenTopoMap all prohibit bulk tile downloading in their terms
of use, so `default`/`open`/`basemaps` never include them — name them
explicitly (`--cache-tiles carto_voyager`) only if you have permission. Because
of that, when a tile cache exists the builder switches the load-time basemap to
a cached one (NOAA Chart + Satellite) rather than putting the page straight back
on the network. Override with `--basemap`.

**Why the cached area is wider than the bbox.** A map pane is usually wider
than it is tall, and Leaflet's `fitBounds` fits the tighter axis — so a tall
narrow bbox like Long Beach Island ends up displaying far more longitude than
you asked for. A cache cut exactly to the bbox leaves a blank band on screen.
The download area is therefore first grown to a 2:1 on-screen aspect ratio
(accounting for latitude), then padded by `--tile-margin`.

## Deployment

The live site is published from the `gh-pages` branch:

- **[marston-j.github.io/long-beach-island](https://marston-j.github.io/long-beach-island/)** — island detail map
- **[/region.html](https://marston-j.github.io/long-beach-island/region.html)** — Barnegat Bay to the Pine Barrens

`deploy.sh` builds both pages with their tile caches and publishes them:

```bash
./deploy.sh            # rebuild from cache and deploy
./deploy.sh --fetch    # refetch all layer data first (slow)
```

Source stays on `main`; the rendered site is an orphan `gh-pages` branch holding
`index.html`, `region.html`, `tiles/` and `lib/` at its root. The script builds
that branch with `commit-tree` against a temporary index, so your working tree
is never checked out or stashed. Because the tile cache ships with the site,
visitors get chart and overlay tiles from GitHub rather than re-hitting NOAA,
NJDEP, FEMA and USFWS on every page view.

## Two-Page Deployment

`--page-link` adds a cross-link in the sidebar header, which is how the
deployed site offers both a fast island map and the full regional one:

```bash
# Fast island map as the landing page
python3 lbi_map.py --bbox lbi --out site/index.html \
  --cache-tiles default --tile-zooms 9-14 \
  --page-link "region.html|Full Pine Barrens region"

# Wider regional map
python3 lbi_map.py --bbox lbi-region --out site/region.html \
  --cache-tiles default --tile-zooms 9-13 \
  --page-link "index.html|Island detail map"
```

## CLI Flags

| Flag | Description |
|---|---|
| `--bbox` | Bounding box `S,W,N,E` or a preset name (default `lbi-region`) |
| `--out` | Write a standalone HTML map here (default `output/lbi/index.html`) |
| `--target` | Instead of `--out`, inject a Map tab into an existing checklist page |
| `--cache-dir` | Cache directory (default: the output file's parent) |
| `--center` | Base marker coordinate `lat,lng` (default `39.6444,-74.18`, the Ship Bottom causeway landing) |
| `--center-label` | Label under the base marker (default `Ship Bottom`) |
| `--title` | Page title |
| `--zoom` | Initial zoom before the search area is framed |
| `--basemap` | Basemap shown on load. Defaults to `Voyager`, or a cached basemap when tiles have been cached |
| `--ebird-key` | eBird API key, or set `EBIRD_API_KEY` |
| `--back` | eBird lookback days (default 30) |
| `--fire-bbox` | Wider bbox for the wildfire query, to catch fires just outside the map |
| `--only` | Comma-separated layer keys to refetch; everything else comes from cache |
| `--simplify` | Geometry generalization tolerance in degrees (default `0.00005`, ~6 m). Raise it to shrink the page on a large bbox; `0` keeps full-resolution geometry |
| `--habitat-rank` | Minimum Landscape Project habitat rank, 1–5 (default 4) |
| `--skip` | Comma-separated layer keys to leave out of a build entirely |
| `--render-only` | Rebuild the HTML from cache with zero network calls |
| `--page-link` | Add a sidebar header link, `HREF\|LABEL`. Repeatable |

Tile-cache flags are listed in [Working Offline](#working-offline-local-tile-cache).

## Caching & Iterating

All fetched data lands in `.map_cache.json` next to the output file. Layout or
styling changes need no network access at all:

```bash
python3 lbi_map.py --bbox lbi-region --out output/lbi/index.html --render-only
```

To refresh just one or two layers:

```bash
python3 lbi_map.py --bbox lbi-region --out output/lbi/index.html \
  --only ice_cream,mini_golf
```

Layer keys are the identifiers in `LAYER_DEFS`.

## Notes on the Data

**Page weight and generalization.** Agency salt-marsh and refuge boundaries are
digitised at survey resolution — Edwin B. Forsythe NWR alone is 74,000 vertices,
and Conservation Focal Areas arrive as 19 polygons carrying 233,000 — so a
first pass produced a 22 MB page. Geometry is generalized in three tiers:

| Tier | Tolerance | Applied to |
|---|---|---|
| Fine | ~2 m | Historical shorelines, trails, beaches — where shape carries the meaning |
| Standard | ~6 m | Most areas and historic footprints |
| Coarse | ~22 m | Regional planning and habitat polygons: focal areas, shellfish beds, MPAs, Pinelands management areas, PAD-US, refuge boundaries, OSM wetlands |

Nobody reads a regional conservation boundary to the metre, and marsh edges are
fuzzy in reality. The tolerance is requested from the service as
`maxAllowableOffset` *and* applied again client-side with Douglas-Peucker,
because server support is uneven: NJDEP honours it at a coarser scale than
documented, and hosted ArcGIS Online services such as the NOAA MPA inventory
ignore it outright. The tolerance is part of the cache key, so changing it
refetches rather than silently serving geometry at the old resolution. Override
all three tiers together with `--simplify`.

**NJDEP Wetlands 2012 only renders in a narrow scale window.** That service
blanks its image outside roughly 1:68k regardless of the documented
`minScale`/`maxScale`, so the overlay is pinned to zoom 13 and upscaled beyond
it. Use the USFWS National Wetlands Inventory overlay when you need wetland
detail at high zoom — it renders at every zoom and is the better source anyway.

**Overpass member geometry.** Overpass answers `out body; >; out skel qt;` with
the child nodes and ways of every match so geometry can be reconstructed.
Converted naively, those become untagged features and pad a layer badly — 554
of a first pass's 591 "amusements" were recursion artifacts. Features that
retain none of a layer's tags are dropped, and every layer keeps the tag that
made it match so real features are never filtered out.

**Shellfish classifications change.** Check the current NJDEP notice before
harvesting anything.

**Overpass rate limits and mirrors.** The public endpoint returns 429 and 504
freely on a bbox this size, and this build makes about 20 OSM queries, so
`lbi_map.py` rotates through mirrors instead of spending its whole backoff on
one host. Two failure modes are handled because both look like success:

- Overpass reports a server-side timeout as HTTP 200 with an empty element
  list and a `remark` field. That is treated as a failure rather than cached.
- **Several public mirrors are country-scoped.** `overpass.osm.ch` answers a
  New Jersey bbox in under a second with HTTP 200 and zero elements, which is
  indistinguishable from "there is nothing there" — it silently emptied 13
  layers before it was caught. Only add a mirror after verifying it returns
  results for a query in this region with known answers, not just a 200.

Both configured mirrors were checked that way. If you widen `OVERPASS_MIRRORS`,
do the same.

## Output

```
output/lbi/
  index.html         Standalone map (~4 MB island, ~21 MB full region)
  .map_cache.json    All fetched layer data
  tiles/             Pre-downloaded basemap and overlay tiles (--cache-tiles)
    manifest.json    Which sources and zoom ranges are available locally
  lib/               Vendored Leaflet and webfont (--vendor-libs)
```

Generated files are gitignored — regenerate locally.

## Security

- Never commit API keys. Use `EBIRD_API_KEY` or `--ebird-key`.
- The page ships a strict Content-Security-Policy allowing only the tile and
  API hosts it actually needs.
- Generated output is gitignored to prevent accidental data leakage.

## License

[MIT](LICENSE)
