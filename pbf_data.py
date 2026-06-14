#!/usr/bin/env python3
"""
PBF map-data loader.

This module is the fast alternative to the live Overpass/OSM API path. The
whole planet has been pre-split into 1 degree x 1 degree ``.osm.pbf`` tiles
named ``lon{X}_lat{Y}.osm.pbf`` where ``X = floor(longitude)`` and
``Y = floor(latitude)``. Negative indices use an ``m`` prefix, e.g. tile
``lonm1_lat51.osm.pbf`` covers longitude ``[-1, 0)`` and latitude ``[51, 52)``.

For a given centre point + radius we work out every tile the poster's bounding
box touches, read just that bounding box out of each tile (GDAL's OSM driver
supports spatial + attribute filtering), and stitch the pieces together.

Tile resolution order for every required tile:
  1. Local ``1/`` folder next to the running script (instant, no network).
  2. A previously downloaded copy in the tile cache.
  3. Download from the CDN (``https://gis.veergosai.com/1/<tile>``) into the
     tile cache so subsequent runs are instant.

A missing tile on the CDN (HTTP 404) is treated as empty ocean and skipped,
which keeps coastal/island posters working while the CDN is still filling up.
"""

import contextlib
import math
import os
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import shapely
from pyogrio import read_dataframe
from shapely.geometry import LineString

# The GDAL OSM driver emits a RuntimeWarning for every self-touching/unclosed
# ring it auto-repairs. These are cosmetic (the geometry is still usable for a
# fill) and would otherwise spam the console with thousands of lines per tile.
warnings.filterwarnings(
    "ignore", message="Non closed ring detected", category=RuntimeWarning
)

# --- Configuration (override via environment variables) --------------------

# Folder holding local tiles, relative to the script's working directory.
LOCAL_TILE_DIR = Path(os.environ.get("PBF_DIR", "1"))

# CDN base URL (no trailing slash). Each tile lives at "<base>/<tile_name>".
CDN_BASE = os.environ.get("PBF_CDN", "https://gis.veergosai.com/1").rstrip("/")

# Where downloaded tiles are cached between runs.
_CACHE_ROOT = Path(os.environ.get("CACHE_DIR", "cache"))
TILE_CACHE_DIR = _CACHE_ROOT / "pbf_tiles"

# Network timeout (seconds) for a single tile download.
DOWNLOAD_TIMEOUT = int(os.environ.get("PBF_DOWNLOAD_TIMEOUT", "120"))

# How many tiles to download + read concurrently. Each tile is resolved and
# parsed into memory on its own thread, so the program keeps moving (and the
# UI keeps updating) instead of freezing on any single slow/broken tile.
# GDAL/pyogrio reads release the GIL, so this genuinely parallelises the work.
_PBF_WORKERS = max(
    1, int(os.environ.get("PBF_WORKERS", str(min(6, (os.cpu_count() or 4)))))
)

# --- Tile-seam stitching ---------------------------------------------------
# The planet is pre-cut into 1 deg tiles, so a road that crosses a degree line
# is split into two separate LineStrings with a small gap where the straddling
# segment was dropped by the clip. After loading we reconnect endpoints that
# sit just either side of a tile boundary so roads stay continuous in the
# export. Tuned via env vars; set PBF_STITCH=0 to disable entirely.
#   _SEAM_TOL_DEG    : how far (deg) from the boundary an endpoint may sit to
#                      be considered for bridging (~0.0020 deg ~= 220 m).
#   _SEAM_BRIDGE_DEG : maximum length (deg) of a bridge connector; longer
#                      candidate pairs are left untouched to avoid wrong joins
#                      (~0.0025 deg ~= 275 m).
_SEAM_ENABLED = os.environ.get("PBF_STITCH", "1").strip().lower() not in (
    "0", "false", "no", "off",
)
_SEAM_TOL_DEG = float(os.environ.get("PBF_SEAM_TOL", "0.0020"))
_SEAM_BRIDGE_DEG = float(os.environ.get("PBF_SEAM_BRIDGE", "0.0025"))

# Approx. metres per degree of latitude (good enough for tile selection).
_M_PER_DEG = 111_320.0

# Columns we actually need from each layer (keeps reads lean).
_LINE_COLS = ["highway", "other_tags"]
_POLY_COLS = ["natural", "landuse", "leisure", "other_tags"]

# Each tile is parsed at most twice (once per layer), so the WHERE clauses are
# the *union* of what we want; the individual feature classes are split out of
# the result in pandas afterwards (see the mask helpers below).
_COAST_WHERE = "other_tags LIKE '%\"natural\"=>\"coastline\"%'"

# Major roads only — used for very large extents where pulling every footway
# would mean millions of features.
_MAJOR_HW_WHERE = (
    "highway IN ('motorway', 'motorway_link', 'trunk', 'trunk_link', "
    "'primary', 'primary_link', 'secondary', 'secondary_link', "
    "'tertiary', 'tertiary_link')"
)

# Drivable network — residential/unclassified included, but footpaths, cycle
# tracks, service alleys and steps dropped. Used for mid-range extents.
_DRIVE_HW_WHERE = (
    "highway IS NOT NULL AND highway NOT IN "
    "('footway', 'path', 'cycleway', 'steps', 'pedestrian', 'track', "
    "'service', 'bridleway', 'corridor', 'elevator', 'construction', "
    "'proposed', 'raceway')"
)


def _lines_where_for_dist(dist):
    """Distance-graduated WHERE clause for the 'lines' layer.

    Mirrors the live-API path's graduated detail levels so large-extent posters
    stay fast and legible instead of drowning in footpaths:
      - <= 200 km: every highway (full street texture, including paths).
      - > 200 km : major roads only (motorway..tertiary).
    Coastline ways are always included regardless of distance.
    """
    if dist <= 200_000:
        roads = "highway IS NOT NULL"
    else:
        roads = _MAJOR_HW_WHERE
    return f"({roads}) OR {_COAST_WHERE}"

_POLYS_WHERE = (
    "natural IN ('water', 'bay', 'strait') "
    "OR other_tags LIKE '%\"waterway\"=>\"riverbank\"%' "
    "OR other_tags LIKE '%\"water\"=>%' "
    "OR leisure IN ('park', 'garden', 'nature_reserve') "
    "OR landuse IN ('grass', 'forest', 'meadow', 'recreation_ground')"
)

_WATER_NATURAL = ("water", "bay", "strait")
_PARK_LEISURE = ("park", "garden", "nature_reserve")
_PARK_LANDUSE = ("grass", "forest", "meadow", "recreation_ground")
_COAST_MARKER = '"natural"=>"coastline"'

# At very large extents the small parks/grass patches are invisible anyway and
# only add millions of features (and broken-geometry hits). Beyond 200 km we
# keep just significant water bodies and forests.
_POLYS_WHERE_MAJOR = (
    "natural IN ('water', 'bay', 'strait') "
    "OR other_tags LIKE '%\"water\"=>%' "
    "OR landuse = 'forest'"
)


def _polys_where_for_dist(dist):
    """Distance-graduated WHERE clause for the 'multipolygons' layer.

    Mirrors the road filter: full water+parks detail up to 200 km, then only
    major water bodies and forests for very large extents so the read stays
    fast and the poster stays legible.
    """
    if dist <= 200_000:
        return _POLYS_WHERE
    return _POLYS_WHERE_MAJOR


class PBFDataError(Exception):
    """Raised when PBF data cannot be resolved or read."""


# --- Progress reporting ----------------------------------------------------

# Optional callback so a UI (e.g. the GUI) can show live tile/download status.
# It receives a single dict event. Never let a misbehaving callback break a
# render, so every dispatch is wrapped in a try/except.
_progress_cb = None


def set_progress_callback(fn):
    """Register (or clear with None) a callback fn(event: dict) for progress.

    Event ``type`` values and their extra keys:
      - ``plan``     : ``tile_count`` (int), ``source`` ('local'/'cdn'),
                       ``bbox`` (tuple).
      - ``tile``     : ``name`` (str), ``status`` (one of 'local', 'cached',
                       'downloading', 'downloaded', 'missing', 'reading',
                       'error'), and for download progress ``downloaded`` and
                       ``total`` (bytes, ``total`` may be 0 if unknown).
      - ``done``     : ``roads``, ``water``, ``parks``, ``coastline`` counts.
    """
    global _progress_cb
    _progress_cb = fn


def _emit(event_type, **fields):
    cb = _progress_cb
    if cb is None:
        return
    try:
        cb({"type": event_type, **fields})
    except Exception:  # noqa: BLE001 - UI hiccups must never stop a render
        pass


@contextlib.contextmanager
def _silence_native_stderr():
    """Redirect C-level stderr (fd 2) to /dev/null for the duration.

    GDAL/GEOS print ``Non closed ring detected`` style warnings straight to the
    process stderr, bypassing Python's ``warnings`` system entirely - and when
    the read runs on a worker thread they escape pyogrio's handler too. Briefly
    redirecting the underlying file descriptor is the only reliable way to keep
    that noise out of the console/GUI.
    """
    try:
        stderr_fd = sys.stderr.fileno()
    except (AttributeError, ValueError, OSError):
        # No real fd (already redirected to an in-memory stream): nothing to do.
        yield
        return
    saved_fd = os.dup(stderr_fd)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, stderr_fd)
        yield
    finally:
        os.dup2(saved_fd, stderr_fd)
        os.close(devnull)
        os.close(saved_fd)


# --- Tile math -------------------------------------------------------------

def _enc(index: int) -> str:
    """Encode a signed tile index the way the filenames do (negatives -> mN)."""
    return f"m{abs(index)}" if index < 0 else str(index)


def tile_name(lon_idx: int, lat_idx: int) -> str:
    """Return the filename for the tile at the given integer lon/lat index."""
    return f"lon{_enc(lon_idx)}_lat{_enc(lat_idx)}.osm.pbf"


def bbox_from_point(lat: float, lon: float, dist_m: float):
    """Return a (min_lon, min_lat, max_lon, max_lat) box around a point.

    ``dist_m`` is the half-width (radius) of the box in metres, matching the
    ``dist`` semantics used everywhere else in the project.
    """
    dlat = dist_m / _M_PER_DEG
    # Guard against cos -> 0 near the poles.
    dlon = dist_m / (_M_PER_DEG * max(0.01, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def tiles_for_bbox(min_lon, min_lat, max_lon, max_lat):
    """List every (lon_idx, lat_idx) tile the bounding box intersects."""
    lon_lo, lon_hi = math.floor(min_lon), math.floor(max_lon)
    lat_lo, lat_hi = math.floor(min_lat), math.floor(max_lat)
    tiles = []
    for lat_idx in range(lat_lo, lat_hi + 1):
        for lon_idx in range(lon_lo, lon_hi + 1):
            tiles.append((lon_idx, lat_idx))
    return tiles


# --- Tile resolution (local -> cache -> CDN) -------------------------------

def has_local_tiles() -> bool:
    """True if a local tile folder with at least one .osm.pbf file exists."""
    if not LOCAL_TILE_DIR.is_dir():
        return False
    for entry in LOCAL_TILE_DIR.iterdir():
        if entry.suffix == ".pbf" or entry.name.endswith(".osm.pbf"):
            return True
    return False


def _download_tile(name: str, dest: Path) -> bool:
    """Download a single tile from the CDN to ``dest``.

    Returns True on success, False if the tile does not exist (HTTP 404).
    Raises PBFDataError on other network failures.
    """
    url = f"{CDN_BASE}/{name}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as resp:
            if resp.status_code == 404:
                return False
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0) or 0)
            dest.parent.mkdir(parents=True, exist_ok=True)
            done = 0
            last_emit = 0
            _emit("tile", name=name, status="downloading",
                  downloaded=0, total=total)
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if chunk:
                        fh.write(chunk)
                        done += len(chunk)
                        # Throttle UI updates to ~ every 4 MB so a big tile
                        # doesn't schedule hundreds of redraws.
                        if done - last_emit >= (4 << 20) or done == total:
                            last_emit = done
                            _emit("tile", name=name, status="downloading",
                                  downloaded=done, total=total)
        tmp.replace(dest)
        return True
    except requests.RequestException as exc:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise PBFDataError(f"Failed to download tile {name}: {exc}") from exc


def resolve_tile(lon_idx: int, lat_idx: int):
    """Return a local path to the requested tile, or None if unavailable.

    Prefers the local ``1/`` folder, then the download cache, then fetches
    from the CDN. A tile that is missing on the CDN (ocean / not yet uploaded)
    yields None and is silently skipped by the caller.
    """
    name = tile_name(lon_idx, lat_idx)

    local = LOCAL_TILE_DIR / name
    if local.exists():
        _emit("tile", name=name, status="local")
        return local

    cached = TILE_CACHE_DIR / name
    if cached.exists():
        _emit("tile", name=name, status="cached")
        return cached

    ok = _download_tile(name, cached)
    if not ok:
        _emit("tile", name=name, status="missing")
        return None
    _emit("tile", name=name, status="downloaded")
    return cached


# --- Layer reading ---------------------------------------------------------

# Smallest bbox cell (in degrees) we will subdivide down to before giving up on
# a stubborn geometry. ~0.01 deg is roughly 1 km, small enough to isolate a
# single bad way while keeping the number of reads bounded.
_MIN_SUBDIVIDE_DEG = 0.01


def _empty_gdf():
    return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")


def _normalize(gdf):
    """Coerce a read result into a non-empty EPSG:4326 GDF or None."""
    if gdf is None or len(gdf) == 0:
        return None
    if gdf.crs is None:
        gdf.set_crs("EPSG:4326", inplace=True)
    return gdf


def _read_layer(path: Path, layer: str, where: str, bbox, columns=None,
                _depth=0, _stats=None):
    """Read one layer from a tile within bbox, returning an empty GDF on error.

    Some OSM tiles contain a handful of broken geometries (e.g. a LinearRing
    with only 2 points) that make GDAL/GEOS raise while assembling
    multipolygons for the *whole* requested window. Rather than lose every
    feature in the tile, we recursively split the bounding box into quadrants
    and read each one independently, so a single bad way only costs us its own
    ~1 km cell instead of the entire tile. Bad cells are counted in ``_stats``
    (so the caller can print a single summary line) instead of spamming a
    warning per cell.
    """
    try:
        gdf = read_dataframe(
            path,
            layer=layer,
            bbox=bbox,
            where=where,
            columns=columns,
        )
        norm = _normalize(gdf)
        return norm if norm is not None else _empty_gdf()
    except Exception:  # noqa: BLE001 - never let one bad tile kill a render
        min_lon, min_lat, max_lon, max_lat = bbox
        width = max_lon - min_lon
        height = max_lat - min_lat
        # Stop subdividing once cells get tiny; just drop this sliver.
        if max(width, height) <= _MIN_SUBDIVIDE_DEG or _depth >= 8:
            if _stats is not None:
                _stats["bad_cells"] = _stats.get("bad_cells", 0) + 1
            return _empty_gdf()
        mid_lon = (min_lon + max_lon) / 2
        mid_lat = (min_lat + max_lat) / 2
        quadrants = [
            (min_lon, min_lat, mid_lon, mid_lat),
            (mid_lon, min_lat, max_lon, mid_lat),
            (min_lon, mid_lat, mid_lon, max_lat),
            (mid_lon, mid_lat, max_lon, max_lat),
        ]
        parts = [
            _read_layer(path, layer, where, q, columns=columns,
                        _depth=_depth + 1, _stats=_stats)
            for q in quadrants
        ]
        return _concat(parts)


def _concat(frames):
    """Concatenate a list of GeoDataFrames into one EPSG:4326 GDF."""
    frames = [f for f in frames if f is not None and len(f) > 0]
    if not frames:
        return gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
    combined = pd.concat(frames, ignore_index=True)
    return gpd.GeoDataFrame(combined, geometry="geometry", crs="EPSG:4326")


def _road_endpoints(roads):
    """Return (pts, geom_pos) for the two extreme endpoints of every road.

    ``pts`` is an (M, 2) float array of endpoint coordinates and ``geom_pos``
    is the matching (M,) positional index into ``roads`` for each endpoint.
    Every road contributes its very first and very last vertex, so a road that
    was clipped at a tile boundary exposes the boundary-side endpoint here.
    """
    geoms = roads.geometry.values
    coords, idx = shapely.get_coordinates(geoms, return_index=True)
    if len(idx) == 0:
        return np.empty((0, 2)), np.empty((0,), dtype=int)
    # idx is ascending and grouped per geometry; first/last occurrence of each
    # group are that geometry's start and end vertices.
    uniq, first = np.unique(idx, return_index=True)
    last = np.empty_like(first)
    last[:-1] = first[1:] - 1
    last[-1] = len(idx) - 1
    pts = np.vstack([coords[first], coords[last]])
    geom_pos = np.concatenate([uniq, uniq])
    return pts, geom_pos


def _stitch_tile_seams(roads, bbox):
    """Reconnect roads split across 1 deg tile boundaries.

    A road crossing a degree line is loaded as two LineStrings with a small gap
    where the straddling segment was dropped by the tile clip. For every tile
    boundary inside ``bbox`` we pair endpoints on opposite sides using a
    shortest-first greedy matcher with a tight distance cap and road-class
    compatibility. This bridges true splits while avoiding unrelated joins.

    Returns ``roads`` unchanged if stitching is disabled, scipy is unavailable,
    there are no interior boundaries, or nothing needs bridging.
    """
    if not _SEAM_ENABLED or roads is None or len(roads) < 2:
        return roads
    try:
        from scipy.spatial import cKDTree
    except Exception:  # noqa: BLE001 - stitching is best-effort
        return roads

    min_lon, min_lat, max_lon, max_lat = bbox
    lon_lines = [L for L in range(math.ceil(min_lon), math.floor(max_lon) + 1)
                 if min_lon < L < max_lon]
    lat_lines = [L for L in range(math.ceil(min_lat), math.floor(max_lat) + 1)
                 if min_lat < L < max_lat]
    if not lon_lines and not lat_lines:
        return roads

    pts, geom_pos = _road_endpoints(roads)
    if len(pts) == 0:
        return roads
    highways = roads["highway"].to_numpy()

    def _norm_hw(val):
        if isinstance(val, list):
            return val[0] if val else ""
        return str(val or "")

    def _hw_class(val):
        hw = _norm_hw(val)
        if hw.endswith("_link"):
            hw = hw[:-5]
        if hw in {"motorway", "trunk", "primary", "secondary", "tertiary"}:
            return hw
        if hw in {"footway", "path", "cycleway", "bridleway", "steps", "pedestrian"}:
            return "path"
        if hw in {"residential", "living_street", "unclassified", "service", "road", "track"}:
            return "local_drive"
        return hw or "local_other"

    hw_classes = np.array([_hw_class(v) for v in highways], dtype=object)

    # Longitude degrees are shorter than latitude degrees away from the
    # equator; scale x so the nearest-neighbour match is roughly isotropic.
    mean_lat = (min_lat + max_lat) / 2.0
    cos_lat = max(0.1, math.cos(math.radians(mean_lat)))
    used = np.zeros(len(pts), dtype=bool)
    connectors = []
    conn_hw = []

    def _bridge(axis, line):
        coord = pts[:, axis]
        tol = _SEAM_TOL_DEG
        side_a = np.where((coord >= line - tol) & (coord < line) & ~used)[0]
        side_b = np.where((coord >= line) & (coord <= line + tol) & ~used)[0]

        def _pick_one_endpoint_per_geom(side_idx):
            if len(side_idx) <= 1:
                return side_idx
            # Keep only the endpoint closest to the seam for each geometry.
            order = np.argsort(np.abs(coord[side_idx] - line))
            chosen = []
            seen_geom = set()
            for pos in order:
                idx = int(side_idx[pos])
                g = int(geom_pos[idx])
                if g in seen_geom:
                    continue
                seen_geom.add(g)
                chosen.append(idx)
            return np.array(chosen, dtype=int)

        side_a = _pick_one_endpoint_per_geom(side_a)
        side_b = _pick_one_endpoint_per_geom(side_b)

        if len(side_a) == 0 or len(side_b) == 0:
            return
        scale = np.array([cos_lat, 1.0])
        a_xy = pts[side_a] * scale
        b_xy = pts[side_b] * scale
        tree_b = cKDTree(b_xy)
        candidates = []

        for ia, gi in enumerate(side_a):
            near = tree_b.query_ball_point(a_xy[ia], r=_SEAM_BRIDGE_DEG)
            if not near:
                continue
            for jb in near:
                gj = side_b[jb]
                if geom_pos[gi] == geom_pos[gj]:
                    continue
                if hw_classes[geom_pos[gi]] != hw_classes[geom_pos[gj]]:
                    continue
                dist = float(np.linalg.norm(a_xy[ia] - b_xy[jb]))
                candidates.append((dist, ia, jb))

        if not candidates:
            return

        candidates.sort(key=lambda x: x[0])
        for _dist, ia, jb in candidates:
            gi = side_a[ia]
            gj = side_b[jb]
            if used[gi] or used[gj]:
                continue
            used[gi] = True
            used[gj] = True
            connectors.append(LineString([tuple(pts[gi]), tuple(pts[gj])]))
            conn_hw.append(highways[geom_pos[gi]])

    for line in lon_lines:
        _bridge(0, line)
    for line in lat_lines:
        _bridge(1, line)

    if not connectors:
        return roads

    data = {col: [None] * len(connectors)
            for col in roads.columns if col != roads.geometry.name}
    if "highway" in data:
        data["highway"] = conn_hw
    extra = gpd.GeoDataFrame(
        data, geometry=connectors, crs=roads.crs or "EPSG:4326")
    return _concat([roads, extra])


def _col(gdf, name):
    """Return a column as a string Series, or an all-empty Series if absent."""
    if name in gdf.columns:
        return gdf[name].fillna("")
    return pd.Series([""] * len(gdf), index=gdf.index)


def _split_lines(lines):
    """Split a read 'lines' GDF into (roads, coastline) GeoDataFrames."""
    if len(lines) == 0:
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        return empty, empty.copy()
    highway = _col(lines, "highway")
    other = _col(lines, "other_tags")
    roads = lines[highway != ""]
    coast = lines[other.str.contains(_COAST_MARKER, regex=False)]
    return roads, coast


def _split_polys(polys):
    """Split a read 'multipolygons' GDF into (water, parks) GeoDataFrames."""
    if len(polys) == 0:
        empty = gpd.GeoDataFrame(geometry=[], crs="EPSG:4326")
        return empty, empty.copy()
    natural = _col(polys, "natural")
    landuse = _col(polys, "landuse")
    leisure = _col(polys, "leisure")
    other = _col(polys, "other_tags")
    water_mask = (
        natural.isin(_WATER_NATURAL)
        | other.str.contains('"waterway"=>"riverbank"', regex=False)
        | other.str.contains('"water"=>', regex=False)
    )
    parks_mask = leisure.isin(_PARK_LEISURE) | landuse.isin(_PARK_LANDUSE)
    return polys[water_mask], polys[parks_mask]


def _read_tile(path: Path, bbox, lines_where, polys_where,
               include_polygons=True):
    """Read both layers of one tile into memory; return split GDFs + bad count.

    Returns (roads, coast, water, parks, bad_cells). ``bad_cells`` is the
    number of ~1 km cells dropped because of unrepairable broken geometry. When
    ``include_polygons`` is False the multipolygons layer (water/parks) is
    skipped entirely - faster, and it sidesteps the broken-ring geometries that
    only ever live in that layer. GDAL's native warnings are silenced for the
    duration so nothing leaks to the console.
    """
    stats = {"bad_cells": 0}
    lines = _read_layer(path, "lines", lines_where, bbox,
                        columns=_LINE_COLS, _stats=stats)
    if include_polygons:
        polys = _read_layer(path, "multipolygons", polys_where, bbox,
                            columns=_POLY_COLS, _stats=stats)
    else:
        polys = _empty_gdf()
    roads, coast = _split_lines(lines)
    water, parks = _split_polys(polys)
    return roads, coast, water, parks, stats["bad_cells"]


def _process_tile(lon_idx, lat_idx, bbox, lines_where, polys_where,
                  include_polygons=True):
    """Resolve (download/locate) and read one tile. Runs on a worker thread.

    Returns a (kind, name, payload) tuple where ``kind`` is one of 'ok',
    'missing' or 'error'. For 'ok', payload is (roads, coast, water, parks).
    This never raises; a broken tile only loses its own cells, and a network
    failure is reported as 'error' so the rest of the map still renders.
    """
    name = tile_name(lon_idx, lat_idx)
    try:
        path = resolve_tile(lon_idx, lat_idx)
    except PBFDataError:
        _emit("tile", name=name, status="error")
        return ("error", name, None)
    if path is None:
        return ("missing", name, None)

    _emit("tile", name=path.name, status="reading")
    try:
        roads, coast, water, parks, _bad = _read_tile(
            path, bbox, lines_where, polys_where,
            include_polygons=include_polygons)
    except Exception:  # noqa: BLE001 - a tile must never kill the run
        _emit("tile", name=path.name, status="error")
        return ("error", name, None)

    _emit("tile", name=path.name, status="loaded")
    return ("ok", name, (roads, coast, water, parks))


def load_pbf_elements(point, dist, include_polygons=True):
    """Resolve roads + water + parks + coastline for a poster from PBF tiles.

    Args:
        point: (latitude, longitude) tuple of the map centre.
        dist:  Half-width of the map bounding box in metres.
        include_polygons: When False, the multipolygons layer (water/parks) is
            not read at all - returns empty water/parks frames. Much faster for
            large extents and avoids the broken-geometry recovery entirely.

    Returns:
        dict with keys 'roads', 'water', 'parks', 'coastline'. Each value is a
        GeoDataFrame in EPSG:4326 (possibly empty). 'roads' is the street
        network; the others are feature layers used for fills/strokes.

    Raises:
        PBFDataError: if no tiles could be resolved at all (e.g. CDN offline
            and no local data) so the caller can surface a clear message.
    """
    lat, lon = point
    bbox = bbox_from_point(lat, lon, dist)
    tiles = tiles_for_bbox(*bbox)
    lines_where = _lines_where_for_dist(dist)
    polys_where = _polys_where_for_dist(dist)

    source = "local folder" if has_local_tiles() else "CDN"
    # Tell the UI the full plan up front (so it can draw a grid of blocks).
    tile_meta = [(lo, la, tile_name(lo, la)) for lo, la in tiles]
    _emit("plan", tile_count=len(tiles),
          source="local" if source == "local folder" else "cdn",
          bbox=bbox, tiles=tile_meta)

    roads_frames, water_frames, parks_frames, coast_frames = [], [], [], []
    resolved = 0
    network_errors = 0

    # Resolve + read every tile concurrently. Each worker downloads (if needed)
    # and parses its tile into memory independently, so one slow or broken tile
    # never blocks the others and the program keeps moving. Results are
    # accumulated here on the main thread, where pandas concat is cheap.
    #
    # GDAL prints broken-ring warnings straight to C-level stderr from inside
    # the (GIL-released) read. Silencing per-tile would be racy across workers,
    # so we redirect stderr once for the whole concurrent section instead.
    with _silence_native_stderr():
        with ThreadPoolExecutor(max_workers=_PBF_WORKERS) as pool:
            futures = [
                pool.submit(_process_tile, lon_idx, lat_idx, bbox,
                            lines_where, polys_where, include_polygons)
                for lon_idx, lat_idx in tiles
            ]
            for future in as_completed(futures):
                kind, _name, payload = future.result()
                if kind == "error":
                    network_errors += 1
                elif kind == "ok":
                    resolved += 1
                    tile_roads, tile_coast, tile_water, tile_parks = payload
                    roads_frames.append(tile_roads)
                    coast_frames.append(tile_coast)
                    water_frames.append(tile_water)
                    parks_frames.append(tile_parks)
                # kind == "missing": open ocean / not on CDN, nothing to add.

    if resolved == 0:
        if network_errors:
            raise PBFDataError(
                "No PBF tiles could be downloaded (network error) and no "
                "local data was found. Check your connection or place tiles "
                f"in the '{LOCAL_TILE_DIR}' folder."
            )
        # Every needed tile was a 404 (open ocean). Nothing to draw.
        raise PBFDataError(
            "No map data found for this location in the PBF tiles "
            "(the area may be open ocean, or tiles are not yet on the CDN)."
        )

    roads = _concat(roads_frames)
    water = _concat(water_frames)
    parks = _concat(parks_frames)
    coastline = _concat(coast_frames)

    # Reconnect roads that were split where they cross a 1 deg tile boundary so
    # the export shows continuous streets instead of tiny seam gaps.
    roads = _stitch_tile_seams(roads, bbox)

    _emit("done", roads=len(roads), water=len(water),
          parks=len(parks), coastline=len(coastline))

    return {
        "roads": roads,
        "water": water,
        "parks": parks,
        "coastline": coastline,
    }
