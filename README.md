# MComix (bmfrosty fork)

This is a personal fork of [MComix](https://sourceforge.net/projects/mcomix/)
maintained at [github.com/bmfrosty/mcomix](https://github.com/bmfrosty/mcomix).

## Why this fork exists

This fork was primarily created to fix dark mode detection when running MComix
as a Flatpak on [Bazzite](https://bazzite.gg/). Inside the Flatpak sandbox,
GTK's built-in dark theme heuristics fail; this fork queries the
`org.freedesktop.portal.Settings` D-Bus portal instead.

**It has only been tested as a Flatpak on Bazzite.** It has not been tested
as a Flatpak on other distributions, nor as a native package.

Maintaining a fork is not a goal. The hope is that the upstream MComix authors
will adopt these patches so this fork can be retired.

## Current Status - untested as a flatpak

Latest set of fixes is for fixes to the the file picker around selecting files
in SMB, caching them in the Recent menu, and getting Previous and Next Archive
buttons work in local mode, and getting those commited, I'm not going to try
and fix it until I'm full up on Claude credits again.

## Installation (Flatpak)

```bash
# Add the repository (one time)
flatpak remote-add --user bmfrosty-mcomix \
  https://bmfrosty.github.io/mcomix/ --no-gpg-verify

# Install
flatpak install bmfrosty-mcomix io.github.bmfrosty.mcomix

# Update when a new version is available
flatpak update io.github.bmfrosty.mcomix
```

The `--no-gpg-verify` flag is required because the repository is not GPG-signed.

## Additional features in this fork

- **Dark mode fix** — detects the system dark/light theme via the XDG Settings
  portal so it works correctly inside a Flatpak sandbox
- **Vertical continuous scroll** — a new reading mode for webcomics; toggle it
  with the toolbar button or a keybinding
- **SMB / network filesystem support** — open comics directly from Samba shares
  and other network locations via the file chooser portal

## About MComix (upstream)

MComix is a user-friendly, customizable image viewer. It is specifically
designed to handle comic books (both Western comics and manga) and supports a
variety of container formats.

MComix is a fork of Comix. It is written in Python and uses GTK 3 through the
PyGObject bindings.

## Installation

For the upstream release, please follow the
[installation instructions](https://sourceforge.net/p/mcomix/wiki/Installation/) on the Wiki.

Most users will find it convenient to use the package provided by their
operating system package manager.

## Dependencies

For a list of packages and libraries needed to run MComix, please refer to
[the upstream documentation](https://sourceforge.net/p/mcomix/wiki/Home/#Dependencies).

## Credits

Thanks to everyone who have contributed translations, suggestions, bug
reports, fixes and donations!

Icons with a filename starting with "gimp" are taken from The GIMP, and
icons with a filename starting with "tango" are taken from the Tango Desktop
Project. Most other icons are made by Victor Castillejo, creator of the
GNOME-Colors icon theme.

The directory mcomix/_vendor/packaging/ contains portions of
'packaging' version 21.0, (c) Donald Stufft and individual contributors.
The packaging code is made available under the terms of either the
Apache 2.0 license or BSD 2-clause license (user's choice).
See mcomix/_vendor/packaging-21.0.dist-info/LICENSE for details.

## Contact

Please use the [issue tracker](https://sourceforge.net/p/mcomix/_list/tickets) to get in touch with the MComix developers.
