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

    def __init__(self, window):
        self._window = window
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

            # Fetch the folder URI unconditionally — we need it both for the
            # "last browsed" pref and to reconstruct network URIs below.
            folder_uri = self._native.get_current_folder_uri() or ''
            folder_gfile = Gio.File.new_for_uri(folder_uri)
            log.debug('file chooser response: folder_uri=%s', folder_uri)

            # Store the raw URI when the folder has no POSIX path so that
            # _restore_folder() can navigate back to a network location next time.
            if prefs['store recent file info']:
                local_folder = folder_gfile.get_path()
                prefs['path of last browsed in filechooser'] = \
                    local_folder if local_folder else folder_uri
            else:
                prefs['path of last browsed in filechooser'] = constants.HOME_DIR

            # Build the list of paths/URIs to open.
            #
            # The portal file chooser hands us back document-portal URIs
            # (file:///run/user/*/doc/<id>/filename) rather than the original
            # network URIs.  The document portal exposes only the single chosen
            # file, so the local file provider cannot list siblings for
            # next/previous archive navigation.
            #
            # When the folder URI is a genuine network URI (smb://, ftp://, …)
            # we reconstruct the original network URI for each chosen file so
            # that file_handler._resolve_uri() receives the real smb:// URI and
            # can set _source_uri, enabling GIO-based sibling enumeration.
            #
            # For ordinary local files we keep the local POSIX path as before.
            is_network_folder = folder_uri and '://' in folder_uri and \
                not folder_uri.startswith('file://')
            log.debug('file chooser response: is_network_folder=%s', is_network_folder)

            paths = []
            for uri in uris:
                gfile = Gio.File.new_for_uri(uri)
                local = gfile.get_path()
                log.debug('file chooser response: uri=%s local=%s', uri, local)
                if local and is_network_folder:
                    # Reconstruct the network URI from the folder URI + basename.
                    net_uri = folder_gfile.get_child(gfile.get_basename()).get_uri()
                    log.debug('file chooser response: using network URI=%s', net_uri)
                    paths.append(net_uri)
                else:
                    paths.append(local if local else uri)

            first_gfile = Gio.File.new_for_uri(uris[0])
            _MainFileChooserDialog._last_activated_file = \
                first_gfile.get_path() or uris[0]

            _close_main_filechooser_dialog()

            files = paths if len(paths) > 1 else paths[0]
            self._window.filehandler.open_file(files)
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


def _close_main_filechooser_dialog(*args):
    """Close the main filechooser dialog."""
    global _main_filechooser_dialog
    if _main_filechooser_dialog is not None:
        _main_filechooser_dialog.destroy()
        _main_filechooser_dialog = None

# vim: expandtab:sw=4:ts=4
