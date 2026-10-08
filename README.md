# FORGE

Research code for *From Poisoned Evidence to Research Drift in Deep Research Agents*.

Includes document construction, three framework adapters, QP/KE/RQA/LLM-judge defenses, a minimal PoisonedRAG adaptation, and E/P/A/T evaluation utilities. Datasets, model resources, credentials and experimental outputs are supplied externally.

## Setup

Python 3.12+, Node.js 22+, Windows.

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

Configure credentials in `.env` and install the resources reported by the setup check.

## Usage

```powershell
python forge.py construct --help
python forge.py init --help
python forge.py run --help
python forge.py score --help
```

Frameworks: `gpt-researcher`, `perplexica`, `webthinker`.

Defenses: `none`, `query_paraphrasing`, `knowledge_expansion`, `root_query_anchoring`, `llm_judge`.

Runs use external frozen corpus manifests. Construction accepts only `target_narrative`, `topic_keywords` and `question_direction`. PoisonedRAG exposes external generation and review callbacks under `code/baselines/`.

Scoring accepts finalized annotations or CSV columns `model,framework,condition,question_id,repetition,E,P,A,T`. CSV cohorts require 100 questions and three repetitions; specify `--score-scale fraction` or `percent`. Empty E/P/A stages are rejected.
