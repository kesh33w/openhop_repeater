"""Keep a loaded file-backed identity out of persisted configuration."""
import base64
import copy
from pathlib import Path


def config_for_persistence(config):
    candidate = copy.deepcopy(config)
    repeater = candidate.get('repeater') or {}
    identity_file = repeater.get('identity_file')
    if identity_file and 'identity_key' in repeater:
        runtime_key = repeater['identity_key']
        if isinstance(runtime_key, str):
            runtime_key = bytes.fromhex(runtime_key)
        file_key = base64.b64decode(Path(identity_file).read_bytes().strip(), validate=True)
        if not isinstance(runtime_key, bytes) or len(file_key) not in (32, 64) or runtime_key != file_key:
            raise ValueError('File-backed identity differs from the runtime identity; configuration was not saved')
        del repeater['identity_key']
    return candidate
