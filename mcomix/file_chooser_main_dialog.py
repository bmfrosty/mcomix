"""file_chooser_main_dialog.py - Custom FileChooserDialog implementations."""

import os
from gi.repository import Gtk, Gio

from mcomix.preferences import prefs
from mcomix import archive_tools
from mcomix import image_tools
from mcomix import constants
from mcomix import log
from mcomix.i18n import _

_main_filechooser_dialog = None


class _MainFileChooserDialog:
    """Main file chooser using Gtk.FileChooserNative.

    On GNOME desktops with xdg-desktop-portal this delegates to the platform
    file chooser, which fully supports network/GVFS locations (SMB, SFTP,
    etc.).  On other desktops it falls back to the regular GTK file-chooser
    dialog, keeping behaviour identical to the old implementation.

    Because the native dialog does not support custom-callback filters, all
    filters are built with add_mime_type() / add_pattern() only.
    """

    _last_activated_file = None

    def __init__(self, window, start_folder=None):
        self._window = window
        self._start_folder = start_folder
        self._native = Gtk.FileChooserNative.new(
            _('Open'),
            window,
            Gtk.FileChooserAction.OPEN,
            None,
            None,
        )
        self._native.set_select_multiple(True)
        self._build_filters()
        self._restore_folder()
        self._native.connect('response', self._response)
        self._native.show()

    # ------------------------------------------------------------------
    # Filter construction
    # ------------------------------------------------------------------

    def _build_filters(self):
        # Filter order mirrors the old _BaseFileChooserDialog layout so that the
        # 'last filter in main filechooser' preference index stays compatible:
        # 0: All files, 1: All archives, 2+: individual archive formats,
        # then All images followed by individual image formats.
        #
        # add_custom() callback filters are not supported by the native/portal
        # dialog, so every filter is built with add_mime_type() / add_pattern().

        # "All files" — index 0
        all_files = Gtk.FileFilter()
        all_files.set_name(_('All files'))
        all_files.add_pattern('*')
        self._native.add_filter(all_files)

        # "All archives" — index 1, then individual archive formats
        all_archives = Gtk.FileFilter()
        all_archives.set_name(_('All archives'))
        self._native.add_filter(all_archives)
        for name in sorted(archive_tools.get_supported_formats()):
            mime_types, extensions = archive_tools.get_supported_formats()[name]
            fmt = Gtk.FileFilter()
            fmt.set_name(_('%s archives') % name)
            for mime in mime_types:
                fmt.add_mime_type(mime)
                all_archives.add_mime_type(mime)  # keep "All archives" in sync
            for ext in extensions:
                pat = '*.%s' % ext
                fmt.add_pattern(pat)
                all_archives.add_pattern(pat)
            self._native.add_filter(fmt)

        # "All images", then individual image formats
        all_images = Gtk.FileFilter()
        all_images.set_name(_('All images'))
        self._native.add_filter(all_images)
        for name in sorted(image_tools.get_supported_formats()):
            mime_types, extensions = image_tools.get_supported_formats()[name]
            fmt = Gtk.FileFilter()
            fmt.set_name(_('%s images') % name)
            for mime in mime_types:
                fmt.add_mime_type(mime)
                all_images.add_mime_type(mime)  # keep "All images" in sync
            for ext in extensions:
                pat = '*.%s' % ext
                fmt.add_pattern(pat)
                all_images.add_pattern(pat)
            self._native.add_filter(fmt)

        try:
            self._native.set_filter(
                self._native.list_filters()[prefs['last filter in main filechooser']])
        except Exception:
            self._native.set_filter(self._native.list_filters()[0])

    # ------------------------------------------------------------------
    # Folder restoration
    # ------------------------------------------------------------------

    def _restore_folder(self):
        if self._start_folder:
            try:
                if '://' in self._start_folder and not self._start_folder.startswith('file://'):
                    self._native.set_current_folder_uri(self._start_folder)
                else:
                    gfile = Gio.File.new_for_uri(self._start_folder)
                    local = gfile.get_path()
                    if local:
                        self._native.set_current_folder(local)
                    else:
                        self._native.set_current_folder_uri(self._start_folder)
            except Exception as ex:
                log.debug('_restore_folder start_folder: %s', ex)
            return
        try:
            from mcomix import main
            current_file = main.main_window().filehandler.get_path_to_base()
            last_file = _MainFileChooserDialog._last_activated_file

            if current_file and os.path.exists(current_file):
                self._native.set_current_folder(os.path.dirname(current_file))
            # last_file may be a non-local URI if the user previously opened a
            # network file; set_current_folder_uri() accepts both local and remote URIs.
            elif last_file:
                if '://' in last_file:
                    gfile = Gio.File.new_for_uri(last_file)
                    parent = gfile.get_parent()
                    if parent:
                        self._native.set_current_folder_uri(parent.get_uri())
                elif os.path.exists(last_file):
                    self._native.set_filename(last_file)
            # The stored pref may also be a non-local URI (written by _response
            # when the user last browsed a network location).
            else:
                last_browsed = prefs['path of last browsed in filechooser']
                if '://' in last_browsed:
                    self._native.set_current_folder_uri(last_browsed)
                elif os.path.isdir(last_browsed):
                    if prefs['store recent file info']:
                        self._native.set_current_folder(last_browsed)
                    else:
                        self._native.set_current_folder(constants.HOME_DIR)
        except Exception as ex:
            log.debug(ex)

    # ------------------------------------------------------------------
    # Response handling
    # ------------------------------------------------------------------

    def _response(self, dialog, response):
        # FileChooserNative uses ACCEPT/CANCEL, not OK/CANCEL like FileChooserDialog.
        if response == Gtk.ResponseType.ACCEPT:
            uris = self._native.get_uris()
            if not uris:
                _close_main_filechooser_dialog()
                return

            # Persist active filter index
            try:
                idx = self._native.list_filters().index(self._native.get_filter())
                prefs['last filter in main filechooser'] = idx
            except Exception:
                pass

            # Fetch the folder URI unconditionally — needed for "last browsed"
            # pref and for the folder-document-portal sibling navigation below.
            folder_uri = self._native.get_current_folder_uri() or ''
            folder_gfile = Gio.File.new_for_uri(folder_uri)
            folder_local = folder_gfile.get_path()
            log.debug('file chooser response: folder_uri=%s folder_local=%s',
                      folder_uri, folder_local)

            # Store the raw URI when the folder has no POSIX path so that
            # _restore_folder() can navigate back to a network location next time.
            if prefs['store recent file info']:
                prefs['path of last browsed in filechooser'] = \
                    folder_local if folder_local else folder_uri
            else:
                prefs['path of last browsed in filechooser'] = constants.HOME_DIR

            # Build the list of paths/URIs to open.
            #
            # When the portal file chooser is used (Flatpak/portal sandbox), both
            # the chosen file and its parent folder are surfaced as document-portal
            # FUSE paths (/run/user/*/doc/<id>/…).  A single-file document gives
            # access only to that one file, so the local file provider cannot list
            # siblings for next/previous archive navigation.
            #
            # The FOLDER document (folder_local = /run/user/*/doc/<folder-id>)
            # grants access to ALL files in the directory.  By constructing the
            # path as folder_local/basename we give file_handler a path whose
            # parent directory IS enumerable, enabling sibling navigation.
            #
            # For plain local files folder_local won't be a doc-portal path, so
            # we fall through to the normal local-path behaviour.
            is_folder_doc_portal = (folder_local and
                                    '/run/user/' in folder_local and
                                    '/doc/' in folder_local)
            is_network_folder = (folder_uri and '://' in folder_uri and
                                 not folder_uri.startswith('file://'))
            log.debug('file chooser response: is_folder_doc_portal=%s is_network_folder=%s',
                      is_folder_doc_portal, is_network_folder)

            paths = []
            for uri in uris:
                gfile = Gio.File.new_for_uri(uri)
                local = gfile.get_path()
                log.debug('file chooser response: uri=%s local=%s', uri, local)
                if local and is_network_folder:
                    # Direct network URI (non-portal case).
                    net_uri = folder_gfile.get_child(gfile.get_basename()).get_uri()
                    log.debug('file chooser response: network URI=%s', net_uri)
                    paths.append(net_uri)
                else:
                    paths.append(local if local else uri)

            first_gfile = Gio.File.new_for_uri(uris[0])
            _MainFileChooserDialog._last_activated_file = \
                first_gfile.get_path() or uris[0]

            _close_main_filechooser_dialog()

            # Pass the folder document URI as a hint so file_handler can
            # enumerate sibling archives for next/prev navigation.
            # Only set when the folder is a portal document (no direct SMB
            # path available); for plain local folders _source_uri handles it.
            folder_hint = folder_uri if is_folder_doc_portal else None
            log.debug('file chooser response: folder_hint=%s', folder_hint)

            files = paths if len(paths) > 1 else paths[0]
            self._window.filehandler.open_file(files, folder_hint=folder_hint)
        else:
            _close_main_filechooser_dialog()

    # ------------------------------------------------------------------
    # Public interface expected by open_main_filechooser_dialog
    # ------------------------------------------------------------------

    def present(self):
        self._native.show()

    def destroy(self):
        self._native.destroy()


def open_main_filechooser_dialog(action, window):
    """Open the main filechooser dialog."""
    global _main_filechooser_dialog
    if _main_filechooser_dialog is None:
        _main_filechooser_dialog = _MainFileChooserDialog(window)
    else:
        _main_filechooser_dialog.present()


def open_main_filechooser_dialog_at(folder_uri, window):
    """Open the main filechooser dialog pre-navigated to folder_uri."""
    global _main_filechooser_dialog
    if _main_filechooser_dialog is not None:
        _main_filechooser_dialog.destroy()
        _main_filechooser_dialog = None
    _main_filechooser_dialog = _MainFileChooserDialog(window, start_folder=folder_uri)


def _close_main_filechooser_dialog(*args):
    """Close the main filechooser dialog."""
    global _main_filechooser_dialog
    if _main_filechooser_dialog is not None:
        _main_filechooser_dialog.destroy()
        _main_filechooser_dialog = None

# vim: expandtab:sw=4:ts=4
