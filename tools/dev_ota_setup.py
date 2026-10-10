#!/usr/bin/env python3
"""Provision an owner-only LAN development OTA service from existing local build inputs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def private_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n'); path.chmod(0o600)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', type=Path, required=True)
    p.add_argument('--checkout', type=Path, required=True)
    p.add_argument('--host', required=True, help='Explicit private IPv4/VPN address reachable by the phone')
    p.add_argument('--port', type=int, default=8779)
    p.add_argument('--sdk', type=Path, required=True)
    p.add_argument('--java-home', type=Path, required=True)
    p.add_argument('--build-tools', type=Path, required=True)
    p.add_argument('--debug-key', type=Path, required=True, help='Existing matching Android debug key; development only')
    p.add_argument('--minimum-version', type=int, required=True)
    p.add_argument('--launch-agents', action='store_true', help='Install and start two macOS user LaunchAgents')
    args = p.parse_args()
    import ipaddress
    address = ipaddress.ip_address(args.host)
    if address.version != 4 or address.is_unspecified or not address.is_private:
        p.error('Use an explicit private IPv4 address')
    os.umask(0o077)
    state = args.state.expanduser().resolve(); state.mkdir(parents=True, mode=0o700, exist_ok=True); state.chmod(0o700)
    checkout = args.checkout.resolve(strict=True)
    token = state / 'ota.key'
    if not token.exists(): token.write_text(secrets.token_urlsafe(48)); token.chmod(0o600)
    key, cert = state / 'tls.key', state / 'tls.pem'
    if not key.exists() and not cert.exists():
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-sha256', '-nodes', '-days', '365',
                        '-keyout', str(key), '-out', str(cert), '-subj', '/CN=Assistant dev OTA',
                        '-addext', 'subjectAltName=IP:' + args.host + ',IP:127.0.0.1,IP:10.0.2.2'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key.chmod(0o600); cert.chmod(0o600)
    if not key.exists() or not cert.exists(): p.error('Incomplete existing TLS identity; inspect it manually')
    details = subprocess.check_output(['openssl', 'x509', '-in', str(cert), '-text', '-noout'], text=True)
    if ('IP Address:' + args.host) not in details:
        p.error('Existing certificate SAN does not match this IP; explicitly re-provision TLS identity')
    pin = hashlib.sha256(subprocess.check_output(['openssl', 'x509', '-in', str(cert), '-outform', 'DER'])).hexdigest()
    signing = state / 'dev-signing.keystore'
    if not signing.exists(): shutil.copyfile(args.debug_key, signing); signing.chmod(0o600)
    environment = {**os.environ, 'JAVA_HOME': str(args.java_home)}
    signature = subprocess.check_output([str(args.java_home / 'bin/keytool'), '-list', '-v', '-keystore', str(signing),
                                         '-storepass', 'android', '-alias', 'androiddebugkey'], env=environment, text=True)
    import re
    certificate = re.search(r'SHA256: ([A-Fa-f0-9:]+)', signature)[1].replace(':', '').lower()
    private_json(state / 'signing.json', {'KEYSTORE_PASSWORD': 'android', 'KEY_PASSWORD': 'android'})
    inputs = state / 'inputs'; inputs.mkdir(mode=0o700, exist_ok=True)
    for directory in ('vendor', 'app/libs'):
        shutil.copytree(checkout / directory, inputs / directory, dirs_exist_ok=True)
    # Only the ignored offline weights; do not freeze tracked catalog, licenses or UI.
    ignored = subprocess.check_output(['git', '-C', str(checkout), 'ls-files', '--others', '--ignored', '--exclude-standard',
                                       'app/src/main/assets'], text=True).splitlines()
    for relative in ignored:
        source = checkout / relative
        if source.is_symlink() or not source.is_file(): p.error('Expected regular prepared asset')
        target = inputs / relative; target.parent.mkdir(mode=0o700, parents=True, exist_ok=True); shutil.copy2(source, target)
    (inputs / 'dependencies.sha256').write_text(hashlib.sha256((checkout / 'dependencies.json').read_bytes()).hexdigest())
    config = dict(host=args.host, port=args.port, output=str(state / 'releases'), token_file=str(token),
                  tls_cert=str(cert), tls_key=str(key), checkout=str(checkout), branch='main', workspace=str(state / 'worker'),
                  prepared_inputs=str(inputs), java_home=str(args.java_home.resolve()), android_home=str(args.sdk.resolve()),
                  build_tools=str(args.build_tools.resolve()), keystore=str(signing), alias='androiddebugkey',
                  certificate_sha256=certificate, credentials_file=str(state / 'signing.json'), minimum_version=args.minimum_version,
                  poll_seconds=30)
    private_json(state / 'config.json', config)
    private_json(state / 'phone-setup.json', dict(endpoint=f'https://{args.host}:{args.port}', token=token.read_text().strip(),
                                                 certificateSha256=pin, automatic=True))
    if args.launch_agents:
        if sys.platform != 'darwin': p.error('LaunchAgents are only available on macOS')
        directory = Path.home() / 'Library/LaunchAgents'; directory.mkdir(parents=True, exist_ok=True)
        for command in ('serve', 'watch'):
            label = 'local.assistant.dev-ota.' + command
            path = directory / (label + '.plist')
            with path.open('wb') as stream:
                plistlib.dump(dict(Label=label, ProgramArguments=[sys.executable, str(ROOT / 'backend/dev_ota.py'), command,
                                                                 '--config', str(state / 'config.json')],
                                  RunAtLoad=True, KeepAlive=True, ThrottleInterval=30, WorkingDirectory=str(ROOT),
                                  StandardOutPath=str(state / (command + '.log')), StandardErrorPath=str(state / (command + '.log'))), stream)
            path.chmod(0o600)
            domain = 'gui/' + str(os.getuid())
            subprocess.run(['launchctl', 'bootout', domain + '/' + label], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)
    print('Dev OTA configured: ' + f'https://{args.host}:{args.port}')
    print('Private phone configuration: ' + str(state / 'phone-setup.json'))
    print('Certificate SHA-256: ' + pin)


if __name__ == '__main__': main()
