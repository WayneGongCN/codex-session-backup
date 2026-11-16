"""Non-secret settings for a single COS repository."""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DATA = Path.home() / 'Library/Application Support/Codex Session Backup'


def documents_path(data):
    value = data.get('documents_source')
    if value is None:
        return None
    if not isinstance(value, str) or '\0' in value or not Path(value).is_absolute():
        raise ValueError('documents_source must be an absolute path')
    return Path(value)


@dataclass(frozen=True)
class CosTarget:
    bucket: str
    prefix: str
    endpoint: str
    region: str
    backup_host: str
    gpg_fingerprint: str = ''
    gpg_public_key_url: str = ''
    documents_source: Optional[Path] = None

    @classmethod
    def load(cls, file):
        if not file.is_file():
            raise ValueError(f'Create {file} from connection.example.json')
        data = json.loads(file.read_bytes())
        if data.get('transport', 'direct') != 'direct':
            raise ValueError('This release supports direct COS access only. Configure a COS '
                             'endpoint and dedicated credentials in a separate data directory. '
                             'See docs/usage.md for migration steps.')
        try:
            fields = {key: data[key] for key in ('bucket', 'prefix', 'endpoint', 'region')}
        except KeyError as error:
            raise ValueError(f'Missing COS setting {error.args[0]} in {file}') from error
        if not re.fullmatch(r'[a-z0-9-]+-\d+', fields['bucket']):
            raise ValueError('Invalid COS bucket')
        if not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', fields['prefix']):
            raise ValueError('Invalid COS repository prefix')
        if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+', fields['endpoint']):
            raise ValueError('Use a COS endpoint hostname without a scheme or path')
        if not re.fullmatch(r'[a-z0-9-]+', fields['region']):
            raise ValueError('Invalid COS region')
        return cls(**fields, backup_host=data.get('backup_host', 'codex-mac'),
                   gpg_fingerprint=data.get('gpg_fingerprint', ''),
                   gpg_public_key_url=data.get('gpg_public_key_url', ''),
                   documents_source=documents_path(data))

    @property
    def repository_path(self):
        return f'{self.bucket}/{self.prefix}'

    @property
    def repository(self):
        return f's3:https://{self.endpoint}/{self.repository_path}'
