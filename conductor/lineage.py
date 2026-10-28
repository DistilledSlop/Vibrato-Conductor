"""Current independent admission for source runs of earned-context templates."""
import json
import re
from contextvars import ContextVar
from pathlib import Path
from .storage import IDENTIFIER

# A compact source can itself have earned lineage. Cycles must never authorize.
_checking = ContextVar('lineage_checking', default=frozenset())


def source_admitted(admission_root: Path | None, source_run: str, manifest_sha: str) -> bool:
    if (admission_root is None or not isinstance(source_run, str) or not IDENTIFIER.fullmatch(source_run)
            or not isinstance(manifest_sha, str) or not re.fullmatch('[0-9a-f]{64}', manifest_sha)):
        return False
    admission_root = Path(admission_root)
    path = admission_root / (source_run + '.json')
    if path.is_symlink(): return False
    key = (str(admission_root.resolve()), source_run)
    active = _checking.get()
    if key in active: return False
    token = _checking.set(active | {key})
    try:
        receipt = json.loads(path.read_text())
        from .artifacts import QUALITY_POLICY_VERSION
        if (receipt.get('runId') != source_run
                or receipt.get('quality', {}).get('policyVersion') != QUALITY_POLICY_VERSION
                or receipt.get('quality', {}).get('eligible') is not True):
            return False
        if receipt.get('compactSchemaVersion') is not None:
            # Only the recorded original and current compact identities may refer
            # to this source, and both require verification of the entire compact
            # record and its current external receipt. Never trust a bare alias.
            if manifest_sha not in (receipt.get('sourceManifestSha256'), receipt.get('manifestSha256')):
                return False
            root = admission_root.parent / source_run
            if root.is_symlink() or not root.is_dir(): return False
            from .compact import compact_quality
            return compact_quality(root, admission_root).get('eligible') is True
        return receipt.get('manifestSha256') == manifest_sha
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return False
    finally:
        _checking.reset(token)


def lineage_admitted(admission_root: Path | None, lineage: object) -> bool:
    return (isinstance(lineage, dict) and source_admitted(admission_root, lineage.get('sourceRun'),
                                                           lineage.get('sourceManifestSha256')))
