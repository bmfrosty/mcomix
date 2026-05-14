"""Portability functions for MComix."""

import ctypes
import locale
import sys

from mcomix import constants


def uri_prefix() -> str:
    """The prefix used for creating file URIs. This is 'file://' on
    Linux, but 'file:' on Windows due to urllib using a different
    URI creating scheme here."""
    if sys.platform == "win32":
        return "file:"
    else:
        return "file://"


def normalize_uri(uri: str) -> str:
    """Normalize URIs passed into the program by different applications,
    normally via drag-and-drop."""

    if uri.startswith("file://localhost/"):  # Correctly formatted.
        return uri[16:]
    elif uri.startswith("file:///"):  # Nautilus etc.
        return uri[7:]
    elif uri.startswith("file:/"):  # Xffm etc.
        return uri[5:]
    else:
        return uri


def invalid_filesystem_chars() -> str:
    """List of characters that cannot be used in filenames on the target platform."""
    if sys.platform == "win32":
        return r':*?"<>|' + "".join([chr(i) for i in range(0, 32)])
    else:
        return ""


def get_default_locale() -> str:
    """Gets the user's default locale."""
    if sys.platform == "win32":
        windll = ctypes.windll.kernel32
        code = windll.GetUserDefaultUILanguage()
        return locale.windows_locale[code]
    else:
        lang, _ = locale.getdefaultlocale(("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"))
        if lang:
            return str(lang)
        else:
            return "C"


def is_system_ui_dark_themed() -> constants.SystemThemeLightness:
    """Determine if the system is configured to use a dark theme by default."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
            ) as personalize_handle:
                _, values_count, _ = winreg.QueryInfoKey(personalize_handle)
                for key_index in range(values_count):
                    key_name, key_value, _ = winreg.EnumValue(
                        personalize_handle, key_index
                    )

                    if key_name == "AppsUseLightTheme":
                        if key_value == 0:
                            return constants.SystemThemeLightness.DARK
                        else:
                            return constants.SystemThemeLightness.LIGHT

                return constants.SystemThemeLightness.LIGHT
        except OSError:
            return constants.SystemThemeLightness.UNKNOWN
    else:
        return _detect_unix_dark_theme()


def _detect_unix_dark_theme() -> constants.SystemThemeLightness:
    """Detect dark theme preference on Linux/Unix.

    Tries the XDG settings portal first (works in flatpak sandboxes and on
    any desktop with a portal implementation), then falls back to reading
    org.gnome.desktop.interface via GSettings (works on a GNOME desktop
    outside a sandbox).

    XDG appearance color-scheme values: 0=no-preference, 1=prefer-dark,
    2=prefer-light.
    """
    try:
        from gi.repository import GLib, Gio
    except ImportError:
        return constants.SystemThemeLightness.UNKNOWN

    # Primary: XDG settings portal via D-Bus
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Settings",
            "Read",
            GLib.Variant("(ss)", ("org.freedesktop.appearance", "color-scheme")),
            GLib.VariantType.new("(v)"),
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
        # The return type is (v); unwrap any number of nested v-typed variants
        # to reach the actual uint32 value.
        inner = result.get_child_value(0)
        while inner.get_type_string() == "v":
            inner = inner.get_variant()
        color_scheme = inner.get_uint32()
        if color_scheme == 1:
            return constants.SystemThemeLightness.DARK
        if color_scheme == 2:
            return constants.SystemThemeLightness.LIGHT
    except Exception:
        pass

    # Fallback: GSettings (GNOME desktop, outside flatpak)
    try:
        gsettings = Gio.Settings.new("org.gnome.desktop.interface")
        color_scheme = gsettings.get_string("color-scheme")
        if color_scheme == "prefer-dark":
            return constants.SystemThemeLightness.DARK
        if color_scheme == "prefer-light":
            return constants.SystemThemeLightness.LIGHT
    except Exception:
        pass

    return constants.SystemThemeLightness.UNKNOWN


# vim: expandtab:sw=4:ts=4
