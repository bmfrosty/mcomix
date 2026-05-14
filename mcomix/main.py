"""main.py - Main window."""

import sys
import math
import os
import shutil
import threading

from gi.repository import GObject, Gdk, Gtk, GLib

from mcomix import constants
from mcomix import cursor_handler
from mcomix import i18n
from mcomix import icons
from mcomix import enhance_backend
from mcomix import event
from mcomix import file_handler
from mcomix import image_handler
from mcomix import image_tools
from mcomix import lens
from mcomix import preferences
from mcomix.preferences import prefs
from mcomix import ui
from mcomix import slideshow
from mcomix import status
from mcomix import thumbbar
from mcomix import clipboard
from mcomix import pageselect
from mcomix import osd
from mcomix import keybindings
from mcomix import zoom
from mcomix import bookmark_backend
from mcomix import message_dialog
from mcomix import callback
from mcomix.library import backend, main_dialog
from mcomix import tools
from mcomix import box
from mcomix import layout
from mcomix import log
from mcomix.transform import Matrix, Transform
from mcomix.i18n import _

from typing import List

# Number of pages before/after the current page to load in continuous scroll strip
_CONTINUOUS_BEFORE = 1
_CONTINUOUS_AFTER = 3


class MainWindow(Gtk.Window):

    """The main window, is created at start and terminates the
    program when closed.
    """

    def __init__(self, fullscreen=False, is_slideshow=slideshow,
            show_library=False, manga_mode=False, double_page=False,
            zoom_mode=None, open_path=None, open_page=1):
        super(MainWindow, self).__init__(Gtk.WindowType.TOPLEVEL)

        # ----------------------------------------------------------------
        # Attributes
        # ----------------------------------------------------------------
        # Used to detect window fullscreen state transitions.
        self.was_fullscreen = False
        self.is_manga_mode = False
        self.previous_size = (None, None)
        self.was_out_of_focus = False
        self._continuous_strip_pages = []
        self._continuous_y_offsets = []
        self._continuous_scaled_heights = []
        self._continuous_adjusting = False
        self._continuous_vadjust_handler = None
        self._continuous_archive_direction = None  # 'next' or 'previous'
        #: Used to remember if changing to fullscreen enabled 'Hide all'
        self.hide_all_forced = False
        # Remember last scroll destination.
        self._last_scroll_destination = constants.SCROLL_TO_START

        self.layout = layout.create_dummy_layout()
        self.transforms: List[Matrix] = []
        self._spacing = prefs['space between two pages']
        self._waiting_for_redraw = False

        # XXX transitional(kept for osd.py)
        self._image_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 2)

        self._main_layout = Gtk.Layout()
        # Wrap main layout into an event box so
        # we  can change its background color.
        self._event_box = Gtk.EventBox()
        self._event_box.add(self._main_layout)
        self._event_handler = event.EventHandler(self)
        self._vadjust = self._main_layout.get_vadjustment()
        self._hadjust = self._main_layout.get_hadjustment()
        self._scroll = (
            Gtk.Scrollbar.new(Gtk.Orientation.HORIZONTAL, self._hadjust),
            Gtk.Scrollbar.new(Gtk.Orientation.VERTICAL, self._vadjust),
        )

        self.filehandler = file_handler.FileHandler(self)
        self.filehandler.file_closed += self._on_file_closed
        self.filehandler.file_opened += self._on_file_opened
        self.imagehandler = image_handler.ImageHandler(self)
        self.imagehandler.page_available += self._page_available
        self.thumbnailsidebar = thumbbar.ThumbnailSidebar(self)

        self.statusbar = status.Statusbar()
        self.clipboard = clipboard.Clipboard(self)
        self.slideshow = slideshow.Slideshow(self)
        self.cursor_handler = cursor_handler.CursorHandler(self)
        self.enhancer = enhance_backend.ImageEnhancer(self)
        self.lens = lens.MagnifyingLens(self)
        self.osd = osd.OnScreenDisplay(self)
        self.zoom = zoom.ZoomModel()
        self.uimanager = ui.MainUI(self)
        self.menubar = self.uimanager.get_widget('/Menu')
        self.toolbar = self.uimanager.get_widget('/Tool')
        self.popup = self.uimanager.get_widget('/Popup')
        self.actiongroup = self.uimanager.get_action_groups()[0]

        self.images = [Gtk.Image(), Gtk.Image()] # XXX limited to at most 2 pages

        # ----------------------------------------------------------------
        # Setup
        # ----------------------------------------------------------------
        self.set_title(constants.APPNAME)
        self.set_size_request(300, 300)  # Avoid making the window *too* small

        # Hook up keyboard shortcuts
        self._event_handler.register_key_events()

        # This is a hack to get the focus away from the toolbar so that
        # we don't activate it with space or some other key (alternative?)
        self.toolbar.set_focus_child(
            self.uimanager.get_widget('/Tool/expander'))
        self.toolbar.set_style(Gtk.ToolbarStyle.ICONS)
        self.toolbar.set_icon_size(Gtk.IconSize.LARGE_TOOLBAR)

        for img in self.images:
            self._main_layout.put(img, 0, 0)
        self.set_bg_colour(prefs['bg colour'])

        self._vadjust.step_increment = 15
        self._vadjust.page_increment = 1
        self._hadjust.step_increment = 15
        self._hadjust.page_increment = 1

        table = Gtk.Table(2, 2, False)
        table.attach(self.thumbnailsidebar, 0, 1, 2, 5, Gtk.AttachOptions.FILL,
            Gtk.AttachOptions.FILL|Gtk.AttachOptions.EXPAND, 0, 0)

        table.attach(self._event_box, 1, 2, 2, 3, Gtk.AttachOptions.FILL|Gtk.AttachOptions.EXPAND,
            Gtk.AttachOptions.FILL|Gtk.AttachOptions.EXPAND, 0, 0)
        table.attach(self._scroll[constants.PageAxis.HEIGHT], 2, 3, 2, 3, Gtk.AttachOptions.FILL|Gtk.AttachOptions.SHRINK,
            Gtk.AttachOptions.FILL|Gtk.AttachOptions.SHRINK, 0, 0)
        table.attach(self._scroll[constants.PageAxis.WIDTH], 1, 2, 4, 5, Gtk.AttachOptions.FILL|Gtk.AttachOptions.SHRINK,
            Gtk.AttachOptions.FILL, 0, 0)
        table.attach(self.menubar, 0, 3, 0, 1, Gtk.AttachOptions.FILL|Gtk.AttachOptions.SHRINK,
            Gtk.AttachOptions.FILL, 0, 0)
        table.attach(self.toolbar, 0, 3, 1, 2, Gtk.AttachOptions.FILL|Gtk.AttachOptions.SHRINK,
            Gtk.AttachOptions.FILL, 0, 0)
        table.attach(self.statusbar, 0, 3, 5, 6, Gtk.AttachOptions.FILL|Gtk.AttachOptions.SHRINK,
            Gtk.AttachOptions.FILL, 0, 0)

        if prefs['continuous scroll']:
            self.actiongroup.get_action('continuous_scroll').activate()

        if (prefs['default double page'] or double_page) and not prefs['continuous scroll']:
            self.actiongroup.get_action('double_page').activate()

        if prefs['default manga mode'] or manga_mode:
            self.actiongroup.get_action('manga_mode').activate()

        # Determine zoom mode. If zoom_mode is passed, it overrides
        # the zoom mode preference.
        zoom_actions = { constants.ZoomMode.BEST: 'best_fit_mode',
                constants.ZoomMode.WIDTH: 'fit_width_mode',
                constants.ZoomMode.HEIGHT: 'fit_height_mode',
                constants.ZoomMode.SIZE: 'fit_size_mode',
                constants.ZoomMode.MANUAL: 'fit_manual_mode' }

        if zoom_mode is not None:
            zoom_action = zoom_actions[zoom_mode]
        else:
            zoom_action = zoom_actions[prefs['zoom mode']]

        if zoom_action == 'fit_manual_mode':
            # This little ugly hack is to get the activate call on
            # 'fit_manual_mode' to actually create an event (and callback).
            # Since manual mode is the default selected radio button action
            # it won't send an event if we activate it when it is already
            # the selected one.
            self.actiongroup.get_action('best_fit_mode').activate()

        self.actiongroup.get_action(zoom_action).activate()

        if prefs['stretch']:
            self.actiongroup.get_action('stretch').activate()

        if prefs['invert smart scroll']:
            self.actiongroup.get_action('invert_scroll').activate()

        if prefs['keep transformation']:
            prefs['keep transformation'] = False
            self.actiongroup.get_action('keep_transformation').activate()
        else:
            prefs['rotation'] = 0
            prefs['vertical flip'] = False
            prefs['horizontal flip'] = False

        # List of "toggles" than can be shown/hidden by the user.
        self._toggle_list = (
            # Preference        Action        Widget(s)
            ('show menubar'   , 'menubar'   , (self.menubar,)         ),
            ('show scrollbar' , 'scrollbar' , self._scroll            ),
            ('show statusbar' , 'statusbar' , (self.statusbar,)       ),
            ('show thumbnails', 'thumbnails', (self.thumbnailsidebar,)),
            ('show toolbar'   , 'toolbar'   , (self.toolbar,)         ),
        )

        # Each "toggle" widget "eats" part of the main layout visible area.
        self._toggle_axis = {
            self.thumbnailsidebar              : constants.PageAxis.WIDTH,
            self._scroll[constants.PageAxis.HEIGHT]: constants.PageAxis.WIDTH,
            self._scroll[constants.PageAxis.WIDTH] : constants.PageAxis.HEIGHT,
            self.statusbar                     : constants.PageAxis.HEIGHT,
            self.toolbar                       : constants.PageAxis.HEIGHT,
            self.menubar                       : constants.PageAxis.HEIGHT,
        }

        # Start with all "toggle" widgets hidden to avoid ugly transitions.
        for preference, action, widget_list in self._toggle_list:
            for widget in widget_list:
                widget.hide()

        toggleaction = self.actiongroup.get_action('hide_all')
        toggleaction.set_active(prefs['hide all'])

        # Sync each "toggle" widget active state with its preference.
        for preference, action, widget_list in self._toggle_list:
            self.actiongroup.get_action(action).set_active(prefs[preference])

        self.actiongroup.get_action('menu_autorotate_width').set_sensitive(False)
        self.actiongroup.get_action('menu_autorotate_height').set_sensitive(False)

        self.add(table)
        table.show()
        self._event_box.show_all()

        self._main_layout.set_events(Gdk.EventMask.BUTTON1_MOTION_MASK |
                                     Gdk.EventMask.BUTTON2_MOTION_MASK |
                                     Gdk.EventMask.BUTTON_PRESS_MASK |
                                     Gdk.EventMask.BUTTON_RELEASE_MASK |
                                     Gdk.EventMask.POINTER_MOTION_MASK)

        self._main_layout.drag_dest_set(Gtk.DestDefaults.ALL,
                                        [Gtk.TargetEntry.new('text/uri-list', 0, 0)],
                                        Gdk.DragAction.COPY |
                                        Gdk.DragAction.MOVE)

        self.connect('focus-in-event', self.gained_focus)
        self.connect('focus-out-event', self.lost_focus)
        self.connect('delete_event', self.close_program)
        self.connect('key_press_event', self._event_handler.key_press_event)
        self.connect('key_release_event', self._event_handler.key_release_event)
        self.connect('configure_event', self._event_handler.resize_event)
        self.connect('window-state-event', self._event_handler.window_state_event)

        self._main_layout.connect('button_release_event',
            self._event_handler.mouse_release_event)
        self._main_layout.connect('scroll_event',
            self._event_handler.scroll_wheel_event)
        self._main_layout.connect('button_press_event',
            self._event_handler.mouse_press_event)
        self._main_layout.connect('motion_notify_event',
            self._event_handler.mouse_move_event)
        self._main_layout.connect('drag_data_received',
            self._event_handler.drag_n_drop_event)

        self.uimanager.set_sensitivities()
        # Restore twice, before and after show(), to make sure it works...
        self.restore_window_geometry()
        self.show()
        self.restore_window_geometry()

        if prefs['default fullscreen'] or fullscreen:
            toggleaction = self.actiongroup.get_action('fullscreen')
            toggleaction.set_active(True)


        if prefs['previous quit was quit and save']:
            fileinfo = self.filehandler.read_fileinfo_file()

            if fileinfo != None:

                open_path = fileinfo[0]
                open_page = fileinfo[1] + 1

        prefs['previous quit was quit and save'] = False

        if open_path is not None:
            self.filehandler.open_file(open_path)

        if is_slideshow:
            self.actiongroup.get_action('slideshow').activate()

        if show_library:
            self.actiongroup.get_action('library').activate()

        self.cursor_handler.auto_hide_on()
        # Make sure we receive *all* mouse motion events,
        # even if a modal dialog is being shown.
        def _on_event(event):
            if Gdk.EventType.MOTION_NOTIFY == event.type:
                self.cursor_handler.refresh()
            Gtk.main_do_event(event)
        Gdk.event_handler_set(_on_event)

    def gained_focus(self, *args):
        def _delayed_unset_out_of_focus(_):
            self.was_out_of_focus = False
            return False

        if self.was_out_of_focus:
            # Since clicking into an unfocused window triggers the
            # focus event first, then the mouse event, the mouse event
            # can no longer detect that it should be skipped. Thus, delay
            # unsetting was_out_of_focus.
            GLib.idle_add(_delayed_unset_out_of_focus, None,
                          priority=GLib.PRIORITY_DEFAULT_IDLE)

    def lost_focus(self, *args):
        self.was_out_of_focus = True

        # If the user presses CTRL for a keyboard shortcut, e.g. to
        # open the library, key_release_event isn't fired and force_single_step
        # isn't properly unset.
        self.imagehandler.force_single_step = False

    def draw_image(self, scroll_to=None):
        """Draw the current pages and update the titlebar and statusbar.
        """
        # FIXME: what if scroll_to is different?
        if not self._waiting_for_redraw:  # Don't stack up redraws.
            self._waiting_for_redraw = True
            GLib.idle_add(self._draw_image, scroll_to,
                             priority=GLib.PRIORITY_HIGH_IDLE)

    def _update_toggle_preference(self, preference, toggleaction):
        ''' Update "toggle" widget corresponding <preference>.

        Note: the widget visibily itself is left unchanged. '''
        prefs[preference] = toggleaction.get_active()
        if 'hide all' == preference:
            self._update_toggles_sensitivity()
        # Since the size of the drawing area is dependent
        # on the visible "toggles", redraw the page.
        self.draw_image()

    def _should_toggle_be_visible(self, preference):
        ''' Return <True> if "toggle" widget for <preference> should be visible. '''
        if self.is_fullscreen:
            visible = not prefs['hide all in fullscreen']
        else:
            visible = not prefs['hide all']
        visible &= prefs[preference]
        if 'show thumbnails' == preference:
            visible &= self.filehandler.file_loaded
            visible &= self.imagehandler.get_number_of_pages() > 0
        return visible

    def _update_toggles_sensitivity(self):
        ''' Update each "toggle" widget sensitivity. '''
        sensitive = True
        if prefs['hide all']:
            sensitive = False
        elif prefs['hide all in fullscreen'] and self.is_fullscreen:
            sensitive = False
        for preference, action, widget_list in self._toggle_list:
            self.actiongroup.get_action(action).set_sensitive(sensitive)

    def _update_toggles_visibility(self):
        ''' Update each "toggle" widget visibility. '''
        for preference, action, widget_list in self._toggle_list:
            should_be_visible = self._should_toggle_be_visible(preference)
            for widget in widget_list:
                # No change in visibility?
                if should_be_visible != widget.get_visible():
                    (widget.show if should_be_visible else widget.hide)()

    def _draw_image(self, scroll_to):

        self._update_toggles_visibility()

        self.osd.clear()

        # _continuous_adjusting suppresses _on_continuous_scroll for the
        # duration of the render, including the synchronous value-changed that
        # GTK fires inside set_size() before _continuous_strip_pages is updated.
        if prefs['continuous scroll']:
            self._continuous_adjusting = True
            result = self._draw_image_continuous(scroll_to)
            self._continuous_adjusting = False
            self._waiting_for_redraw = False
            return result

        if not self.filehandler.file_loaded:
            self._clear_main_area()
            self._waiting_for_redraw = False
            return False

        if self.imagehandler.page_is_available():
            distribution_axis = constants.DISTRIBUTION_AXIS
            alignment_axis = constants.ALIGNMENT_AXIS
            pixbuf_count = 2 if self.displayed_double() else 1 # XXX limited to at most 2 pages
            pixbuf_list = list(self.imagehandler.get_pixbufs(pixbuf_count))
            do_not_transform = [image_tools.is_animation(x) for x in pixbuf_list]
            size_list = [[pixbuf.get_width(), pixbuf.get_height()]
                         for pixbuf in pixbuf_list]

            if self.is_manga_mode:
                orientation = constants.MANGA_ORIENTATION
            else:
                orientation = constants.WESTERN_ORIENTATION

            # Rotation handling:
            # - apply Exif rotation on individual images
            # - apply automatic rotation (size based) on whole page
            # - apply manual rotation on whole page
            if prefs['auto rotate from exif']:
                rotation_list = [image_tools.get_implied_rotation(pixbuf)
                                 for pixbuf in pixbuf_list]
            else:
                rotation_list = [0] * len(pixbuf_list)
            virtual_size = [0, 0]
            for i in range(pixbuf_count):
                if tools.rotation_swaps_axes(rotation_list[i]):
                    size_list[i].reverse()
                size = size_list[i]
                virtual_size[distribution_axis] += size[distribution_axis]
                virtual_size[alignment_axis] = max(virtual_size[alignment_axis],
                                                   size[alignment_axis])
            rotation = tools.compile_rotations(
                image_tools.get_size_rotation(*virtual_size), prefs['rotation'])
            if tools.rotation_swaps_axes(rotation):
                distribution_axis, alignment_axis = alignment_axis, distribution_axis
                orientation = list(orientation)
                orientation.reverse() # 2D only
                for i in range(pixbuf_count):
                    if do_not_transform[i]:
                        continue
                    size_list[i].reverse() # 2D only
            if rotation in (180, 270):
                orientation = tools.vector_opposite(orientation)
            for i in range(pixbuf_count):
                rotation_list[i] = tools.compile_rotations(rotation_list[i], rotation)
            if prefs['vertical flip'] and tools.rotation_swaps_axes(rotation):
                orientation = tools.vector_opposite(orientation)
            if prefs['horizontal flip'] and not tools.rotation_swaps_axes(rotation):
                orientation = tools.vector_opposite(orientation)

            self.layout = layout.FiniteLayout.create_finite_layout(
                pixbuf_count, orientation, self._spacing, distribution_axis,
                alignment_axis, self._show_scrollbars, self.get_visible_area_size,
                lambda zoom_dummy_size: self.zoom.get_zoomed_size(size_list, zoom_dummy_size,
                distribution_axis, do_not_transform,
                prefs['double page autoresize'] in (constants.DOUBLE_PAGE_AUTORESIZE_SIZE,
                constants.DOUBLE_PAGE_AUTORESIZE_FIT_SIZE),
                prefs['double page autoresize'] == constants.DOUBLE_PAGE_AUTORESIZE_FIT_SIZE))
            content_boxes = self.layout.get_content_boxes()
            scaled_sizes = list(map(box.Box.get_size, content_boxes))

            self.transforms = [Transform.ID] * pixbuf_count
            for i in range(pixbuf_count):
                if do_not_transform[i]:
                    continue
                pixbuf_list[i] = image_tools.fit_pixbuf_to_rectangle(
                    pixbuf_list[i], scaled_sizes[i], rotation_list[i])
                self.transforms[i] += Transform.from_rotation(rotation_list[i]) # FIXME also include scales

            for i in range(pixbuf_count):
                if do_not_transform[i]:
                    continue
                if prefs['horizontal flip']:
                    pixbuf_list[i] = image_tools.flip_pixbuf(pixbuf_list[i], 0)
                    self.transforms[i] += Transform.from_flips(True, False)
                if prefs['vertical flip']:
                    pixbuf_list[i] = image_tools.flip_pixbuf(pixbuf_list[i], 1)
                    self.transforms[i] += Transform.from_flips(False, True)
                pixbuf_list[i] = self.enhancer.enhance(pixbuf_list[i])

            for i in range(pixbuf_count):
                image_tools.set_from_pixbuf(self.images[i], pixbuf_list[i])

            scales = tuple(map(lambda x, y: math.sqrt(tools.div(
                tools.volume(x), tools.volume(y))), scaled_sizes, size_list))

            resolutions = tuple(map(lambda sz, sc, ds: sz + [sc, ds], size_list,
                scales, self.layout.get_content_distorted()))
            if self.is_manga_mode:
                resolutions = tuple(reversed(resolutions))
            self.statusbar.set_resolution(resolutions)
            self.statusbar.update()

            smartbg = prefs['smart bg']
            smartthumbbg = prefs['smart thumb bg'] and prefs['show thumbnails']
            if smartbg or smartthumbbg:
                bg_colour = self.imagehandler.get_pixbuf_auto_background(pixbuf_count)
            if smartbg:
                self.set_bg_colour(bg_colour)
            if smartthumbbg:
                self.thumbnailsidebar.change_thumbnail_background_color(bg_colour)

            self._main_layout.get_bin_window().freeze_updates()

            self._main_layout.set_size(*(self.layout.get_union_box().get_size()))
            for i in range(pixbuf_count):
                self._main_layout.move(self.images[i],
                    *content_boxes[i].get_position())

            for i in range(pixbuf_count):
                self.images[i].show()
            for i in range(pixbuf_count, len(self.images)):
                self.images[i].hide()

            # Reset orientation so scrolling behaviour is sane.
            if self.is_manga_mode:
                self.layout.set_orientation(constants.MANGA_ORIENTATION)
            else:
                self.layout.set_orientation(constants.WESTERN_ORIENTATION)

            if scroll_to is not None:
                destination = (scroll_to,) * 2
                if constants.SCROLL_TO_START == scroll_to:
                    index = constants.FIRST_INDEX
                elif constants.SCROLL_TO_END == scroll_to:
                    index = constants.LAST_INDEX
                else:
                    index = None
                self.scroll_to_predefined(destination, index)

            self._main_layout.get_bin_window().thaw_updates()
        else:
            # Save scroll destination for when the page becomes available.
            self._last_scroll_destination = scroll_to
            # If the pixbuf for the current page(s) isn't available,
            # hide all images to clear any old pixbufs.
            # XXX How about calling self._clear_main_area?
            for i in range(len(self.images)):
                self.images[i].hide()
            self._show_scrollbars([False] * len(self._scroll))

        self._waiting_for_redraw = False

        return False

    def _update_page_information(self) -> None:
        """ Updates the window with information that can be gathered
        even when the page pixbuf(s) aren't ready yet. """

        page_number = self.imagehandler.get_current_page()
        if not page_number:
            return
        double = self.displayed_double()

        def make_status(info):
            if not isinstance(info, tuple):
                return info
            if self.is_manga_mode:
                info = reversed(info)
            return ", ".join(info)

        filename = make_status(self.imagehandler.get_page_filename(double=double))
        if filename is None:
            return
        filesize = make_status(self.imagehandler.get_page_filesize(double=double))
        self.statusbar.set_page_number(page_number,
                                       self.imagehandler.get_number_of_pages(),
                                       2 if double else 1)
        self.statusbar.set_filename(filename)
        self.statusbar.set_root(self.filehandler.get_base_filename())
        self.statusbar.set_filesize(filesize)
        self.statusbar.update()
        self.update_title()

    def update_icon(self, default=False):
        if (self.filehandler.archive_type is not None
            and prefs['archive thumbnail as icon']):
            pixbuf = self.imagehandler.get_thumbnail(1, 48, 48)
            pixbuf = self.enhancer.enhance(pixbuf)
            self.set_icon(pixbuf)
        elif (default):
            self.set_icon_list(icons.mcomix_icons())

    def _page_available(self, page):
        """ Called whenever a new page is ready for displaying. """
        # In continuous scroll mode, redraw whenever any page in the visible
        # strip becomes ready, not just the current page. The empty-strip
        # fallback handles the first page of a newly opened archive before
        # the strip has been built.
        if prefs['continuous scroll']:
            current_page = self.imagehandler.get_current_page()
            if page in self._continuous_strip_pages or (
                    not self._continuous_strip_pages and page == current_page):
                self.draw_image(scroll_to=self._last_scroll_destination)
                self._update_page_information()
        else:
            current_page = self.imagehandler.get_current_page()
            nb_pages = 2 if self.displayed_double() else 1
            if current_page <= page < (current_page + nb_pages):
                self.draw_image(scroll_to=self._last_scroll_destination)
                self._update_page_information()

        # Use first page as application icon when opening archives.
        if page == 1:
            self.update_icon(False)

    def _on_file_opened(self):
        # Reset the strip on every archive open. Scroll destination is set from
        # _continuous_archive_direction so that crossing into a previous archive
        # lands at the bottom rather than the top.
        if prefs['continuous scroll']:
            self._continuous_strip_pages = []
            self._continuous_y_offsets = []
            self._continuous_scaled_heights = []
            if self._continuous_archive_direction == 'previous':
                self._last_scroll_destination = constants.SCROLL_TO_END
            else:
                self._last_scroll_destination = constants.SCROLL_TO_START
            # Keep _continuous_archive_direction set until _draw_image_continuous
            # successfully renders, so intermediate SCROLL_TO_START calls from
            # set_page/new_page don't overwrite the intended destination.
        self.uimanager.set_sensitivities()
        number, count = self.filehandler.get_file_number()
        self.statusbar.set_file_number(number, count)
        self.statusbar.update()

    def _on_file_closed(self):
        self._continuous_strip_pages = []
        self._continuous_y_offsets = []
        self._continuous_scaled_heights = []
        self.clear()
        self.thumbnailsidebar.hide()
        self.thumbnailsidebar.clear()
        self.uimanager.set_sensitivities()
        self.set_icon_list(icons.mcomix_icons())

    def new_page(self, at_bottom=False):
        """Draw a *new* page correctly (as opposed to redrawing the same
        image with a new size or whatever).
        """
        if not prefs['keep transformation']:
            prefs['rotation'] = 0
            prefs['horizontal flip'] = False
            prefs['vertical flip'] = False

        if at_bottom:
            scroll_to = constants.SCROLL_TO_END
        else:
            scroll_to = constants.SCROLL_TO_START

        self.draw_image(scroll_to=scroll_to)

    @callback.Callback
    def page_changed(self):
        """ Called on page change. """
        self.thumbnailsidebar.load_thumbnails()
        self._update_page_information()

    def set_page(self, num, at_bottom=False):
        if num == self.imagehandler.get_current_page():
            return
        self.imagehandler.set_page(num)
        self.page_changed()
        self.new_page(at_bottom=at_bottom)
        self.slideshow.update_delay()

    def next_book(self):
        if prefs['continuous scroll']:
            self._continuous_archive_direction = 'next'
        archive_open = self.filehandler.archive_type is not None
        next_archive_opened = False
        if (self.slideshow.is_running() and \
            prefs['slideshow can go to next archive']) or \
           prefs['auto open next archive']:
            next_archive_opened = self.filehandler._open_next_archive()

        # If "Auto open next archive" is disabled, do not go to the next
        # directory if current file was an archive.
        if not next_archive_opened and \
           prefs['auto open next directory'] and \
           (not archive_open or prefs['auto open next archive']):
            self.filehandler.open_next_directory()

    def previous_book(self):
        if prefs['continuous scroll']:
            self._continuous_archive_direction = 'previous'
        archive_open = self.filehandler.archive_type is not None
        previous_archive_opened = False
        if (self.slideshow.is_running() and \
            prefs['slideshow can go to next archive']) or \
            prefs['auto open next archive']:
            previous_archive_opened = self.filehandler._open_previous_archive()

        # If "Auto open next archive" is disabled, do not go to the previous
        # directory if current file was an archive.
        if not previous_archive_opened and \
            prefs['auto open next directory'] and \
            (not archive_open or prefs['auto open next archive']):
            self.filehandler.open_previous_directory()

    def flip_page(self, step, single_step=False):

        if not self.filehandler.file_loaded:
            return

        current_page = self.imagehandler.get_current_page()
        number_of_pages = self.imagehandler.get_number_of_pages()

        new_page = current_page + step
        if (1 == abs(step) and
            not single_step and
            prefs['default double page'] and
            prefs['double step in double page mode']):
            if +1 == step and not self.imagehandler.get_virtual_double_page():
                new_page += 1
            elif -1 == step and not self.imagehandler.get_virtual_double_page(new_page - 1):
                new_page -= 1

        if new_page <= 0:
            # Only switch to previous page when flipping one page before the
            # first one. (Note: check for (page number <= 1) to handle empty
            # archive case).
            if -1 == step and current_page <= 1:
                return self.previous_book()
            # Handle empty archive case.
            new_page = min(1, number_of_pages)
        elif new_page > number_of_pages:
            if 1 == step:
                return self.next_book()
            new_page = number_of_pages

        if new_page != current_page:
            self.set_page(new_page, at_bottom=(-1 == step))

    def first_page(self):
        number_of_pages = self.imagehandler.get_number_of_pages()
        if number_of_pages:
            self.set_page(1)

    def last_page(self):
        number_of_pages = self.imagehandler.get_number_of_pages()
        if number_of_pages:
            self.set_page(number_of_pages)

    def page_select(self, *args):
        pageselect.Pageselector(self)

    def rotate_90(self, *args):
        prefs['rotation'] = tools.compile_rotations(prefs['rotation'], 90)
        self.draw_image()

    def rotate_180(self, *args):
        prefs['rotation'] = tools.compile_rotations(prefs['rotation'], 180)
        self.draw_image()

    def rotate_270(self, *args):
        prefs['rotation'] = tools.compile_rotations(prefs['rotation'], 270)
        self.draw_image()

    def flip_horizontally(self, *args):
        prefs['horizontal flip'] = not prefs['horizontal flip']
        self.draw_image()

    def flip_vertically(self, *args):
        prefs['vertical flip'] = not prefs['vertical flip']
        self.draw_image()

    def change_double_page(self, toggleaction):
        prefs['default double page'] = toggleaction.get_active()
        self._update_page_information()
        self.draw_image()

    def change_manga_mode(self, toggleaction):
        prefs['default manga mode'] = toggleaction.get_active()
        self.is_manga_mode = toggleaction.get_active()
        self._update_page_information()
        self.draw_image()

    def change_continuous_scroll(self, toggleaction):
        # Wire or unwire the vadjust signal that drives the continuous strip.
        # Double-page mode is incompatible, so disable it when enabling this.
        # Clearing the strip on disable prevents stale scroll events from firing
        # against old strip data after the next draw_image uses the normal path.
        prefs['continuous scroll'] = toggleaction.get_active()
        if prefs['continuous scroll']:
            self.actiongroup.get_action('double_page').set_active(False)
            if self._continuous_vadjust_handler is None:
                self._continuous_vadjust_handler = self._vadjust.connect(
                    'value-changed', self._on_continuous_scroll)
        else:
            if self._continuous_vadjust_handler is not None:
                self._vadjust.disconnect(self._continuous_vadjust_handler)
                self._continuous_vadjust_handler = None
            self._continuous_strip_pages = []
        self._update_page_information()
        self.draw_image()

    def _draw_image_continuous(self, scroll_to):
        """Render a vertical strip of pages for continuous scroll mode."""
        if not self.filehandler.file_loaded:
            self._clear_main_area()
            return False

        n_total = self.imagehandler.get_number_of_pages()
        current_page = self.imagehandler.get_current_page()

        if not current_page or not n_total:
            return False

        # If a direction-based scroll is pending from an archive transition,
        # override any intermediate SCROLL_TO_START from set_page/new_page.
        if self._continuous_archive_direction is not None:
            scroll_to = (constants.SCROLL_TO_END
                         if self._continuous_archive_direction == 'previous'
                         else constants.SCROLL_TO_START)

        if not self.imagehandler.page_is_available():
            self._last_scroll_destination = scroll_to
            for img in self.images:
                img.hide()
            self._show_scrollbars([False, False])
            return False

        # Build the page range: _CONTINUOUS_BEFORE pages before current,
        # _CONTINUOUS_AFTER pages after, clamped to archive bounds.
        first_page = max(1, current_page - _CONTINUOUS_BEFORE)
        last_page = min(n_total, current_page + _CONTINUOUS_AFTER)
        strip_pages = list(range(first_page, last_page + 1))
        n_strip = len(strip_pages)

        # Grow the image widget pool on demand; widgets are never removed,
        # just hidden, to avoid GTK allocation churn.
        while len(self.images) < n_strip:
            img = Gtk.Image()
            self._main_layout.put(img, 0, 0)
            self.images.append(img)

        vis_width, vis_height = self.get_visible_area_size()
        if vis_width <= 0:
            vis_width = 1
        if vis_height <= 0:
            vis_height = 1
        # Scale every page to the same width: half the viewport height.
        # This keeps the strip readable without filling the screen horizontally.
        target_width = max(1, vis_height // 2)

        scaled_pixbufs = []
        y_offsets = []
        scaled_heights = []
        orig_sizes = []
        y = 0

        # Scale and transform each page, recording y positions as we go.
        for i, page in enumerate(strip_pages):
            if self.imagehandler.page_is_available(page):
                pixbuf = self.imagehandler._get_pixbuf(page - 1)
            else:
                pixbuf = image_tools.MISSING_IMAGE_ICON

            is_anim = image_tools.is_animation(pixbuf)

            if prefs['auto rotate from exif'] and not is_anim:
                img_rotation = image_tools.get_implied_rotation(pixbuf)
            else:
                img_rotation = 0
            rotation = tools.compile_rotations(img_rotation, prefs['rotation'])

            orig_w = pixbuf.get_width()
            orig_h = pixbuf.get_height()
            if tools.rotation_swaps_axes(rotation):
                orig_w, orig_h = orig_h, orig_w

            scale = target_width / orig_w if orig_w > 0 else 1.0
            scaled_w = max(1, int(orig_w * scale))
            scaled_h = max(1, int(orig_h * scale))

            if not is_anim:
                pixbuf = image_tools.fit_pixbuf_to_rectangle(
                    pixbuf, [scaled_w, scaled_h], rotation)
                if prefs['horizontal flip']:
                    pixbuf = image_tools.flip_pixbuf(pixbuf, 0)
                if prefs['vertical flip']:
                    pixbuf = image_tools.flip_pixbuf(pixbuf, 1)
                pixbuf = self.enhancer.enhance(pixbuf)

            image_tools.set_from_pixbuf(self.images[i], pixbuf)
            scaled_pixbufs.append(pixbuf)
            y_offsets.append(y)
            scaled_heights.append(scaled_h)
            orig_sizes.append((orig_w, orig_h))
            y += scaled_h

        total_height = max(1, y) if n_strip > 0 else 1

        # freeze_updates / thaw_updates suppresses GTK redraws while we
        # reposition widgets, preventing visible tearing. Note: it does NOT
        # suppress signal emission, so value-changed can still fire during
        # set_size(); _continuous_adjusting guards against that.
        self._main_layout.get_bin_window().freeze_updates()
        self._main_layout.set_size(vis_width, total_height)

        # Centre each image horizontally; hide unused pool widgets.
        for i in range(n_strip):
            actual_w = scaled_pixbufs[i].get_width()
            x_off = max(0, (vis_width - actual_w) // 2)
            self._main_layout.move(self.images[i], x_off, y_offsets[i])
            self.images[i].show()

        for i in range(n_strip, len(self.images)):
            self.images[i].hide()

        self._show_scrollbars([False, True])

        # Update statusbar with the scale of the current page.
        if current_page in strip_pages:
            curr_idx = strip_pages.index(current_page)
            ow, oh = orig_sizes[curr_idx]
            if oh > 0:
                curr_scale = scaled_heights[curr_idx] / oh
                resolutions = ([ow, oh, curr_scale, False],)
                self.statusbar.set_resolution(resolutions)
        self.statusbar.set_page_number(current_page, n_total, 1)
        self.statusbar.update()
        self.update_title()

        # Commit the new strip geometry so _on_continuous_scroll sees fresh data.
        self._continuous_strip_pages = strip_pages
        self._continuous_y_offsets = y_offsets
        self._continuous_scaled_heights = scaled_heights

        # Scroll to the requested position within the current page.
        # SCROLL_TO_END shows the bottom edge; anything else shows the top.
        if scroll_to is not None:
            current_idx = strip_pages.index(current_page) if current_page in strip_pages else 0
            current_y = y_offsets[current_idx]

            if scroll_to == constants.SCROLL_TO_END:
                new_vadjust = max(current_y,
                    current_y + scaled_heights[current_idx] - vis_height)
            else:
                new_vadjust = current_y

            max_vadjust = max(0, total_height - vis_height)
            new_vadjust = max(0, min(max_vadjust, new_vadjust))
            self._vadjust.set_value(new_vadjust)
            self._hadjust.set_value(0)
            # Archive direction has been consumed — clear it now.
            self._continuous_archive_direction = None

        self._main_layout.get_bin_window().thaw_updates()
        return False

    def _on_continuous_scroll(self, adjustment):
        """Called when the viewport scrolls in continuous scroll mode."""
        # _continuous_adjusting is True while we are programmatically moving
        # the scroll position (strip rebuild, set_value, etc.).  Ignore those
        # events to avoid re-entrant strip updates.
        if self._continuous_adjusting or not self._continuous_strip_pages:
            return
        if not prefs['continuous scroll']:
            return

        scroll_y = adjustment.get_value()
        vis_height = self.get_visible_area_size()[1]

        # Determine which page occupies the top of the viewport: the last page
        # whose y_offset is at or above the current scroll position.
        visible_page = self._continuous_strip_pages[0]
        for i, page in enumerate(self._continuous_strip_pages):
            if scroll_y >= self._continuous_y_offsets[i]:
                visible_page = page

        first_in_strip = self._continuous_strip_pages[0]
        last_in_strip = self._continuous_strip_pages[-1]
        n_total = self.imagehandler.get_number_of_pages()

        # Keep current_page in sync with what the user is actually looking at.
        # _continuous_adjusting suppresses the value-changed re-entry that
        # do_cacheing / page_changed may indirectly trigger.
        current_page = self.imagehandler.get_current_page()
        if visible_page != current_page:
            self._continuous_adjusting = True
            self.imagehandler._current_image_index = visible_page - 1
            self.imagehandler.do_cacheing()
            self._last_scroll_destination = None
            self.page_changed()
            self._continuous_adjusting = False

        # Extend the strip when the viewport is approaching either edge.
        # Lower edge: viewport bottom is past the start of the last strip page
        # and there are more archive pages to append.
        # Upper edge: viewport top is within the first strip page and there are
        # archive pages to prepend.
        viewport_bottom = scroll_y + vis_height
        last_page_top = self._continuous_y_offsets[-1]
        if (viewport_bottom >= last_page_top and last_in_strip < n_total
                and not self._waiting_for_redraw):
            self._schedule_strip_extend()
        elif (scroll_y <= self._continuous_y_offsets[0] + self._continuous_scaled_heights[0]
                and first_in_strip > 1
                and not self._waiting_for_redraw):
            self._schedule_strip_extend()

    def _schedule_strip_extend(self):
        """Schedule a strip re-render without blocking the scroll event."""
        if not self._waiting_for_redraw:
            self._waiting_for_redraw = True
            GLib.idle_add(self._do_extend_strip, priority=GLib.PRIORITY_HIGH_IDLE)

    def _do_extend_strip(self):
        """Re-render the continuous strip, preserving exactly what's at the viewport top."""
        # Snapshot the viewport-top reference BEFORE re-rendering.
        # We record which page is at the top of the viewport and how many pixels
        # into that page the scroll position sits.  After the strip is rebuilt we
        # restore that same content to the same screen position, regardless of how
        # much current_page may have drifted since _schedule_strip_extend() fired.
        old_vadjust = self._vadjust.get_value()
        ref_page = None
        ref_offset = old_vadjust
        if self._continuous_strip_pages and self._continuous_y_offsets:
            ref_page = self._continuous_strip_pages[0]
            ref_offset = old_vadjust
            for i, page in enumerate(self._continuous_strip_pages):
                if old_vadjust >= self._continuous_y_offsets[i]:
                    ref_page = page
                    ref_offset = old_vadjust - self._continuous_y_offsets[i]

        # Hold _continuous_adjusting so that the set_size() call inside
        # _draw_image_continuous doesn't fire _on_continuous_scroll re-entrantly.
        self._continuous_adjusting = True
        self._draw_image_continuous(None)

        # Restore scroll so the reference page stays at the same screen position.
        # If ref_page is no longer in the new strip (edge case), leave scroll as-is.
        if ref_page is not None and ref_page in self._continuous_strip_pages:
            ref_idx = self._continuous_strip_pages.index(ref_page)
            new_vadjust = self._continuous_y_offsets[ref_idx] + ref_offset
            if self._continuous_y_offsets and self._continuous_scaled_heights:
                total_h = self._continuous_y_offsets[-1] + self._continuous_scaled_heights[-1]
                vis_height = self.get_visible_area_size()[1]
                max_vadjust = max(0, total_h - vis_height)
                self._vadjust.set_value(max(0, min(max_vadjust, new_vadjust)))

        self._continuous_adjusting = False
        self._waiting_for_redraw = False
        return False

    def change_invert_scroll(self, toggleaction):
        prefs['invert smart scroll'] = toggleaction.get_active()

    @property
    def is_fullscreen(self):
        window_state = self.get_window().get_state()
        return 0 != (window_state & Gdk.WindowState.FULLSCREEN)

    def change_fullscreen(self, toggleaction):
        # Disable action until transition if complete.
        toggleaction.set_sensitive(False)
        if toggleaction.get_active():
            if self.previous_size != (None, None):
                self.save_window_geometry()
            self.fullscreen()
        else:
            self.unfullscreen()
        # No need to call draw_image explicitely,
        # as we'll be receiving a window state
        # change or resize event.

    def change_invert_color(self, toggleaction):
        prefs['invert color'] = not self.enhancer.invert_color
        self.enhancer.invert_color = prefs['invert color']
        self.enhancer.signal_update()

    def change_zoom_mode(self, radioaction=None, *args):
        if radioaction:
            prefs['zoom mode'] = radioaction.get_current_value()
        self.zoom.set_fit_mode(prefs['zoom mode'])
        self.zoom.set_scale_up(prefs['stretch'])
        self.zoom.reset_user_zoom()
        self.draw_image()

    def change_autorotation(self, radioaction=None, *args):
        """ Switches between automatic rotation modes, depending on which
        radiobutton is currently activated. """
        if radioaction:
            prefs['auto rotate depending on size'] = radioaction.get_current_value()
        self.draw_image()

    def change_stretch(self, toggleaction, *args):
        """ Toggles stretching small images. """
        prefs['stretch'] = toggleaction.get_active()
        self.zoom.set_scale_up(prefs['stretch'])
        self.draw_image()

    def change_toolbar_visibility(self, toggleaction):
        self._update_toggle_preference('show toolbar', toggleaction)

    def change_menubar_visibility(self, toggleaction):
        self._update_toggle_preference('show menubar', toggleaction)

    def change_statusbar_visibility(self, toggleaction):
        self._update_toggle_preference('show statusbar', toggleaction)

    def change_scrollbar_visibility(self, toggleaction):
        self._update_toggle_preference('show scrollbar', toggleaction)

    def change_thumbnails_visibility(self, toggleaction):
        self._update_toggle_preference('show thumbnails', toggleaction)

    def change_hide_all(self, toggleaction):
        self._update_toggle_preference('hide all', toggleaction)

    def change_keep_transformation(self, *args):
        prefs['keep transformation'] = not prefs['keep transformation']

    def manual_zoom_in(self, *args):
        self.zoom.zoom_in()
        self.draw_image()

    def manual_zoom_out(self, *args):
        self.zoom.zoom_out()
        self.draw_image()

    def manual_zoom_original(self, *args):
        self.zoom.reset_user_zoom()
        self.draw_image()

    def _show_scrollbars(self, request):
        """ Enables scroll bars depending on requests and preferences. """

        limit = self._should_toggle_be_visible('show scrollbar')
        for i in range(len(self._scroll)):
            if limit and request[i]:
                self._scroll[i].show()
            else:
                self._scroll[i].hide()

    def is_scrollable(self):
        """ Returns True if the current images do not fit into the viewport. """
        if prefs['continuous scroll']:
            return True
        if self.layout is None:
            return False
        return not all(tools.smaller_or_equal(self.layout.get_union_box().get_size(),
            self.get_visible_area_size()))

    def scroll_with_flipping(self, x, y):
        """Returns true if able to scroll without flipping to
        a new page and False otherwise."""
        return self._event_handler._scroll_with_flipping(x, y)

    def scroll(self, x, y, bound=None):
        """Scroll <x> px horizontally and <y> px vertically. If <bound> is
        'first' or 'second', we will not scroll out of the first or second
        page respectively (dependent on manga mode). The <bound> argument
        only makes sense in double page mode.

        Return True if call resulted in new adjustment values, False
        otherwise.
        """
        old_hadjust = self._hadjust.get_value()
        old_vadjust = self._vadjust.get_value()

        visible_width, visible_height = self.get_visible_area_size()

        hadjust_upper = max(0, self._hadjust.get_upper() - visible_width)
        vadjust_upper = max(0, self._vadjust.get_upper() - visible_height)
        hadjust_lower = 0

        if bound is not None and self.is_manga_mode:
            bound = {'first': 'second', 'second': 'first'}[bound]

        if bound == 'first':
            hadjust_upper = max(0, hadjust_upper -
                self.images[1].size_request().width - 2) # XXX transitional(double page limitation)

        elif bound == 'second':
            hadjust_lower = self.images[0].size_request().width + 2 # XXX transitional(double page limitation)

        new_hadjust = old_hadjust + x
        new_vadjust = old_vadjust + y

        new_hadjust = max(hadjust_lower, new_hadjust)
        new_vadjust = max(0, new_vadjust)

        new_hadjust = min(hadjust_upper, new_hadjust)
        new_vadjust = min(vadjust_upper, new_vadjust)

        self._vadjust.set_value(new_vadjust)
        self._hadjust.set_value(new_hadjust)
        self._scroll[0].queue_resize_no_redraw()
        self._scroll[1].queue_resize_no_redraw()

        return old_vadjust != new_vadjust or old_hadjust != new_hadjust

    def scroll_to_predefined(self, destination, index=None):
        self.layout.scroll_to_predefined(destination, index)
        self.update_viewport_position()

    def update_viewport_position(self):
        viewport_position = self.layout.get_viewport_box().get_position()
        self._hadjust.set_value(viewport_position[0]) # 2D only
        self._vadjust.set_value(viewport_position[1]) # 2D only
        self._scroll[0].queue_resize_no_redraw()
        self._scroll[1].queue_resize_no_redraw()

    def update_layout_position(self):
        self.layout.set_viewport_position(
            (int(round(self._hadjust.get_value())), int(round(self._vadjust.get_value()))))

    def clear(self):
        """Clear the currently displayed data (i.e. "close" the file)."""
        self.set_title(constants.APPNAME)
        self.statusbar.set_message('')
        self.draw_image()

    def _clear_main_area(self):
        for i in self.images:
            i.hide()
        for i in self.images:
            i.clear()
        self._show_scrollbars([False] * len(self._scroll))
        self.layout = layout.create_dummy_layout()
        self._main_layout.set_size(*self.layout.get_union_box().get_size())
        self.set_bg_colour(prefs['bg colour'])

    def displayed_double(self):
        """Return True if two pages are currently displayed."""
        if prefs['continuous scroll']:
            return False
        return (self.imagehandler.get_current_page() and
                prefs['default double page'] and
                not self.imagehandler.get_virtual_double_page() and
                self.imagehandler.get_current_page() != self.imagehandler.get_number_of_pages())

    def get_visible_area_size(self):
        """Return a 2-tuple with the width and height of the visible part
        of the main layout area.
        """
        dimensions = list(self.get_size())

        for preference, action, widget_list in self._toggle_list:
            for widget in widget_list:
                if widget.get_visible():
                    axis = self._toggle_axis[widget]
                    requisition = widget.size_request()
                    if constants.PageAxis.WIDTH == axis:
                        size = requisition.width
                    elif constants.PageAxis.HEIGHT == axis:
                        size = requisition.height
                    dimensions[axis] -= size

        return tuple(dimensions)

    def get_layout_pointer_position(self):
        """Return a 2-tuple with the x and y coordinates of the pointer
        on the main layout area, relative to the layout.
        """
        x, y = self._main_layout.get_pointer()
        x += self._hadjust.get_value()
        y += self._vadjust.get_value()

        return (x, y)

    def set_cursor(self, mode):
        """Set the cursor on the main layout area to <mode>. You should
        probably use the cursor_handler instead of using this method
        directly.
        """
        self._main_layout.get_bin_window().set_cursor(mode)

    def update_title(self):
        """Set the title acording to current state."""
        this_screen = 2 if self.displayed_double() else 1 # XXX limited to at most 2 pages
        # TODO introduce formatter to merge these string ops with the ops for status bar updates
        title = '['
        for i in range(this_screen):
            title += '%d' % (self.imagehandler.get_current_page() + i)
            if i < this_screen - 1:
                title += ','
        title += ' / %d]  %s' % (self.imagehandler.get_number_of_pages(),
            self.imagehandler.get_pretty_current_filename())
        title = i18n.to_unicode(title)

        if self.slideshow.is_running():
            title = '[%s] %s' % (_('SLIDESHOW'), title)

        self.set_title(i18n.to_display_string(title))

    def set_bg_colour(self, colour):
        """Set the background colour to <colour>. Colour is a sequence in the
        format (r, g, b). Values are 16-bit.
        """
        colour = colour[:3]
        self._event_box.modify_bg(Gtk.StateType.NORMAL, Gdk.Color(*colour))
        if prefs['thumbnail bg uses main colour']:
            self.thumbnailsidebar.change_thumbnail_background_color(prefs['bg colour'][:3])
        self._bg_colour = colour

    def get_bg_colour(self):
        return self._bg_colour

    def extract_page(self, *args):
        """Save the currently displayed images to disk, appending a number if a
        file with an identical name was already found in the target directory.
        """
        this_screen = 2 if self.displayed_double() else 1 # XXX limited to at most 2 pages
        for i in reversed(range(this_screen)) if self.is_manga_mode \
        else range(this_screen):
            file_path = self.imagehandler.get_path_to_page(
                self.imagehandler.get_current_page() + i)
            if not file_path:
                return
            file_name = os.path.split(file_path)[-1]

            if self.filehandler.archive_type is not None:
                # Prepend the archive base name to the filename being displayed
                archive_name = self.filehandler.get_pretty_current_filename()
                file_name = (
                    os.path.splitext(archive_name)[0] + '_' + file_name)

            target_dir = prefs['path of last saved in filechooser'] + os.sep
            suggest_name = i18n.to_unicode(file_name)
            attempt = 1
            while os.path.exists(target_dir + suggest_name):
                suggest_name = tools.append_number_to_filename(
                    file_name, number=attempt)
                attempt += 1

            save_dialog = Gtk.FileChooserDialog(_('Save page as'), self,
                Gtk.FileChooserAction.SAVE,
                (Gtk.STOCK_OK, Gtk.ResponseType.ACCEPT,
                Gtk.STOCK_CANCEL, Gtk.ResponseType.REJECT)
            )
            save_dialog.set_do_overwrite_confirmation(True)
            save_dialog.set_create_folders(True)
            save_dialog.set_current_name(suggest_name)
            save_dialog.set_current_folder(target_dir)

            if save_dialog.run() == Gtk.ResponseType.ACCEPT:
                target = save_dialog.get_filename()
                if target:
                    target = i18n.to_unicode(target)
                    try:
                        shutil.copy2(file_path, target)
                    except Exception as e:
                        log.warning(e)

                prefs['path of last saved in filechooser'] = \
                    save_dialog.get_current_folder() \
                    if prefs['store last saved in directory'] \
                    else constants.HOME_DIR

            save_dialog.destroy()

    def delete(self, *args):
        """ The currently opened file/archive will be deleted after showing
        a confirmation dialog. """

        current_file = self.imagehandler.get_real_path()
        dialog = message_dialog.MessageDialog(self, Gtk.DialogFlags.MODAL, Gtk.MessageType.QUESTION,
                Gtk.ButtonsType.NONE)
        dialog.set_should_remember_choice('delete-opend-file', (Gtk.ResponseType.OK,))
        dialog.set_text(
                _('Delete "%s"?') % os.path.basename(current_file),
                _('The file will be deleted from your harddisk.'))
        dialog.add_button(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL)
        dialog.add_button(Gtk.STOCK_DELETE, Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        result = dialog.run()

        if result == Gtk.ResponseType.OK:
            # Go to next page/archive, and delete current file
            if self.filehandler.archive_type is not None:
                self.filehandler.last_read_page.clear_page(current_file)

                next_opened = self.filehandler._open_next_archive()
                if not next_opened:
                    next_opened = self.filehandler._open_previous_archive()
                if not next_opened:
                    self.filehandler.close_file()

                if os.path.isfile(current_file):
                    os.unlink(current_file)
            else:
                if self.imagehandler.get_number_of_pages() > 1:
                    # Open the next/previous file
                    if self.imagehandler.get_current_page() >= self.imagehandler.get_number_of_pages():
                        self.flip_page(-1)
                    else:
                        self.flip_page(+1)
                    # Unlink the desired file
                    if os.path.isfile(current_file):
                        os.unlink(current_file)
                    # Refresh the directory
                    self.filehandler.refresh_file()
                else:
                    self.filehandler.close_file()
                    if os.path.isfile(current_file):
                        os.unlink(current_file)

    def show_info_panel(self):
        """ Shows an OSD displaying information about the current page. """

        if not self.filehandler.file_loaded:
            return

        text = ''
        filename = self.imagehandler.get_pretty_current_filename()
        if filename:
            text += '%s\n' % filename
        file_number, file_count = self.filehandler.get_file_number()
        if file_count:
            text += '(%d / %d)\n' % (file_number, file_count)
        else:
            text += '\n'
        page_number = self.imagehandler.get_current_page()
        number_of_pages = self.imagehandler.get_number_of_pages()
        if page_number:
            text += '%s %d / %d' % (_('Page'), page_number, number_of_pages)
        text = text.strip('\n')
        if text:
            self.osd.show(text)

    def minimize(self, *args):
        """ Minimizes the MComix window. """
        self.iconify()

    def write_config_files(self):

        self.filehandler.write_fileinfo_file()
        preferences.write_preferences_file()
        bookmark_backend.BookmarksStore.write_bookmarks_file()

        # Write keyboard accelerator map
        keybindings.keybinding_manager(self).save()

    def save_and_terminate_program(self, *args):
        prefs['previous quit was quit and save'] = True

        self.terminate_program()

    def get_window_geometry(self):
        return self.get_position() + self.get_size()

    def save_window_geometry(self) -> None:
        x, y, width, height = self.get_window_geometry()
        prefs['window x'] = x
        prefs['window y'] = y
        prefs['window width'] = width
        prefs['window height'] = height
        prefs['window maximized'] = self.is_maximized()

    def restore_window_geometry(self):
        if self.get_window_geometry() == (prefs['window x'],
                                          prefs['window y'],
                                          prefs['window width'],
                                          prefs['window height']) \
           and self.is_maximized() == prefs['window maximized']:
            return False

        self.move(prefs['window x'], prefs['window y'])
        if prefs['window maximized']:
            self.maximize()
        else:
            self.resize(prefs['window width'], prefs['window height'])
        return True

    def update_space(self):
        self._spacing = prefs['space between two pages']
        self.draw_image()

    def close_program(self, *args):
        if not self.is_fullscreen:
            self.save_window_geometry()
        self.terminate_program()

    def terminate_program(self):
        """Run clean-up tasks and exit the program."""

        self.hide()

        if Gtk.main_level() > 0:
            Gtk.main_quit()

        if prefs['auto load last file'] and self.filehandler.file_loaded:
            prefs['path to last file'] = self.imagehandler.get_real_path()
            prefs['page of last file'] = self.imagehandler.get_current_page()

        else:
            prefs['path to last file'] = ''
            prefs['page of last file'] = 1

        if prefs['hide all'] and self.hide_all_forced and self.fullscreen:
            prefs['hide all'] = False

        self.write_config_files()

        self.filehandler.close_file()
        if main_dialog._dialog is not None:
            main_dialog._dialog.close()
        backend.LibraryBackend().close()

        # This hack is to avoid Python issue #1856.
        for thread in threading.enumerate():
            if thread is not threading.currentThread() and not isinstance(thread, threading._DummyThread):
                log.debug('Waiting for thread %s to finish before exit', thread)
                thread.join()

#: Main window instance
__main_window = None


def main_window():
    """ Returns the global main window instance. """
    return __main_window


def set_main_window(window):
    global __main_window
    __main_window = window


# vim: expandtab:sw=4:ts=4
