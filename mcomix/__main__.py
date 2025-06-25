"""MComix - GTK Comic Book Viewer
"""

# -------------------------------------------------------------------------
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# -------------------------------------------------------------------------

import multiprocessing as mp
from .run import run


def main() -> None:
    run()


if __name__ == '__main__':
    mp.freeze_support()
    mp.set_start_method('spawn')
    main()
