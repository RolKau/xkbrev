import argparse
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


def read_layout_map(source):
    """\
    Read layout map from layout source code.

    :param source_line: Generator that returns each line of source code.
    """
    num_keys = read_num_keys(source)


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
