"""Read-only dependency check; no model calls or private data inspection."""
from contextlib import closing
import argparse
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-only', action='store_true', help='Check software only; external resources may be absent')
    args = parser.parse_args()
    node = shutil.which('node')
    checks = {'python_3_12': sys.version_info >= (3, 12), 'node_available': bool(node)}
    if node:
        version = subprocess.run([node, '--version'], capture_output=True, text=True).stdout.strip()
        checks['node_22'] = version.startswith('v') and int(version[1:].split('.')[0]) >= 22
        for name in ('playwright', 'esbuild'):
            checks['node_' + name] = subprocess.run([node, '-e', f"require.resolve('{name}')"], cwd=ROOT, capture_output=True).returncode == 0
    for name in ('requests', 'truststore', 'pyarrow', 'torch', 'transformers', 'langchain_openai', 'openai', 'nltk', 'tiktoken'):
        checks['python_' + name] = importlib.util.find_spec(name) is not None
    with closing(sqlite3.connect(':memory:')) as db:
        try:
            db.execute('CREATE VIRTUAL TABLE probe USING fts5(text)')
            checks['sqlite_fts5'] = True
        except sqlite3.OperationalError:
            checks['sqlite_fts5'] = False
    if not args.code_only:
        checks['embedding_model'] = all((ROOT / 'models/bge-small-en-v1.5' / name).is_file() for name in ('config.json', 'model.safetensors'))
        checks['tokenizers'] = all((ROOT / 'assets/webthinker/tokenizers' / name / 'tokenizer.json').is_file() for name in ('QwQ-32B', 'Qwen2.5-32B-Instruct'))
        checks['nltk_resources'] = (ROOT / 'assets/webthinker/nltk_data/tokenizers/punkt_tab').is_dir()
        checks['perplexica_bundle'] = (ROOT / 'code/frameworks/perplexica/run.cjs').is_file()
    result = {'checks': checks, 'ready': all(checks.values()), 'scope': 'software/resources only; external datasets validated at init'}
    print(json.dumps(result, indent=2))
    return 0 if result['ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
