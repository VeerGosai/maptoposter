#!/usr/bin/env python3
"""
South Africa Road Poster Generator
==================================

Generates a high-resolution black + yellow wallpaper-style map of South Africa
from the GeoJSON road data shipped in ./TEMP/, overlaid with coastline,
international borders and provincial boundaries fetched from the public
Natural Earth dataset (cached locally on first run).

Layering, bottom -> top:
    Provincial boundaries (dim)
    M roads (metropolitan / dim yellow, thinnest)
    R roads (regional / medium yellow)
    N roads (national / brightest yellow, thickest)
    International borders
    Coastline (brightest, on top)

Text is intentionally omitted -- pure wallpaper.

Usage
-----
    python make_sa_roads_poster.py
    python make_sa_roads_poster.py --out exports/south_africa_dark_yellow.png \
                                   --width 5400 --height 7200 --dpi 300
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import LineCollection
from matplotlib.path import Path as MplPath
from pyproj import Transformer

# ---------------------------------------------------------------------------
# Theme: themes/dark/dark_yellow.json
# ---------------------------------------------------------------------------
THEME = {
    "bg":              "#000000",
    "text":            "#FFD700",
    "road_motorway":   "#FFE040",  # N roads (top, brightest)
    "road_primary":    "#FFD700",
    "road_secondary":  "#E0B800",  # R roads (middle)
    "road_tertiary":   "#B88A00",
    "road_residential":"#785800",  # M roads (bottom, dim)
}

# Per-layer styling. Order in this list = draw order (bottom first).
LAYERS = [
    {
        "name":     "M",
        "file":     "TEMP/M.geojson",
        "color":    THEME["road_residential"],
        "width":    0.35,
        "alpha":    0.85,
        "zorder":   2,
        "label":    "Metropolitan (M)",
    },
    {
        "name":     "R",
        "file":     "TEMP/R.geojson",
        "color":    THEME["road_secondary"],
        "width":    0.7,
        "alpha":    0.95,
        "zorder":   3,
        "label":    "Regional (R)",
    },
    {
        "name":     "N",
        "file":     "TEMP/N.geojson",
        "color":    THEME["road_motorway"],
        "width":    1.6,
        "alpha":    1.0,
        "zorder":   4,
        "label":    "National (N)",
    },
]

# South Africa bounding box (lon_min, lon_max, lat_min, lat_max).
# Generous -- covers the mainland; outliers are clipped by the projection.
SA_BBOX = (15.5, 33.5, -35.5, -21.5)

# Projected CRS: South African Albers Equal Area (commonly used for ZA maps).
# Standard parallels 18°S and 32°S, centred on 25°E.
PROJ_FROM = "EPSG:4326"
PROJ_TO = (
    "+proj=aea +lat_1=-18 +lat_2=-32 +lat_0=-30 +lon_0=25 "
    "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
)

# ---------------------------------------------------------------------------
# Natural Earth boundary feeds (1:10m, public CDN, cached locally)
# ---------------------------------------------------------------------------
NE_BASE = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/"
)
NE_FILES = {
    "countries":     "ne_10m_admin_0_countries.geojson",
    "coastline":     "ne_10m_coastline.geojson",
    "intl_borders":  "ne_10m_admin_0_boundary_lines_land.geojson",
    "provinces":     "ne_10m_admin_1_states_provinces_lines.geojson",
}
NE_CACHE_DIR = Path("TEMP/cache_ne")

# Buffer (in degrees) used when clipping line layers to the SA polygon.
# ~0.05° ≈ 5 km — enough to keep boundary/coastline segments that lie exactly
# on the polygon edge, while excluding neighbouring countries.
SA_CLIP_BUFFER_DEG = 0.05

BOUNDARY_LAYERS = [
    {
        "name":   "provinces",
        "color":  THEME["road_tertiary"],   # dim yellow
        "width":  1.1,
        "alpha":  0.75,
        "zorder": 1,            # below roads
        "filter_iso": "ZAF",    # only South African provincial lines
        "clip_buffer": 0.0,     # already filtered to ZA via ISO
    },
    {
        "name":   "intl_borders",
        "color":  THEME["road_primary"],    # gold
        "width":  3.0,
        "alpha":  1.0,
        "zorder": 5,            # above roads
        "filter_iso": None,
        "clip_buffer": SA_CLIP_BUFFER_DEG,  # keep lines along SA border
    },
    {
        "name":   "coastline",
        "color":  THEME["text"],            # brightest gold
        "width":  3.6,
        "alpha":  1.0,
        "zorder": 6,            # on top
        "filter_iso": None,
        "clip_buffer": SA_CLIP_BUFFER_DEG,  # keep coast lying on SA polygon
    },
]


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------
def _iter_linestrings(geometry):
    """Yield each LineString's coordinate list from a GeoJSON geometry."""
    if geometry is None:
        return
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "LineString":
        yield coords
    elif gtype == "MultiLineString":
        for line in coords:
            yield line
    elif gtype == "GeometryCollection":
        for sub in geometry.get("geometries", []):
            yield from _iter_linestrings(sub)
    # Polygons / Points are ignored -- this poster is roads only.


def load_geojson_lines(path: Path, bbox):
    """Load a GeoJSON file and return a list of coordinate arrays (lon, lat).

    Any segment whose entire bbox lies outside `bbox` is dropped early to
    keep the projection / plotting work focused on South Africa.
    """
    lon_min, lon_max, lat_min, lat_max = bbox
    t0 = time.time()
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    lines = []
    for feature in data.get("features", []):
        for coords in _iter_linestrings(feature.get("geometry")):
            if len(coords) < 2:
                continue
            arr = np.asarray(coords, dtype=np.float64)
            if arr.ndim != 2 or arr.shape[1] < 2:
                continue
            arr = arr[:, :2]  # drop z / m if present
            lons = arr[:, 0]
            lats = arr[:, 1]
            if (lons.max() < lon_min or lons.min() > lon_max or
                    lats.max() < lat_min or lats.min() > lat_max):
                continue
            lines.append(arr)

    print(f"  loaded {len(lines):>7,d} lines from {path.name} "
          f"({time.time() - t0:.1f}s)")
    return lines


def project_lines(lines, transformer: Transformer):
    """Project a list of lon/lat coordinate arrays to the target CRS."""
    out = []
    for arr in lines:
        x, y = transformer.transform(arr[:, 0], arr[:, 1])
        out.append(np.column_stack([x, y]))
    return out


# ---------------------------------------------------------------------------
# Natural Earth fetching (cached)
# ---------------------------------------------------------------------------
def fetch_natural_earth(key: str) -> Path:
    """Download a Natural Earth GeoJSON to TEMP/cache_ne/ if not already cached."""
    NE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fname = NE_FILES[key]
    dest = NE_CACHE_DIR / fname
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    url = NE_BASE + fname
    print(f"  fetching {url}")
    req = urllib.request.Request(
        url, headers={"User-Agent": "maptoposter/1.0"}
    )
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
    tmp.replace(dest)
    print(f"  cached -> {dest} ({dest.stat().st_size/1024:.0f} KB)")
    return dest


def load_ne_lines(key: str, bbox, filter_iso: str | None):
    """Load a Natural Earth GeoJSON as a list of (lon, lat) arrays.

    `filter_iso` (e.g. 'ZAF') keeps only features whose adm0 ISO matches
    -- used to restrict provincial lines to South Africa only.
    """
    path = fetch_natural_earth(key)
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    lon_min, lon_max, lat_min, lat_max = bbox
    out = []
    for feature in data.get("features", []):
        if filter_iso is not None:
            props = feature.get("properties") or {}
            iso = (props.get("adm0_a3")
                   or props.get("ADM0_A3")
                   or props.get("iso_a3")
                   or props.get("ISO_A3"))
            if iso != filter_iso:
                continue
        for coords in _iter_linestrings(feature.get("geometry")):
            if len(coords) < 2:
                continue
            arr = np.asarray(coords, dtype=np.float64)
            if arr.ndim != 2 or arr.shape[1] < 2:
                continue
            arr = arr[:, :2]
            lons, lats = arr[:, 0], arr[:, 1]
            if (lons.max() < lon_min or lons.min() > lon_max or
                    lats.max() < lat_min or lats.min() > lat_max):
                continue
            out.append(arr)
    return out


def _iter_polygon_rings(geometry):
    """Yield each polygon's exterior + interior rings from a GeoJSON geometry."""
    if geometry is None:
        return
    gtype = geometry.get("type")
    coords = geometry.get("coordinates")
    if gtype == "Polygon":
        for ring in coords:
            yield ring
    elif gtype == "MultiPolygon":
        for poly in coords:
            for ring in poly:
                yield ring
    elif gtype == "GeometryCollection":
        for sub in geometry.get("geometries", []):
            yield from _iter_polygon_rings(sub)


def load_sa_paths():
    """Return (mpl_paths, rings) for the South Africa country polygon.

    - mpl_paths: matplotlib Paths used for inside-polygon clipping. Only the
      largest ring (mainland exterior) is kept so the clip mask is simple.
    - rings: list of (lon, lat) coordinate arrays for every ring of the
      mainland polygon (exterior + interior holes such as Lesotho), suitable
      for drawing the country outline directly.
    """
    path = fetch_natural_earth("countries")
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    for feature in data.get("features", []):
        props = feature.get("properties") or {}
        iso = (props.get("ADM0_A3") or props.get("adm0_a3")
               or props.get("ISO_A3") or props.get("iso_a3"))
        if iso != "ZAF":
            continue
        geom = feature.get("geometry") or {}
        # Pick the largest sub-polygon (mainland), then keep all of its rings.
        polys = []
        if geom.get("type") == "Polygon":
            polys = [geom["coordinates"]]
        elif geom.get("type") == "MultiPolygon":
            polys = list(geom["coordinates"])
        if not polys:
            continue
        polys.sort(key=lambda rings: len(rings[0]), reverse=True)
        mainland = polys[0]

        rings = []
        for ring in mainland:
            arr = np.asarray(ring, dtype=np.float64)
            if arr.ndim != 2 or arr.shape[0] < 4:
                continue
            rings.append(arr[:, :2])
        if not rings:
            continue
        mpl_paths = [MplPath(rings[0])]  # exterior only, for clipping mask
        return mpl_paths, rings
    raise RuntimeError("South Africa (ZAF) not found in Natural Earth countries")


def clip_lines_to_paths(lines, paths, radius=0.0):
    """Split each line at vertices outside the union of `paths`.

    Returns a new list containing contiguous runs (>=2 vertices) of points
    whose lon/lat falls inside any of the provided matplotlib Paths,
    expanded by `radius` degrees.
    """
    out = []
    for arr in lines:
        if len(arr) < 2:
            continue
        inside = np.zeros(len(arr), dtype=bool)
        for p in paths:
            inside |= p.contains_points(arr, radius=radius)
        i, n = 0, len(inside)
        while i < n:
            if not inside[i]:
                i += 1
                continue
            j = i
            while j < n and inside[j]:
                j += 1
            if j - i >= 2:
                out.append(arr[i:j])
            i = j
    return out


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
def build_poster(out_path: Path, width_px: int, height_px: int, dpi: int):
    transformer = Transformer.from_crs(PROJ_FROM, PROJ_TO, always_xy=True)

    figsize = (width_px / dpi, height_px / dpi)
    fig = plt.figure(figsize=figsize, dpi=dpi, facecolor=THEME["bg"])
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.set_facecolor(THEME["bg"])
    ax.set_aspect("equal")
    ax.set_axis_off()

    all_x_min = math.inf
    all_x_max = -math.inf
    all_y_min = math.inf
    all_y_max = -math.inf

    def _track(projected):
        nonlocal all_x_min, all_x_max, all_y_min, all_y_max
        for arr in projected:
            if arr.size:
                xs, ys = arr[:, 0], arr[:, 1]
                all_x_min = min(all_x_min, xs.min())
                all_x_max = max(all_x_max, xs.max())
                all_y_min = min(all_y_min, ys.min())
                all_y_max = max(all_y_max, ys.max())

    # -------- Roads --------
    for layer in LAYERS:
        path = Path(layer["file"])
        if not path.exists():
            print(f"!! missing {path} -- skipping", file=sys.stderr)
            continue
        print(f"[{layer['name']}] reading {path}...")
        raw = load_geojson_lines(path, SA_BBOX)
        if not raw:
            continue
        print(f"[{layer['name']}] projecting {len(raw):,} lines...")
        projected = project_lines(raw, transformer)
        _track(projected)

        lc = LineCollection(
            projected,
            colors=layer["color"],
            linewidths=layer["width"],
            alpha=layer["alpha"],
            antialiaseds=True,
            capstyle="round",
            joinstyle="round",
            zorder=layer["zorder"],
        )
        ax.add_collection(lc)
        print(f"[{layer['name']}] drawn ({len(projected):,} segments).")

    # -------- Boundaries from Natural Earth --------
    print("loading South Africa polygon for clipping...")
    sa_paths, sa_rings = load_sa_paths()

    for layer in BOUNDARY_LAYERS:
        print(f"[{layer['name']}] loading Natural Earth...")
        raw = load_ne_lines(layer["name"], SA_BBOX, layer["filter_iso"])
        if layer["filter_iso"] is None:
            before = len(raw)
            raw = clip_lines_to_paths(
                raw, sa_paths, radius=layer["clip_buffer"],
            )
            print(f"[{layer['name']}] clipped to SA polygon: "
                  f"{before:,} -> {len(raw):,} segments")
        if not raw:
            print(f"[{layer['name']}] no features after clipping.")
            continue
        projected = project_lines(raw, transformer)
        _track(projected)
        lc = LineCollection(
            projected,
            colors=layer["color"],
            linewidths=layer["width"],
            alpha=layer["alpha"],
            antialiaseds=True,
            capstyle="round",
            joinstyle="round",
            zorder=layer["zorder"],
        )
        ax.add_collection(lc)
        print(f"[{layer['name']}] drawn ({len(projected):,} segments).")

    # -------- South Africa country outline (coastline + land borders) --------
    # Drawn directly from the SA polygon rings so it is guaranteed visible.
    # Includes the Lesotho enclave interior ring.
    print("[country_outline] drawing SA polygon outline...")
    projected_outline = project_lines(sa_rings, transformer)
    _track(projected_outline)
    ax.add_collection(LineCollection(
        projected_outline,
        colors=THEME["text"],          # brightest gold
        linewidths=4.0,
        alpha=1.0,
        antialiaseds=True,
        capstyle="round",
        joinstyle="round",
        zorder=7,                      # above everything
    ))
    print(f"[country_outline] drawn ({len(projected_outline)} rings).")

    if not math.isfinite(all_x_min):
        raise RuntimeError(
            "No geometry was loaded -- check TEMP/*.geojson files."
        )

    # Tight extent (no breathing room -- edge-to-edge wallpaper).
    ax.set_xlim(all_x_min, all_x_max)
    ax.set_ylim(all_y_min, all_y_max)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"saving {out_path} ({width_px}x{height_px} @ {dpi} dpi)...")
    fig.savefig(out_path, dpi=dpi, facecolor=THEME["bg"],
                bbox_inches=None, pad_inches=0)
    plt.close(fig)
    print("done.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--out", type=Path,
                   default=Path("exports/south_africa_dark_yellow.png"),
                   help="Output PNG path")
    p.add_argument("--width", type=int, default=5400,
                   help="Output width in pixels (default 5400)")
    p.add_argument("--height", type=int, default=7200,
                   help="Output height in pixels (default 7200, poster ratio)")
    p.add_argument("--dpi", type=int, default=300,
                   help="Output DPI (default 300)")
    return p.parse_args()


def main():
    args = parse_args()
    os.chdir(Path(__file__).resolve().parent)
    build_poster(
        out_path=args.out,
        width_px=args.width,
        height_px=args.height,
        dpi=args.dpi,
    )


if __name__ == "__main__":
    main()
