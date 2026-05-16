#!/usr/bin/env python3
"""Minimal CARLA client smoke test.

Start the CARLA server first, for example:
    /home/user/fc/CARLA_0.9.15/CarlaUE4.sh -RenderOffScreen -carla-port=2000

Then run:
    python carla_smoke/minimal_carla_client.py --port 2000
"""

import argparse
import glob
import os
import sys


def add_carla_python_api(carla_root):
    """Add CARLA PythonAPI paths from a local CARLA install."""
    candidates = [
        os.path.join(carla_root, "PythonAPI", "carla"),
        os.path.join(carla_root, "PythonAPI", "carla", "agents"),
    ]
    candidates.extend(glob.glob(os.path.join(carla_root, "PythonAPI", "carla", "dist", "carla-*.egg")))
    candidates.extend(glob.glob(os.path.join(carla_root, "PythonAPI", "carla", "dist", "carla-*.whl")))

    added = []
    for path in candidates:
        if os.path.exists(path) and path not in sys.path:
            sys.path.insert(0, path)
            added.append(path)
    return added


def main():
    parser = argparse.ArgumentParser(description="Connect to CARLA and print basic world info.")
    parser.add_argument("--carla-root", default="/mnt/data2/congfeng/carla915")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--town", default=None, help="Optional map to load, e.g. Town01.")
    args = parser.parse_args()

    '''
    added_paths = add_carla_python_api(args.carla_root)
    if not added_paths:
        print(f"ERROR: no CARLA PythonAPI files found under {args.carla_root}")
        return 1
    '''

    try:
        import carla
    except ImportError as exc:
        add_carla_python_api(args.carla_root)
        import carla

    client = carla.Client(args.host, args.port)
    client.set_timeout(args.timeout)

    if args.town:
        world = client.load_world(args.town)
    else:
        world = client.get_world()

    settings = world.get_settings()
    print("CARLA connection OK")
    print(f"Server version: {client.get_server_version()}")
    print(f"Client version: {client.get_client_version()}")
    print(f"Map: {world.get_map().name}")
    print(f"Synchronous mode: {settings.synchronous_mode}")
    print(f"Fixed delta seconds: {settings.fixed_delta_seconds}")
    print(f"Actors: {len(world.get_actors())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
