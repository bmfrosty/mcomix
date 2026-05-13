"""file_handler.py - File handler that takes care of opening archives and images."""


import os
import shutil
import tempfile
import threading
import re
import pickle
from gi.repository import Gtk

from mcomix.preferences import prefs
from mcomix import archive_extractor
from mcomix import archive_tools
from mcomix import image_tools
from mcomix import tools
from mcomix import constants
from mcomix import file_provider
from mcomix import callback
from mcomix import log
from mcomix import last_read_page
from mcomix import message_dialog
from mcomix.library import backend
from mcomix.i18n import _


class FileHandler(object):

    """The FileHandler keeps track of the actual files/archives opened.

    While ImageHandler takes care of pages/images, this class provides
    the raw file names for archive members and image files, extracts
    archives, and lists directories for image files.
    """

    def __init__(self, window):
        #: Indicates if files/archives are currently loaded/loading.
        self.file_loaded = False
        self.file_loading = False
        #: None if current file is not an archive, or unrecognized format.
        self.archive_type = None

        #: Either path to the current archive, or first file in image list.
        #: This is B{not} the path to the currently open page.
        self._current_file = None
        #: Reference to L{MainWindow}.
        self._window = window
        #: Path to opened archive file, or directory containing current images.
        self._base_path = None
        #: Temporary directory used for extracting archives.
        self._tmp_dir = None
        #: If C{True}, no longer wait for files to get extracted.
        self._stop_waiting = False
        #: List of comment files inside of the currently opened archive.
        self._comment_files = []
        #: Mapping of absolute paths to archive path names.
        self._name_table = {}
        #: Archive extractor.
        self._extractor = archive_extractor.Extractor()
        self._extractor.file_extracted += self._extracted_file
        self._extractor.contents_listed += self._listed_contents
        #: Condition to wait on when extracting archives and waiting on files.
        self._condition = None
        #: Provides a list of available files/archives in the open directory.
        self._file_provider = None
        #: Temp dir for files downloaded from non-local URIs.
        self._net_tmp_dir = None
        #: Original URI of the currently open file when it came from a non-local
        #: location; None for ordinary local files.  Needed so next/prev archive
        #: navigation can enumerate the remote parent directory instead of the
        #: local temp dir (which only ever contains the one downloaded file).
        self._source_uri = None
        #: Sliding window cache for remote directory navigation.
        #: Dict with keys: parent_uri, current_uri, prev (list, closest-first),
        #: next (list, closest-first), complete (bool — True means the lists cover
        #: the entire directory so no background refetch is needed).
        self._net_sibling_cache = None
        #: True while a background thread is fetching the full sibling list.
        self._sibling_fetch_running = False
        #: Keeps track of the last read page in archives
        self.last_read_page = last_read_page.LastReadPage(backend.LibraryBackend())
        #: Regexp used for determining which archive files are comment files.
        self._comment_re = None
        self.update_comment_extensions()

        self.last_read_page.set_enabled(bool(prefs['store recent file info']))

    def _scan_fds_for_backing_path(self, doc_portal_path):
        """Scan /proc/self/fd/ for the real backing path of a document-portal file.

        When the kernel uses FUSE passthrough mode (Linux 5.16+) the file
        descriptor obtained by open()-ing a FUSE file points directly to the
        backing file rather than the FUSE path.  In that case readlink on the
        /proc/self/fd/<n> entry reveals the real path (e.g. an smb:// location
        cached by the portal daemon) which we can use for sibling enumeration.

        Returns the backing path string, or None if not found.
        """
        import glob
        basename = os.path.basename(doc_portal_path)
        log.debug('_scan_fds: looking for fd with basename=%s', basename)
        for fd_link in sorted(glob.glob('/proc/self/fd/*')):
            try:
                target = os.readlink(fd_link)
                log.debug('_scan_fds: %s -> %s', fd_link, target)
                if os.path.basename(target) == basename and target != doc_portal_path:
                    log.debug('_scan_fds: backing path found: %s', target)
                    return target
            except OSError:
                pass
        log.debug('_scan_fds: no backing path found')
        return None

    @staticmethod
    def _normalize_kio_fuse_path(path):
        """Lowercase host and share name in a KIO FUSE path for consistent keys.

        KIO FUSE path structure: /run/user/<uid>/kio-fuse-<id>/<scheme>/<host>/<share>/...
        SMB share names are case-insensitive, but different code paths (direct
        file-chooser vs _try_kio_fuse_path) may produce different capitalizations.
        Normalizing here makes last_read_page lookups consistent.
        """
        m = re.match(r'^(/run/user/\d+/kio-fuse-[^/]+/[^/]+/)([^/]+)/([^/]+)/(.+)$', path)
        if m:
            return m.group(1) + m.group(2).lower() + '/' + m.group(3).lower() + '/' + m.group(4)
        return path

    def _resolve_uri(self, path):
        """Resolve a path or list of paths that may be URIs to local paths.

        For GIO locations that have a local POSIX mount (e.g. GVFS smb
        mounts under /run/user/*/gvfs/…) the local path is returned directly.
        For truly non-local URIs the file is copied to a per-open temp dir
        so the rest of the pipeline can work on a normal local path.

        Returns the same structure (string or list) as was passed in.
        """
        if isinstance(path, list):
            return [self._resolve_uri(p) for p in path]
        if '://' not in path:
            # KIO FUSE paths are treated as plain local paths — the local file
            # provider enumerates siblings via the FUSE mount.  _archive_opened
            # extracts the smb:// URI for recents without setting _source_uri.
            return self._normalize_kio_fuse_path(path)
        from gi.repository import Gio
        log.debug('_resolve_uri: input URI=%s', path)
        gfile = Gio.File.new_for_uri(path)
        local = gfile.get_path()
        if local:
            log.debug('_resolve_uri: resolved to local path=%s', local)
            if not path.startswith('file://'):
                # Non-file URI (smb://, sftp://, …) with a GVFS local path —
                # store the original URI so GIO can enumerate the remote dir.
                self._source_uri = path
                log.debug('_resolve_uri: set _source_uri=%s', path)
            elif '/run/user/' in local and '/gvfs/' in local:
                # file:// URI resolving to a GVFS path (navigation step after
                # readlink recovered the real path).  Keep it as _source_uri so
                # subsequent prev/next presses continue to enumerate the same dir.
                self._source_uri = path
                log.debug('_resolve_uri: GVFS file:// URI -> _source_uri=%s', path)
            return self._normalize_kio_fuse_path(local)
        # No GVFS FUSE path — try KIO FUSE (KDE) before falling back to copy.
        kio_path = self._try_kio_fuse_path(path)
        if kio_path:
            # Treat as local — don't set _source_uri so the file provider
            # handles sibling navigation via the FUSE mount.
            kio_path = self._normalize_kio_fuse_path(kio_path)
            log.debug('_resolve_uri: KIO FUSE mountUrl -> %s', kio_path)
            return kio_path
        # For smb:// URIs, use smbprotocol directly when KIO FUSE is unavailable
        # (e.g. inside the Flatpak sandbox where KIO FUSE D-Bus is not accessible).
        if path.startswith('smb://'):
            from mcomix import smb_client as _sc
            if _sc.is_available():
                if self._net_tmp_dir is None:
                    self._net_tmp_dir = tempfile.mkdtemp(prefix='mcomix_net.')
                basename = gfile.get_basename() or 'mcomix_net_file'
                tmp_path = os.path.join(self._net_tmp_dir, basename)
                try:
                    import shutil
                    with _sc.open_file(path) as src:
                        with open(tmp_path, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                    self._source_uri = path
                    log.debug('_resolve_uri: SMB direct copy to tmp=%s, _source_uri=%s',
                              tmp_path, path)
                    return tmp_path
                except Exception as ex:
                    log.debug('_resolve_uri: SMB direct copy failed: %s', ex)
                    raise IOError(_('Could not open SMB file "%s": %s') % (path, str(ex)))
        # Last resort: download to a managed temp directory via GIO.
        if self._net_tmp_dir is None:
            self._net_tmp_dir = tempfile.mkdtemp(prefix='mcomix_net.')
        basename = gfile.get_basename() or 'mcomix_net_file'
        tmp_path = os.path.join(self._net_tmp_dir, basename)
        dest = Gio.File.new_for_path(tmp_path)
        try:
            gfile.copy(dest, Gio.FileCopyFlags.OVERWRITE, None, None, None)
        except Exception as ex:
            raise IOError(_('Could not copy remote file "%s": %s') % (path, str(ex)))
        # Remember the original URI so next/prev archive can enumerate the
        # remote parent directory rather than the single-file temp dir.
        self._source_uri = path
        log.debug('_resolve_uri: no local path, copied to tmp=%s, _source_uri=%s', tmp_path, path)
        return tmp_path

    def _try_kio_fuse_path(self, uri):
        """Ask the KIO FUSE daemon to mount uri and return its local FUSE path.

        Returns the POSIX path string on success, or None if KIO FUSE is
        unavailable or does not support the URI scheme.
        """
        try:
            from gi.repository import Gio, GLib
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            result = conn.call_sync(
                'org.kde.KIOFuse',
                '/org/kde/KIOFuse',
                'org.kde.KIOFuse.VFS',
                'mountUrl',
                GLib.Variant('(s)', (uri,)),
                GLib.VariantType('(s)'),
                Gio.DBusCallFlags.NONE,
                30000,
                None,
            )
            local_path = result.get_child_value(0).get_string()
            return local_path if local_path else None
        except Exception as ex:
            log.debug('_try_kio_fuse_path: %s', ex)
            return None

    def refresh_file(self, *args, **kwargs):
        """ Closes the current file(s)/archive and reloads them. """
        if self.file_loaded:
            current_file = os.path.abspath(self._window.imagehandler.get_real_path())
            if self.archive_type is not None:
                start_page = self._window.imagehandler.get_current_page()
            else:
                start_page = 0
            self.open_file(current_file, start_page, keep_fileprovider=True)

    def open_file(self, path, start_page=0, keep_fileprovider=False, smb_context=None):
        """Open the file pointed to by <path>.

        If <start_page> is not set we set the current
        page to 1 (first page), if it is set we set the current page to the
        value of <start_page>. If <start_page> is non-positive it means the
        last image.

        Return True if the file is successfully loaded.
        """

        self._close()

        # Convert any URI strings (e.g. smb://) to local POSIX paths before the
        # rest of the pipeline, which assumes os.path-compatible strings throughout.
        try:
            path = self._resolve_uri(path)
        except IOError as ex:
            self._window.statusbar.set_message(str(ex))
            self._window.osd.show(str(ex))
            return False

        try:
            path = self._initialize_fileprovider(path, keep_fileprovider)
        except ValueError as ex:
            self._window.statusbar.set_message(str(ex))
            self._window.osd.show(str(ex))
            return False

        error_message = self._check_access(path)
        if error_message:
            self._window.statusbar.set_message(error_message)
            self._window.osd.show(error_message)
            self.file_opened()
            return False

        self.filelist = self._file_provider.list_files()
        self.archive_type = archive_tools.archive_mime_type(path)
        self._start_page = start_page
        self._current_file = os.path.abspath(path)
        self._stop_waiting = False

        image_files = []
        current_image_index = 0

        # Actually open the file(s)/archive passed in path.
        if self.archive_type is not None:
            try:
                self._open_archive(self._current_file)
            except Exception as ex:
                self._window.statusbar.set_message(str(ex))
                self._window.osd.show(str(ex))
                self.file_opened()
                return False
            # After the archive is open its file descriptor is live.  If the
            # file came from the document portal and we have no _source_uri yet,
            # check /proc/self/fd/ for a FUSE-passthrough backing path.
            if not self._source_uri and '/run/user/' in self._current_file and '/doc/' in self._current_file:
                backing = self._scan_fds_for_backing_path(self._current_file)
                if backing and '/doc/' not in backing:
                    from gi.repository import Gio
                    self._source_uri = Gio.File.new_for_path(backing).get_uri()
                    log.debug('open_file: fd-scan backing path -> _source_uri=%s', self._source_uri)
            self.file_loading = True
        else:
            image_files, current_image_index = \
                self._open_image_files(self.filelist, self._current_file)
            self._archive_opened(image_files)

        if smb_context and self._source_uri:
            self._set_smb_sibling_cache(smb_context)
        return True

    def _archive_opened(self, image_files):
        """ Called once the archive has been opened and its contents listed.
        """

        self._window.imagehandler._base_path = self._base_path
        self._window.imagehandler._image_files = image_files
        self.file_opened()

        if not image_files:
            msg = _("No images in '%s'") % os.path.basename(self._current_file)
            self._window.statusbar.set_message(msg)
            self._window.osd.show(msg)

        else:
            if self.archive_type is None:
                # If no extraction is required, mark all files as available.
                self.file_available(self.filelist)
                # Set current page to current file.
                if self._current_file in self.filelist:
                    current_image_index = self.filelist.index(self._current_file)
                else:
                    current_image_index = 0
            else:
                last_image_index = self._get_index_for_page(self._start_page,
                                                            len(image_files),
                                                            self._current_file)
                if self._start_page or \
                   prefs['stored dialog choices'].get('resume-from-last-read-page', False):
                    current_image_index = last_image_index
                else:
                    # Don't switch to last page yet; since we have not asked
                    # the user for confirmation yet.
                    current_image_index = 0
                if last_image_index != current_image_index:
                    # Bump last page closer to the front of the extractor queue.
                    self._window.set_page(last_image_index + 1)

            self._window.set_page(current_image_index + 1)

            if self.archive_type is not None:
                self._extractor.extract()
                if last_image_index != current_image_index and \
                   self._ask_goto_last_read_page(self._current_file, last_image_index + 1):
                    self._window.set_page(last_image_index + 1)

            self.write_fileinfo_file()

        kio_m = re.match(r'^/run/user/\d+/kio-fuse-[^/]+/([^/]+)/(.+)$',
                         self._current_file)
        if kio_m:
            from urllib.parse import unquote, quote as _quote
            scheme = kio_m.group(1)
            parts = _quote(unquote(kio_m.group(2)), safe='/').split('/', 2)
            parts[0] = parts[0].lower()          # normalize host (case-insensitive)
            if len(parts) > 1:
                parts[1] = parts[1].lower()      # normalize share name (case-insensitive)
            recent_target = scheme + '://' + '/'.join(parts)
        elif self._source_uri and not self._source_uri.startswith('file://'):
            recent_target = self._source_uri
        else:
            recent_target = self._current_file
        self._window.uimanager.recent.add(recent_target)

    @callback.Callback
    def file_opened(self):
        """ Called when a new set of files has successfully been opened. """
        self.file_loaded = True

    @callback.Callback
    def file_closed(self):
        """ Called when the current file has been closed. """
        pass

    def close_file(self):
        """Close the currently opened file and its provider. """
        self._net_sibling_cache = None
        self._close(close_provider=True)

    def _close(self, close_provider=False):
        """Run tasks for "closing" the currently opened file(s)."""
        if self.file_loaded or self.file_loading:
            if close_provider:
                self._file_provider = None
            self.update_last_read_page()
            if self.archive_type is not None:
                self._extractor.close()
            self._window.imagehandler.cleanup()
            self.file_loaded = False
            self.file_loading = False
            self.archive_type = None
            self._current_file = None
            self._base_path = None
            self._stop_waiting = True
            self._comment_files = []
            self._name_table.clear()
            self._source_uri = None
            self.file_closed()
        # Catch up on UI events, so we don't leave idle callbacks.
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        tools.garbage_collect()
        if self._tmp_dir is not None:
            self.thread_delete(self._tmp_dir)
            self._tmp_dir = None
        # Clean up any file copied from a non-local URI (see _resolve_uri).
        if self._net_tmp_dir is not None:
            self.thread_delete(self._net_tmp_dir)
            self._net_tmp_dir = None

    def _initialize_fileprovider(self, path, keep_fileprovider):
        """ Creates the L{file_provider.FileProvider} for C{path}.

        If C{path} is a list, assumes that only the files in the list
        should be available. If C{path} is a string, assume that it is
        either a directory or an image file, and all files in that directory
        should be opened.

        @param path: List of file names, or single file/directory as string.
        @param keep_fileprovider: If C{True}, no new provider is constructed.
        @return: If C{path} was a list, returns the first list element.
            Otherwise, C{path} is not modified."""

        if isinstance(path, list) and len(path) == 0:
            # This is a programming error and does not need translation.
            assert False, "Tried to open an empty list of files."

        elif isinstance(path, list) and len(path) > 0:
            # A list of files was passed - open only these files.
            if self._file_provider is None or not keep_fileprovider:
                self._file_provider = file_provider.get_file_provider(path)

            return path[0]
        else:
            # A single file was passed - use Comix' classic open mode
            # and open all files in its directory.
            if self._file_provider is None or not keep_fileprovider:
                self._file_provider = file_provider.get_file_provider([ path ])

            return path

    def _check_access(self, path):
        """ Checks for various error that could occur when opening C{path}.

        @param path: Path to file that should be opened.
        @return: An appropriate error string, or C{None} if no error was found.
        """
        if not os.path.exists(path):
            return _('Could not open %s: No such file.') % path

        elif not os.access(path, os.R_OK):
            return _('Could not open %s: Permission denied.') % path

        else:
            return None

    def _open_archive(self, path):
        """ Opens the archive passed in C{path}.

        Creates an L{archive_extractor.Extractor} and extracts all images
        found within the archive.

        @return: A tuple containing C{(image_files, image_index)}. """

        self._tmp_dir = tempfile.mkdtemp(prefix='mcomix.', suffix=os.sep)
        self._base_path = path
        try:
            self._condition = self._extractor.setup(self._base_path,
                                                self._tmp_dir,
                                                self.archive_type)
        except Exception:
            self._condition = None
            raise

    def _listed_contents(self, archive, files):

        if not self.file_loading:
            return
        self.file_loading = False

        files = self._extractor.get_files()
        archive_images = [image for image in files
            if image_tools.is_image_file(image)
            # Remove MacOS meta files from image list
            and not '__MACOSX' in os.path.normpath(image).split(os.sep)]

        self._sort_archive_images(archive_images)
        image_files = [ os.path.join(self._tmp_dir, f)
                        for f in archive_images ]

        comment_files = list(filter(self._comment_re.search, files))
        tools.alphanumeric_sort(comment_files)
        self._comment_files = [ os.path.join(self._tmp_dir, f)
                                for f in comment_files ]

        self._name_table = dict(list(zip(image_files, archive_images)))
        self._name_table.update(list(zip(self._comment_files, comment_files)))

        self._extractor.set_files(archive_images + comment_files)

        self._archive_opened(image_files)

    def _sort_archive_images(self, filelist):
        """ Sorts the image list passed in C{filelist} based on the sorting
        preference option. """

        if prefs['sort archive by'] == constants.SORT_NAME:
            tools.alphanumeric_sort(filelist)
        elif prefs['sort archive by'] == constants.SORT_NAME_LITERAL:
            filelist.sort()
        else:
            # No sorting
            pass

        if prefs['sort archive order'] == constants.SORT_DESCENDING:
            filelist.reverse()

    def _get_index_for_page(self, start_page, num_of_pages, path):
        """ Returns the page that should be displayed for an archive.
        @param start_page: If -1, show last page. If 0, show either first page
                           or last read page. If > 0, show C{start_page}.
        @param num_of_pages: Page count.
        @param path: Archive path.
        """
        if start_page < 0 and prefs['default double page']:
            current_image_index = num_of_pages - 2
        elif start_page < 0 and not prefs['default double page']:
            current_image_index = num_of_pages - 1
        elif start_page == 0:
            current_image_index = (self.last_read_page.get_page(path) or 1) - 1
        else:
            current_image_index = start_page - 1

        return min(max(0, current_image_index), num_of_pages - 1)

    def _ask_goto_last_read_page(self, path, last_read_page):
        """ If the user read an archive previously, ask to continue from
        that time, or from page 1. This method returns a page index, that is,
        index + 1. """

        read_date = self.last_read_page.get_date(path)

        dialog = message_dialog.MessageDialog(self._window, Gtk.DialogFlags.MODAL, Gtk.MessageType.INFO,
            Gtk.ButtonsType.YES_NO)
        dialog.set_default_response(Gtk.ResponseType.YES)
        dialog.set_should_remember_choice('resume-from-last-read-page',
            (Gtk.ResponseType.YES, Gtk.ResponseType.NO))
        dialog.set_text(
            (_('Continue reading from page %d?') % last_read_page),
            _('You stopped reading here on %(date)s, %(time)s. '
            'If you choose "Yes", reading will resume on page %(page)d. Otherwise, '
            'the first page will be loaded.') % {'date': read_date.date().strftime("%x"),
                'time': read_date.time().strftime("%X"), 'page': last_read_page})
        result = dialog.run()

        return result == Gtk.ResponseType.YES

    def _open_image_files(self, filelist, image_path):
        """ Opens all files passed in C{filelist}.

        If C{image_path} is found in C{filelist}, the current page will be set
        to its index within C{filelist}.

        @return: Tuple of C{(image_files, image_index)}
        """

        self._base_path = self._file_provider.get_directory()

        if image_path in filelist:
            current_image_index = filelist.index(image_path)
        else:
            current_image_index = 0

        return filelist, current_image_index

    def get_file_number(self):
        if self.archive_type is None:
            # No file numbers for images.
            return 0, 0
        file_list = self._file_provider.list_files(file_provider.FileProvider.ARCHIVES)
        if self._current_file in file_list:
            current_index = file_list.index(self._current_file)
        else:
            current_index = 0
        return current_index + 1, len(file_list)

    def get_number_of_comments(self):
        """Return the number of comments in the current archive."""
        return len(self._comment_files)

    def get_comment_text(self, num):
        """Return the text in comment <num> or None if comment <num> is not
        readable.
        """
        self._wait_on_comment(num)
        try:
            fd = open(self._comment_files[num - 1], 'r')
            text = fd.read()
            fd.close()
        except Exception:
            text = None
        return text

    def get_comment_name(self, num):
        """Return the filename of comment <num>."""
        return self._comment_files[num - 1]

    def update_comment_extensions(self):
        """Update the regular expression used to filter out comments in
        archives by their filename.
        """
        exts = '|'.join(prefs['comment extensions'])
        self._comment_re = re.compile(r'\.(%s)\s*$' % exts, re.I)

    def get_path_to_base(self):
        """Return the full path to the current base (path to archive or
        image directory.)
        """
        if self.archive_type is not None:
            return self._base_path
        elif self._window.imagehandler._image_files:
            img_index = self._window.imagehandler._current_image_index
            filename = self._window.imagehandler._image_files[img_index]
            return os.path.dirname(filename)
        else:
            return None

    def get_base_filename(self):
        """Return the filename of the current base (archive filename or
        directory name).
        """
        return os.path.basename(self.get_path_to_base())

    def get_pretty_current_filename(self):
        """Return a string with the name of the currently viewed file that is
        suitable for printing.
        """

        return self._window.imagehandler.get_pretty_current_filename()

    def _build_net_sibling_cache(self):
        """Enumerate the remote parent directory once and store a ±10 window
        of archive URIs around self._source_uri in self._net_sibling_cache.

        The window shifts cheaply on each prev/next press; re-enumeration only
        happens when the user navigates more than 10 steps from the last
        enumeration point.  Sets self._net_sibling_cache to None on any error.
        """
        log.debug('_build_net_sibling_cache: _source_uri=%s', self._source_uri)

        archive_exts = set()
        for mime_types, extensions in archive_tools.get_supported_formats().values():
            archive_exts.update(ext.lower() for ext in extensions)

        # For smb:// URIs use smbprotocol directly — GIO cannot enumerate SMB
        # shares inside the Flatpak sandbox (no GVFS FUSE mount available).
        if self._source_uri and self._source_uri.startswith('smb://'):
            from mcomix import smb_client as _sc
            from urllib.parse import urlparse, unquote
            if _sc.is_available():
                parent_smb_uri = _sc.parent_uri(self._source_uri)
                if not parent_smb_uri or parent_smb_uri == 'smb://':
                    log.debug('_build_net_sibling_cache: no smb parent for %s', self._source_uri)
                    self._net_sibling_cache = None
                    return
                try:
                    entries = _sc.list_directory(parent_smb_uri)
                except Exception as ex:
                    log.error('Could not list SMB directory for archive navigation: %s', ex)
                    self._net_sibling_cache = None
                    return
                names = [e.name for e in entries
                         if not e.is_dir
                         and os.path.splitext(e.name)[1].lstrip('.').lower() in archive_exts]
                tools.alphanumeric_sort(names)
                log.debug('_build_net_sibling_cache: SMB found %d archive siblings', len(names))
                current_name = unquote(urlparse(self._source_uri).path.rstrip('/').split('/')[-1])
                log.debug('_build_net_sibling_cache: current_name=%s', current_name)
                try:
                    idx = names.index(current_name)
                except ValueError:
                    log.debug('_build_net_sibling_cache: %s not in sibling list (first 5: %s)',
                              current_name, names[:5])
                    self._net_sibling_cache = None
                    return
                prev_names = list(reversed(names[:idx]))
                next_names = names[idx + 1:]
                log.debug('_build_net_sibling_cache: idx=%d, %d prev, %d next',
                          idx, len(prev_names), len(next_names))
                self._net_sibling_cache = {
                    'parent_uri': parent_smb_uri,
                    'current_uri': self._source_uri,
                    'prev': [_sc.child_uri(parent_smb_uri, n) for n in prev_names],
                    'next': [_sc.child_uri(parent_smb_uri, n) for n in next_names],
                    'complete': True,
                }
                return

        # GIO path for GVFS FUSE mounts and other network URIs.
        from gi.repository import Gio
        gfile = Gio.File.new_for_uri(self._source_uri)
        parent = gfile.get_parent()
        if parent is None:
            log.debug('_build_net_sibling_cache: no parent, cannot enumerate')
            self._net_sibling_cache = None
            return
        log.debug('_build_net_sibling_cache: enumerating parent=%s', parent.get_uri())
        try:
            enumerator = parent.enumerate_children(
                'standard::name', Gio.FileQueryInfoFlags.NONE, None)
        except Exception as ex:
            log.error('Could not list remote directory for archive navigation: %s', ex)
            self._net_sibling_cache = None
            return

        names = []
        while True:
            info = enumerator.next_file(None)
            if info is None:
                break
            name = info.get_name()
            if os.path.splitext(name)[1].lstrip('.').lower() in archive_exts:
                names.append(name)
        enumerator.close(None)

        tools.alphanumeric_sort(names)
        log.debug('_build_net_sibling_cache: found %d archive siblings', len(names))

        current_name = gfile.get_basename()
        log.debug('_build_net_sibling_cache: current_name=%s', current_name)
        try:
            idx = names.index(current_name)
        except ValueError:
            log.debug('_build_net_sibling_cache: current_name not found in sibling list (first 5: %s)',
                      names[:5])
            self._net_sibling_cache = None
            return

        parent_uri = parent.get_uri()
        prev_names = list(reversed(names[:idx]))
        next_names = names[idx + 1:]
        log.debug('_build_net_sibling_cache: idx=%d, %d prev, %d next', idx, len(prev_names), len(next_names))
        self._net_sibling_cache = {
            'parent_uri': parent_uri,
            'current_uri': self._source_uri,
            'prev': [parent.get_child(n).get_uri() for n in prev_names],
            'next': [parent.get_child(n).get_uri() for n in next_names],
            'complete': True,
        }

    def _set_smb_sibling_cache(self, context):
        """Pre-populate the sibling cache from an smb_context dict supplied by
        the SMB browser dialog.  context keys: parent_uri, siblings (list of
        smb:// URIs sorted, up to ±100 around the selection), complete (bool).
        """
        parent_uri = context['parent_uri']
        siblings   = context['siblings']
        complete   = context.get('complete', False)
        current    = self._source_uri
        try:
            idx = siblings.index(current)
        except ValueError:
            log.debug('_set_smb_sibling_cache: %s not in context siblings', current)
            return
        self._net_sibling_cache = {
            'parent_uri': parent_uri,
            'current_uri': current,
            'prev': list(reversed(siblings[:idx])),
            'next': siblings[idx + 1:],
            'complete': complete,
        }
        log.debug('_set_smb_sibling_cache: %d prev, %d next, complete=%s',
                  len(self._net_sibling_cache['prev']),
                  len(self._net_sibling_cache['next']), complete)

    _SIBLING_PREFETCH_THRESHOLD = 10

    def _maybe_prefetch_net_siblings(self):
        """If the cache is incomplete and we are within THRESHOLD steps of
        either edge, fetch the full directory listing in a background thread
        and replace the cache (via GLib.idle_add) once done.
        """
        cache = self._net_sibling_cache
        if cache is None or cache.get('complete', True):
            return
        if (len(cache.get('next', [])) > self._SIBLING_PREFETCH_THRESHOLD and
                len(cache.get('prev', [])) > self._SIBLING_PREFETCH_THRESHOLD):
            return
        if self._sibling_fetch_running:
            return
        parent_uri = cache['parent_uri']
        self._sibling_fetch_running = True
        log.debug('_maybe_prefetch_net_siblings: fetching %s', parent_uri)

        def _fetch():
            try:
                from mcomix import smb_client as _sc
                entries = _sc.list_directory(parent_uri)
                from gi.repository import GLib
                GLib.idle_add(self._apply_full_sibling_cache, parent_uri, entries)
            except Exception as ex:
                log.debug('_maybe_prefetch_net_siblings: fetch failed: %s', ex)
                self._sibling_fetch_running = False

        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_full_sibling_cache(self, parent_uri, entries):
        """Called on the main thread after a background sibling fetch completes.
        Rebuilds the cache around the current _source_uri with the full listing.
        """
        self._sibling_fetch_running = False
        source_uri = self._source_uri
        if not source_uri:
            return False

        from urllib.parse import urlparse, unquote as _unquote
        from mcomix import smb_client as _sc

        archive_exts = set()
        for _mimes, extensions in archive_tools.get_supported_formats().values():
            archive_exts.update(ext.lower() for ext in extensions)

        names = [e.name for e in entries
                 if not e.is_dir and
                 os.path.splitext(e.name)[1].lstrip('.').lower() in archive_exts]
        tools.alphanumeric_sort(names)

        current_name = _unquote(urlparse(source_uri).path.rstrip('/').split('/')[-1])
        try:
            idx = names.index(current_name)
        except ValueError:
            log.debug('_apply_full_sibling_cache: current file not found in listing')
            return False

        self._net_sibling_cache = {
            'parent_uri': parent_uri,
            'current_uri': source_uri,
            'prev': [_sc.child_uri(parent_uri, n) for n in reversed(names[:idx])],
            'next': [_sc.child_uri(parent_uri, n) for n in names[idx + 1:]],
            'complete': True,
        }
        log.debug('_apply_full_sibling_cache: %d prev, %d next, complete=True',
                  idx, len(names) - idx - 1)
        return False

    def _open_next_archive(self, *args):
        """Open the archive that comes directly after the currently loaded
        archive in that archive's directory listing, sorted alphabetically.
        Returns True if a new archive was opened, False otherwise.
        """
        if self.archive_type is not None:

            # For network files the file provider only knows about the single
            # downloaded temp file; use smb_client enumeration instead.
            log.debug('_open_next_archive: _source_uri=%s', self._source_uri)
            if self._source_uri:
                cache = self._net_sibling_cache
                if cache is None or cache.get('current_uri') != self._source_uri:
                    self._build_net_sibling_cache()
                    cache = self._net_sibling_cache
                if not cache or not cache['next']:
                    if cache and not cache.get('complete', True):
                        self._maybe_prefetch_net_siblings()
                    log.debug('_open_next_archive: no next archive available')
                    return False
                next_uri = cache['next'][0]
                self._net_sibling_cache = {
                    'parent_uri': cache['parent_uri'],
                    'current_uri': next_uri,
                    'prev': [cache['current_uri']] + cache['prev'],
                    'next': cache['next'][1:],
                    'complete': cache.get('complete', False),
                }
                self._maybe_prefetch_net_siblings()
                self._close()
                self.open_file(next_uri)
                return True

            files = self._file_provider.list_files(file_provider.FileProvider.ARCHIVES)
            absolute_path = os.path.abspath(self._base_path)
            if absolute_path not in files: return
            current_index = files.index(absolute_path)

            for path in files[current_index + 1:]:
                if archive_tools.archive_mime_type(path) is not None:
                    self._close()
                    self.open_file(path, keep_fileprovider=True)
                    return True

        return False

    def _open_previous_archive(self, *args):
        """Open the archive that comes directly before the currently loaded
        archive in that archive's directory listing, sorted alphabetically.
        Returns True if a new archive was opened, False otherwise.
        """
        if self.archive_type is not None:

            # For network files the file provider only knows about the single
            # downloaded temp file; use smb_client enumeration instead.
            log.debug('_open_previous_archive: _source_uri=%s', self._source_uri)
            if self._source_uri:
                cache = self._net_sibling_cache
                if cache is None or cache.get('current_uri') != self._source_uri:
                    self._build_net_sibling_cache()
                    cache = self._net_sibling_cache
                if not cache or not cache['prev']:
                    if cache and not cache.get('complete', True):
                        self._maybe_prefetch_net_siblings()
                    log.debug('_open_previous_archive: no prev archive available')
                    return False
                prev_uri = cache['prev'][0]
                self._net_sibling_cache = {
                    'parent_uri': cache['parent_uri'],
                    'current_uri': prev_uri,
                    'prev': cache['prev'][1:],
                    'next': [cache['current_uri']] + cache['next'],
                    'complete': cache.get('complete', False),
                }
                self._maybe_prefetch_net_siblings()
                self._close()
                self.open_file(prev_uri, prefs['open first file in prev archive'] - 1)
                return True

            files = self._file_provider.list_files(file_provider.FileProvider.ARCHIVES)
            absolute_path = os.path.abspath(self._base_path)
            if absolute_path not in files: return
            current_index = files.index(absolute_path)

            for path in reversed(files[:current_index]):
                if archive_tools.archive_mime_type(path) is not None:
                    self._close()
                    self.open_file(path, prefs['open first file in prev archive']-1,
                                   keep_fileprovider=True)
                    return True

        return False

    def open_next_directory(self, *args):
        """ Opens the next sibling directory of the current file, as specified by
        file provider. Returns True if a new directory was opened and files found. """

        if self._file_provider is None:
            return

        if self.archive_type is not None:
            listmode = file_provider.FileProvider.ARCHIVES
        else:
            listmode = file_provider.FileProvider.IMAGES

        current_dir = self._file_provider.get_directory()
        if not self._file_provider.next_directory():
            # Restore current directory if no files were found
            self._file_provider.set_directory(current_dir)
            return False

        files = self._file_provider.list_files(listmode)
        self._close()
        if len(files) > 0:
            path = files[0]
        else:
            path = self._file_provider.get_directory()
        self.open_file(path, keep_fileprovider=True)
        return True

    def open_previous_directory(self, *args):
        """ Opens the previous sibling directory of the current file, as specified by
        file provider. Returns True if a new directory was opened and files found. """

        if self._file_provider is None:
            return

        if self.archive_type is not None:
            listmode = file_provider.FileProvider.ARCHIVES
        else:
            listmode = file_provider.FileProvider.IMAGES

        current_dir = self._file_provider.get_directory()
        if not self._file_provider.previous_directory():
            # Restore current directory if no files were found
            self._file_provider.set_directory(current_dir)
            return False

        files = self._file_provider.list_files(listmode)
        self._close()
        if len(files) > 0:
            path = files[prefs['open first file in prev directory']-1]
        else:
            path = self._file_provider.get_directory()

        self.open_file(path, (
            prefs['open first file in prev archive'] or \
            prefs['open first file in prev directory'])-1,
                       keep_fileprovider=True)
        return True

    def file_is_available(self, filepath):
        """ Returns True if the file specified by "filepath" is available
        for reading, i.e. extracted to harddisk. """

        if self.archive_type is not None:
            with self._condition:
                return self._extractor.is_ready(self._name_table[filepath])

        elif filepath is None:
            return False

        elif os.path.isfile(filepath):
            return True

        else:
            return False

    @callback.Callback
    def file_available(self, filepaths):
        """ Called every time a new file from the Filehandler's opened
        files becomes available. C{filepaths} is a list of now available files.
        """
        pass

    def _extracted_file(self, extractor, name):
        """ Called when the extractor finishes extracting the file at
        <name>. This name is relative to the temporary directory
        the files were extracted to. """
        if not self.file_loaded:
            return
        filepath = os.path.join(extractor.get_directory(), name)
        self.file_available([filepath])

    def _wait_on_comment(self, num):
        """Block the running (main) thread until the file corresponding to
        comment <num> has been fully extracted.
        """
        path = self._comment_files[num - 1]
        self._wait_on_file(path)

    def _wait_on_file(self, path):
        """Block the running (main) thread if the file <path> is from an
        archive and has not yet been extracted. Return when the file is
        ready.
        """
        if self.archive_type == None or path == None:
            return

        try:
            name = self._name_table[path]
            with self._condition:
                while not self._extractor.is_ready(name) and not self._stop_waiting:
                    self._condition.wait()
        except Exception as ex:
            log.error('Waiting on extraction of "%s" failed: %s', path, ex)
            return

    def _ask_for_files(self, files):
        """Ask for <files> to be given priority for extraction.
        """
        if self.archive_type == None:
            return

        with self._condition:
            extractor_files = self._extractor.get_files()
            for path in reversed(files):
                name = self._name_table[path]
                if not self._extractor.is_ready(name):
                    extractor_files.remove(name)
                    extractor_files.insert(0, name)
            self._extractor.set_files(extractor_files)

    def thread_delete(self, path):
        """Start a threaded removal of the directory tree rooted at <path>.
        This is to avoid long blockings when removing large temporary dirs.
        """
        del_thread = threading.Thread(target=shutil.rmtree, args=(path, True))
        del_thread.name += '-delete'
        del_thread.setDaemon(False)
        del_thread.start()

    def write_fileinfo_file(self):
        """Write current open file information."""

        if self.file_loaded:
            config = open(constants.FILEINFO_PICKLE_PATH, 'wb')

            path = self._window.imagehandler.get_real_path()
            page_index = self._window.imagehandler.get_current_page() - 1
            current_file_info = [ path, page_index ]

            pickle.dump(current_file_info, config, pickle.HIGHEST_PROTOCOL)
            config.close()

    def read_fileinfo_file(self):
        """Read last loaded file info from disk."""

        fileinfo = None

        if os.path.isfile(constants.FILEINFO_PICKLE_PATH):
            config = None
            try:
                config = open(constants.FILEINFO_PICKLE_PATH, 'rb')

                fileinfo = pickle.load(config)

                config.close()

            except Exception as ex:
                log.error(_('! Corrupt preferences file "%s", deleting...'),
                        constants.FILEINFO_PICKLE_PATH )
                log.info('Error was: %s', ex)
                if config is not None:
                    config.close()
                os.remove(constants.FILEINFO_PICKLE_PATH)

        return fileinfo

    def update_last_read_page(self):
        """ Stores the currently viewed page. """
        if self.archive_type is None or not self.file_loaded:
            return

        archive_path = self.get_path_to_base()
        page = self._window.imagehandler.get_current_page()
        # Do not store first page (first page is default
        # behaviour and would waste space unnecessarily)
        try:
            if page == 1:
                self.last_read_page.clear_page(archive_path)
            else:
                self.last_read_page.set_page(archive_path, page)
        except ValueError:
            # The book no longer exists in the library and has been deleted
            pass


# vim: expandtab:sw=4:ts=4
