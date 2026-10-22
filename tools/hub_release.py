"""Build a complete stdlib-compatible Hub release or publish it over VPN.

Publication stages code only; activation remains the password owner's action.
Credentials are read from an owned 0600 file and never printed.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tarfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from hub_updates import RUNTIME, version, MAX_ARCHIVE
from hub_connections import vpn_endpoint, check_dns, read_private


def package(backend, release, output):
    version(release)
    files = {}
    backend = Path(backend)
    for path in sorted(backend.rglob('*')):
        if (not path.is_file() or path.is_symlink() or '__pycache__' in path.parts or
                path.name.startswith('test_') or path.suffix not in {'.py', '.html', '.css', '.js', '.json'}):
            continue
        name = 'backend/' + path.relative_to(backend).as_posix()
        files[name] = path.read_bytes()
    manifest = {'version':release, 'runtime':RUNTIME,
                'files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}
    files['release.json'] = json.dumps(manifest, ensure_ascii=False).encode()
    with tarfile.open(output, 'w:gz') as archive:
        for name,data in files.items():
            info = tarfile.TarInfo(name); info.size=len(data); info.mode=0o600
            archive.addfile(info, io.BytesIO(data))
    raw=Path(output).read_bytes()
    if len(raw)>MAX_ARCHIVE: raise ValueError('Release archive exceeds 4 MiB')
    return hashlib.sha256(raw).hexdigest()


def publish(origin, key_file, archive):
    # Reuse the VPN origin policy; this endpoint belongs to Hub rather than /v1.
    origin=vpn_endpoint(origin.rstrip('/')+'/v1').removesuffix('/v1')
    check_dns(origin+'/v1')
    token=read_private(key_file).strip()
    raw=Path(archive).read_bytes()
    if not 1<=len(raw)<=MAX_ARCHIVE:raise ValueError('Invalid release size')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs):return None
    request=urllib.request.Request(origin+'/api/releases/stage',data=raw,
        headers={'Authorization':'Bearer '+token,'Content-Type':'application/gzip',
                 'X-Release-SHA256':hashlib.sha256(raw).hexdigest()})
    with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=30) as response:
        result=json.loads(response.read(65536))
    return {'staged':True,'active':result.get('active'),'releases':result.get('releases',[])}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    build=sub.add_parser('build');build.add_argument('--backend',required=True);build.add_argument('--version',required=True);build.add_argument('--output',required=True)
    push=sub.add_parser('publish');push.add_argument('--origin',required=True);push.add_argument('--key-file',required=True);push.add_argument('--archive',required=True)
    args=parser.parse_args()
    if args.command=='build':print(json.dumps({'sha256':package(args.backend,args.version,args.output)}))
    else:print(json.dumps(publish(args.origin,args.key_file,args.archive)))


if __name__=='__main__':main()
