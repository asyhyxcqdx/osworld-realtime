#!/usr/bin/env python3
"""Render offline HTML from an existing realtime trajectory or results directory."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from lib_realtime_trajectory import render_trajectory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path, help='trajectory.jsonl, its task directory, or a results root')
    parser.add_argument('--output', type=Path, help='Output HTML path (single trajectory only)')
    parser.add_argument('--recursive', action='store_true', help='Render trajectory.jsonl / traj.jsonl beneath a results root')
    args = parser.parse_args()
    if args.recursive and (args.output or not args.path.is_dir()):
        parser.error('--recursive requires a directory and cannot be combined with --output')
    paths = sorted(args.path.rglob('trajectory.jsonl')) if args.recursive else [args.path]
    if args.recursive:
        paths += sorted(p for p in args.path.rglob('traj.jsonl') if not (p.parent / 'trajectory.jsonl').exists())
    if not paths:
        parser.error('No trajectory files found')
    errors = 0
    for path in paths:
        try:
            print(render_trajectory(path, args.output))
        except (OSError, ValueError) as exc:
            errors += 1
            print(f'{path}: {exc}', file=sys.stderr)
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
