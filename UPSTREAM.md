# Third-party source

This release retains supplied upstream source for GPT-Researcher, Perplexica, and WebThinker under `vendor/`. Their original license files and source copyright notices are preserved. The adapters under `code/frameworks/` connect these implementations to the local experiment sandbox.

| Component | License notice |
| --- | --- |
| GPT-Researcher | `vendor/gpt-researcher/LICENSE` |
| Perplexica | `vendor/perplexica/LICENSE` |
| WebThinker | `vendor/webthinker/LICENSE` |

The source package did not provide verified revision provenance. Manuscript revision IDs are references, not verified checkout identities. Only dependency source used by the experiment entrypoints is retained: the GPT-Researcher package, Perplexica's statically reachable native modules, and WebThinker's report runner and imported support modules. Unused user interfaces, demos, deployment scaffolding and alternative launchers were removed. This is not a standalone upstream distribution.
