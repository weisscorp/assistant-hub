"""Explicit local/VPN listener addresses. Never bind wildcard or public addresses."""
import ipaddress


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
