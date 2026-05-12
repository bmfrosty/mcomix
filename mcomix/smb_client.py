"""smb_client.py — Synchronous SMB access via smbprotocol/smbclient.

Public API (all paths use smb:// URIs):
    set_credentials(host, username, password)
    list_directory(smb_uri)  -> list[DirEntry]
    open_file(smb_uri)       -> binary file-like object
    parent_uri(smb_uri)      -> str | None
    is_available()           -> bool
"""

import io
import struct
from typing import NamedTuple
from urllib.parse import urlparse, unquote, quote

from mcomix import log

# Samba guest/anonymous sessions have no signing key so negotiate verification
# always fails.  Disable it globally; for authenticated sessions the signing
# key is present so this doesn't weaken security there.
try:
    import smbclient as _smbclient
    _smbclient.ClientConfig(require_secure_negotiate=False)
except Exception:
    pass


class DirEntry(NamedTuple):
    name: str
    is_dir: bool
    size: int  # bytes; 0 for directories


# ---------------------------------------------------------------------------
# URI <-> UNC conversion
# ---------------------------------------------------------------------------

def _smb_uri_to_unc(uri: str):
    """Parse smb://host/share/sub/path and return (host, share, unc).

    unc is the \\\\host\\share\\sub\\path string smbclient expects.
    """
    parsed = urlparse(uri)
    host = (parsed.hostname or '').lower()
    parts = [unquote(p) for p in parsed.path.split('/') if p]
    share = parts[0] if parts else ''
    sub = '\\'.join(parts[1:])
    unc = f'\\\\{host}\\{share}'
    if sub:
        unc += f'\\{sub}'
    return host, share, unc


def _unc_to_smb_uri(host: str, share: str, subpath: str = '') -> str:
    """Build a normalised smb://host/share/sub/path URI."""
    parts = [p for p in subpath.replace('\\', '/').split('/') if p]
    encoded = '/'.join(quote(p, safe='') for p in parts)
    base = f'smb://{host.lower()}/{share.lower()}'
    return f'{base}/{encoded}' if encoded else base


# ---------------------------------------------------------------------------
# Credential store (session-scoped, in memory)
# ---------------------------------------------------------------------------

_credentials: dict[str, tuple] = {}  # host -> (username, password)


def _is_guest(username) -> bool:
    return not username or username.lower() == 'guest'


def _register(host: str, username, password) -> None:
    """Call smbclient.register_session with the right flags for the account type."""
    import smbclient
    kw = dict(auth_protocol='ntlm')
    if _is_guest(username):
        kw['require_signing'] = False
    smbclient.register_session(host, username=username, password=password, **kw)


def set_credentials(host: str, username, password) -> None:
    """Store credentials for host and register an smbclient session."""
    host = host.lower()
    _credentials[host] = (username, password)
    try:
        _register(host, username, password)
        log.debug('smb_client: registered session for %s as %s', host, username)
    except Exception as ex:
        log.debug('smb_client: register_session %s: %s', host, ex)


def clear_credentials(host: str = None) -> None:
    """Clear cached credentials and drop smbclient connection pool entries.

    If host is None, clear everything.
    """
    import smbclient
    if host is None:
        _credentials.clear()
    else:
        _credentials.pop(host.lower(), None)
    smbclient.reset_connection_cache()
    log.debug('smb_client: cleared credentials for %s', host or 'all hosts')


def get_credentials(host: str):
    return _credentials.get(host.lower(), (None, None))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_directory(smb_uri: str) -> list:
    """List the directory at smb_uri.

    Returns a sorted list of DirEntry: directories first (alpha), then files
    (alpha).  If no credentials are stored for the host, tries guest access
    first (Samba anonymous / map-to-guest).  Raises on failure.

    smb://            → network discovery (mDNS / nmblookup)
    smb://host        → share enumeration via DCE/RPC NetrShareEnum
    smb://host/share/ → directory listing
    """
    parsed_path_parts = [p for p in smb_uri.split('/') if p and p != 'smb:']
    if not parsed_path_parts:
        return _list_servers()

    host = parsed_path_parts[0].lower()
    has_share = len(parsed_path_parts) > 1

    if not has_share:
        return _list_shares(host)

    host, _share, unc = _smb_uri_to_unc(smb_uri)
    log.debug('smb_client.list_directory: %s', unc)

    if host not in _credentials:
        # Try Samba guest/anonymous before prompting for credentials.
        try:
            _register(host, 'guest', '')
            raw = _scandir(unc)
            _credentials[host] = ('guest', '')  # cache without re-registering
            log.debug('smb_client: guest access OK for %s', host)
            return _sort_entries(raw)
        except Exception as ex:
            log.debug('smb_client: guest probe failed for %s: %s', host, ex)
            # Fall through — caller (browser dialog) will prompt for credentials
            raise

    # Ensure the session is registered (handles app restarts / cache reset).
    username, password = _credentials[host]
    _register(host, username, password)
    return _sort_entries(_scandir(unc))


def _list_servers() -> list:
    """Discover SMB servers on the local network via mDNS (avahi-browse) and nmblookup."""
    import subprocess
    servers: set[str] = set()

    # mDNS: avahi-browse -r -t _smb._tcp  (available on most Linux desktops)
    try:
        out = subprocess.check_output(
            ['avahi-browse', '-r', '-t', '_smb._tcp'],
            stderr=subprocess.DEVNULL, timeout=5, text=True,
        )
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('hostname = ['):
                h = line[12:].rstrip(']').rstrip('.').lower()
                if h:
                    servers.add(h)
        log.debug('smb_client: avahi found: %s', servers)
    except Exception as ex:
        log.debug('smb_client: avahi-browse: %s', ex)

    # NetBIOS broadcast fallback (requires samba-client / nmblookup)
    if not servers:
        try:
            out = subprocess.check_output(
                ['nmblookup', '-M', '--', '-'],
                stderr=subprocess.DEVNULL, timeout=3, text=True,
            )
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and not line.startswith('querying'):
                    servers.add(parts[1].lower())
            log.debug('smb_client: nmblookup found: %s', servers)
        except Exception as ex:
            log.debug('smb_client: nmblookup: %s', ex)

    if not servers:
        raise RuntimeError(
            'No SMB servers found via mDNS.\n'
            'Install avahi and ensure _smb._tcp services are advertised, '
            'or type a server address directly, e.g.  smb://myserver'
        )

    return sorted([DirEntry(s, True, 0) for s in servers], key=lambda e: e.name.lower())


def _list_shares(host: str) -> list:
    """Enumerate shares on host via DCE/RPC NetrShareEnum over IPC$\\srvsvc."""
    log.debug('smb_client._list_shares: %s', host)
    # Ensure we have a session for this host.
    if host not in _credentials:
        try:
            _register(host, 'guest', '')
            _credentials[host] = ('guest', '')
        except Exception as ex:
            log.debug('smb_client: guest probe for %s: %s', host, ex)
            raise
    u, p = _credentials[host]
    _register(host, u, p)

    import smbclient

    # SRVSVC interface UUID: 4b324fc8-1670-01d3-1278-5a47bf6ee188 v3.0
    # NDR   transfer  UUID: 8a885d04-1ceb-11c9-9fe8-08002b104860 v2.0
    # Encoded as mixed-endian Windows GUIDs (Data1/2/3 LE, Data4 BE).
    _SRVSVC = bytes.fromhex('c84f324b7016d30112785a47bf6ee188')
    _NDR    = bytes.fromhex('045d888aeb1cc9119fe808002b104860')

    def _hdr(ptype, call_id, body):
        frag_len = 16 + len(body)
        h  = struct.pack('BBBB', 5, 0, ptype, 0x03)   # ver, ptype, PFC_FIRST|LAST
        h += struct.pack('<I',   0x10)                  # data_rep: little-endian
        h += struct.pack('<HHI', frag_len, 0, call_id)
        return h + body

    def _bind():
        ctx  = struct.pack('<HH', 0, 1)                # ctx_id=0, num_xfer_syn=1
        ctx += _SRVSVC + struct.pack('<I', 3)           # abstract: SRVSVC v3.0
        ctx += _NDR    + struct.pack('<I', 2)           # transfer: NDR v2.0
        body  = struct.pack('<HHI', 4280, 4280, 0)     # max_xmit/recv, assoc_group
        body += struct.pack('BBH',  1,    0,    0)     # n_ctx_items + 3 pad bytes
        body += ctx
        return _hdr(11, 1, body)                        # ptype BIND=11, call_id=1

    def _net_share_enum():
        # Stub: NULL ServerName, Level=1, container ref, PrefMaxLen, NULL ResumeHandle.
        stub  = struct.pack('<I', 0)           # ServerName = NULL
        stub += struct.pack('<II', 1, 1)       # Level=1, union discriminant=1
        stub += struct.pack('<I', 0x00020000)  # SHARE_INFO_1_CONTAINER referent
        stub += struct.pack('<I', 0xFFFFFFFF)  # PrefMaxLen = unlimited
        stub += struct.pack('<I', 0)           # ResumeHandle = NULL
        # Deferred: SHARE_INFO_1_CONTAINER (count=0, buffer=NULL)
        stub += struct.pack('<II', 0, 0)
        extra = struct.pack('<IHH', len(stub), 0, 15)  # alloc_hint, ctx_id, opnum 15
        return _hdr(0, 2, extra + stub)                # ptype REQUEST=0, call_id=2

    def _read_pdu(f):
        data = b''
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            data += chunk
            if len(data) >= 10:
                frag_len = struct.unpack_from('<H', data, 8)[0]
                if len(data) >= frag_len:
                    break
        return data

    def _parse(data):
        if len(data) < 26 or data[2] != 2:   # ptype RESPONSE=2
            raise RuntimeError(f'Unexpected DCE/RPC response (ptype={data[2] if data else "?"})')
        log.debug('smb_client._parse: %d bytes hex=%s', len(data), data[24:80].hex())
        # Response header: 16 common + alloc_hint(4) + ctx_id(2) + cancel(1) + pad(1) = 24
        # Samba inlines SHARE_INFO_1_CONTAINER immediately after cont_ref
        # (no separate TotalEntries/ResumeHandle-ref before the container).
        off = 24
        _level, _disc, cont_ref = struct.unpack_from('<III', data, off); off += 12
        if not cont_ref:
            return []
        entries_read = struct.unpack_from('<I', data, off)[0]; off += 4
        buf_ref      = struct.unpack_from('<I', data, off)[0]; off += 4
        if not buf_ref or not entries_read:
            return []
        _max_count = struct.unpack_from('<I', data, off)[0]; off += 4
        # Fixed part of each SHARE_INFO_1: three DWORDs (name_ref, type, remark_ref)
        infos = []
        for _ in range(entries_read):
            name_ref, shi_type, rem_ref = struct.unpack_from('<III', data, off)
            off += 12
            infos.append((name_ref, shi_type, rem_ref))
        # Deferred: conformant varying wide-char strings
        def _wstr():
            nonlocal off
            _mx, _ofs, act = struct.unpack_from('<III', data, off); off += 12
            nbytes = act * 2
            s = data[off:off + nbytes].decode('utf-16-le', errors='replace').rstrip('\x00')
            off += nbytes + (4 - nbytes % 4) % 4
            return s
        shares = []
        for name_ref, shi_type, rem_ref in infos:
            name   = _wstr() if name_ref else ''
            if rem_ref:
                _wstr()                         # skip remark
            if name and (shi_type & 0xFF) == 0: # disk share only
                shares.append(DirEntry(name, True, 0))
        return sorted(shares, key=lambda e: e.name.lower())

    # Try the canonical \pipe\srvsvc path first (required by Samba), then bare srvsvc.
    for pipe_name in ('pipe\\srvsvc', 'srvsvc'):
        pipe_path = f'\\\\{host}\\IPC$\\{pipe_name}'
        log.debug('smb_client._list_shares: trying %s', pipe_path)
        try:
            with smbclient.open_file(pipe_path, mode='rb+', buffering=0, file_type='pipe') as pipe:
                pipe.write(_bind())
                ack = _read_pdu(pipe)
                log.debug('smb_client: bind ack %d bytes ptype=%s hex=%s',
                          len(ack), ack[2] if len(ack) > 2 else 'n/a',
                          ack[:24].hex() if ack else '')
                if not ack or ack[2] != 12:     # BIND_ACK=12
                    ptype = ack[2] if len(ack) > 2 else 'empty'
                    raise RuntimeError(f'DCE/RPC bind not acknowledged (ptype={ptype})')
                pipe.write(_net_share_enum())
                resp = _read_pdu(pipe)
                shares = _parse(resp)
            log.debug('smb_client: share listing OK for %s (%d shares)', host, len(shares))
            return shares
        except Exception as ex:
            log.debug('smb_client: %s failed: %s', pipe_path, ex)
            last_ex = ex
            continue

    raise RuntimeError(
        f'Cannot list shares on {host}: {last_ex}\n'
        f'Type a path directly, e.g.  smb://{host}/sharename'
    ) from last_ex


def _scandir(unc: str) -> list:
    """Scan a UNC path; session must already be registered."""
    import smbclient
    raw = []
    for entry in smbclient.scandir(unc):
        try:
            size = 0 if entry.is_dir() else entry.stat().st_size
        except Exception:
            size = 0
        raw.append(DirEntry(entry.name, entry.is_dir(), size))
    return raw


def _sort_entries(raw: list) -> list:
    dirs  = sorted((e for e in raw if e.is_dir),     key=lambda e: e.name.lower())
    files = sorted((e for e in raw if not e.is_dir), key=lambda e: e.name.lower())
    return dirs + files


def open_file(smb_uri: str):
    """Open a file on an SMB share for binary reading.

    Returns a file-like object.  The caller must close it.
    """
    import smbclient

    host, _share, unc = _smb_uri_to_unc(smb_uri)
    username, password = get_credentials(host)
    _register(host, username or 'guest', password or '')
    log.debug('smb_client.open_file: %s', unc)
    return smbclient.open_file(unc, mode='rb')


def parent_uri(smb_uri: str):
    """Return the smb:// URI of the parent, or None when already at network level.

    smb://host/share/sub → smb://host/share
    smb://host/share     → smb://host
    smb://host           → smb://          (network / server-list level)
    smb://               → None
    """
    parsed = urlparse(smb_uri)
    host = (parsed.hostname or '').lower()
    parts = [p for p in parsed.path.split('/') if p]
    if not host:
        return None           # already at smb:// network level
    if not parts:
        return 'smb://'       # smb://host → network level
    if len(parts) == 1:
        return f'smb://{host}'  # share → server level
    share = unquote(parts[0])
    parent_parts = parts[1:-1]
    encoded = '/'.join(quote(unquote(p), safe='') for p in parent_parts)
    base = f'smb://{host}/{share.lower()}'
    return f'{base}/{encoded}' if encoded else base


def child_uri(smb_uri: str, name: str) -> str:
    """Return smb_uri + name as a child path."""
    # 'smb://'.rstrip('/') → 'smb:' which would give 'smb:/name'; keep the double-slash.
    if smb_uri.rstrip('/') in ('smb:', ''):
        return f'smb://{quote(name, safe="")}'
    return f'{smb_uri.rstrip("/")}/{quote(name, safe="")}'


def is_available() -> bool:
    """Return True if smbprotocol is importable."""
    try:
        import smbclient  # noqa: F401
        return True
    except ImportError:
        return False
