"""Explicit local/VPN listener addresses. Never bind wildcard or public addresses."""
import ipaddress
import re
import urllib.parse


PRIVATE = tuple(ipaddress.ip_network(n) for n in (
    '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '100.64.0.0/10', 'fc00::/7'))


def bind_ip(value):
    address = ipaddress.ip_address(value)
    if not address.is_loopback and not any(address.version == n.version and address in n for n in PRIVATE):
        raise ValueError('Bind a literal loopback, private LAN or VPN address, not a wildcard/public address')
    return address


def authority(host, port):
    address = bind_ip(host)
    return (f'[{address}]' if address.version == 6 else str(address)) + ':' + str(port)


def hub_url(config):
    return 'http://' + authority(config.get('host', '127.0.0.1'), config.get('port', 8778))


class ProxyOrigin:
    """A single explicit HTTPS origin, accepted only through named proxy IPs."""
    def __init__(self, config):
        self.origin = self.host = None
        self.proxies = ()
        public = config.get('public_url')
        proxies = config.get('trusted_proxies', [])
        if not public and not proxies:
            return
        if not isinstance(public, str) or not isinstance(proxies, list) or not 1 <= len(proxies) <= 16:
            raise ValueError('public_url and 1..16 trusted_proxies must be configured together')
        url = urllib.parse.urlsplit(public)
        if (url.scheme != 'https' or not url.hostname or url.username is not None or url.password is not None
                or url.path not in ('', '/') or url.query or url.fragment or not url.netloc.isascii()):
            raise ValueError('public_url must be an HTTPS origin without credentials, path or query')
        # Explicit authority; reject ambiguous spellings, empty/zero ports, escapes
        # and host suffix matching. DNS names and bracketed IP literals are supported.
        port = url.port
        host = url.hostname
        try:
            address = ipaddress.ip_address(host)
            host = '['+str(address)+']' if address.version == 6 else str(address)
        except ValueError:
            if len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in host.split('.')):
                raise ValueError('Invalid public hostname')
        if port is not None and not 1 <= port <= 65535:
            raise ValueError('Invalid public port')
        expected = host + (':'+str(port) if port is not None else '')
        if url.netloc != expected:
            raise ValueError('public_url must use a canonical lowercase authority')
        networks = tuple(ipaddress.ip_network(p, strict=True) for p in proxies)
        # Broad public ranges/wildcards must not make arbitrary callers proxies.
        for network in networks:
            if network.prefixlen == 0 or not (network.network_address.is_loopback or any(
                    network.version == n.version and network.subnet_of(n) for n in PRIVATE)):
                raise ValueError('trusted_proxies must be explicit loopback/private/VPN IPs or subnets')
        self.host, self.origin, self.proxies = expected, 'https://'+expected, networks

    def trusted(self, peer):
        address = ipaddress.ip_address(peer)
        return any(address.version == n.version and address in n for n in self.proxies)
