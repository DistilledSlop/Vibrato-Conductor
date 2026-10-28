"""Current independent admission for source runs of earned-context templates."""
import json
import re
from pathlib import Path
from .storage import IDENTIFIER


def source_admitted(admission_root: Path | None, source_run: str, manifest_sha: str) -> bool:
    if (admission_root is None or not isinstance(source_run, str) or not IDENTIFIER.fullmatch(source_run)
            or not isinstance(manifest_sha, str) or not re.fullmatch('[0-9a-f]{64}', manifest_sha)):
        return False
    path = Path(admission_root) / (source_run + '.json')
    if path.is_symlink(): return False
    try:
        receipt = json.loads(path.read_text())
        from .artifacts import QUALITY_POLICY_VERSION
        return (receipt.get('runId') == source_run and receipt.get('manifestSha256') == manifest_sha
                and receipt.get('quality', {}).get('policyVersion') == QUALITY_POLICY_VERSION
                and receipt.get('quality', {}).get('eligible') is True)
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def lineage_admitted(admission_root: Path | None, lineage: object) -> bool:
    return (isinstance(lineage, dict) and source_admitted(admission_root, lineage.get('sourceRun'),
                                                           lineage.get('sourceManifestSha256')))
