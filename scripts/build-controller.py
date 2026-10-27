#!/usr/bin/env python3
"""Build the fleet service, API, and local gallery dashboard."""
import argparse
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--tag', default='vibrato-controller:dev')
args = parser.parse_args()
subprocess.run(['docker', 'build', '--progress=plain', '-f', str(root / 'controller/Dockerfile'),
                '-t', args.tag, str(root)], check=True)
