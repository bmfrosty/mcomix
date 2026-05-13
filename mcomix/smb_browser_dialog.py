"""smb_browser_dialog.py — GTK file browser for SMB shares.

Entry point:
    open_smb_dialog(gtk_window, start_uri=None) -> str | None

Returns the selected smb:// URI, or None if the user cancelled.
"""

import os
import threading

from gi.repository import GLib, Gtk

from mcomix import log
from mcomix import smb_client

_OPENABLE_EXTS = frozenset([
    '.cbz', '.cbr', '.cbt', '.cb7', '.cba',
    '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.lzma', '.xz',
    '.pdf',
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.tiff', '.tif',
    '.bmp',
])

_AUTH_KEYWORDS = ('logon', 'authentication', 'credential', 'password',
                  'ntlm', 'spnego', 'access', 'unauthor')


_last_dir_uri = None  # session-level memory of the last navigated SMB directory


def _is_auth_error(ex):
    return any(k in str(ex).lower() for k in _AUTH_KEYWORDS)


def _default_smb_uri():
    """Return the best default SMB directory URI to pre-populate the dialog.

    Prefers the session-level last-navigated directory, then falls back to
    the parent of the most recently opened SMB file in GTK RecentManager.
    """
    if _last_dir_uri:
        return _last_dir_uri
    try:
        manager = Gtk.RecentManager.get_default()
        smb_items = [i for i in manager.get_items()
                     if i.get_uri().startswith('smb://') and not i.get_private_hint()]
        if smb_items:
            smb_items.sort(key=lambda i: -i.get_modified())
            return smb_client.parent_uri(smb_items[0].get_uri())
    except Exception:
        pass
    return 'smb://'


def _fmt_size(n):
    for unit, threshold in (('GB', 1 << 30), ('MB', 1 << 20), ('KB', 1 << 10)):
        if n >= threshold:
            return f'{n / threshold:.1f} {unit}'
    return f'{n} B'


# ---------------------------------------------------------------------------
# Credential dialog
# ---------------------------------------------------------------------------

def _ask_credentials(parent, host, existing_user=None):
    """GTK modal credential dialog. Returns (username, password) or None."""
    dlg = Gtk.Dialog(
        title=f'Connect to {host}',
        transient_for=parent,
        modal=True,
        destroy_with_parent=True,
    )
    dlg.add_buttons('_Cancel', Gtk.ResponseType.CANCEL,
                    '_Connect', Gtk.ResponseType.ACCEPT)
    dlg.set_default_response(Gtk.ResponseType.ACCEPT)

    grid = Gtk.Grid(row_spacing=8, column_spacing=8)
    grid.set_margin_start(12)
    grid.set_margin_end(12)
    grid.set_margin_top(12)
    grid.set_margin_bottom(12)

    grid.attach(Gtk.Label(label='Username:', halign=Gtk.Align.END), 0, 0, 1, 1)
    grid.attach(Gtk.Label(label='Password:',  halign=Gtk.Align.END), 0, 1, 1, 1)

    user_entry = Gtk.Entry(text=existing_user or '', width_chars=28,
                           activates_default=True)
    pass_entry = Gtk.Entry(visibility=False, width_chars=28,
                           activates_default=True)
    grid.attach(user_entry, 1, 0, 1, 1)
    grid.attach(pass_entry, 1, 1, 1, 1)

    dlg.get_content_area().pack_start(grid, True, True, 0)
    dlg.show_all()

    (pass_entry if existing_user else user_entry).grab_focus()

    response = dlg.run()
    result = (user_entry.get_text(), pass_entry.get_text()) \
        if response == Gtk.ResponseType.ACCEPT else None
    dlg.destroy()
    return result


# ---------------------------------------------------------------------------
# Main browser dialog
# ---------------------------------------------------------------------------

class _SmbBrowserDialog(Gtk.Dialog):

    def __init__(self, parent=None, start_uri=None):
        super().__init__(
            title='Open from Network',
            transient_for=parent,
            modal=True,
            destroy_with_parent=True,
        )
        self._result = None
        self._current_uri = (start_uri or '').rstrip('/')
        self._loading = False
        self._alive = True
        self.connect('destroy', lambda *_: setattr(self, '_alive', False))

        self.set_default_size(760, 520)
        self.add_buttons('_Cancel', Gtk.ResponseType.CANCEL,
                         '_Open',   Gtk.ResponseType.ACCEPT)
        self._open_btn = self.get_widget_for_response(Gtk.ResponseType.ACCEPT)
        self._open_btn.set_sensitive(False)

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        content = self.get_content_area()
        content.set_spacing(0)

        # ── Address bar ────────────────────────────────────────────────
        addr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        addr_box.set_margin_start(6)
        addr_box.set_margin_end(6)
        addr_box.set_margin_top(6)
        addr_box.set_margin_bottom(2)

        addr_box.pack_start(Gtk.Label(label='Location:'), False, False, 0)

        self._addr_entry = Gtk.Entry(text=self._current_uri)
        self._addr_entry.connect('activate', lambda _e: self._go_address())
        addr_box.pack_start(self._addr_entry, True, True, 6)

        go_btn = Gtk.Button(label='Go')
        go_btn.connect('clicked', lambda _b: self._go_address())
        addr_box.pack_start(go_btn, False, False, 0)

        up_btn = Gtk.Button(label='↑ Up')
        up_btn.connect('clicked', lambda _b: self._go_parent())
        addr_box.pack_start(up_btn, False, False, 4)

        content.pack_start(addr_box, False, False, 0)

        # ── File listing ───────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.ALWAYS)
        scroll.set_margin_start(6)
        scroll.set_margin_end(6)
        scroll.set_margin_top(2)
        scroll.set_margin_bottom(2)

        # columns: icon, name, size_str, is_dir
        self._store = Gtk.ListStore(str, str, str, bool)
        self._tree = Gtk.TreeView(model=self._store)
        self._tree.set_headers_visible(True)

        icon_col = Gtk.TreeViewColumn('', Gtk.CellRendererText(), text=0)
        icon_col.set_min_width(32)
        self._tree.append_column(icon_col)

        name_col = Gtk.TreeViewColumn('Name', Gtk.CellRendererText(), text=1)
        name_col.set_expand(True)
        self._tree.append_column(name_col)

        size_col = Gtk.TreeViewColumn('Size', Gtk.CellRendererText(), text=2)
        size_col.set_min_width(80)
        self._tree.append_column(size_col)

        self._tree.connect('row-activated', self._on_row_activated)
        self._tree.get_selection().connect('changed', self._on_selection_changed)

        scroll.add(self._tree)
        content.pack_start(scroll, True, True, 0)

        # ── Status bar ─────────────────────────────────────────────────
        self._status = Gtk.Label(label='Enter an smb:// address and press Go.',
                                 halign=Gtk.Align.START, xalign=0.0)
        self._status.set_margin_start(6)
        self._status.set_margin_end(6)
        self._status.set_margin_top(2)
        self._status.set_margin_bottom(4)
        content.pack_start(self._status, False, False, 0)

        content.show_all()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _go_address(self):
        uri = self._addr_entry.get_text().strip()
        if not uri:
            return
        if not uri.startswith('smb://'):
            uri = 'smb://' + uri
        self._navigate(uri.rstrip('/'))

    def _go_parent(self):
        p = smb_client.parent_uri(self._current_uri)
        if p:
            self._navigate(p)

    def _navigate(self, uri):
        if not uri or self._loading:
            return
        self._loading = True
        self._status.set_text(f'Loading {uri} …')
        self._store.clear()
        self._open_btn.set_sensitive(False)

        def _worker():
            try:
                entries = smb_client.list_directory(uri)
                GLib.idle_add(self._populate, uri, entries, None)
            except Exception as ex:
                GLib.idle_add(self._populate, uri, None, ex)

        threading.Thread(target=_worker, daemon=True).start()

    def _populate(self, uri, entries, error):
        if not self._alive:
            return False
        self._loading = False

        if error is not None:
            log.debug('smb_browser: list_directory %s: %s', uri, error)
            if _is_auth_error(error):
                from urllib.parse import urlparse
                host = urlparse(uri).hostname or uri
                existing, _ = smb_client.get_credentials(host)
                creds = _ask_credentials(self, host, existing)
                if creds:
                    smb_client.set_credentials(host, *creds)
                    self._navigate(uri)
                else:
                    self._status.set_text('Authentication cancelled.')
            else:
                self._status.set_text(f'Error: {error}')
            return False

        global _last_dir_uri
        _last_dir_uri = uri
        self._current_uri = uri
        self._addr_entry.set_text(uri)
        self._store.clear()

        n_dirs = n_files = 0
        for entry in entries:
            if entry.is_dir:
                self._store.append(['📁', entry.name, '', True])
                n_dirs += 1
            else:
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in _OPENABLE_EXTS:
                    continue
                self._store.append(['📄', entry.name, _fmt_size(entry.size), False])
                n_files += 1

        self._status.set_text(f'{uri}   —   {n_dirs} folder(s), {n_files} file(s)')
        return False

    # ------------------------------------------------------------------
    # Selection / activation
    # ------------------------------------------------------------------

    def _on_selection_changed(self, selection):
        model, it = selection.get_selected()
        is_file = (it is not None) and not model.get_value(it, 3)
        self._open_btn.set_sensitive(is_file)

    def _on_row_activated(self, _tree, path, _col):
        it = self._store.get_iter(path)
        is_dir = self._store.get_value(it, 3)
        name   = self._store.get_value(it, 1)
        target = smb_client.child_uri(self._current_uri, name)
        if is_dir:
            self._navigate(target)
        else:
            self._result = target
            self.response(Gtk.ResponseType.ACCEPT)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run_dialog(self):
        if self._current_uri:
            self._navigate(self._current_uri)
        response = self.run()
        if response == Gtk.ResponseType.ACCEPT and self._result is None:
            model, it = self._tree.get_selection().get_selected()
            if it and not model.get_value(it, 3):
                name = model.get_value(it, 1)
                self._result = smb_client.child_uri(self._current_uri, name)
        self.destroy()
        return self._result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def open_smb_dialog(gtk_window, start_uri=None):
    """Open the SMB browser dialog (blocks until closed).

    Returns the selected smb:// URI string, or None if cancelled.
    Shows a GTK error dialog if smbprotocol is not installed.
    """
    if not smb_client.is_available():
        d = Gtk.MessageDialog(
            transient_for=gtk_window,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text='smbprotocol is not installed.',
        )
        d.format_secondary_text(
            'Install with:\n  pip3 install --user smbprotocol')
        d.run()
        d.destroy()
        return None

    try:
        dialog = _SmbBrowserDialog(parent=gtk_window,
                                   start_uri=start_uri or _default_smb_uri())
        return dialog.run_dialog()
    except Exception as ex:
        log.error('smb_browser_dialog: %s', ex)
        return None
