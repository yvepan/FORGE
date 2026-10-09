"""Paper F.7: offline GPT-2 filtering of clean and attack articles before retrieval."""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3

THRESHOLD = 111.71  # Paper's clean-corpus 95th percentile; never fitted to attack text.


class GPT2Perplexity:
    def __init__(self, model_path):
        import torch
        from transformers import GPT2LMHeadModel, GPT2TokenizerFast
        self.torch = torch
        self.tokenizer = GPT2TokenizerFast.from_pretrained(model_path, local_files_only=True)
        self.model = GPT2LMHeadModel.from_pretrained(model_path, local_files_only=True).eval()
        config = self.model.config
        if (config.n_layer, config.n_embd, config.vocab_size) != (12, 768, 50257):
            raise ValueError('Use the base GPT-2 checkpoint, not another GPT-2 variant')

    def __call__(self, text):
        # Full stored article text, without truncation or extra title/special tokens.
        ids = self.tokenizer(text, return_tensors='pt', add_special_tokens=False)['input_ids']
        length = ids.shape[1]
        if length < 2:
            raise ValueError('Article needs at least two tokens for causal perplexity')
        window = self.model.config.n_positions
        stride = window // 2
        total_nll, total_tokens, previous_end = 0.0, 0, 0
        with self.torch.no_grad():
            for start in range(0, length, stride):
                end = min(start + window, length)
                chunk = ids[:, start:end]
                labels = chunk.clone()
                labels[:, :max(0, previous_end - start)] = -100
                # GPT-2 shifts labels internally: count only actual prediction targets.
                count = int((labels[:, 1:] != -100).sum().item())
                loss = float(self.model(chunk, labels=labels).loss.item())
                if not math.isfinite(loss):
                    raise ValueError('Non-finite GPT-2 loss')
                total_nll += loss * count
                total_tokens += count
                previous_end = end
                if end == length:
                    break
        return math.exp(total_nll / total_tokens)


def filter_corpus(manifest_path, output_database, output_manifest, score):
    """Copy corpus, filter every article equally, rebuild FTS, and publish retained IDs."""
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8-sig'))
    manifest.setdefault('base_article_count', 6407814)
    source = Path(manifest['database']).resolve()
    output_database, output_manifest = Path(output_database).resolve(), Path(output_manifest).resolve()
    if not source.is_file() or output_database == output_manifest:
        raise ValueError('Supply an existing source and distinct output paths')
    if output_database.exists() or output_manifest.exists():
        raise FileExistsError('Do not overwrite the source or existing outputs')
    attack_ids = {d['id'] for d in manifest['documents']}
    if len(attack_ids) != len(manifest['documents']):
        raise ValueError('Duplicate attack IDs in manifest')
    removed_ids, removed_clean = [], 0
    output_database.parent.mkdir(parents=True, exist_ok=True)
    output_manifest.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True)) as src:
        count = src.execute('SELECT count(*) FROM articles').fetchone()[0]
        if count != manifest['base_article_count'] + len(attack_ids):
            raise ValueError('Source count differs from manifest')
        for doc in manifest['documents']:
            if src.execute('SELECT url,title,text FROM articles WHERE id=?', (doc['id'],)).fetchone() != (doc['url'], doc['title'], doc['text']):
                raise ValueError('Source attack document differs from manifest')
        # Exclusive creation prevents accidental replacement of an existing corpus.
        output_database.touch(exist_ok=False)
        with closing(sqlite3.connect(output_database)) as dst:
            src.backup(dst)
            with dst:
                for article_id, text in src.execute('SELECT id,text FROM articles ORDER BY id'):
                    ppl = score(text)
                    if not math.isfinite(ppl) or ppl <= 0:
                        raise ValueError('Invalid article perplexity: ' + str(article_id))
                    if ppl > THRESHOLD:
                        dst.execute('DELETE FROM articles WHERE id=?', (article_id,))
                        removed_ids.append(article_id)
                        removed_clean += article_id not in attack_ids
                # Preserve original tokenizer/ranking schema; refresh global BM25 statistics.
                dst.execute("INSERT INTO article_fts(article_fts) VALUES('rebuild')")
                if dst.execute('SELECT count(*) FROM article_fts_docsize').fetchone()[0] != count - len(removed_ids):
                    raise ValueError('FTS index must use articles as its external content table')
    removed = set(removed_ids)
    manifest['documents'] = [d for d in manifest['documents'] if d['id'] not in removed]
    manifest['base_article_count'] -= removed_clean
    manifest['database'] = str(output_database)
    manifest['name'] += '-ppl'
    manifest['ppl_filter'] = {'model': 'gpt2', 'threshold': THRESHOLD,
        'scope': 'all_articles_before_retrieval', 'text': 'stored_article_text',
        'scoring': 'token_weighted_sliding_window_half_context_stride',
        'removed_clean_articles': removed_clean,
        'removed_attack_ids': sorted(removed & attack_ids)}
    with output_manifest.open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output-database', type=Path, required=True)
    parser.add_argument('--output-manifest', type=Path, required=True)
    parser.add_argument('--model-path', required=True, help='Local base GPT-2 weights and tokenizer')
    args = parser.parse_args()
    filter_corpus(args.manifest, args.output_database, args.output_manifest,
                  GPT2Perplexity(args.model_path))
    print(args.output_manifest)


if __name__ == '__main__':
    main()
