"""Bounded A/AAAA queries to the LAN upstream, bypassing optimizer hosts/cache.

This only obtains comparison addresses. The caller must still reject non-public IPs
and verify the actual hostname's certificate before making any DNS publication.
"""
import ipaddress
import secrets
import socket
import struct
from pathlib import Path


def skip_name(packet, offset):
    for _ in range(128):
        if offset >= len(packet):raise ValueError('truncated DNS name')
        length = packet[offset]
        if length & 0xc0 == 0xc0:
            if offset + 1 >= len(packet):raise ValueError('truncated DNS pointer')
            pointer = ((length & 0x3f) << 8) | packet[offset+1]
            if pointer >= len(packet):raise ValueError('invalid DNS pointer')
            return offset + 2
        if length > 63:raise ValueError('invalid DNS label')
        offset += 1
        if not length:return offset
        offset += length
    raise ValueError('DNS name too long')


def parse(packet, transaction, question):
    if len(packet) < 12:raise ValueError('short DNS packet')
    ident, flags, qd, an, ns, ar = struct.unpack('!6H', packet[:12])
    if ident != transaction or not flags & 0x8000 or flags & 0x7800 or qd != 1:
        raise ValueError('mismatched DNS reply')
    if flags & 0x0200:raise ValueError('truncated DNS reply; no unsafe partial baseline')
    if packet[12:12+len(question)] != question:raise ValueError('mismatched DNS question')
    pos = 12 + len(question)
    query_type = struct.unpack('!H', question[-4:-2])[0]
    addresses = []
    if an > 128:raise ValueError('too many DNS answers')
    for _ in range(an):
        pos = skip_name(packet, pos)
        if pos + 10 > len(packet):raise ValueError('short DNS record')
        kind, klass, ttl, length = struct.unpack('!HHIH', packet[pos:pos+10]);pos += 10
        if pos + length > len(packet):raise ValueError('short DNS data')
        if query_type == kind == 1 and klass == 1 and length == 4:
            addresses.append(socket.inet_ntop(socket.AF_INET, packet[pos:pos+4]))
        elif query_type == kind == 28 and klass == 1 and length == 16:
            addresses.append(socket.inet_ntop(socket.AF_INET6, packet[pos:pos+16]))
        pos += length
    return {'status': flags & 15, 'addresses':list(dict.fromkeys(addresses))}


def encode_query(domain, query_type=1, transaction=None):
    labels = domain.encode('ascii').split(b'.')
    if not labels or any(not 1 <= len(x) <= 63 for x in labels):raise ValueError('invalid DNS name')
    question = b''.join(bytes([len(x)]) + x for x in labels) + b'\0' + struct.pack('!HH', query_type, 1)
    transaction = secrets.randbelow(65536) if transaction is None else transaction
    request = struct.pack('!6H', transaction, 0x0100, 1, 0, 0, 0) + question
    return transaction, question, request


def _resolve(domain, server, timeout, query_type):
    server = str(ipaddress.IPv4Address(server))
    transaction, question, request = encode_query(domain, query_type)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        # Connected UDP also restricts replies to the selected upstream endpoint.
        sock.connect((server, 53));sock.send(request)
        packet = sock.recv(4096)
    return {**parse(packet, transaction, question), 'server':server}


def resolve_a(domain, server='1.1.1.1', timeout=2):
    return _resolve(domain, server, timeout, 1)


def resolve_aaaa(domain, server='1.1.1.1', timeout=2):
    return _resolve(domain, server, timeout, 28)


def has_global_ipv6(path='/proc/net/if_inet6'):
    try:
        return any(ipaddress.IPv6Address(int(line.split()[0], 16)).is_global
                   for line in Path(path).read_text().splitlines() if line.split())
    except (OSError, ValueError):
        return False
