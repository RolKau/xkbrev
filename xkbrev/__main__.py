import argparse
import logging
import sys


# add NullHandler so that we don't get any messages if the application
# hasn't set up any loggers to receive events
log = logging.getLogger(__name__)  #pylint: disable=invalid-name
log.addHandler(logging.NullHandler())


def main(args):
    # setup in main routine
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname).1s: %(message).76s")

    # parse command-line arguments
    parser = argparse.ArgumentParser ()
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args ()

    # alter verbosity if specified on the command-line
    if args.verbose:
        log.setLevel(logging.DEBUG)
    if args.quiet:
        log.setLevel(logging.WARNING)


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
