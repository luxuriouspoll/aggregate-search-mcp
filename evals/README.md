# Offline retrieval evaluation

The 34 cases in `cases.json` are manually constructed response snapshots. They cover long-page tail sections, GitHub features/releases, Chinese primary-source targets, reprints, independent-page comparisons, tight shared excerpt budgets, and numeric/boundary `focus` localization. URLs identify acceptable sources, while `FIXTURE_FACT` markers denote synthetic expectations. These snapshots make no claims about the current contents of the linked websites.

Each case records query, category, acceptable sources, expected facts, ranked provider results, document text, and recorded upstream cost/latency (plus optional `focus`, `start_index`, `settings`, `fetch_facts`, and `search_facts`). Replaying a fixed snapshot measures presentation and ranking behavior. Live source reliability requires separate online measurements.

```sh
python scripts/eval_search.py --output evals/result-0.5.4.json --compare evals/baseline-0.5.3-expanded.json
```

The runner creates a temporary configuration directory, strips ambient account/proxy environment variables before importing the app, enforces `SEARCH_NO_ACCOUNT_KEYS=true`, and blocks HTTP calls. When `--compare` is passed, `eval_search.py` verifies matching `fixture_sha256` and case IDs and fails with a non-zero exit code if any per-case `top5_hit`, `fact_retention`, or `search_fact_retention` regresses.

Top-5 hit rate is the fraction of questions with an acceptable source among the first five results. First rank is one-based, with zero indicating no hit. Fact retention measures expected marker presence separately in search excerpts and fetched content. Search/fetch character counts include formatting. Keyed requests and recorded upstream milliseconds come from the snapshot. Replay milliseconds measure local execution only.

Against the expanded 34-case `v0.5.3` snapshot baseline (`evals/baseline-0.5.3-expanded.json`, captured at `2ad822e`), `v0.5.4` keeps Top-5 hit rate at `34/34` (`100%`), raises focused document fact retention from `28/34` to `34/34`, and raises search fact retention from `32/34` to `34/34`.

The reserved `--live` switch exits with an explicit explanation until a separately budgeted live recorder is configured. Offline evaluation never silently sends a network request. These fixtures and reports are excluded from the runtime distributions.
