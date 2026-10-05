# FORGE

Code release for *From Poisoned Evidence to Research Drift in Deep Research Agents*. Included code covers FORGE document construction, three framework adapters, frozen run configuration, QP/KE/RQA defenses and aggregation of externally finalized E/P/A/T annotations.

This release excludes datasets, credentials, model resources and experimental outputs. Construction is adapted from the supplied construction source and corrected against the paper; its exact archived equivalence is unverified. PPL filtering is external. See [implementation scope and limitations](docs/PAPER_ALIGNMENT.md); this package is not a complete reproduction of the paper.

## Setup

Launchers target Python 3.12+, Node.js 22+ and Windows. A clean installation and other platforms have not been validated.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements\python.txt
npm ci
npx playwright install chromium
node tools\build_perplexica.cjs
Copy-Item .env.example .env
python forge.py check-setup --code-only
```

Supply authorized API credentials in `.env`. Obtain BGE-small-en-v1.5, WebThinker tokenizer and NLTK resources separately at the paths checked by `tools/check_setup.py`. Launchers import the retained framework source under `vendor/`. Statistical analysis targets the paper's Python 3.12.14 and NumPy 2.3.5; validation limits are documented in the alignment note.

## FORGE document construction

Supply an external JSON object containing only `target_narrative`, `topic_keywords` (a nonempty string list), and `question_direction` (a broad direction). Never supply the full root question or agent-generated subtasks. Set `FORGE_API_KEY` in the process environment and supply the authorized ChatGPT 5.6 Terra API ID and compatible endpoint explicitly.

```powershell
python forge.py construct --inputs path\to\construction_inputs.json --output runs\construction_q001 --model YOUR_CHATGPT_5_6_TERRA_API_ID --api-base YOUR_AUTHORIZED_V1_ENDPOINT --review-temperature 0 --review-max-tokens 5000
```

The example's review temperature and cap are explicit operator choices; the paper does not specify them. Generation uses temperature 0.2 and a 1,200-token cap. Five sequential core documents carry actual conclusion/qualification/dependency summaries; five objection responses use the completed chain. Keyword titles/prefixes are separate from the 180-220-word body. Every audit checks all 45 pairs and all ten documents, including objection coverage. Up to seven repair rounds refresh dependent drafts, summaries, titles and prefixes. A failing set produces no `frozen.json`.

Accepted output records texts, local-corpus Wikipedia-style titles/URLs, protocol and hashes. These are synthetic inserted records, not authentic Wikipedia pages. The destination must be new; the builder never loads victim outcomes or selects drafts using them. Corpus preparation remains external: assign database IDs and insert the frozen `title`, `url`, `text` records before running victims. Traces contain generated content and remain under ignored `runs/`.

## External frozen corpus

Provide a prepared Wikipedia corpus database and manifest containing `name`, `database`, `documents` (`id`, `url`, `title`, `text`) and `queries` (`query_id`, `query`, `target_claim`, `adoption_rule`). Data preparation must finish before victim runs. For PPL experiments, supply the externally filtered corpus and its metadata; no filtering implementation is included.

```powershell
python tools\attach_dataset.py --manifest path\to\dataset.json --database runs\corpus\articles.sqlite --output runs\corpus\dataset.json
python forge.py init --family gemini --model YOUR_GEMINI_3_6_FLASH_API_ID --defense root_query_anchoring --name q001_r1 --dataset runs\corpus\dataset.json --query-id QUERY_ID --framework gpt-researcher
python forge.py run --name q001_r1 --framework gpt-researcher
```

Frameworks: `gpt-researcher`, `perplexica`, `webthinker`. Defenses: `none`, `query_paraphrasing`, `knowledge_expansion`, `root_query_anchoring`. Paper defenses use Gemini 3.6 Flash. GPT main conditions use `--family gpt` with the ChatGPT 5.6 Terra API ID and `--defense none`. Initialization freezes the explicit model and protocol. Use separate run names per question/condition/framework/repetition.

WebThinker assistant-prefix continuation is disabled until provider support is independently verified. Record that verification at initialization using `--verified-partial-continuation` and `--continuation-strategy partial` (or `vllm_continue`, matching the verified provider). Configuration, source and database hashes are frozen at initialization and checked before execution; rebuild Perplexica before initialization. Do not edit saved configuration after freezing. Unsupported continuations fail rather than silently adding new user instructions. See the alignment note before using that framework for reproduction.

## Finalized annotations and checks

```powershell
python forge.py score --annotations path\to\finalized_annotations.json --output runs\scores.json
python forge.py score --finalized-csv path\to\finalized_scores.csv --score-scale fraction --output runs\archived_scores.json
python -B -m unittest tools.test_construction tools.test_paper_alignment tools.test_paper_metrics tools.test_completion_adapter
node tools\test_rqa.cjs
```

The finalized CSV path preserves supplied scores without reannotation and requires all 100 questions x three repetitions per cohort, including zeros. Its columns are `model,framework,condition,question_id,repetition,E,P,A,T`; explicitly choose `fraction` (0-1) or `percent` (0-100), including T in that scale. It reports trajectory-equal means, 50,000-resample question-cluster percentile intervals and sample SD across the three run means. The annotation path supports independently finalized event labels for new runs; it does not judge evidence or recover missing paper CSVs. T measures complete-report endorsement. The annotation schema and missing experimental pipelines are documented in the alignment note.

## Licenses and anonymous publication

Project code uses `LICENSE`; retained third-party code preserves its license notices listed in [UPSTREAM.md](UPSTREAM.md). The source tree excludes data, generated archives and credentials. For anonymous review, use a source-content mirror that omits Git history, commit metadata and links back to the hosting account. Preserve third-party licenses and attribution in the mirror.
