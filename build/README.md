# Transit-fingers widget — data pipeline

This directory holds the offline data pipeline. The widget itself (`../index.html`)
runs against pre-built static files in `../data/`. The pipeline is only re-run when
GHSL releases new epochs, or when we want to tweak the threshold / palette / cities.

## What it produces

- `../data/{copenhagen,stockholm,munich}_{1975..2025}.png` — cumulative built-up
  rasters, one per 5-year epoch, RGBA, indexed-colour palette. Each pixel's colour
  encodes the epoch it first became "built" (≥25% surface in GHSL terms) and stays
  that way.
- `../data/{copenhagen,stockholm,munich}_rail.geojson` — commuter-rail networks
  (S-tog / pendeltåg / S-Bahn) as MultiLineString features, one per route ref,
  with a hand-curated `opened_year` property.
- `../data/{copenhagen,stockholm,munich}_bounds.json` — bbox + palette manifest
  used to debug. The widget hardcodes equivalents in JS.

Total payload ≈ 3.9 MB.

## Re-running

```bash
cd build/
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python process_ghsl.py --city all     # ~45–60s total; downloads cached in .cache/
python fetch_rail.py                  # ~10s; hits Overpass API
```

## Data sources

- **Built-up area**: [GHS-BUILT-S R2023A](https://human-settlement.emergency.copernicus.eu/),
  100m resolution, ESRI:54009 (Mollweide). CC-BY 4.0 — attribute "© European
  Commission JRC". The pipeline downloads three 10°×10° tiles per epoch
  (R3_C19 for Copenhagen, R3_C20 for Stockholm, R4_C19 for Munich), zipped at
  13–40 MB each.
- **Rail networks**: OpenStreetMap via Overpass API. ODbL — attribute "©
  OpenStreetMap contributors". Filters in `fetch_rail.py` are tuned to each
  operator's tagging style.
- **Opening years**: hand-curated from Wikipedia + each operator's history pages
  (see `LINE_HISTORY` dict in `fetch_rail.py`). One year per route ref, picking
  the date the current end-to-end alignment was substantially in place.

## Knobs to tune

- `BUILT_THRESHOLD` (`process_ghsl.py`): currently 2500 = 25% built surface.
  Lower → more rural noise; higher → urban edges shrink. 2500 looked right for
  Copenhagen so we kept it for the others.
- `MIN_PATCH_PIXELS` (`process_ghsl.py`): drops connected components below this
  size. 4 pixels removes per-pixel speckle without affecting visible features.
- `PALETTE` (`process_ghsl.py`): 11 (R, G, B, A) tuples. The 1975 baseline is
  deliberately desaturated and half-opaque so post-1975 growth reads against it.
- `LINE_HISTORY` (`fetch_rail.py`): opening years per route ref. Adjust per
  operator as needed; the `default` key catches refs that aren't listed.

## Adding a fourth city

1. Pick a bbox in WGS84.
2. Figure out which Mollweide tile contains it (`process_ghsl.py` has a small
   helper at the top — or transform corner coords with pyproj and check tile
   R{row}_C{col} from the GHSL tile listing).
3. Add an entry to `CITIES` in `process_ghsl.py` with the tile and bbox.
4. Add an Overpass query + `LINE_HISTORY` entry to `fetch_rail.py`.
5. Run both scripts.
6. Add the city to `CITIES` and the tab list in `../index.html`.
