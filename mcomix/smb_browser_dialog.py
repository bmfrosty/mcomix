"""smb_browser_dialog.py — GTK file browser for SMB shares.

Entry point:
    open_smb_dialog(gtk_window, start_uri=None) -> str | None

Returns the selected smb:// URI, or None if the user cancelled.
"""

import json
import os
import threading
from typing import NamedTuple
from urllib.parse import unquote

from gi.repository import GLib, Gtk, Pango

from mcomix import constants, log, smb_client

_OPENABLE_EXTS = frozenset([
    '.cbz', '.cbr', '.cbt', '.cb7', '.cba',
    '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.lzma', '.xz',
    '.pdf',
    '.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.tiff', '.tif',
    '.bmp',
])

_AUTH_KEYWORDS = ('logon', 'authentication', 'credential', 'password',
                  'ntlm', 'spnego', 'access', 'unauthor')

_last_dir_uri = None

_BOOKMARKS_PATH = os.path.join(constants.CONFIG_DIR, 'smb_bookmarks.json')

_BATCH_SIZE = 500
_SIBLINGS_WINDOW = 100  # how many files before/after the selection to include


class SmbPickResult(NamedTuple):
    """Returned by open_smb_dialog on success."""
    uri: str         # the selected smb:// file URI
    smb_context: dict  # parent_uri, siblings (±100 list), complete (bool)


def _is_auth_error(ex):
    return any(k in str(ex).lower() for k in _AUTH_KEYWORDS)


def _bookmark_label(uri):
    """Short display label for a bookmark URI."""
    display = unquote(uri)
    if display.startswith('smb://'):
        display = display[6:]
    return display.rstrip('/') or 'smb://'


def _load_bookmarks():
    try:
        with open(_BOOKMARKS_PATH) as f:
            data = json.load(f)
            if isinstance(data, list):
                return [str(u) for u in data if u]
    except Exception:
        pass
    return []


def _save_bookmarks(bookmarks):
    try:
        os.makedirs(os.path.dirname(_BOOKMARKS_PATH), exist_ok=True)
        with open(_BOOKMARKS_PATH, 'w') as f:
            json.dump(bookmarks, f, indent=2)
    except Exception as ex:
        log.debug('smb_browser: save_bookmarks: %s', ex)


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
        self._bookmarks = _load_bookmarks()
        # Batch-insert state
        self._pending_entries = None
        self._pending_uri = None
        self._pending_offset = 0
        self._pending_n_dirs = 0
        self._pending_n_files = 0
        self.connect('destroy', lambda *_: setattr(self, '_alive', False))

        self.set_default_size(940, 580)
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

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_margin_start(6)
        paned.set_margin_end(6)
        paned.set_margin_top(6)
        paned.set_margin_bottom(2)
        paned.set_position(200)

        # ── Left: bookmarks panel ──────────────────────────────────────
        bm_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        bm_label = Gtk.Label(label='Bookmarks', halign=Gtk.Align.START)
        bm_outer.pack_start(bm_label, False, False, 0)

        bm_scroll = Gtk.ScrolledWindow()
        bm_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        bm_scroll.set_min_content_width(180)
        bm_scroll.set_shadow_type(Gtk.ShadowType.IN)

        self._bm_listbox = Gtk.ListBox()
        self._bm_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._bm_listbox.connect('row-activated', self._on_bookmark_activated)
        bm_scroll.add(self._bm_listbox)
        bm_outer.pack_start(bm_scroll, True, True, 0)

        bm_btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        add_bm_btn = Gtk.Button(label='+ Add')
        add_bm_btn.set_tooltip_text('Bookmark current folder')
        add_bm_btn.connect('clicked', self._add_bookmark)
        rem_bm_btn = Gtk.Button(label='− Remove')
        rem_bm_btn.set_tooltip_text('Remove selected bookmark')
        rem_bm_btn.connect('clicked', self._remove_bookmark)
        bm_btn_box.pack_start(add_bm_btn, True, True, 0)
        bm_btn_box.pack_start(rem_bm_btn, True, True, 0)
        bm_outer.pack_start(bm_btn_box, False, False, 0)

        paned.pack1(bm_outer, resize=False, shrink=False)

        # ── Right: address bar + filter + file list + status ───────────
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Address bar
        addr_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        addr_box.set_margin_start(4)
        addr_box.set_margin_bottom(2)

        addr_box.pack_start(Gtk.Label(label='Location:'), False, False, 0)

        self._addr_entry = Gtk.Entry(text=self._current_uri)
        self._addr_entry.connect('activate', lambda _e: self._go_address())
        addr_box.pack_start(self._addr_entry, True, True, 6)

        # pack_end anchors these to the right edge so long URIs in the entry
        # never push the buttons off screen.
        up_btn = Gtk.Button(label='↑ Up')
        up_btn.connect('clicked', lambda _b: self._go_parent())
        addr_box.pack_end(up_btn, False, False, 0)

        go_btn = Gtk.Button(label='Go')
        go_btn.connect('clicked', lambda _b: self._go_address())
        addr_box.pack_end(go_btn, False, False, 4)

        right_box.pack_start(addr_box, False, False, 0)

        # Filter bar
        filter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        filter_box.set_margin_start(4)
        filter_box.set_margin_top(2)
        filter_box.set_margin_bottom(2)

        filter_box.pack_start(Gtk.Label(label='Filter:'), False, False, 0)

        self._filter_entry = Gtk.SearchEntry()
        self._filter_entry.set_placeholder_text('Filter files and folders…')
        self._filter_entry.connect('search-changed', self._on_filter_changed)
        filter_box.pack_start(self._filter_entry, True, True, 4)

        right_box.pack_start(filter_box, False, False, 0)

        # File listing
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.ALWAYS)
        scroll.set_shadow_type(Gtk.ShadowType.IN)
        scroll.set_margin_top(2)
        scroll.set_margin_bottom(2)

        # columns: icon, name, size_str, is_dir
        self._store = Gtk.ListStore(str, str, str, bool)
        self._filter_model = self._store.filter_new()
        self._filter_model.set_visible_func(self._row_visible)

        self._tree = Gtk.TreeView(model=self._filter_model)
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
        right_box.pack_start(scroll, True, True, 0)

        # Status bar
        self._status = Gtk.Label(label='Enter an smb:// address and press Go.',
                                 halign=Gtk.Align.START, xalign=0.0)
        self._status.set_margin_start(4)
        self._status.set_margin_top(2)
        self._status.set_margin_bottom(4)
        right_box.pack_start(self._status, False, False, 0)

        paned.pack2(right_box, resize=True, shrink=True)

        content.pack_start(paned, True, True, 0)
        content.show_all()

        self._rebuild_bookmarks_panel()

    # ------------------------------------------------------------------
    # Bookmarks
    # ------------------------------------------------------------------

    def _rebuild_bookmarks_panel(self):
        for child in list(self._bm_listbox.get_children()):
            self._bm_listbox.remove(child)
        for uri in self._bookmarks:
            lbl = Gtk.Label(label=_bookmark_label(uri),
                            halign=Gtk.Align.START, xalign=0.0)
            lbl.set_max_width_chars(24)
            lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            lbl.set_tooltip_text(uri)
            row = Gtk.ListBoxRow()
            row.add(lbl)
            self._bm_listbox.add(row)
        self._bm_listbox.show_all()

    def _on_bookmark_activated(self, _listbox, row):
        idx = row.get_index()
        if 0 <= idx < len(self._bookmarks):
            self._navigate(self._bookmarks[idx])

    def _add_bookmark(self, *args):
        uri = self._current_uri
        if not uri or not uri.startswith('smb://'):
            return
        if uri not in self._bookmarks:
            self._bookmarks.append(uri)
            _save_bookmarks(self._bookmarks)
            self._rebuild_bookmarks_panel()

    def _remove_bookmark(self, *args):
        row = self._bm_listbox.get_selected_row()
        if row is None:
            return
        idx = row.get_index()
        if 0 <= idx < len(self._bookmarks):
            del self._bookmarks[idx]
            _save_bookmarks(self._bookmarks)
            self._rebuild_bookmarks_panel()

    # ------------------------------------------------------------------
    # Filter
    # ------------------------------------------------------------------

    def _row_visible(self, model, it, _data=None):
        text = self._filter_entry.get_text().lower()
        if not text:
            return True
        return text in model.get_value(it, 1).lower()

    def _on_filter_changed(self, _entry):
        self._filter_model.refilter()

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
        self._tree.set_model(None)
        self._store.clear()
        self._open_btn.set_sensitive(False)
        self._filter_entry.set_text('')

        def _worker():
            def _on_progress(count):
                GLib.idle_add(self._update_fetch_status, uri, count)
            try:
                entries = smb_client.list_directory(uri, progress_cb=_on_progress)
                GLib.idle_add(self._begin_populate, uri, entries)
            except Exception as ex:
                GLib.idle_add(self._populate_error, uri, ex)

        threading.Thread(target=_worker, daemon=True).start()

    def _update_fetch_status(self, uri, count):
        if not self._alive:
            return False
        self._status.set_text(f'Loading {uri}… {count:,} entries so far')
        return False

    def _populate_error(self, uri, error):
        if not self._alive:
            return False
        self._loading = False
        self._tree.set_model(self._filter_model)
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

    def _begin_populate(self, uri, entries):
        if not self._alive:
            return False
        global _last_dir_uri
        _last_dir_uri = uri
        self._current_uri = uri
        self._addr_entry.set_text(uri)

        self._pending_entries = entries
        self._pending_uri = uri
        self._pending_offset = 0
        self._pending_n_dirs = 0
        self._pending_n_files = 0

        n = len(entries)
        if n > _BATCH_SIZE:
            self._status.set_text(f'Populating… 0 / {n}')
        GLib.idle_add(self._populate_batch)
        return False

    def _populate_batch(self):
        if not self._alive:
            return False

        entries = self._pending_entries
        offset  = self._pending_offset
        end     = min(offset + _BATCH_SIZE, len(entries))

        for i in range(offset, end):
            entry = entries[i]
            if entry.is_dir:
                self._store.append(['📁', entry.name, '', True])
                self._pending_n_dirs += 1
            else:
                ext = os.path.splitext(entry.name)[1].lower()
                if ext not in _OPENABLE_EXTS:
                    continue
                self._store.append(['📄', entry.name, _fmt_size(entry.size), False])
                self._pending_n_files += 1

        self._pending_offset = end

        if end < len(entries):
            self._status.set_text(
                f'Populating… {end} / {len(entries)}')
            return True  # schedule next batch

        # All batches done — reattach model and update status
        self._tree.set_model(self._filter_model)
        uri = self._pending_uri
        nd, nf = self._pending_n_dirs, self._pending_n_files
        self._status.set_text(f'{uri}   —   {nd} folder(s), {nf} file(s)')
        self._pending_entries = None
        self._loading = False
        return False

    # ------------------------------------------------------------------
    # Selection / activation
    # ------------------------------------------------------------------

    def _on_selection_changed(self, selection):
        model, it = selection.get_selected()
        is_file = (it is not None) and not model.get_value(it, 3)
        self._open_btn.set_sensitive(is_file)

    def _on_row_activated(self, _tree, path, _col):
        it = self._filter_model.get_iter(path)
        is_dir = self._filter_model.get_value(it, 3)
        name   = self._filter_model.get_value(it, 1)
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

        selected = self._result
        self.destroy()
        if not selected:
            return None
        return SmbPickResult(uri=selected,
                             smb_context=self._build_smb_context(selected))

    def _build_smb_context(self, selected_uri):
        """Collect ±100 openable file URIs around selected_uri from the loaded store."""
        all_files = []
        it = self._store.get_iter_first()
        while it:
            if not self._store.get_value(it, 3):  # not a directory
                name = self._store.get_value(it, 1)
                all_files.append(smb_client.child_uri(self._current_uri, name))
            it = self._store.iter_next(it)

        try:
            idx = all_files.index(selected_uri)
        except ValueError:
            idx = 0

        start = max(0, idx - _SIBLINGS_WINDOW)
        end   = min(len(all_files), idx + _SIBLINGS_WINDOW + 1)
        return {
            'parent_uri': self._current_uri,
            'siblings':   all_files[start:end],
            'complete':   start == 0 and end == len(all_files),
        }


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
