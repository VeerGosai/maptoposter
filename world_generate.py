#!/usr/bin/env python3
"""
world_generate.py - Render every 1 deg x 1 deg block of the planet as a square
map image, ready to be tiled together in a browser like mapping software.

Just run:

    python world_generate.py

and leave it overnight. It will:
  * walk every 1 deg cell of the world (lon -180..179, lat -90..89),
  * pull that cell's road/water/park data straight from the CDN
    (``https://gis.veergosai.com/1/<tile>``) - downloaded tiles are cached
    under ``cache/pbf_tiles`` so a re-run is instant,
  * render it at full 200 km detail (every street, including paths) in the
    ``dark_white`` theme,
  * save a square PNG sized exactly to the coordinate cell into
    ``exports/world_project/tiles/`` named ``tile_{lon}_{lat}.png``,
  * drive a tonne of worker processes in parallel (tuned for an M-series Pro
    with lots of RAM), with a live CLI progress bar,
  * and write a ``viewer.html`` + ``manifest.json`` so you can open the whole
    thing in a browser and pan/zoom around like a slippy map.

It is fully resumable: stop it any time (Ctrl-C) and run it again - already
rendered cells and known ocean/empty cells are skipped instantly.

Tiles are drawn in plate-carree (raw lon/lat), each cell exactly 1 deg square,
so the images line up edge-to-edge into one seamless world map.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

# Headless rendering - must be set before pyplot is imported, in every worker.
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

# Run everything relative to this script so the tile cache / output land in the
# repo regardless of where the command was launched from.
SCRIPT_DIR = Path(__file__).resolve().parent
os.chdir(SCRIPT_DIR)

import pbf_data  # noqa: E402  (imported after chdir so its paths resolve here)

try:
    from tqdm import tqdm
    _HAVE_TQDM = True
except Exception:  # noqa: BLE001 - fall back to a tiny built-in bar
    _HAVE_TQDM = False


# --- Configuration ---------------------------------------------------------

# Full detail: anything <= 200 km keeps every highway/path and all water+parks.
DETAIL_DIST = 200_000

THEME_PATH = SCRIPT_DIR / "themes" / "dark" / "dark_white.json"
OUT_ROOT = SCRIPT_DIR / "exports" / "world_project"
TILES_DIR = OUT_ROOT / "tiles"
PROGRESS_LOG = OUT_ROOT / "progress.log"
MANIFEST_PATH = OUT_ROOT / "manifest.json"
VIEWER_PATH = OUT_ROOT / "viewer.html"

# Road tiers (lowest -> highest priority, drawn bottom to top). Mirrors the
# poster renderer so streets keep the familiar hierarchy.
_ROAD_TIERS = [
    ({"residential", "living_street", "unclassified", "service", "road",
      "track", "path", "footway", "cycleway", "bridleway", "steps",
      "pedestrian"}, "road_residential", 0.14),
    ({"tertiary", "tertiary_link"}, "road_tertiary", 0.28),
    ({"secondary", "secondary_link"}, "road_secondary", 0.5),
    ({"trunk", "trunk_link", "primary", "primary_link"}, "road_primary", 0.8),
    ({"motorway", "motorway_link"}, "road_motorway", 1.05),
]
_ALL_TIER_TYPES = set().union(*[t[0] for t in _ROAD_TIERS])


@dataclass
class Config:
    size: int
    dpi: int
    road_width: float
    theme: dict
    resume: bool
    lines_where: str
    polys_where: str


# Per-worker global config (set by the pool initializer so we don't re-pickle
# the theme/where-clauses for every one of the ~64k tasks).
_CFG: Config | None = None


def _init_worker(cfg: Config):
    global _CFG
    _CFG = cfg
    # Keep each worker quiet and deterministic.
    plt.ioff()


# --- Rendering -------------------------------------------------------------

def _hw_value(val):
    if isinstance(val, list):
        return val[0] if val else "unclassified"
    return val or "unclassified"


def _draw(lon, lat, roads, coast, water, parks, out_path: Path):
    cfg = _CFG
    theme = cfg.theme
    size_in = cfg.size / cfg.dpi

    fig = plt.figure(figsize=(size_in, size_in), dpi=cfg.dpi,
                     facecolor=theme["bg"])
    try:
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_facecolor(theme["bg"])
        # Exact 1 deg cell extent in raw lon/lat (plate-carree) so adjacent
        # tiles line up perfectly.
        ax.set_xlim(lon, lon + 1)
        ax.set_ylim(lat, lat + 1)
        ax.set_axis_off()

        # Water (lowest), then parks, then coastline outline.
        if water is not None and len(water) > 0:
            wp = water[water.geometry.type.isin(["Polygon", "MultiPolygon"])]
            if len(wp) > 0:
                wp.plot(ax=ax, facecolor=theme["water"], edgecolor="none",
                        zorder=0.5)
        if parks is not None and len(parks) > 0:
            pp = parks[parks.geometry.type.isin(["Polygon", "MultiPolygon"])]
            if len(pp) > 0:
                pp.plot(ax=ax, facecolor=theme["parks"], edgecolor="none",
                        zorder=0.8)
        if coast is not None and len(coast) > 0:
            cg = coast[coast.geometry.type.isin(
                ["LineString", "MultiLineString", "Polygon", "MultiPolygon"])]
            if len(cg) > 0:
                cg.plot(ax=ax, edgecolor=theme.get("coastline", "#FFFFFF"),
                        facecolor="none", linewidth=0.5 * cfg.road_width,
                        zorder=1.5)

        # Roads in hierarchical passes (major on top).
        if roads is not None and len(roads) > 0 and "highway" in roads.columns:
            typed = set()
            for z, (hw_set, color_key, base_w) in enumerate(_ROAD_TIERS,
                                                            start=2):
                mask = roads["highway"].apply(lambda v: _hw_value(v) in hw_set)
                subset = roads[mask]
                typed |= hw_set
                if subset.empty:
                    continue
                width = base_w * cfg.road_width
                if width < 0.05:
                    continue
                color = theme.get(color_key, theme.get("road_default",
                                                       "#A0A0A0"))
                subset.plot(ax=ax, color=color, linewidth=width, zorder=z,
                            capstyle="round", joinstyle="round")
            # Anything not in the tier table.
            mask_other = roads["highway"].apply(
                lambda v: _hw_value(v) not in typed)
            other = roads[mask_other]
            if not other.empty:
                width = 0.25 * cfg.road_width
                if width >= 0.05:
                    other.plot(ax=ax,
                               color=theme.get("road_default", "#A0A0A0"),
                               linewidth=width, zorder=2,
                               capstyle="round", joinstyle="round")

        tmp = out_path.with_name(out_path.name + ".tmp")
        fig.savefig(tmp, format="png", dpi=cfg.dpi, facecolor=theme["bg"],
                    pad_inches=0)
        os.replace(tmp, out_path)
    finally:
        plt.close(fig)


def render_cell(cell):
    """Worker entry point. Returns (lon, lat, status)."""
    lon, lat = cell
    cfg = _CFG
    out_path = TILES_DIR / f"tile_{lon}_{lat}.png"

    if cfg.resume and out_path.exists():
        return (lon, lat, "ok")

    try:
        path = pbf_data.resolve_tile(lon, lat)
    except Exception:  # noqa: BLE001 - network hiccup, retry on next run
        return (lon, lat, "error")

    if path is None:
        return (lon, lat, "ocean")

    bbox = (lon, lat, lon + 1, lat + 1)
    try:
        with pbf_data._silence_native_stderr():
            roads, coast, water, parks, _bad = pbf_data._read_tile(
                path, bbox, cfg.lines_where, cfg.polys_where,
                include_polygons=True)
    except Exception:  # noqa: BLE001 - one bad tile must not kill the run
        return (lon, lat, "error")

    if (len(roads) == 0 and len(water) == 0
            and len(parks) == 0 and len(coast) == 0):
        return (lon, lat, "empty")

    try:
        _draw(lon, lat, roads, coast, water, parks, out_path)
    except Exception:  # noqa: BLE001
        return (lon, lat, "error")

    return (lon, lat, "ok")


# --- Progress / resume bookkeeping -----------------------------------------

def _load_done(retry_errors: bool) -> set:
    """Cells already processed in a previous run (skip them instantly)."""
    done = set()
    if not PROGRESS_LOG.exists():
        return done
    with open(PROGRESS_LOG, "r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) != 3:
                continue
            lon, lat, status = parts
            try:
                key = (int(lon), int(lat))
            except ValueError:
                continue
            if status == "error" and retry_errors:
                continue
            done.add(key)
    return done


def _all_cells(lon_min, lon_max, lat_min, lat_max):
    for lat in range(lat_min, lat_max):
        for lon in range(lon_min, lon_max):
            yield (lon, lat)


class _PlainBar:
    """Minimal progress bar used only if tqdm is unavailable."""

    def __init__(self, total):
        self.total = max(1, total)
        self.n = 0
        self.start = time.time()
        self.postfix = ""

    def update(self, k=1):
        self.n += k
        if self.n % 25 == 0 or self.n == self.total:
            self._render()

    def set_postfix_str(self, s):
        self.postfix = s

    def _render(self):
        frac = self.n / self.total
        filled = int(frac * 30)
        bar = "#" * filled + "-" * (30 - filled)
        elapsed = time.time() - self.start
        rate = self.n / elapsed if elapsed > 0 else 0
        eta = (self.total - self.n) / rate if rate > 0 else 0
        sys.stdout.write(
            f"\r[{bar}] {self.n}/{self.total} "
            f"({frac*100:5.1f}%) {rate:5.1f}/s ETA {eta/3600:4.1f}h "
            f"{self.postfix}")
        sys.stdout.flush()

    def close(self):
        self._render()
        sys.stdout.write("\n")
        sys.stdout.flush()


def _write_manifest(size):
    """Scan rendered tiles and write manifest.json for the browser viewer."""
    tiles = []
    for p in TILES_DIR.glob("tile_*_*.png"):
        stem = p.stem  # tile_{lon}_{lat}
        try:
            _, lon, lat = stem.split("_")
            tiles.append([int(lon), int(lat)])
        except ValueError:
            continue
    tiles.sort()
    MANIFEST_PATH.write_text(json.dumps({
        "tile_size": size,
        "count": len(tiles),
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tiles": tiles,
    }), encoding="utf-8")
    return len(tiles)


_VIEWER_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>World Map - maptoposter</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  html, body { margin: 0; height: 100%; background: #000; }
  #map { width: 100%; height: 100%; background: #000; }
  .info { position: absolute; z-index: 1000; top: 10px; left: 10px;
          color: #fff; font: 12px monospace; background: rgba(0,0,0,.6);
          padding: 6px 8px; border-radius: 4px; }
</style>
</head>
<body>
<div id="map"></div>
<div class="info" id="info">loading…</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const map = L.map('map', {
  crs: L.CRS.EPSG4326,
  center: [20, 0],
  zoom: 3,
  minZoom: 1,
  maxZoom: 9,
  worldCopyJump: false,
});

const layerByKey = new Map();
let tileSet = new Set();
let tileSize = 1024;

fetch('manifest.json').then(r => r.json()).then(m => {
  tileSize = m.tile_size || 1024;
  m.tiles.forEach(([lon, lat]) => tileSet.add(lon + ',' + lat));
  document.getElementById('info').textContent =
    m.count + ' tiles · generated ' + m.generated;
  refresh();
}).catch(e => {
  document.getElementById('info').textContent = 'manifest.json not found';
});

function refresh() {
  const b = map.getBounds();
  const lonMin = Math.floor(b.getWest()), lonMax = Math.ceil(b.getEast());
  const latMin = Math.floor(b.getSouth()), latMax = Math.ceil(b.getNorth());
  const wanted = new Set();
  for (let lon = lonMin; lon <= lonMax; lon++) {
    for (let lat = latMin; lat <= latMax; lat++) {
      const key = lon + ',' + lat;
      if (!tileSet.has(key)) continue;
      wanted.add(key);
      if (!layerByKey.has(key)) {
        const url = 'tiles/tile_' + lon + '_' + lat + '.png';
        const bounds = [[lat, lon], [lat + 1, lon + 1]];
        const ov = L.imageOverlay(url, bounds, { opacity: 1 });
        ov.addTo(map);
        layerByKey.set(key, ov);
      }
    }
  }
  // Drop overlays that scrolled far out of view to keep the DOM light.
  for (const [key, ov] of layerByKey) {
    if (!wanted.has(key)) { map.removeLayer(ov); layerByKey.delete(key); }
  }
}

map.on('moveend', refresh);
map.on('zoomend', refresh);
</script>
</body>
</html>
"""


def _write_viewer():
    VIEWER_PATH.write_text(_VIEWER_HTML, encoding="utf-8")


# --- Main ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Render every 1 deg world block as a square tile image.")
    parser.add_argument("--workers", type=int, default=0,
                        help="Parallel worker processes (default: CPU count).")
    parser.add_argument("--size", type=int, default=1024,
                        help="Tile image size in pixels (square). Default 1024.")
    parser.add_argument("--dpi", type=int, default=100, help="Render DPI.")
    parser.add_argument("--road-width", type=float, default=1.5,
                        help="Road line-width multiplier. Default 1.5.")
    parser.add_argument("--lon-min", type=int, default=-180)
    parser.add_argument("--lon-max", type=int, default=180)
    parser.add_argument("--lat-min", type=int, default=-90)
    parser.add_argument("--lat-max", type=int, default=90)
    parser.add_argument("--no-resume", action="store_true",
                        help="Re-render tiles even if they already exist.")
    parser.add_argument("--retry-errors", action="store_true",
                        help="Re-attempt cells that errored previously.")
    args = parser.parse_args()

    workers = args.workers or (os.cpu_count() or 8)

    TILES_DIR.mkdir(parents=True, exist_ok=True)

    theme = json.loads(THEME_PATH.read_text(encoding="utf-8"))

    cfg = Config(
        size=args.size,
        dpi=args.dpi,
        road_width=args.road_width,
        theme=theme,
        resume=not args.no_resume,
        lines_where=pbf_data._lines_where_for_dist(DETAIL_DIST),
        polys_where=pbf_data._polys_where_for_dist(DETAIL_DIST),
    )

    all_cells = list(_all_cells(args.lon_min, args.lon_max,
                                args.lat_min, args.lat_max))
    done = set() if args.no_resume else _load_done(args.retry_errors)
    pending = [c for c in all_cells if c not in done]

    total_world = len(all_cells)
    print("=" * 60)
    print("  World tile generator  (theme: dark_white, 200 km detail)")
    print("=" * 60)
    print(f"  Output      : {OUT_ROOT}")
    print(f"  Source      : CDN ({pbf_data.CDN_BASE}) + cache")
    print(f"  Tile size   : {args.size}x{args.size} px, 1 deg square")
    print(f"  Workers     : {workers}")
    print(f"  World cells : {total_world}")
    print(f"  Already done: {total_world - len(pending)}")
    print(f"  To process  : {len(pending)}")
    print("=" * 60)

    if not pending:
        print("Nothing to do - everything is already rendered.")
        n = _write_manifest(args.size)
        _write_viewer()
        print(f"Manifest: {n} tiles. Open {VIEWER_PATH} in a browser.")
        return

    counts = {"ok": 0, "ocean": 0, "empty": 0, "error": 0}
    bar = (tqdm(total=len(pending), unit="cell", smoothing=0.05)
           if _HAVE_TQDM else _PlainBar(len(pending)))

    # Ignore Ctrl-C in workers; the main process handles it so we can flush.
    orig_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)

    log = open(PROGRESS_LOG, "a", encoding="utf-8", buffering=1)
    last_manifest = time.time()
    interrupted = False
    try:
        with ProcessPoolExecutor(
                max_workers=workers,
                initializer=_init_worker,
                initargs=(cfg,),
                max_tasks_per_child=150) as ex:
            # Restore default SIGINT in the parent now that workers inherited
            # the ignore handler.
            signal.signal(signal.SIGINT, orig_sigint)
            try:
                for lon, lat, status in ex.map(render_cell, pending,
                                               chunksize=4):
                    counts[status] = counts.get(status, 0) + 1
                    log.write(f"{lon},{lat},{status}\n")
                    bar.update(1)
                    if _HAVE_TQDM:
                        bar.set_postfix_str(
                            f"ok={counts['ok']} sea={counts['ocean']} "
                            f"empty={counts['empty']} err={counts['error']}")
                    else:
                        bar.set_postfix_str(
                            f"ok={counts['ok']} sea={counts['ocean']} "
                            f"err={counts['error']}")
                    now = time.time()
                    if now - last_manifest > 120:
                        _write_manifest(args.size)
                        _write_viewer()
                        last_manifest = now
            except KeyboardInterrupt:
                interrupted = True
                print("\nInterrupted - shutting down workers "
                      "(progress saved, just re-run to resume)…")
                ex.shutdown(wait=False, cancel_futures=True)
    finally:
        bar.close()
        log.flush()
        log.close()
        n = _write_manifest(args.size)
        _write_viewer()

    print("-" * 60)
    print(f"  rendered : {counts['ok']}")
    print(f"  ocean    : {counts['ocean']}")
    print(f"  empty    : {counts['empty']}")
    print(f"  errors   : {counts['error']}  (re-run with --retry-errors)")
    print(f"  manifest : {n} tiles -> {MANIFEST_PATH.name}")
    print("-" * 60)
    print(f"Open in a browser:  {VIEWER_PATH}")
    if interrupted:
        sys.exit(130)


if __name__ == "__main__":
    main()
