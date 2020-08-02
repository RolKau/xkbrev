This utility reads a keyboard definition from the
[Xkb](https://freedesktop.org/wiki/Software/XKeyboardConfig/)
keyboard layout database into a data structure, which can then be
used to output a representation of that layout in a different format.

Primarily, the goal of the program was to generate a layout file for the
[Programmer Dvorak](https://www.kaufmann.no/roland/dvorak) layout to
use in an [XRDP](https://xrdp.org) setup.

However, while the program is heavily bound to current versions of the
xkb-data (2.16) and x11-xkb-utils (7.7) Debian packages, it is not
bound to a certain keyboard layout and can thus be used to generate
files for other layouts as well.

It takes some of the same options as would be passed to the setxkbmap
utility through the `~/.Xkbmap` file, or which is printed with the
`-query` option, so a command-line execution to dump the current layout
would be:

```
bin/xkbrev $(setxkbmap -query | sed "s,\([a-z]\+\):\s*\(.*\),-\1 \2," | xargs) --generate=xrdp --output=km-A0000409.ini
```
