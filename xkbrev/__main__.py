import argparse
import enum
import functools as fun
import logging
import operator as op
import os
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


# the values are all different exponents of two, so that we can combine them
# into a single value and preserve the semantics
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
    act_map = {'ONE_LEVEL': {0: 0}}
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
            act_rec = {0: 0}
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
                bitmask = sum([m.value for m in mods])
                act_rec[bitmask] = level

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


def read_layout_map(source):
    """\
    Read layout map from layout source code.

    :param source_line: Generator that returns each line of source code.
    """
    num_keys = read_num_keys(source)
    key_names = read_key_names(num_keys, source)
    act_map = read_activation_map(source)
    key_types = read_key_types(source)


def main(args):
    # setup in main routine
    logging.basicConfig(level=logging.INFO,
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
    args = parser.parse_args ()

    # alter verbosity if specified on the command-line
    if args.verbose:
        log.setLevel(logging.DEBUG)
    elif args.quiet:
        log.setLevel(logging.WARNING)

    # run the layout specification through the compiler which will give us the
    # composition in the forms of C source code
    layout_source = compile_layout(args.layout, args.variant, args.option)

    layout_map = read_layout_map(layout_source)


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
