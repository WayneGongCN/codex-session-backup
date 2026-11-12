#!/usr/bin/env python3
"""Provision the two prefix-scoped CAM users using the bucket owner's tccli profile."""
import argparse
import json
import os
import re
import subprocess
import tempfile
from urllib.parse import quote

from pathlib import Path

from backup import BackupError, keychain
from configuration import CosTarget, DATA

def document(target, role):
    bucket_resource = f'qcs::cos:{target.region}:uid/{target.bucket.rsplit("-", 1)[1]}:{target.bucket}'

    def allow(actions, resource, condition=None):
        statement = {'effect': 'allow', 'action': ['name/cos:' + a for a in actions], 'resource': [resource]}
        # The virtual-hosted endpoint and HTTPS are checked on every request.
        statement['condition'] = {'string_equal': {'cos:host': target.bucket + '.' + target.endpoint}}
        if condition:
            statement['condition'].update(condition)
        return statement

    prefix = quote(target.prefix, safe='')
    return {'version': '2.0', 'statement': [
        allow(['GetBucket'], bucket_resource + '/*',
              {'string_like': {'cos:prefix': [prefix, prefix + '%2F*']}}),
        allow(['HeadObject'], bucket_resource + '/' + target.prefix),
        allow(['HeadObject', 'GetObject', 'PutObject', 'InitiateMultipartUpload', 'UploadPart',
               'CompleteMultipartUpload', 'ListParts', 'AbortMultipartUpload'],
              bucket_resource + '/' + target.prefix + '/*'),
        allow(['DeleteObject'], bucket_resource + '/' + target.prefix +
              ('/locks/*' if role == 'writer' else '/*')),
    ]}


def invoke(profile, service, action, **params):
    command = ['tccli', service, action, '--profile', profile, '--output', 'json']
    if params:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.json') as request:
            json.dump(params, request)
            request.flush()
            result = subprocess.run(command + ['--cli-input-json', 'file://' + request.name],
                                    capture_output=True, text=True)
    else:
        result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        error = re.search(r'\b(?:AuthFailure|ResourceNotFound|InvalidParameter(?:Value)?|FailedOperation|UnauthorizedOperation|LimitExceeded|ResourceInUse|OperationDenied)(?:\.[A-Za-z0-9]+)*\b',
                          result.stdout + result.stderr)
        raise BackupError(action + ' failed: ' + (error.group() if error else 'see account and permissions'))
    return json.loads(result.stdout)


def provision(target, profile, role):
    purpose = 'Codex encrypted session backup; restricted to ' + target.prefix
    name = 'codex-session-backup-' + role
    try:
        user = invoke(profile, 'cam', 'GetUser', Name=name)
        if user.get('Remark') != purpose:
            raise BackupError('A CAM user with the backup name already exists with a different purpose')
    except BackupError as error:
        if 'ResourceNotFound.UserNotExist' not in str(error):
            raise
        user = invoke(profile, 'cam', 'AddUser', Name=name, Remark=purpose, ConsoleLogin=0, UseApi=0)
    uin = user['Uin']
    policy = document(target, role)
    matched = [p for p in (invoke(profile, 'cam', 'ListPolicies', Keyword=name).get('List') or [])
               if p.get('PolicyName') == name]
    if matched:
        policy_id = matched[0]['PolicyId']
        saved = invoke(profile, 'cam', 'GetPolicy', PolicyId=policy_id)
        if json.loads(saved['PolicyDocument']) != policy:
            raise BackupError('A CAM policy with the backup name already exists with different permissions')
    else:
        policy_id = invoke(profile, 'cam', 'CreatePolicy', PolicyName=name,
                           Description=purpose, PolicyDocument=json.dumps(policy))['PolicyId']
    attached = invoke(profile, 'cam', 'ListAttachedUserPolicies', TargetUin=uin).get('List') or []
    if any(p.get('PolicyId') != policy_id for p in attached):
        raise BackupError('The backup user has another policy attached')
    if not attached:
        invoke(profile, 'cam', 'AttachUserPolicy', AttachUin=uin, PolicyId=policy_id)
    access = invoke(profile, 'cam', 'ListAccessKeys', TargetUin=uin).get('AccessKeys') or []
    try:
        existing = json.loads(keychain('cos-' + role))
    except BackupError as error:
        if '(-25300)' not in str(error):
            raise
        existing = None
    if existing and len(access) == 1 and access[0].get('AccessKeyId') == existing['id']:
        print(role + ' credential already present in Keychain')
        return
    if access:
        raise BackupError('This CAM user already has a key. Import its key or review it in CAM before retrying.')
    key = invoke(profile, 'cam', 'CreateAccessKey', TargetUin=uin, Description=name)['AccessKey']
    try:
        keychain('cos-' + role, json.dumps({'id': key['AccessKeyId'], 'key': key['SecretAccessKey']}).encode())
    except BackupError:
        invoke(profile, 'cam', 'DeleteAccessKey', TargetUin=uin, AccessKeyId=key['AccessKeyId'])
        raise
    print(role + ' credential saved in Keychain')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=DATA)
    parser.add_argument('--profile')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    target = CosTarget.load(args.data_dir / 'connection.json')
    if not args.apply:
        print(json.dumps({role: document(target, role) for role in ('writer', 'maintenance')}, indent=2))
        return
    if not args.profile:
        parser.error('--apply requires the COS owner tccli profile')
    buckets = subprocess.run(['tccli', 'cos', 'list_buckets', '--profile', args.profile],
                             capture_output=True, text=True, check=True).stdout
    if not re.search(r'(?<![a-z0-9-])' + re.escape(target.bucket) + r'(?![a-z0-9-])', buckets):
        raise BackupError('The selected profile does not list the target bucket')
    for role in ('writer', 'maintenance'):
        provision(target, args.profile, role)


if __name__ == '__main__':
    try:
        main()
    except (BackupError, KeyError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
