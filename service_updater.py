#!/usr/bin/env python3
"""Root host controller extracted from the immutable service-updater image."""
import argparse
from pathlib import Path
import sys
import core


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('action', choices=('check', 'run', 'stop', 'update', 'stage', 'close'))
    args = parser.parse_args()
    # Classic is the only supported adapter. Adding another requires reviewed
    # source and its lifecycle tests; configuration can never import arbitrary code.
    config = core.load(args.config)
    core.need(config.get('adapter') == 'classic', 'unsupported service adapter')
    from adapters import classic
    return classic.main()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('Atrinik service update failed: ' + (str(error) if isinstance(error, core.Rejected) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
