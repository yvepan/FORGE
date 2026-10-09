"""Public CLI for frozen runs and finalized event-score aggregation."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
ENTRYPOINTS = {'init': 'tools/run.py', 'run': 'tools/run.py',
              'construct': 'code/construction/construct.py',
              'score': 'code/evaluation/evaluate.py', 'check-setup': 'tools/check_setup.py',
              'ppl-filter': 'code/shared/src/ppl_filter.py'}


def main():
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print('Usage: python forge.py {' + ','.join(ENTRYPOINTS) + '} [arguments]')
        print('Use python forge.py COMMAND --help for command-specific options.')
        return 0
    action = args.pop(0)
    if action not in ENTRYPOINTS:
        print('Unknown command: ' + action, file=sys.stderr)
        return 2
    script = ROOT / ENTRYPOINTS[action]
    if action in ('init', 'run'):
        args.insert(0, action)
    return subprocess.run([sys.executable, str(script), *args], cwd=ROOT).returncode


if __name__ == '__main__':
    raise SystemExit(main())
