# Offline retrieval evaluation

The 25 cases in cases.json are manually constructed response snapshots. They cover long-page tail sections, GitHub features/releases, Chinese primary-source targets, reprints and independent-page comparisons. URLs identify acceptable sources, while FIXTURE_FACT markers denote synthetic expectations. These snapshots make no claims about the current contents of the linked websites.

Each case records query, category, acceptable sources, expected facts, ranked provider results, document text and recorded upstream cost/latency. Replaying a fixed snapshot measures presentation and ranking behavior. Live source reliability requires separate online measurements.

```sh
python scripts/eval_search.py --output evals/result-0.5.3.json --compare evals/baseline-0.5.2.json
python scripts/eval_search.py --source-root /path/to/89fd46f-checkout --output evals/baseline-0.5.2.json
```

The runner uses a temporary configuration directory, clears account settings and blocks HTTP. It exercises the production merge, reprint folding and search formatting code. Document adapters are replaced by snapshot responses; adapter and cache behavior are covered by selftest. On versions supporting focus, the query is passed as focus. The baseline uses the earlier sequential-reading API.

Top-5 hit rate is the fraction of questions with an acceptable source among the first five results. First rank is one-based, with zero indicating no hit. Fact retention measures expected marker presence separately in search excerpts and fetched content. Search/fetch character counts include formatting. Keyed requests and recorded upstream milliseconds come from the snapshot. Replay milliseconds measure local execution only.

The baseline was recorded against 89fd46f. Both versions retain all expected search facts and achieve 100% Top-5 hits on these fixtures. Focused long-document fact retention rises from 0% to 100%. The committed JSON reports include all per-case measurements and local processing overhead.

The reserved --live switch exits with an explicit explanation until a separately budgeted live recorder is configured. Offline evaluation never silently sends a network request. These fixtures and reports are excluded from the runtime distributions.
