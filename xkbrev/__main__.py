import argparse
import collections as col
import enum
import functools as fun
import logging
import operator as op
import os
import os.path as path
import re
import subprocess
import sys
import tempfile


# add NullHandler so that we don't get any messages if the application
# hasn't set up any loggers to receive events
log = logging.getLogger(__name__)  #pylint: disable=invalid-name
log.addHandler(logging.NullHandler())


# pattern to recognize the xkb_symbols output from setxkbmap
SYMBOLS_PAT = re.compile(r'\txkb_symbols\s*{\sinclude\s\"(.*)\"\s*};')

def identify_layout(f):
    """\
    Determine which keyboard layout an input specification is for.
    """
    log.debug("Parsing setxkbmap output")
    f.seek(os.SEEK_SET, 0)
    while True:
        line = f.readline()

        # exit the loop if we get end-of-file
        if len(line) == 0:
            break

        # if we find a match, then return this right away
        m = re.match(SYMBOLS_PAT, line)
        if m is not None:
            parts = m.group(1).split("+")
            # filter out standard parts that are typically added
            parts = list(filter(lambda x: x != "pc", parts))
            parts = list(filter(lambda x: x != "inet(evdev)", parts))
            layout = "+".join(parts)
            log.debug("Found an xkb_symbols line")
            return layout

    # this indicates that we read through the entire output, but didn't
    # get any match; setxkbmap didn't return a proper result
    log.debug("Did not find an xkb_symbols line")
    return None


def compile_layout(layout, variant, options):
    """\
    :param layout:  Name of the layout, e.g. "us"
    :param variant: Variant, e.g. "dvorak", or None
    :param options: List of options to apply
    """
    # create list of arguments to pass to setxkbmap, which will prepare a
    # full keyboard definitions for us
    setxkbmap_prog = '/usr/bin/setxkbmap'
    layout_args = ['-layout', layout] if layout is not None else []
    variant_args = ['-variant', variant] if variant is not None else []
    opt_args = (fun.reduce(op.concat, map(lambda x: ['-option', x], options))
                if options is not None else [])
    setxkbmap_cmdline = ([setxkbmap_prog] + layout_args + variant_args +
                         opt_args + ['-print'])

    # output file will stay open until we are done parsing it
    with tempfile.TemporaryFile(mode='w+t') as outfile:

        # input file to xkbcomp is the output of setxkbmap; first
        with tempfile.TemporaryFile(mode='w+t') as infile:
            subprocess.call(setxkbmap_cmdline, stdout=infile.fileno())
            # retrieve description of the layout
            layout_descr = identify_layout(infile)
            if layout_descr is not None:
                log.info("Layout: %s", layout_descr)
            # start over again as we feed it to the compiler
            infile.seek(os.SEEK_SET, 0)
            xkbcomp_prog = '/usr/bin/xkbcomp'
            xkbcomp_cmdline = [xkbcomp_prog, '-w', '0', '-C', '-', '-o', '-']
            subprocess.call(xkbcomp_cmdline,
                            stdin=infile.fileno(),
                            stdout=outfile.fileno())

        # read every line from the output and yield it; when we have been
        # through all the lines, the file will close and automatically be
        # deleted (since it is temporary)
        outfile.seek(os.SEEK_SET, 0)
        while True:
            line = outfile.readline()
            if len(line) == 0:
                break
            else:
                # if someone sends a True value into the generator, then repeat
                # the previous line one more time
                stripped = line.rstrip()
                again = yield(stripped)
                if again:
                    # the first value is yielded to the 'send' function
                    yield None
                    yield stripped


# pattern to recognize the declaration of the number of keys
NUMKEY_PAT = re.compile(r'^#define NUM_KEYS\s+([0-9]+)')

def read_num_keys(source_line):
    """\
    Read the number of virtual keys defined in the layout
    """
    while True:
        line = next(source_line, None)
        m = re.match(NUMKEY_PAT, line)
        if m is not None:
            number = int(m.group(1))
            log.debug("Number of keys: %d", number)
            return number
    return None


# header for keyname section
KEYNAME_HEAD = 'static XkbKeyNameRec	keyNames[NUM_KEYS]= {'

# pattern for declaration of a key name
KEYNAME_PAT = re.compile(r'\s{4}{\s+\"([A-Z0-9_\+\-]*)\"\s{2}}(,?)')

def read_key_names(num_keys, source_line):
    """\
    Read the names of the keys, return an array with them
    """
    # preallocate the array
    key_names = ['']*num_keys
    i = 0
    while True:
        line = next(source_line, None)
        if line.startswith(KEYNAME_HEAD):
            while line != '};':
                for m in re.finditer(KEYNAME_PAT, line):
                    name = m.group(1)
                    key_names[i] = name
                    i += 1
                line = next(source_line, None)
            break
    # verify that we read exactly the number of keys expected
    log.debug('Read %d key names', i)
    return key_names


# enumeration of various modifier keys that can be pressed
class Modifier(enum.Enum):
    Shift = 2**0
    AltGr = 2**1
    NumLock = 2**2
    CapsLock = 2**3
    Super = 2**4
    LevelFive = 2**5
    Control = 2**6
    Alt = 2**7
    LeftCtrl = 2**8
    LeftAlt = 2**9
    RightCtrl = 2**10
    RightAlt = 2**11


# mapping of modifier names as used in the source code, to the modifier enum
MOD_NAME_TO_ENUM = {
    'Shift': Modifier.Shift,
    'Lock': Modifier.CapsLock,
    'Control': Modifier.Control,
    'NumLoc': Modifier.NumLock,
    'LevelThre': Modifier.AltGr,
    'Mod4': Modifier.Super,
    'Al': Modifier.Alt,
    'LevelFiv': Modifier.LevelFive,
    'LAl': Modifier.LeftAlt,
    'RAl': Modifier.RightAlt,
    'LContro': Modifier.LeftCtrl,
    'RContro': Modifier.RightCtrl,
}


# key type definitions start with this line; this also ends modifier maps
KEYTYPE_HEAD = 'static XkbKeyTypeRec dflt_types[]= {'

# header that introduces each activation map, and pattern for each line in it
ACT_HEAD_PAT = r'static XkbKTMapEntryRec map_([A-Z0-9_]+)\[([0-9]+)\]= {'
ACT_REC_PAT = r'\s+{\s*([01]),\s*([0-9]+),\s*{\s*(.*),\s*(.*),\s*(.*)\s}\s},?'

def read_activation_map(source_line):
    """\
    Activation map is a dictionary where the key is the name of the type and the
    content is a dictionary where the key is a bitmask of modifiers and the value
    is the level that is activated by this combination of modifiers, when a key
    is of that type.
    """
    # there is always a default 'ONE_LEVEL', with a level that is activated
    # regardless of any modifiers
    act_map = {'ONE_LEVEL': {frozenset(): 0}}
    while True:
        line = next(source_line, None)
        # type entries go on until this line appears
        if line.startswith(KEYTYPE_HEAD):
            # ask the iterator to unget the last line
            source_line.send(True)
            break
        # if we find a map record, the start parsing its entries
        m = re.match(ACT_HEAD_PAT, line)
        if m is not None:
            # the number of lines are specified in the declaration
            map_name = m.group(1)
            num_rec = int(m.group(2))
            log.debug("Key type %s contains %d activations", map_name, num_rec)
            # pre-allocate an empty activation record; no modifiers always
            # activates the first level declared
            act_rec = {frozenset(): 0}
            # read each activation record
            for i in range(num_rec):
                line = next(source_line, None)
                m = re.match(ACT_REC_PAT, line)
                if m is None:
                    log.fatal("Map entry does not match expected pattern")
                    log.fatal("%s", line)
                    break
                # first column indicates if this level determines shift
                shift = bool(m.group(1))
                # second column is the level this combination controls
                level = int(m.group(2))
                # third column is the modifiers that applies to this level
                mods = m.group(3).split('|')
                # fourth column should be the same as third in the
                # auto-generated files
                if m.group(4) != m.group(3):
                    log.warning("Unexpected modifier declaration")
                # fifth column is additional modifiers
                mods.extend(m.group(5).split('|'))
                # remove both prefix and suffix from modifiers
                mods = [m[len('vmod_'):] if m.startswith('vmod_') else m
                        for m in mods]
                mods = [m[:-len('Mask')] if m.endswith('Mask') else m
                        for m in mods]
                # normal state is not a modifier
                mods = list(filter(lambda x: x != '0', mods))
                # map to modifier enum
                mods = [MOD_NAME_TO_ENUM[m] for m in mods]
                log.debug("Level %d is activated on modifiers %s", level,
                          ', '.join([m.name for m in mods]))
                act_rec[frozenset(mods)] = level

            # create a set of activation records for this map
            act_map[map_name] = act_rec

            # expect the end of the record next, after the entries
            line = next(source_line, None)
            if line != '};':
                log.fatal("Unexpected end of map entry record")

    # return the collected activation map for all key types
    return act_map


def read_key_types(source_line):
    """\
    Create a list of which key type index that has which name
    """
    key_types = []
    # discard source lines until we arrive at our header line
    while True:
        line = next(source_line, None)
        if line == KEYTYPE_HEAD:
            break

    # now read the list of types
    while True:
        # each type consists of six lines: first line is just an opening brace,
        # unless it is the end of the map (the actual number of entries is not
        # defined as a readily available number, so we build a dynamic list)
        line = next(source_line, None)
        if line == '};':
            break
        if line != '    {':
            log.fatal('Parsing error: Expected opening brace')
        # second line is the modifiers that are in use; we already know this
        # from the activation map, so we can just ignore it
        line = next(source_line, None)
        # third line is the number of levels which is defined for this type
        line = next(source_line, None)
        num_levels = int(line.split(',')[0].strip())
        # fourth line is a comma-separated list where the first item is the
        # number of activation records for this type (which we already know, the
        # second item is the name of the key type, in form of the identifier
        # that was used to declare the map further above, and the last item is
        # either NULL or a preserve_ pointer. however, we don't use this variant
        # because it won't pick up types without a map_
        line = next(source_line, None)
        # fifth line is a comma-separated list where the first item is always
        # None as an unused field for the name, and the second is the identifier
        # for the atom that contains the name
        line = next(source_line, None)
        map_name = line.split(',')[1].strip()
        map_name = (map_name[len('lnames_'):] if map_name.startswith('lnames_')
                    else map_name)
        # sixth line is the closing brace
        line = next(source_line, None)
        if not line.startswith('    }'):
            log.fatal('Parsing error: Expected closing brace')
        # build an entry for this type and put in the list
        key_types.append({'name': map_name, 'levels': num_levels})

    # this is a list that contains the name of each type, in the position that
    # is used as 'key index' in the symbol table
    return key_types


# list of symbols are started with this declaration
SYMBOLS_HEAD = 'static KeySym\tsymCache[NUM_SYMBOLS]= {'


def read_symbol_list(source_line):
    """\
    The symbols that can be outputted for each key are stored as the data
    section of a sparse array; all the data just follows eachother, and then
    there is an index array that tells where one ends and the next starts.
    """
    # discard source lines until we arrive at our header line
    while True:
        line = next(source_line, None)
        if line.startswith(SYMBOLS_HEAD):
            break

    # start out with an empty list
    symbols = []

    # now read the list of symbols until the list ends
    while True:
        line = next(source_line, None)
        if line == '};':
            break

        # massage the line into a comma-separated list of symbols without any
        # whitespace and the XK_ prefix (which are identifiers in the C code)
        sym_list = line.strip()
        sym_list = sym_list[:-1] if sym_list.endswith(',') else sym_list
        sym_list = sym_list.split(',')
        sym_list = [s.strip() for s in sym_list]
        sym_list = [s[len('XK_'):] if s.startswith('XK_') else s
                    for s in sym_list]
        symbols.extend(sym_list)

    log.debug('Read %d symbols', len(symbols))
    return symbols


# header and entry for each of the symbol triplets
SYMMAP_HEAD = 'static XkbSymMapRec\tsymMap[NUM_KEYS]= {'
SYMMAP_PAT = r'\s*{\s*([0-9]+),\s*0x([01]),\s*([0-9]+)\s*},?'


def read_key_map(source_line):
    """\
    Read a definition for each virtual key: the key type index and the starting
    position in the flat symbol list
    """
    # discard source lines until we arrive at our header line
    while True:
        line = next(source_line, None)
        if line.startswith(SYMMAP_HEAD):
            break

    key_map = []

    # read key mapping until we read the end symbol of the list
    while True:
        line = next(source_line, None)
        if line == '};':
            break

        for m in re.finditer(SYMMAP_PAT, line):
            # first item in the key type index
            type_index = int(m.group(1))
            # second item is the number of groups; note that we only support
            # zero or one groups; we don't have any code to handle more than one
            # group for the time being
            num_groups = int(m.group(2))
            # third item is the offset in the flat symbols list
            offset = int(m.group(3))

            # create an entry for this key
            key_map.append({
                'type': type_index,
                'defined': num_groups > 0,
                'offset': offset
            })

    return key_map


def read_layout_map(source):
    """\
    Read layout map from layout source code.

    :param source_line: Generator that returns each line of source code.

    :return: Dictionary indexed by virtual key name, containing a new dictionary
             indexed by a (frozen) set of modifiers, containing the symbol that
             is generated for this particular combination of key and modifiers.
    """
    num_keys = read_num_keys(source)
    key_names = read_key_names(num_keys, source)
    act_map = read_activation_map(source)
    key_types = read_key_types(source)
    symbols = read_symbol_list(source)
    key_map = read_key_map(source)

    # key_map is a list with an entry for each keycode used in this particular
    # layout; we want to convert this to a dictionary of key names
    layout_map = {}
    for ndx, keydef in enumerate(key_map):
        # if there is no definition of this key, then we don't add it to the
        # final dictionary
        if not keydef['defined']:
            continue

        # get the virtual, scancode-independent name of the _key_ (not the
        # character that is generated)
        virt_key = key_names[ndx]

        # get the type that is designated for this particular key
        t = key_types[keydef['type']]

        # this is the offset into the flat symbols list where the levels for
        # this particular key starts
        ofs = keydef['offset']

        # build a list of level-to-symbol for this particular key
        sym = [''] * t['levels']
        for level in range(t['levels']):
            sym[level] = symbols[ofs + level]

        # generate an entry for each activation combination of modifiers for
        # this particular type
        sym_for_mods = {}
        for _, (mods, level) in enumerate(act_map[t['name']].items()):
            sym_for_mods[mods] = sym[level]

        # add to the global map for this particular virtual key
        layout_map[virt_key] = sym_for_mods

    return layout_map


# header file definition of a named key symbol
SYMDEF_PAT = re.compile(r'^#define\sXK_([A-Za-z_0-9]+)\s+0x0*([A-Fa-f0-9]+)' +
                        r'(\s\s\/\* U\+0*([A-Fa-f0-9]+)\s.*)?.*$')


def read_symbol_map():
    """\
    Get a map with a pair of the character and possibly unicode key that is
    generated, for each defined symbol.
    """
    sym_map = {}

    # read symbols from this (hardcoded) file; the key symbols always mean the
    # same, regardless of layout and keyboard selected
    with open('/usr/include/X11/keysymdef.h', 'rt') as f:
        while True:
            # read until end of file
            line = f.readline()
            if len(line) == 0:
                break

            # look for symbol definitions
            m = re.match(SYMDEF_PAT, line)
            if m is None:
                continue

            # decode the definition; the first group is the symbol name, the
            # second is the character code, and the third is the unicode of the
            # code, if defined, otherwise None
            sym_name = m.group(1)
            sym_char = int(m.group(2), 16)
            sym_unic = None if m.group(4) is None else int(m.group(4), 16)

            # collect these in the global dictionary
            sym_map[sym_name] = (sym_char, sym_unic)

    return sym_map


# definition of key code for a virtual key,
KEYCODE_PAT = re.compile(r'^\s*<([^>]+)>\s*=\s*([0-9]+);.*')
ALIAS_PAT = re.compile(r'^\s*alias\s<([^>]+)>\s*=\s*<([^>]+)>;.*')


def read_keycode_map(name):
    """\
    Read scancode definition for a particular kind of input system.

    :return: List that is indexed by scancode, with virtual key name for this
             scancode, or None if not defined
    """
    key_codes = {}
    max_key_code = 0
    with open(path.join('/usr/share/X11/xkb/keycodes', name), 'rt') as f:
        while True:
            # read until end of file
            line = f.readline()
            if len(line) == 0:
                break

            # read definition; first group is the name of the keycode,
            # the second group is the scancode.
            m = re.match(KEYCODE_PAT, line)
            if m is not None:
                virt_key = m.group(1)
                scancode = int(m.group(2))
                # if we have already seen the virtual key, then we are past the
                # initial basic definition and have started on auxiliary
                # definitions; we don't use those, so skip any redefinitions
                if virt_key not in key_codes:
                    key_codes[virt_key] = scancode
                # keep track of the largest value seen
                max_key_code = max(max_key_code, scancode)
                continue

            # if we didn't get a definition, maybe it is an alias
            m = re.match(ALIAS_PAT, line)
            if m is not None:
                key_codes[m.group(1)] = key_codes[m.group(2)]

    # now turn everything inside-out; we want a list indexed by the scancode,
    # giving the virtual key code
    scancode_map = [None] * (max_key_code+1)
    for _, (virt_key, scancode) in enumerate(key_codes.items()):
        log.debug('virt_key = %s, scancode = %d', virt_key, scancode)
        scancode_map[scancode] = virt_key

    return scancode_map


# modifier combinations that have their own sections in XRDP format
XRDP_MODS = col.OrderedDict([
    ('noshift', frozenset()),
    ('shift', frozenset([Modifier.Shift])),
    ('altgr', frozenset([Modifier.AltGr])),
    ('shiftaltgr', frozenset([Modifier.Shift, Modifier.AltGr])),
    ('capslock', frozenset([Modifier.CapsLock])),
    ('capslockaltgr', frozenset([Modifier.CapsLock, Modifier.AltGr])),
    ('shiftcapslock', frozenset([Modifier.Shift, Modifier.CapsLock])),
    ('shiftcapslockaltgr', frozenset(
        [Modifier.Shift, Modifier.CapsLock, Modifier.AltGr]))
])


def write_xrdp(layout_map, symbol_map, keycode_map, outf):
    """\
    Regenerate the keyboard layout in the format expected by XRDP.

    :param layout_map: Map for virtual key + modifier to symbol
    :param symbol_map: Map for symbol to character code and unicode
    :param keycode_map: Map for scancode to virtual key
    :param outf: File that will receive generated keymap in XRDP format
    """
    # write each section separately
    for ndx, section in enumerate(XRDP_MODS):
        # section header
        outf.write("[{0:s}]\n".format(section))

        # key definitions
        mods = XRDP_MODS[section]
        for keycode, virt_key in enumerate(keycode_map):
            # only generate entries for keycodes that have an associated virtual
            # key (otherwise it is an unused scancode)
            if virt_key is None:
                continue

            # if there is nothing defined for this virtual key in the layout,
            # then just skip it as well
            if virt_key not in layout_map:
                continue

            # if this combination of virtual key and modifiers doesn't exist,
            # then drop it
            if mods not in layout_map[virt_key]:
                continue

            # there is a symbol for this combination; look up the character and
            # possibly printable code, and generate an entry for it
            sym = layout_map[virt_key][mods]

            # if there is no character for this symbol, generate empty entry
            char, unic = symbol_map[sym] if sym in symbol_map else (0, 0)

            # write all the gathered information to file
            outf.write("Key{0:d}={1:d}:{2:d}\n".format(keycode,
                0 if char is None else char, 0 if unic is None else unic))
            log.debug("key = %s, modifier = %s: symbol = %s",
                      virt_key, ",".join([m.name for m in mods]), chr(char))

        # newline at end of each section, unless it's the last
        if ndx < len(XRDP_MODS) - 1:
            outf.write("\n")


def main(args):
    # setup in main routine
    logging.basicConfig(level=logging.INFO,
                        handlers= [logging.StreamHandler(sys.stderr)],
                        format="%(levelname).1s: %(message).76s")

    # parse command-line arguments; this program intentionally takes the same
    # options as setxkbmap so that one can do `xkbrev $(cat ~/.Xkbmap)` to get
    # the output for the current layout of the display server
    parser = argparse.ArgumentParser ()
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-layout", type=str, required=False)
    parser.add_argument("-variant", type=str, required=False)
    parser.add_argument("-option", type=str, action='append')
    parser.add_argument("--generate", choices = ['xrdp'])
    parser.add_argument("--output", type=str, nargs="?", default='-')
    args = parser.parse_args ()

    # alter verbosity if specified on the command-line
    if args.verbose:
        log.setLevel(logging.DEBUG)
    elif args.quiet:
        log.setLevel(logging.WARNING)

    # run the layout specification through the compiler which will give us the
    # composition in the forms of C source code
    layout_source = compile_layout(args.layout, args.variant, args.option)

    # parse the layout definition source file into a data structure
    layout_map = read_layout_map(layout_source)
    symbol_map = read_symbol_map()

    try:
        # if a filename has been specified, then redirect output to that
        if args.output == '-':
            outf = sys.stdout
        else:
            outf = open(args.output, 'w+t')

        # write appropriate output format. XRDP uses the base rules, which again
        # uses the xfree86 input system (and not evdev, which is more common
        # natively) in order to be compatible with the x11vnc backend, so we
        # must load the mapping for that input system too
        if args.generate == 'xrdp':
            keycode_map = read_keycode_map('xfree86')
            write_xrdp(layout_map, symbol_map, keycode_map, outf)

    finally:
        outf.close()


if __name__ == "__main__":
    main(sys.argv[1:])
