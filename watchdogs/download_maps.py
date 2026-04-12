"""Standalone CLI for downloading OSM tiles.

Usage:
    python3 -m watchdogs.download_maps <lat> <lon> [radius_km]

Example:
    python3 -m watchdogs.download_maps 50.67 17.93        # Opole, 100km
    python3 -m watchdogs.download_maps 52.23 21.01 50     # Warsaw, 50km
"""

import sys
from pathlib import Path

from .tile_manager import download_tiles


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    lat = float(sys.argv[1])
    lon = float(sys.argv[2])
    radius = float(sys.argv[3]) if len(sys.argv) > 3 else 100.0

    project_root = Path(__file__).resolve().parent.parent
    maps_dir = project_root / "maps"

    def progress(pct, msg):
        bar_w = 30
        filled = int(pct / 100 * bar_w)
        bar = "#" * filled + "-" * (bar_w - filled)
        print(f"\r[{bar}] {pct:5.1f}%  {msg}", end="", flush=True)

    print(f"Downloading OSM tiles: center=({lat}, {lon}), radius={radius}km")
    print(f"Output: {maps_dir}/")
    print()

    manifest = download_tiles(lat, lon, maps_dir, radius_km=radius,
                              callback=progress)
    print()
    print()
    print(f"Done! {manifest['tile_count']} tiles "
          f"({manifest['errors']} errors, {manifest['skipped']} cached)")


if __name__ == "__main__":
    main()
