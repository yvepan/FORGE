"""Rebase a separate frozen dataset manifest to an installed read-only database."""
from pathlib import Path
import argparse,json,sqlite3
ROOT=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--manifest',type=Path,required=True);p.add_argument('--database',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
database=a.database.resolve();output=a.output.resolve()
if not database.is_file():p.error('Database file is absent')
if not database.is_relative_to((ROOT/'runs').resolve()) or not output.is_relative_to((ROOT/'runs').resolve()):p.error('Install the database and output manifest under this package runs/')
if output.exists():p.error('Output exists; do not overwrite a frozen manifest')
manifest=json.loads(a.manifest.read_text(encoding='utf-8-sig'))
base_count=manifest.get('base_article_count',6407814)
if not isinstance(base_count,int) or base_count<0:p.error('Invalid base_article_count')
if base_count!=6407814 and not (manifest.get('ppl_filter',{}).get('threshold')==111.71 and manifest['ppl_filter'].get('model')=='gpt2'):p.error('Changed base corpus needs documented paper GPT-2 PPL filtering')
expected=base_count+len(manifest['documents'])
with sqlite3.connect(f'file:{database.as_posix()}?mode=ro',uri=True) as db:
    if db.execute('SELECT count(*) FROM articles').fetchone()[0]!=expected:p.error('Article count does not match frozen manifest')
    if db.execute('SELECT count(*) FROM article_fts_docsize').fetchone()[0]!=expected:p.error('FTS count mismatch')
    for d in manifest['documents']:
        if db.execute('SELECT url,title,text FROM articles WHERE id=?',(d['id'],)).fetchone()!=(d['url'],d['title'],d['text']):p.error('Frozen document mismatch')
manifest['database']=str(database);output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'manifest':str(output),'articles':expected,'documents':len(manifest['documents']),'queries':len(manifest['queries']),'database_modified':False}))
