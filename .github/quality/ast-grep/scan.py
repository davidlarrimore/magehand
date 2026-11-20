#!/usr/bin/env python3
"""Run the platform's ast-grep rules on the files pre-commit passes (homelab#611). pre-commit knows a Python script
by its shebang; ast-grep goes by the file name, so a script without `.py` (homelab's `tools/homelab`, `hostctl`)
would be skipped without a word. Each such file is mapped to Python in a temporary config (languageGlobs)."""
import json
import pathlib
import subprocess  # nosec B404  # runs ast-grep with a fixed argv
import sys
import tempfile

RULES = pathlib.Path(__file__).resolve().parent / 'rules'


def config_text(files):
    # By name: ast-grep matches languageGlobs against the file name, not its path. Only the files given are scanned.
    extra = sorted({pathlib.PurePath(f).name for f in files if not f.endswith(('.py', '.pyi'))})
    lines = [f'ruleDirs: [{json.dumps(str(RULES))}]']
    if extra:
        lines += ['languageGlobs:', '  python:'] + [f'    - {json.dumps(f)}' for f in extra]
    return '\n'.join(lines) + '\n'


def main(files):
    if not files:
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        config = pathlib.Path(tmp) / 'sgconfig.yml'
        config.write_text(config_text(files))
        # check=False: ast-grep's own exit code (1 = findings) is the hook's result.
        return subprocess.run(['ast-grep', 'scan', '--config', str(config), *files],  # nosec B603 B607  # fixed argv
                              check=False).returncode


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
