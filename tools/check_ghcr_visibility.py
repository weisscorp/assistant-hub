#!/usr/bin/env python3
"""Refuse publishing to an existing non-private GHCR package."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def check_private_package(owner, image, token):
    # weisscorp is a personal account. The organization endpoint returns 404
    # even for an existing user-owned package and must never be used here.
    url = 'https://api.github.com/users/{}/packages/container/{}'.format(
        urllib.parse.quote(owner, safe=''), urllib.parse.quote(image, safe=''))
    request = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            package = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            # New GHCR packages default to private; a permission-hidden private
            # package will fail the subsequent push rather than become public.
            return 'New GHCR package: retain the private default.'
        raise RuntimeError('Package visibility check failed (HTTP {})'.format(error.code)) from None
    if package.get('visibility') != 'private':
        raise RuntimeError('Refusing to publish: existing GHCR package is not private')
    return 'Existing GHCR package is private.'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', required=True)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    token = os.environ.get('GH_TOKEN', '')
    if not token:
        parser.error('GH_TOKEN is required')
    try:
        print(check_private_package(args.owner, args.image, token))
    except (RuntimeError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
