"""
Fetch rail networks for the transit-fingers-widget and annotate with opening
years (hand-curated from Wikipedia + each operator's history pages).

For each city:
    1. Query Overpass for the relevant commuter-rail / S-Bahn relations.
    2. Resolve each relation to a single GeoJSON LineString per line (merging member ways).
    3. Attach an `opened_year` property per line using the LINE_HISTORY tables below.

We deliberately work at the *line* level (not segment level) to keep curation
tractable. A more granular history (branch-by-branch opening dates) would be
nicer but requires expert-level work; for a narrative visualization, per-line
dates are enough.

Output:
    data/<city>_rail.geojson
"""

import json
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
TIMEOUT = 120
HEADERS = {"User-Agent": "transit-fingers-widget/1.0 (Progress Ireland)"}


# Hand-curated opening years per line. Source: Wikipedia + operator history pages.
# Keys are normalised lowercase line refs (e.g. "S1", "A", "41" for Pendeltåg).
# Where a line has multi-stage history, we use the year the *current end-to-end*
# alignment was substantially in place (so animation timing is plausible).
LINE_HISTORY = {
    "copenhagen": {
        # S-tog lines (OSM route=light_rail, name pattern "S-tog X").
        # Through-running via the Boulevard tunnel between Hovedbanegården and
        # Østerport opened 1917 (steam) / 1934 (electrified S-tog).
        "A": 1934,   # Klampenborg ↔ Hillerød — opened 1934
        "B": 1977,   # Farum line — opened 1977 (note: ref reused historically)
        "Bx": 1989,  # Frederikssund express
        "C": 1934,   # Klampenborg ↔ Frederikssund (Frederikssund extension 1989)
        "E": 1972,   # Køge Bugt — opened 1972
        "F": 2002,   # Ringbanen present alignment
        "H": 1989,   # Frederikssund all-stops
        "default": 1934,
    },
    "munich": {
        # S-Bahn. All lines launched 1972 together with the Stammstrecke
        # through-running tunnel.
        "S1": 1972, "S2": 1972, "S3": 1972, "S4": 1972,
        "S5": 1972, "S6": 1972, "S7": 1972, "S8": 1972,
        "S20": 2004,
        "default": 1972,
    },
    "zurich": {
        # Zurich S-Bahn launched 1990 with the Hirschengrabentunnel +
        # Zürichberg tunnel completing through-running. All S-lines opened
        # together 1990; later additions are mostly branch refinements.
        "S1": 1990, "S2": 1990, "S3": 1990, "S4": 1990,
        "S5": 1990, "S6": 1990, "S7": 1990, "S8": 1990,
        "S9": 1990, "S10": 1990, "S11": 1990, "S12": 1990,
        "S14": 1990, "S15": 1990, "S16": 1990, "S19": 1990,
        "S21": 2014, "S24": 1990, "S25": 1990,
        "default": 1990,
    },
    "paris": {
        # RER. RER A central tunnel (Châtelet-Les Halles) opened 1977.
        # RER B southern link 1981. Other lines later.
        "A": 1977,
        "B": 1981,
        "C": 1979,
        "D": 1987,
        "E": 1999,
        "default": 1977,
    },
    "frankfurt": {
        # Frankfurt S-Bahn Citytunnel opened 1978 in stages.
        "S1": 1978, "S2": 1978, "S3": 1978, "S4": 1978,
        "S5": 1978, "S6": 1978, "S8": 1992, "S9": 1997,
        "default": 1978,
    },
    "stuttgart": {
        # Stuttgart S-Bahn Stammstrecke opened 1978; later lines progressively.
        "S1": 1978, "S2": 1978, "S3": 1978,
        "S4": 1981, "S5": 1985, "S6": 1981,
        "S60": 2010,
        "default": 1978,
    },
    "dublin": {
        # DART electrified suburban network. Loops the bay, no through-running tunnel.
        "default": 1984,
    },
}


# Overpass queries — narrow filters keep response sizes manageable.
# Each query fetches the route relations (with member geometry) AND each
# member way's tags (especially `tunnel=yes`). The output is two element types:
#   - relations: with members[].geometry (no member tags)
#   - ways:      with tags (no geometry)
# We join the two by way id to split rail into tunnel vs non-tunnel segments.
QUERIES = {
    "copenhagen": (
        '[out:json][timeout:90];'
        'relation["route"="light_rail"]["name"~"S-tog"](55.3,11.8,56.0,12.9)->.r;'
        '.r out geom; way(r.r); out tags;'
    ),
    "munich": (
        '[out:json][timeout:90];'
        'relation["route"="train"]["ref"~"^S[0-9]+$"](47.6,10.7,48.7,12.3)->.r;'
        '.r out geom; way(r.r); out tags;'
    ),
    "zurich": (
        '[out:json][timeout:90];'
        'relation["route"="train"]["network"~"ZVV|S-Bahn Zürich",i]["ref"~"^S([1-9]|1[0-6])$"](47.0,8.0,47.8,9.1)->.r;'
        '.r out geom; way(r.r); out tags;'
    ),
    "paris": (
        '[out:json][timeout:120];'
        '(relation["route"="train"]["ref"~"^[A-E]$"]["name"~"RER"](48.4,1.6,49.2,3.2);'
        'relation["route"="subway"]["ref"~"^RER [A-E]$"](48.4,1.6,49.2,3.2);)->.r;'
        '.r out geom; way(r.r); out tags;'
    ),
    "frankfurt": (
        '[out:json][timeout:90];'
        'relation["route"="train"]["network"~"Rhein-Main",i]["ref"~"^S[0-9]+$"](49.8,8.0,50.5,9.2)->.r;'
        '.r out geom; way(r.r); out tags;'
    ),
    "stuttgart": (
        '[out:json][timeout:90];'
        'relation["route"="train"]["network"~"Verkehrs- und Tarifverbund Stuttgart|VVS",i]["ref"~"^S[0-9]+$"](48.4,8.6,49.2,9.7)->.r;'
        '.r out geom; way(r.r); out tags;'
    ),
    "dublin": (
        '[out:json][timeout:90];'
        'relation["route"="train"]["name"~"DART",i](53.2,-6.6,53.5,-6.0)->.r;'
        '.r out geom; way(r.r); out tags;'
    ),
}


# (Previously: a TUNNEL_CONNECTED_REFS whitelist dropped routes that don't pass
# through the through-running tunnel — RER C/E from Paris, S60/S62 from
# Stuttgart, etc. That filter made the visible rail network look fragmented.
# Now we keep ALL routes in the suburban layer so the network reads as
# continuous, and rely on the polyline-buffer filter below to only thicken
# the actual central tunnel section.)
TUNNEL_CONNECTED_REFS = {}


# Per-city CENTRAL THROUGH-RUNNING TUNNEL polylines (lon, lat). A tunnel way is
# kept ONLY if its midpoint is within CENTRAL_TUNNEL_BUFFER_KM of this polyline.
# This is a sharper filter than "distance from a single centre point" — it
# excludes peripheral tunnels (Wipkingen, Käferberg, Wiedikon, Tiefenbrunnen,
# Mühlberg eastern extension, etc.) and keeps only the actual central tunnel.
CENTRAL_TUNNEL_LINES = {
    "zurich": [
        # Hirschengrabentunnel (HB → Stadelhofen) + Zürichbergtunnel (Stadelhofen → Stettbach)
        (8.5403, 47.3781), (8.5478, 47.3667), (8.5700, 47.3850), (8.5995, 47.4023),
    ],
    "paris": [
        # RER A central + B southern link + D — Auber/Étoile → Châtelet → Gare de Lyon → Nation
        (2.2953, 48.8744), (2.3303, 48.8709), (2.3471, 48.8620),
        (2.3736, 48.8443), (2.3964, 48.8484),
    ],
    "frankfurt": [
        # Citytunnel: HBf → Taunusanlage → Hauptwache → Konstablerwache (1978)
        (8.6629, 50.1071), (8.6710, 50.1110), (8.6786, 50.1138), (8.6831, 50.1148),
    ],
    "stuttgart": [
        # Stammstrecke: HBf → Stadtmitte → Feuersee → Schwabstrasse
        (9.1812, 48.7833), (9.1740, 48.7747), (9.1715, 48.7720), (9.1664, 48.7716),
    ],
    "munich": [
        # Stammstrecke: HBf → Karlsplatz → Marienplatz → Isartor → Ostbahnhof
        (11.5601, 48.1402), (11.5660, 48.1392), (11.5755, 48.1374),
        (11.5824, 48.1352), (11.5915, 48.1315), (11.6045, 48.1271),
    ],
    "copenhagen": [
        # Boulevardbanen: Hovedbanegården → Vesterport → Nørreport → Østerport
        (12.5654, 55.6724), (12.5645, 55.6804), (12.5713, 55.6833), (12.5867, 55.6921),
    ],
    "dublin": None,
}
CENTRAL_TUNNEL_BUFFER_KM = 0.5  # how far a way's midpoint can be from the polyline


def call_overpass(query: str) -> dict:
    """POST a query to Overpass with retry/backoff on 5xx."""
    last_exc = None
    for attempt in range(4):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code >= 500:
                last_exc = requests.exceptions.HTTPError(f"{resp.status_code}")
                time.sleep(5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            last_exc = e
            time.sleep(5 * (attempt + 1))
    raise last_exc


def relation_to_linestrings(rel: dict) -> list[list[list[float]]]:
    """
    Convert a relation's member ways (with geom from `out geom;`) to a list of
    LineString coordinate arrays.
    """
    lines = []
    for member in rel.get("members", []):
        if member.get("type") != "way" or "geometry" not in member:
            continue
        coords = [[g["lon"], g["lat"]] for g in member["geometry"]]
        if len(coords) >= 2:
            lines.append(coords)
    return lines


def merge_consecutive(lines: list[list[list[float]]]) -> list[list[list[float]]]:
    """
    Greedy merge of LineStrings that share endpoints, to reduce feature count.
    Not strictly necessary, but the GeoJSON gets a bit smaller and renders cleaner.
    """
    EPS = 1e-6
    remaining = [list(line) for line in lines]
    merged = []
    while remaining:
        cur = remaining.pop(0)
        changed = True
        while changed and remaining:
            changed = False
            for i, other in enumerate(remaining):
                if abs(cur[-1][0] - other[0][0]) < EPS and abs(cur[-1][1] - other[0][1]) < EPS:
                    cur.extend(other[1:]); remaining.pop(i); changed = True; break
                if abs(cur[-1][0] - other[-1][0]) < EPS and abs(cur[-1][1] - other[-1][1]) < EPS:
                    cur.extend(list(reversed(other))[1:]); remaining.pop(i); changed = True; break
                if abs(cur[0][0] - other[-1][0]) < EPS and abs(cur[0][1] - other[-1][1]) < EPS:
                    cur = other + cur[1:]; remaining.pop(i); changed = True; break
                if abs(cur[0][0] - other[0][0]) < EPS and abs(cur[0][1] - other[0][1]) < EPS:
                    cur = list(reversed(other)) + cur[1:]; remaining.pop(i); changed = True; break
        merged.append(cur)
    return merged


def fetch_city(slug: str):
    print(f"\n=== {slug} ===")
    query = QUERIES[slug]
    history = LINE_HISTORY[slug]
    t0 = time.time()
    data = call_overpass(query)
    rels = [e for e in data.get("elements", []) if e.get("type") == "relation"]
    ways = [e for e in data.get("elements", []) if e.get("type") == "way"]
    print(f"  overpass: {len(rels)} relations + {len(ways)} way-tag records in {time.time() - t0:.1f}s")

    # Index way tags by id so we can flag tunnel sections later.
    way_tags = {w["id"]: w.get("tags", {}) for w in ways}
    def is_tunnel(way_id):
        t = way_tags.get(way_id, {}).get("tunnel", "")
        return t in ("yes", "building_passage")

    # Geographic filter for tunnel ways: only keep ways whose midpoint is within
    # CENTRAL_TUNNEL_BUFFER_KM of the city's central tunnel polyline. This is
    # tighter than a radial filter — it follows the actual line of the tunnel,
    # so peripheral tunnels (Wipkingen, Käferberg, Tiefenbrunnen, etc.) get
    # dropped while the central tunnel chain is preserved.
    import math
    from shapely.geometry import LineString, Point
    tunnel_line_wgs84 = CENTRAL_TUNNEL_LINES.get(slug)
    if tunnel_line_wgs84:
        avg_lat = sum(p[1] for p in tunnel_line_wgs84) / len(tunnel_line_wgs84)
        cos_lat = math.cos(math.radians(avg_lat))
        # Convert to a local Cartesian frame (km) so distances are euclidean.
        tunnel_line = LineString([
            (lon * 111.0 * cos_lat, lat * 111.0) for lon, lat in tunnel_line_wgs84
        ])
    else:
        tunnel_line = None
        cos_lat = 1.0
    def way_in_central_tunnel(dedup_coords):
        if tunnel_line is None:
            return False
        avg_x = sum(p[0] for p in dedup_coords) / len(dedup_coords)
        avg_y = sum(p[1] for p in dedup_coords) / len(dedup_coords)
        pt = Point(avg_x * 111.0 * cos_lat, avg_y * 111.0)
        return pt.distance(tunnel_line) <= CENTRAL_TUNNEL_BUFFER_KM

    # Group ways by (ref, is_central_tunnel). KEY POINT: we keep ALL ways in the
    # output, including peripheral tunnel ways (Wipkingen, Käferberg, etc.).
    # Otherwise the network would look full of gaps wherever a line briefly went
    # underground. We just distinguish "central tunnel" ways (the through-running
    # tunnel polyline we want to highlight) from everything else.
    ways_by_key = {}  # (ref, is_central_tunnel_bool) -> dict of signature -> coords
    n_central = 0
    for rel in rels:
        tags = rel.get("tags", {})
        ref = tags.get("ref") or tags.get("name") or str(rel.get("id"))
        for member in rel.get("members", []):
            if member.get("type") != "way" or "geometry" not in member:
                continue
            way_id = member.get("ref")
            tunnel_flag = is_tunnel(way_id)
            coords = [(g["lon"], g["lat"]) for g in member["geometry"]]
            rounded = [[round(x, 4), round(y, 4)] for x, y in coords]
            # collapse duplicate consecutive points introduced by rounding
            dedup = [rounded[0]]
            for p in rounded[1:]:
                if p[0] != dedup[-1][0] or p[1] != dedup[-1][1]:
                    dedup.append(p)
            if len(dedup) < 2:
                continue
            # is_central_tunnel = a tunnel-tagged way that lies on the curated
            # through-running tunnel polyline. These get the hero treatment.
            central_flag = tunnel_flag and way_in_central_tunnel(dedup)
            if central_flag:
                n_central += 1
            sig_fwd = tuple((p[0], p[1]) for p in dedup)
            sig_rev = tuple(reversed(sig_fwd))
            sig = min(sig_fwd, sig_rev)
            key = (ref, central_flag)
            ways_by_key.setdefault(key, {}).setdefault(sig, dedup)
    print(f"  {n_central} central-tunnel ways will get the hero treatment")

    # Optional tunnel-only filter (route refs that don't pass through the
    # through-running tunnel — e.g. RER C/E, Stuttgart S60). Applied at the
    # ref level so it affects both their tunnel and non-tunnel ways uniformly.
    keep_refs = TUNNEL_CONNECTED_REFS.get(slug)
    if keep_refs is not None:
        dropped = sorted({k[0] for k in ways_by_key if k[0] not in keep_refs})
        for k in [k for k in ways_by_key if k[0] not in keep_refs]:
            ways_by_key.pop(k)
        if dropped:
            print(f"  tunnel-only filter: dropped non-tunnel-connected lines {dropped}")

    features = []
    refs_seen = set()
    for (ref, central_flag), ways_for_key in sorted(ways_by_key.items()):
        opened = history.get(ref) or history.get(ref.lstrip("S0")) or history.get("default")
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "MultiLineString",
                "coordinates": list(ways_for_key.values()),
            },
            "properties": {
                "ref": ref,
                "opened_year": opened,
                # The widget only uses this property — everything where
                # is_central_tunnel != true is rendered as a continuous network
                # line; is_central_tunnel == true is the hero tunnel section
                # that opens at tunnel_year.
                "is_central_tunnel": central_flag,
                "segment_count": len(ways_for_key),
            },
        })
        refs_seen.add(ref)

    n_central_feats = sum(1 for f in features if f["properties"]["is_central_tunnel"])
    n_other_feats = sum(1 for f in features if not f["properties"]["is_central_tunnel"])
    print(f"  {len(refs_seen)} routes, {n_other_feats} network features, {n_central_feats} central-tunnel features")
    fc = {"type": "FeatureCollection", "features": features}
    out_path = DATA_DIR / f"{slug}_rail.geojson"
    out_path.write_text(json.dumps(fc))
    print(f"  wrote {out_path.name} ({out_path.stat().st_size / 1024:.1f} KB)")


def main():
    import sys
    if len(sys.argv) > 1:
        targets = sys.argv[1:]
    else:
        targets = ["copenhagen", "munich", "zurich", "paris", "frankfurt", "stuttgart", "dublin"]
    for slug in targets:
        fetch_city(slug)
        time.sleep(2)  # be polite to Overpass
    print("\nDone.")


if __name__ == "__main__":
    main()
