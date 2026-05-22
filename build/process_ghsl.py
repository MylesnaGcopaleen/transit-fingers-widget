"""
GHSL Built-Up Surface pipeline for the transit-fingers-widget.

For each city, downloads the relevant Mollweide tile for every epoch in
[1975, 1980, ..., 2025], reprojects to WebMercator, thresholds pixels as
"built" (>BUILT_THRESHOLD fraction of surface), applies a monotonic
"first-year-built" rule to suppress flicker, then exports 11 cumulative
PNGs per city + a small JSON manifest.

Usage:
    python process_ghsl.py --city copenhagen
    python process_ghsl.py --city all

Output:
    data/<city>_<year>.png       — cumulative built-up footprint by year
    data/<city>_bounds.json      — geographic bounds + palette + epochs
"""

import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

import numpy as np
import rasterio
import requests
from PIL import Image
from pyproj import Transformer
from rasterio.warp import Resampling, calculate_default_transform, reproject
from rasterio.windows import from_bounds

# ---------- Configuration ----------

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
DATA_DIR = ROOT / "data"
CACHE_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

EPOCHS = [1975, 1980, 1985, 1990, 1995, 2000, 2005, 2010, 2015, 2020, 2025]
BUILT_THRESHOLD = 2500  # GHSL BUILT-S stores m^2 of built surface per 100m pixel (10000 m^2 total). 2500 = 25%.
MIN_PATCH_PIXELS = 4    # remove isolated speckle below this size
DILATION_RADIUS = 1     # morphological dilation in pixels to make built-up areas more visible at city scale
CORRIDOR_BUFFER_M = 2000  # 2km buffer around rail lines — the "rail-accessible" zone

BASE_URL = (
    "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/GHSL/"
    "GHS_BUILT_S_GLOBE_R2023A/GHS_BUILT_S_E{year}_GLOBE_R2023A_54009_100/V1-0/"
    "tiles/GHS_BUILT_S_E{year}_GLOBE_R2023A_54009_100_V1_0_{tile}.zip"
)

# Bounding boxes in WGS84 (min_lon, min_lat, max_lon, max_lat) covering the
# full S-Bahn / S-tog / pendeltåg / RER network. `tunnel_year` is when
# through-running service became operational — the pivot for each city's
# colour palette (pre-tunnel = baseline; post-tunnel = saturated growth ramp).
CITIES = {
    "copenhagen": {
        "tile": "R3_C19",
        "bbox_wgs84": (12.28, 55.58, 12.78, 55.85),
        "label": "Copenhagen",
        "tunnel_year": 1934,  # Boulevardbanen electrified for S-tog. GHSL starts 1975 — all data is post-tunnel.
    },
    "munich": {
        "tile": "R4_C19",
        "bbox_wgs84": (11.25, 47.95, 11.90, 48.32),
        "label": "Munich",
        "tunnel_year": 1972,  # Stammstrecke S-Bahn tunnel. GHSL starts 1975, ~3yr post-tunnel.
    },
    "zurich": {
        "tile": "R4_C19",
        "bbox_wgs84": (8.30, 47.20, 8.85, 47.55),
        "label": "Zürich",
        "tunnel_year": 1990,  # Zurich S-Bahn launch + Hirschengrabentunnel. Clean pre/post split inside GHSL range.
    },
    "paris": {
        "tile": "R4_C19",
        "bbox_wgs84": (1.85, 48.55, 2.95, 49.15),
        "label": "Paris",
        "tunnel_year": 1977,  # RER A Châtelet-Les Halles through-running tunnel. Includes RER B (1981) + D (1987).
    },
    "frankfurt": {
        "tile": "R4_C19",
        "bbox_wgs84": (8.42, 49.98, 8.88, 50.25),
        "label": "Frankfurt",
        "tunnel_year": 1978,  # Citytunnel Frankfurt — S-Bahn through-running.
    },
    "stuttgart": {
        "tile": "R4_C19",
        "bbox_wgs84": (9.00, 48.65, 9.40, 48.93),
        "label": "Stuttgart",
        "tunnel_year": 1978,  # Stuttgart S-Bahn Stammstrecke (Hbf–Schwabstrasse) opens.
    },
    "dublin": {
        "tile": "R3_C18",  # Ireland lies in tile R3_C18 (≈ -10°–0° E × 50°–60° N in Mollweide).
        "bbox_wgs84": (-6.50, 53.22, -6.08, 53.48),
        "label": "Dublin",
        "tunnel_year": 1975,  # No through-running tunnel — sentinel year; growth is classified near/far DART.
    },
}

# Three-colour scheme:
#   PRE_TUNNEL_RGBA — the city as it was before the tunnel opened (dark slate).
#   NEAR_RAIL_RGBA  — post-tunnel growth WITHIN CORRIDOR_BUFFER_M of rail (PI yellow). The story.
#   FAR_RAIL_RGBA   — post-tunnel growth FURTHER from rail (light grey). The counterfactual.
PRE_TUNNEL_RGBA = (50, 60, 75, 235)
NEAR_RAIL_RGBA  = (252, 198, 54, 255)
FAR_RAIL_RGBA   = (175, 178, 185, 220)

# Pixel-class sentinels in the classified array.
CLS_NEVER     = 0
CLS_PRE       = 1
CLS_NEAR_RAIL = 2
CLS_FAR_RAIL  = 3


# ---------- Helpers ----------


def download_tile(year: int, tile: str) -> Path:
    """Download a single GHSL tile zip into the cache; return path."""
    cache_path = CACHE_DIR / f"GHS_BUILT_S_E{year}_{tile}.zip"
    if cache_path.exists() and cache_path.stat().st_size > 1000:
        return cache_path

    url = BASE_URL.format(year=year, tile=tile)
    print(f"  downloading {year} {tile}...", end=" ", flush=True)
    t0 = time.time()
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    with open(cache_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            f.write(chunk)
    print(f"{cache_path.stat().st_size / 1e6:.1f} MB in {time.time() - t0:.1f}s")
    return cache_path


def extract_tif(zip_path: Path) -> bytes:
    """Pull the GeoTIFF bytes out of a zip without writing to disk."""
    with zipfile.ZipFile(zip_path) as zf:
        tif_name = next(n for n in zf.namelist() if n.endswith(".tif"))
        return zf.read(tif_name)


def read_window_mollweide(tif_bytes: bytes, bbox_wgs84: tuple) -> tuple[np.ndarray, dict]:
    """
    Open GHSL tile from in-memory bytes, crop to the city bbox.
    Returns: (array of built-up surface m^2 per pixel, src profile updated for window).
    """
    to_moll = Transformer.from_crs("EPSG:4326", "ESRI:54009", always_xy=True)
    min_lon, min_lat, max_lon, max_lat = bbox_wgs84

    # Sample bbox corners + edge midpoints in Mollweide to get a generous bounding rectangle.
    corners = [
        (min_lon, min_lat), (max_lon, min_lat),
        (min_lon, max_lat), (max_lon, max_lat),
        ((min_lon + max_lon) / 2, min_lat), ((min_lon + max_lon) / 2, max_lat),
    ]
    xs, ys = [], []
    for lon, lat in corners:
        x, y = to_moll.transform(lon, lat)
        xs.append(x); ys.append(y)
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    with rasterio.MemoryFile(tif_bytes) as mem:
        with mem.open() as src:
            window = from_bounds(min_x, min_y, max_x, max_y, src.transform)
            arr = src.read(1, window=window)
            win_transform = rasterio.windows.transform(window, src.transform)
            profile = src.profile.copy()
            profile.update(
                transform=win_transform,
                height=arr.shape[0],
                width=arr.shape[1],
            )
    return arr, profile


def reproject_to_webmercator(arr: np.ndarray, src_profile: dict, target_bbox_wgs84: tuple) -> tuple[np.ndarray, dict]:
    """
    Reproject the Mollweide array to WebMercator (EPSG:3857) clipped to the target WGS84 bbox.
    Returns reprojected array + new profile (transform, crs, dims).
    """
    to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    min_lon, min_lat, max_lon, max_lat = target_bbox_wgs84
    left, bottom = to_merc.transform(min_lon, min_lat)
    right, top = to_merc.transform(max_lon, max_lat)

    # Pick output resolution: 100m source ≈ keep it close, but in Mercator units (meters at the equator
    # are not the same as meters at latitude φ). Use ~120 m pixel to keep file sizes reasonable.
    pixel_size = 120.0
    width = int(round((right - left) / pixel_size))
    height = int(round((top - bottom) / pixel_size))

    dst_transform = rasterio.transform.from_bounds(left, bottom, right, top, width, height)
    dst = np.zeros((height, width), dtype=arr.dtype)
    reproject(
        source=arr,
        destination=dst,
        src_transform=src_profile["transform"],
        src_crs=src_profile["crs"],
        dst_transform=dst_transform,
        dst_crs="EPSG:3857",
        resampling=Resampling.average,
    )
    dst_profile = dict(
        crs="EPSG:3857",
        transform=dst_transform,
        width=width,
        height=height,
        bbox_mercator=(left, bottom, right, top),
        bbox_wgs84=target_bbox_wgs84,
    )
    return dst, dst_profile


def first_year_built(stack: np.ndarray) -> np.ndarray:
    """
    Given stack shape (n_epochs, H, W) of built-up surface (m^2 per 100m pixel),
    return an array shape (H, W) where each pixel is the index of the earliest
    epoch from which the pixel is "built" (above threshold) and *stays* built.
    Pixels never reaching the threshold get value 255 (sentinel for "never built").
    """
    n, h, w = stack.shape
    built = stack >= BUILT_THRESHOLD  # (n, h, w) bool
    # Monotonic: pixel must be built at epoch i AND in every subsequent epoch.
    # `stays_built[i]` = True iff built at i AND all later epochs.
    # Compute backwards cumulative AND.
    stays = np.empty_like(built)
    stays[-1] = built[-1]
    for i in range(n - 2, -1, -1):
        stays[i] = built[i] & stays[i + 1]
    # First index where stays is True. argmax returns 0 if all False, so mask first.
    first = stays.argmax(axis=0).astype(np.uint8)
    never = ~stays.any(axis=0)
    first[never] = 255
    return first


def remove_speckle(first_arr: np.ndarray, min_size: int = MIN_PATCH_PIXELS) -> np.ndarray:
    """
    Drop isolated specks: any connected component (any-built mask) smaller than
    min_size pixels gets reset to "never built" (255). Cleans up rural noise
    without touching the dense urban patches.
    """
    from scipy.ndimage import label
    try:
        from scipy.ndimage import label  # noqa: F811
    except ImportError:
        return first_arr  # graceful fallback if scipy unavailable
    cleaned = first_arr.copy()
    mask = cleaned != 255
    labels, n_labels = label(mask, structure=np.ones((3, 3), dtype=int))
    if n_labels == 0:
        return cleaned
    sizes = np.bincount(labels.ravel())
    too_small = (sizes < min_size)
    too_small[0] = False  # background label
    drop = too_small[labels]
    cleaned[drop] = 255
    return cleaned


def dilate_per_epoch(first_arr: np.ndarray, radius: int = DILATION_RADIUS) -> np.ndarray:
    """
    Visually expand each built-up pixel so it reads as a blob at city scale.
    The dilation respects the temporal ordering: a pixel that was empty in
    epoch i but adjacent to an epoch-i built pixel gets coloured as epoch i.
    Empty pixels stay empty if no neighbour was ever built.
    """
    try:
        from scipy.ndimage import grey_dilation
    except ImportError:
        return first_arr
    if radius <= 0:
        return first_arr
    # Treat "never built" (255) as a high value so it loses to any real epoch
    # in a *minimum* filter. We want: a pixel adopts the SMALLEST (earliest)
    # epoch found in its neighbourhood. So we run minimum_filter on first_arr.
    from scipy.ndimage import minimum_filter
    size = 2 * radius + 1
    return minimum_filter(first_arr, size=size, mode="constant", cval=255)


def build_corridor_mask(rail_path: Path, profile: dict) -> np.ndarray:
    """
    Rasterise a CORRIDOR_BUFFER_M buffer around every rail LineString onto the
    same EPSG:3857 grid as the GHSL output. Returns a bool mask (True = within
    the rail corridor).
    """
    import math
    from rasterio.features import rasterize
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    if not rail_path.exists():
        return np.zeros((profile["height"], profile["width"]), dtype=bool)

    with open(rail_path) as f:
        gj = json.load(f)

    # Reproject every LineString to EPSG:3857.
    to_merc = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    lines = []
    for feat in gj.get("features", []):
        geom = feat.get("geometry", {})
        gtype = geom.get("type")
        coord_lists = []
        if gtype == "MultiLineString":
            coord_lists = geom["coordinates"]
        elif gtype == "LineString":
            coord_lists = [geom["coordinates"]]
        for coords in coord_lists:
            proj = [to_merc.transform(x, y) for x, y in coords if x is not None and y is not None]
            if len(proj) >= 2:
                lines.append(LineString(proj))
    if not lines:
        return np.zeros((profile["height"], profile["width"]), dtype=bool)

    # Web Mercator inflates ground distance by ~1/cos(lat) at our latitude, so
    # scale the buffer up to land 2km on the ground.
    min_lon, min_lat, max_lon, max_lat = profile["bbox_wgs84"]
    mid_lat_rad = math.radians((min_lat + max_lat) / 2)
    buffer_merc = CORRIDOR_BUFFER_M / math.cos(mid_lat_rad)

    union = unary_union([line.buffer(buffer_merc) for line in lines])
    if union.is_empty:
        return np.zeros((profile["height"], profile["width"]), dtype=bool)

    mask = rasterize(
        [(union, 1)],
        out_shape=(profile["height"], profile["width"]),
        transform=profile["transform"],
        fill=0,
        dtype=np.uint8,
    )
    return mask.astype(bool)


def classify_pixels(first_arr: np.ndarray, corridor_mask: np.ndarray, pre_tunnel_index: int) -> np.ndarray:
    """
    Assign each pixel one of CLS_NEVER / CLS_PRE / CLS_NEAR_RAIL / CLS_FAR_RAIL.

    pre_tunnel_index = last epoch INDEX strictly before the tunnel year.
    Pixels with first_arr <= pre_tunnel_index → CLS_PRE.
    Pixels with first_arr > pre_tunnel_index AND inside the corridor → CLS_NEAR_RAIL.
    Pixels with first_arr > pre_tunnel_index AND outside the corridor → CLS_FAR_RAIL.
    For cities where the tunnel pre-dates GHSL we pass pre_tunnel_index=0 so the
    1975 footprint is the "pre" baseline and 1980+ is split near/far.
    """
    cls = np.full(first_arr.shape, CLS_NEVER, dtype=np.uint8)
    ever_built = first_arr != 255
    pre_mask = ever_built & (first_arr <= pre_tunnel_index)
    post_mask = ever_built & (first_arr > pre_tunnel_index)
    cls[pre_mask] = CLS_PRE
    cls[post_mask & corridor_mask] = CLS_NEAR_RAIL
    cls[post_mask & ~corridor_mask] = CLS_FAR_RAIL
    return cls


def encode_cumulative_png(first_arr: np.ndarray, classes: np.ndarray, epoch_idx: int, out_path: Path) -> int:
    """
    Render an RGBA PNG showing:
      - all CLS_PRE pixels (already-built city) at all times,
      - CLS_NEAR_RAIL pixels whose first_year_built <= epoch_idx,
      - CLS_FAR_RAIL  pixels whose first_year_built <= epoch_idx.
    """
    h, w = first_arr.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    visible = (first_arr != 255) & (first_arr <= epoch_idx)
    for cls_id, rgba_tuple in (
        (CLS_PRE, PRE_TUNNEL_RGBA),
        (CLS_FAR_RAIL, FAR_RAIL_RGBA),    # paint far-rail first so near-rail wins on overlaps
        (CLS_NEAR_RAIL, NEAR_RAIL_RGBA),
    ):
        m = visible & (classes == cls_id)
        rgba[m, 0] = rgba_tuple[0]
        rgba[m, 1] = rgba_tuple[1]
        rgba[m, 2] = rgba_tuple[2]
        rgba[m, 3] = rgba_tuple[3]
    Image.fromarray(rgba).save(out_path, optimize=True)
    return out_path.stat().st_size


def process_city(slug: str):
    cfg = CITIES[slug]
    print(f"\n=== {cfg['label']} ({slug}) ===")
    bbox = cfg["bbox_wgs84"]
    tile = cfg["tile"]

    # 1) Download + crop every epoch, reproject each to a common WebMercator grid.
    print("Step 1: download + crop + reproject")
    reprojected = []
    common_profile = None
    for year in EPOCHS:
        zip_path = download_tile(year, tile)
        tif_bytes = extract_tif(zip_path)
        arr_moll, src_profile = read_window_mollweide(tif_bytes, bbox)
        arr_merc, prof = reproject_to_webmercator(arr_moll, src_profile, bbox)
        if common_profile is None:
            common_profile = prof
        else:
            # Should be identical because we use the same bbox + pixel size.
            assert prof["width"] == common_profile["width"]
            assert prof["height"] == common_profile["height"]
        reprojected.append(arr_merc)
        print(f"  {year}: arr shape {arr_merc.shape}, built pixels {(arr_merc >= BUILT_THRESHOLD).sum()}")
    stack = np.stack(reprojected, axis=0)

    # 2) Compute first-year-built map + de-speckle + dilate for visibility
    print("Step 2: first-year-built map (monotonic rule)")
    first = first_year_built(stack)
    n_before = (first != 255).sum()
    first = remove_speckle(first)
    n_after_speckle = (first != 255).sum()
    first = dilate_per_epoch(first, radius=DILATION_RADIUS)
    n_built = (first != 255).sum()
    print(f"  pixels: raw={n_before}, after de-speckle={n_after_speckle}, after dilate={n_built}")

    # 3) Build the rail corridor mask from the city's rail GeoJSON.
    print("Step 3: rail corridor mask (2km buffer)")
    rail_path = DATA_DIR / f"{slug}_rail.geojson"
    corridor = build_corridor_mask(rail_path, common_profile)
    print(f"  corridor pixels: {int(corridor.sum())} ({corridor.sum() / corridor.size:.1%} of bbox)")

    # 4) Classify every pixel as pre-tunnel / near-rail / far-rail / never.
    # Pre-tunnel index: largest epoch index with EPOCHS[i] < tunnel_year.
    # If the tunnel pre-dates the dataset, pre-tunnel = the 1975 footprint (index 0).
    tunnel_year = cfg["tunnel_year"]
    pre_tunnel_index = -1
    for i, ep in enumerate(EPOCHS):
        if ep < tunnel_year:
            pre_tunnel_index = i
    if pre_tunnel_index < 0:
        pre_tunnel_index = 0  # take the 1975 footprint as the baseline
    # Special case — `no_tunnel` cities (Dublin counterfactual): the rail corridor
    # is meaningless, so treat the whole bbox as "in corridor" so every post-1975
    # pixel becomes CLS_NEAR_RAIL (rendered in yellow). The narrative is "all this
    # sprawl could have been around rail, but isn't".
    if cfg.get("no_tunnel"):
        corridor = np.ones_like(corridor, dtype=bool)
    classes = classify_pixels(first, corridor, pre_tunnel_index)
    print(f"  classes (pre/near/far): "
          f"{int((classes == CLS_PRE).sum())} / "
          f"{int((classes == CLS_NEAR_RAIL).sum())} / "
          f"{int((classes == CLS_FAR_RAIL).sum())}")

    # 5) Per-epoch area stats (km²): pre-tunnel city, post-tunnel near rail,
    # post-tunnel far from rail. WebMercator inflates pixel area by
    # 1/cos(lat)^2, so multiply by cos²(lat) for the true ground area.
    import math
    pixel_size_m = 120.0
    mid_lat_rad = math.radians((bbox[1] + bbox[3]) / 2)
    true_pixel_area_km2 = (pixel_size_m ** 2) * (math.cos(mid_lat_rad) ** 2) / 1e6
    area_pre = round(int((classes == CLS_PRE).sum()) * true_pixel_area_km2, 2)
    area_near_by_epoch = []
    area_far_by_epoch = []
    for i in range(len(EPOCHS)):
        visible = (first != 255) & (first <= i)
        area_near_by_epoch.append(round(int((visible & (classes == CLS_NEAR_RAIL)).sum()) * true_pixel_area_km2, 2))
        area_far_by_epoch.append(round(int((visible & (classes == CLS_FAR_RAIL)).sum()) * true_pixel_area_km2, 2))
    final_near = area_near_by_epoch[-1]
    final_far = area_far_by_epoch[-1]
    total_post = final_near + final_far
    near_share = (final_near / total_post * 100.0) if total_post > 0 else 0.0

    # 6) Export cumulative PNGs (one per epoch)
    print("Step 6: export cumulative PNGs (3-colour: pre / near-rail / far)")
    total_bytes = 0
    for i, year in enumerate(EPOCHS):
        out_path = DATA_DIR / f"{slug}_{year}.png"
        total_bytes += encode_cumulative_png(first, classes, i, out_path)
    print(f"  total: {total_bytes / 1024:.1f} KB")

    # 7) Per-city manifest with bounds + classified area stats
    bounds_path = DATA_DIR / f"{slug}_bounds.json"
    min_lon, min_lat, max_lon, max_lat = bbox
    bounds_path.write_text(json.dumps({
        "slug": slug,
        "label": cfg["label"],
        "tunnel_year": tunnel_year,
        "tunnel_predates_dataset": tunnel_year < EPOCHS[0],
        "bbox_wgs84": [min_lon, min_lat, max_lon, max_lat],
        "epochs": EPOCHS,
        "corridor_buffer_m": CORRIDOR_BUFFER_M,
        "palette_rgba": {
            "pre_tunnel": list(PRE_TUNNEL_RGBA),
            "near_rail":  list(NEAR_RAIL_RGBA),
            "far_rail":   list(FAR_RAIL_RGBA),
        },
        "area_pre_tunnel_km2": area_pre,
        "area_near_rail_km2_by_epoch": area_near_by_epoch,
        "area_far_rail_km2_by_epoch":  area_far_by_epoch,
        "near_rail_share_pct": round(near_share, 1),
        "raster_width": int(common_profile["width"]),
        "raster_height": int(common_profile["height"]),
    }, indent=2))
    print(f"  wrote {bounds_path.name}: pre-tunnel {area_pre} km², "
          f"post-tunnel +{total_post:.1f} km² ({near_share:.0f}% near rail)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="all", choices=["all", *CITIES.keys()])
    args = parser.parse_args()
    targets = list(CITIES.keys()) if args.city == "all" else [args.city]
    for slug in targets:
        process_city(slug)
    print("\nDone.")


if __name__ == "__main__":
    main()
