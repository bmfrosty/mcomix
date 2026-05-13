"""recent.py - Recent files handler."""

import os
import urllib.request, urllib.parse, urllib.error
from urllib.parse import unquote
from gi.repository import Gtk, GLib, GObject, Gio, Pango
import sys

from mcomix import preferences
from mcomix import i18n
from mcomix import portability
from mcomix import archive_tools
from mcomix import image_tools
from mcomix import log

class RecentFilesMenu(Gtk.RecentChooserMenu):

    def __init__(self, ui, window):
        super(RecentFilesMenu, self).__init__()
        self._window = window
        self._manager = Gtk.RecentManager.get_default()

        self.set_sort_type(Gtk.RecentSortType.MRU)
        self.set_show_tips(True)
        self.set_show_not_found(True)
        self.set_local_only(False)
        self.set_size_request(600, -1)
        # Missing icons crash GTK on Win32
        if sys.platform == 'win32':
            self.set_show_icons(False)
            self.set_show_numbers(True)

        rfilter = Gtk.RecentFilter()
        supported_formats = {}
        supported_formats.update(image_tools.get_supported_formats())
        supported_formats.update(archive_tools.get_supported_formats())
        for name in sorted(supported_formats):
            mime_types, extensions = supported_formats[name]
            patterns = ['*.%s' % ext for ext in extensions]
            for mime in mime_types:
                rfilter.add_mime_type(mime)
            for pat in patterns:
                rfilter.add_pattern(pat)
        self.add_filter(rfilter)

        self.connect('item_activated', self._load)
        self.connect('show', self._widen_labels)

    def _widen_labels(self, *args):
        """Decode percent-encoding in labels and widen the menu items."""
        for item in self.get_children():
            label = item.get_child()
            if label and isinstance(label, Gtk.Label):
                label.set_max_width_chars(120)
                label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                text = label.get_text()
                if '%' in text:
                    label.set_text(unquote(text))

    def _load(self, *args):
        uri = self.get_current_uri()
        if uri.startswith('file://'):
            path = urllib.request.url2pathname(uri[7:])
            did_file_load = self._window.filehandler.open_file(path)
            if not did_file_load:
                self.remove(path)
        else:
            # Network URI (smb://, sftp://, …) — mount the enclosing volume
            # via GVFS first so _resolve_uri can get a local FUSE path.
            # Gtk.MountOperation shows a credential dialog if needed.
            gfile = Gio.File.new_for_uri(uri)
            mount_op = Gtk.MountOperation.new(self._window)

            def _on_mount(source, result, _user_data):
                try:
                    source.mount_enclosing_volume_finish(result)
                except Exception as ex:
                    log.debug('recent _load: mount failed (%s), trying open anyway', ex)
                did_file_load = self._window.filehandler.open_file(uri)
                if not did_file_load:
                    self.remove(uri)

            gfile.mount_enclosing_volume(
                Gio.MountMountFlags.NONE, mount_op, None, _on_mount, None)

    def count(self):
        """ Returns the amount of stored entries. """
        return len(self._manager.get_items())

    def add(self, path_or_uri):
        if not preferences.prefs['store recent file info']:
            return
        if '://' in path_or_uri and not path_or_uri.startswith('file://'):
            uri = path_or_uri
        else:
            uri = portability.uri_prefix() + urllib.request.pathname2url(path_or_uri)
        self._manager.add_item(uri)

    def remove(self, path_or_uri):
        if not preferences.prefs['store recent file info']:
            return
        if '://' in path_or_uri and not path_or_uri.startswith('file://'):
            uri = path_or_uri
        else:
            uri = portability.uri_prefix() + urllib.request.pathname2url(path_or_uri)
        try:
            self._manager.remove_item(uri)
        except GLib.GError:
            # Could not remove item
            pass

    def remove_all(self):
        """ Removes all entries to recently opened files. """
        try:
            self._manager.purge_items()
        except GObject.GError as error:
            log.debug(error)


class RecentPathsMenu(Gtk.Menu):
    """Menu listing unique parent directories from recently opened files.

    Clicking an entry opens the file chooser pre-navigated to that directory.
    """

    def __init__(self, window):
        super().__init__()
        self._window = window
        self._manager = Gtk.RecentManager.get_default()
        self.connect('show', self._rebuild)

    def _rebuild(self, *args):
        for child in self.get_children():
            self.remove(child)

        seen = set()
        folder_uris = []
        for item in self._manager.get_items():
            uri = item.get_uri()
            if uri.startswith('file://'):
                path = urllib.request.url2pathname(uri[7:])
                parent = os.path.dirname(path)
                folder_uri = 'file://' + urllib.request.pathname2url(parent)
            else:
                gfile = Gio.File.new_for_uri(uri)
                parent = gfile.get_parent()
                folder_uri = parent.get_uri() if parent else None
            if not folder_uri or folder_uri in seen:
                continue
            seen.add(folder_uri)
            folder_uris.append(folder_uri)

        if not folder_uris:
            placeholder = Gtk.MenuItem(label=i18n._('(No recent paths)'))
            placeholder.set_sensitive(False)
            self.append(placeholder)
            self.show_all()
            return

        for furi in folder_uris[:30]:
            display = unquote(furi)
            if display.startswith('file://'):
                display = display[7:]
            mi = Gtk.MenuItem()
            lbl = Gtk.Label(label=display)
            lbl.set_max_width_chars(120)
            lbl.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            lbl.set_halign(Gtk.Align.START)
            mi.add(lbl)
            mi.connect('activate', self._open_at, furi)
            self.append(mi)
        self.show_all()

    def _open_at(self, item, folder_uri):
        if folder_uri.startswith('smb://'):
            from mcomix import smb_browser_dialog
            smb_browser_dialog.open_smb_dialog(self._window, start_uri=folder_uri)
        else:
            from mcomix import file_chooser_main_dialog
            file_chooser_main_dialog.open_main_filechooser_dialog_at(folder_uri, self._window)


# vim: expandtab:sw=4:ts=4
