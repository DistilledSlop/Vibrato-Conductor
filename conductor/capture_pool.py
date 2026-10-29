"""Copy and verify the raw training captures included in a dataset export."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shutil


def copy_training_captures(index, destination, jobs=8):
    """Create a new capture directory from the indexed training split.

    The caller must stage the directory privately and publish it only after all
    verification and admission checks have succeeded.
    """
    destination = Path(destination)
    captures = {c['base']: c for run in index['accepted'] for c in run['captures']}
    bases = index['trainBases']
    if len(set(bases)) != len(bases):
        raise ValueError('Duplicate training capture base')
    names = [Path(base).name for base in bases]
    if len(set(names)) != len(names):
        raise ValueError('Duplicate training capture basename')
    if any(base not in captures for base in bases):
        raise ValueError('Training capture missing from accepted captures')
    destination.mkdir()

    def copy_one(item):
        base, suffix, expected = item
        source = Path(base + suffix)
        target = destination / (Path(base).name + suffix)
        with source.open('rb') as src, target.open('xb') as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        h = hashlib.sha256()
        with target.open('rb') as copied:
            for block in iter(lambda: copied.read(1024 * 1024), b''):
                h.update(block)
        if h.hexdigest() != expected:
            raise ValueError(f'Capture copy hash mismatch: {source}')

    items = [(base, suffix, captures[base][field]) for base in bases
             for suffix, field in (('.bin', 'binarySha256'), ('.jsonl', 'indexSha256'))]
    with ThreadPoolExecutor(max_workers=jobs) as executor:
        list(executor.map(copy_one, items))
