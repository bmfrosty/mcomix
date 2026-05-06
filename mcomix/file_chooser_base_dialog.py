"""filechooser_chooser_base_dialbg.py - Custom FileChooserDialog implementations."""

import os
import mimetypes
import fnmatch
from gi.repository import Gtk, Gio, Pango  # Gio added for URI-aware file queries (network support)

from mcomix.preferences import prefs
from mcomix import image_tools
from mcomix import archive_tools
from mcomix import labels
from mcomix import constants
from mcomix import log
from mcomix import thumbnail_tools
from mcomix import message_dialog
from mcomix import file_provider
from mcomix import tools
from mcomix.i18n import _

mimetypes.init()

class _BaseFileChooserDialog(Gtk.Dialog):

    """We roll our own FileChooserDialog because the one in GTK seems
    buggy with the preview widget. The <action> argument dictates what type
    of filechooser dialog we want (i.e. it is Gtk.FileChooserAction.OPEN
    or Gtk.FileChooserAction.SAVE).

    This is a base class for the _MainFileChooserDialog, the
    _LibraryFileChooserDialog and the SimpleFileChooserDialog.

    Subclasses should implement a method files_chosen(paths) that will be
    called once the filechooser has done its job and selected some files.
    If the dialog was closed or Cancel was pressed, <paths> is the empty list.
    """

    _last_activated_file = None

    def __init__(self, action=Gtk.FileChooserAction.OPEN):
        self._action = action
        self._destroyed = False

        if action == Gtk.FileChooserAction.OPEN:
            title = _('Open')
            buttons = (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OPEN, Gtk.ResponseType.OK)

        else:
            title = _('Save')
            buttons = (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_SAVE, Gtk.ResponseType.OK)

        super(_BaseFileChooserDialog, self).__init__(title, None, 0, buttons)
        self.set_default_response(Gtk.ResponseType.OK)

        self.filechooser = Gtk.FileChooserWidget(action=action)
        self.filechooser.set_size_request(680, 420)
        self.vbox.pack_start(self.filechooser, True, True, 0)
        self.set_border_width(4)
        self.filechooser.set_border_width(6)
        self.connect('response', self._response)
        self.filechooser.connect('file_activated', self._response,
            Gtk.ResponseType.OK)

        preview_box = Gtk.Box.new(Gtk.Orientation.VERTICAL, 10)
        preview_box.set_size_request(130, 0)
        self._preview_image = Gtk.Image()
        self._preview_image.set_size_request(130, 130)
        preview_box.pack_start(self._preview_image, False, False, 0)
        self.filechooser.set_preview_widget(preview_box)

        pango_scale_small = (1 / 1.2)

        self._namelabel = labels.FormattedLabel(weight=Pango.Weight.BOLD,
            scale=pango_scale_small)
        self._namelabel.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        preview_box.pack_start(self._namelabel, False, False, 0)

        self._sizelabel = labels.FormattedLabel(scale=pango_scale_small)
        self._sizelabel.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        preview_box.pack_start(self._sizelabel, False, False, 0)
        self.filechooser.set_use_preview_label(False)
        preview_box.show_all()
        self.filechooser.connect('update-preview', self._update_preview)

        self._all_files_filter = self.add_filter( _('All files'), [], ['*'])

        try:
            current_file = self._current_file()
            last_file = self.__class__._last_activated_file

            # If a file is currently open, use its path
            if current_file and os.path.exists(current_file):
                self.filechooser.set_current_folder(os.path.dirname(current_file))
            # last_file may be a non-local URI (e.g. smb://) if the user previously
            # opened a file over the network; set_current_folder() only accepts POSIX paths.
            elif last_file:
                if '://' in last_file:
                    gfile = Gio.File.new_for_uri(last_file)
                    parent = gfile.get_parent()
                    if parent:
                        self.filechooser.set_current_folder_uri(parent.get_uri())
                elif os.path.exists(last_file):
                    self.filechooser.set_filename(last_file)
            # The pref may also hold a non-local URI if the user last browsed a
            # network location; route through set_current_folder_uri() in that case.
            else:
                last_browsed = prefs['path of last browsed in filechooser']
                if '://' in last_browsed:
                    self.filechooser.set_current_folder_uri(last_browsed)
                elif os.path.isdir(last_browsed):
                    if prefs['store recent file info']:
                        self.filechooser.set_current_folder(last_browsed)
                    else:
                        self.filechooser.set_current_folder(constants.HOME_DIR)

        except Exception as ex: # E.g. broken prefs values.
            log.debug(ex)

        self.show_all()

    def add_filter(self, name, mimes, patterns=[]):
        """Add a filter, called <name>, for each mime type in <mimes> and
        each pattern in <patterns> to the filechooser.
        """
        ffilter = Gtk.FileFilter()
        ffilter.add_custom(
                Gtk.FileFilterFlags.FILENAME | Gtk.FileFilterFlags.MIME_TYPE,
                self._filter, (patterns, mimes))

        ffilter.set_name(name)
        self.filechooser.add_filter(ffilter)
        return ffilter

    def add_archive_filters(self):
        """Add archive filters to the filechooser.
        """
        ffilter = Gtk.FileFilter()
        ffilter.set_name(_('All archives'))
        self.filechooser.add_filter(ffilter)
        supported_formats = archive_tools.get_supported_formats()
        for name in sorted(supported_formats):
            mime_types, extensions = supported_formats[name]
            patterns = ['*.%s' % ext for ext in extensions]
            self.add_filter(_('%s archives') % name, mime_types, patterns)
            for mime in mime_types:
                ffilter.add_mime_type(mime)
            for pat in patterns:
                ffilter.add_pattern(pat)

    def add_image_filters(self):
        """Add images filters to the filechooser.
        """
        ffilter = Gtk.FileFilter()
        ffilter.set_name(_('All images'))
        self.filechooser.add_filter(ffilter)
        supported_formats = image_tools.get_supported_formats()
        for name in sorted(supported_formats):
            mime_types, extensions = supported_formats[name]
            patterns = ['*.%s' % ext for ext in extensions]
            self.add_filter(_('%s images') % name, mime_types, patterns)
            for mime in mime_types:
                ffilter.add_mime_type(mime)
            for pat in patterns:
                ffilter.add_pattern(pat)

    def _filter(self, filter_info, data):
        """ Callback function used to determine if a file
        should be filtered or not. C{data} is a tuple containing
        (patterns, mimes) that should pass the test. Returns True
        if the file passed in C{filter_info} should be displayed. """

        match_patterns, match_mimes = data

        matches_mime = bool([match_mime for match_mime in match_mimes if match_mime == filter_info.mime_type])
        matches_pattern = bool([match_pattern for match_pattern in match_patterns if fnmatch.fnmatch(filter_info.filename, match_pattern)])

        return matches_mime or matches_pattern

    def collect_files_from_subdir(self, path, filter, recursive=False):
        """ Finds archives within C{path} that match the
        L{Gtk.FileFilter} passed in C{filter}. """

        for root, dirs, files in os.walk(path):
            for file in files:
                full_path = os.path.join(root, file)
                mimetype = mimetypes.guess_type(full_path)[0] or 'application/octet-stream'
                filter_info = Gtk.FileFilterInfo()
                filter_info.contains = Gtk.FileFilterFlags.FILENAME | Gtk.FileFilterFlags.MIME_TYPE
                filter_info.filename = full_path
                filter_info.mime_type = mimetype

                if (filter == self._all_files_filter or filter.filter(filter_info)):
                    yield full_path

            if not recursive:
                break

    def set_save_name(self, name):
        self.filechooser.set_current_name(name)

    def set_current_directory(self, path):
        self.filechooser.set_current_folder(path)

    def should_open_recursive(self):
        return False

    def _response(self, widget, response):
        """Return a list of the paths of the chosen files, or None if the
        event only changed the current directory.
        """
        if response == Gtk.ResponseType.OK:
            # get_filenames() returns nothing for GIO-only locations (e.g. smb://);
            # get_uris() works for both local and network paths.
            uris = self.filechooser.get_uris()
            if not uris:
                return

            # Use query_file_type() instead of os.path.isdir() because the latter
            # only works on POSIX paths; GIO handles both local and network locations.
            ffilter = self.filechooser.get_filter()
            paths = []
            for uri in uris:
                gfile = Gio.File.new_for_uri(uri)
                file_type = gfile.query_file_type(Gio.FileQueryInfoFlags.NONE, None)
                if file_type == Gio.FileType.DIRECTORY:
                    local_path = gfile.get_path()
                    if local_path:
                        subdir_files = list(self.collect_files_from_subdir(
                            local_path, ffilter, self.should_open_recursive()))
                        file_provider.FileProvider.sort_files(subdir_files)
                        paths.extend(subdir_files)
                    else:
                        # os.walk() can't traverse non-POSIX locations; skip for now.
                        log.warning('Cannot expand non-local directory: %s', uri)
                else:
                    local_path = gfile.get_path()
                    # Pass the raw URI when there is no POSIX path so that
                    # file_handler._resolve_uri() can copy it to a temp location.
                    paths.append(local_path if local_path else uri)

            if not paths:
                return

            # FileChooser.set_do_overwrite_confirmation() doesn't seem to
            # work on our custom dialog, so we use a simple alternative.
            first_gfile = Gio.File.new_for_uri(uris[0])
            first_path = first_gfile.get_path() or uris[0]
            # query_exists() works for both local and remote Gio.File objects.
            first_type = first_gfile.query_file_type(Gio.FileQueryInfoFlags.NONE, None)
            if (self._action == Gtk.FileChooserAction.SAVE and
                first_type != Gio.FileType.DIRECTORY and
                first_gfile.query_exists(None)):

                overwrite_dialog = message_dialog.MessageDialog(None, 0,
                    Gtk.MessageType.QUESTION, Gtk.ButtonsType.OK_CANCEL)
                overwrite_dialog.set_text(
                    _("A file named '%s' already exists. Do you want to replace it?") %
                        first_gfile.get_basename(),
                    _('Replacing it will overwrite its contents.'))
                response = overwrite_dialog.run()

                if response != Gtk.ResponseType.OK:
                    self.emit_stop_by_name('response')
                    return

            # Do not store path if the user chose not to keep a file history.
            # Store the URI string when there is no local path so the next open
            # can restore to a network location via set_current_folder_uri().
            if prefs['store recent file info']:
                folder_uri = self.filechooser.get_current_folder_uri() or ''
                folder_gfile = Gio.File.new_for_uri(folder_uri)
                local_folder = folder_gfile.get_path()
                prefs['path of last browsed in filechooser'] = \
                    local_folder if local_folder else folder_uri
            else:
                prefs['path of last browsed in filechooser'] = constants.HOME_DIR

            self.__class__._last_activated_file = first_path
            self.files_chosen(paths)

        else:
            self.files_chosen([])

        self._destroyed = True

    def _update_preview(self, *args):
        # get_preview_filename() returns None for non-local GIO locations;
        # get_preview_uri() + get_path() degrades gracefully (local_path is None)
        # so we silently skip thumbnailing for network files rather than crashing.
        uri = self.filechooser.get_preview_uri()
        local_path = None
        if uri:
            gfile = Gio.File.new_for_uri(uri)
            local_path = gfile.get_path()

        if local_path and os.path.isfile(local_path):
            thumbnailer = thumbnail_tools.Thumbnailer(size=(128, 128),
                                                      archive_support=True)
            thumbnailer.thumbnail_finished += self._preview_thumbnail_finished
            thumbnailer.thumbnail(local_path, threaded=True)
        else:
            self._preview_image.clear()
            self._namelabel.set_text('')
            self._sizelabel.set_text('')

    def _preview_thumbnail_finished(self, filepath, pixbuf):
        """ Called when the thumbnailer has finished creating
        the thumbnail for <filepath>. """

        if self._destroyed:
            return

        current_path = self.filechooser.get_preview_filename()
        if current_path and current_path == filepath:

            if pixbuf is None:
                self._preview_image.clear()
                self._namelabel.set_text('')
                self._sizelabel.set_text('')

            else:
                pixbuf = image_tools.add_border(pixbuf, 1)
                self._preview_image.set_from_pixbuf(pixbuf)
                self._namelabel.set_text(os.path.basename(filepath))
                self._sizelabel.set_text(tools.format_byte_size(
                    os.stat(filepath).st_size))

    def _current_file(self):
        # XXX: This method defers the import of main to avoid cyclic imports
        # during startup.

        from mcomix import main
        return main.main_window().filehandler.get_path_to_base()

# vim: expandtab:sw=4:ts=4
