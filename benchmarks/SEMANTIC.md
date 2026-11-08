# Local semantic recall: measured behavior and limits

Recorded November 7, 2026. Workspool's optional learned retrieval ran with a
real local quantized **BAAI/bge-small-en-v1.5** ONNX model. On five manually
written questions about a fixed Wikipedia subset, **both lexical FTS5 and
hybrid retrieval found the target article first in all five cases**. This
sample establishes no retrieval advantage over the lexical control. The
hybrid path was slower and required building a vector cache.

A separate synthetic diagnostic demonstrates its narrower purpose: four
queries with no literal matching words retrieved the intended passage first
with the real model, while lexical search returned no hits. Those four
controlled examples are not a representative accuracy benchmark.

The [published evidence](results/2026-11-07-semantic/) contains public source
identities, hashes, questions, ranks, timings, and validation counts. Article
passages, private paths, raw MCP traces, and generated workbook IDs remain
local. No model API calls or paid inference were used.

## Genuine Wikipedia test

The selection is the **first 32 entries** in the acquired Wikipedia manifest:
2,961,912 bytes of original revision wikitext from the public dump described in
[PUBLIC_DATA.md](PUBLIC_DATA.md). It is a small, deterministic selection from
8,192 acquired articles, mostly early page IDs and titles beginning with A.
It is neither random nor representative of the whole corpus. Markup,
references, and templates remain in the text.

The questions and target titles were fixed after inspecting the actual source
articles and before running retrieval. Targets were not selected from search
results, and queries were not rewritten after seeing rankings. The full
questions and exact revision URLs are in
[wikipedia.json](results/2026-11-07-semantic/wikipedia.json).

| Manually chosen target | Question topic | Lexical rank | Hybrid rank |
| --- | --- | ---: | ---: |
| Albedo | Snow reflecting more sunshine than dark roads | 1 | 1 |
| International Atomic Time | Combining laboratory clocks into a continuous reference | 1 | 1 |
| Altruism | Helping someone despite personal sacrifice and without repayment | 1 | 1 |
| ASCII | English text and control signals encoded with seven binary digits | 1 | 1 |
| An American in Paris | Gershwin's orchestral depiction of a visitor, using cab horns | 1 | 1 |

Both arms achieved target-article recall@1 and recall@5 of **5/5**. Rank is the
first occurrence of the target article among the returned passage slots;
several slots can come from the same article. This is not answer correctness,
five distinct-document ranking, or a claim that every returned passage fully
answers its question. These natural-language questions retain useful lexical
cues, so the lexical control is strong here.

All **50 returned citations** across the two arms were independently checked
against the original source files at their exact UTF-8 byte ranges. Source
hashes, model assets, and measured runtime files were unchanged during the
corpus build and question comparison.

## Indexing cost and bounded progress

The selected scope contains **1,942 exact source chunks**. The complete vector
index required **16 calls and 150.52 seconds** in this run: at most 128 new
unique chunks and a 60-second embedding deadline per call. The final call
reported all 1,942 chunks scored, zero pending chunks, and complete lexical
and dense indexing coverage. Later question lookups used the retained cache.

An earlier [budget pilot](results/2026-11-07-semantic/build-budget-pilot.json)
attempted 512 full wikitext chunks per worker with a 30-second deadline. Both
attempts timed out, built zero vectors, and explicitly fell back to lexical
retrieval. The smaller batches enabled bounded progress. The benchmark's
60-second build deadline is explicit; the ordinary runtime embedding deadline
defaults to 20 seconds.

| Warm lookup, five questions | Median elapsed time |
| --- | ---: |
| Persistent lexical FTS5 | 5.06 ms |
| Persistent vectors plus local BGE query embedding and RRF | 402.47 ms |

These are one fixed-order local trial with other public-data checks running
on the same machine. They include process/model startup on the hybrid path
and warm filesystem effects. They are not isolated latency estimates or
evidence that embeddings improve every query.

The model is the 384-dimensional FastEmbed quantized ONNX distribution, with
67,179,163 bytes across five sealed assets. Its manifest SHA-256 is
`689e49ed3d5550db29f2ca5787a9cd71f0dfe8969a463be39d2933b6a04d7361`.
The model, tokenizer, and engine versions form the vector-cache identity.
Runtime workers use `local_files_only=True`, explicit local assets, CPU
execution, and disabled socket connects. Model-file hashes were checked
before and after the probe. Provisioning is a separate explicit download.

`dense_complete` means the selected indexed chunks have valid vectors and
were scored within the scan bound. It does not mean exhaustive semantic
understanding. This English model has a 512-token input limit; a long wikitext
chunk may be truncated by the model even though its returned citation remains
the exact original chunk. The runtime reports that possibility. This test
does not establish dense coverage over all 8,192 articles.

## Actual MCP budget and session restoration

Two fresh `runtime`-profile MCP processes used the already built index, a
reader identity, the local model, and `o200k_base`. The first received a
scripted `recall` call for the ASCII question with a **1,200-token limit**.
The second received that question as a configured startup goal and performed
only MCP initialization: **zero agent tool calls** requested context.

| Delivery | Measured `o200k_base` tokens | Evidence |
| --- | ---: | --- |
| Complete serialized MCP recall result | 897 | One exact ASCII citation, target rank 1 |
| Restored initialization context, including its warning prefix | 868 | One exact ASCII citation, target rank 1 |

Both limits held, and both returned citations were independently verified.
The first session's ordinary setup instructions and listed facade cost 443
additional tokens, measured separately. Startup's 1,200-token limit applies
to the restored context section, not all initialization instructions. These
counts exclude provider framing and billing; the calls were scripted, not
chosen by an LLM.

The two MCP checks were repeated after a later server error-capture fix.
Their source fingerprints and stability check are scoped separately under
`mcp` in the evidence. The earlier index-build and five-question measurements
retain their original fingerprints; those unchanged computations were not
rerun for that error-handling change.

## Separate zero-overlap diagnostic

[synthetic-diagnostic.json](results/2026-11-07-semantic/synthetic-diagnostic.json)
records 20 explicitly synthetic passages and four preselected queries:
`retirement eligibility`, `car repair`, `restore account access`, and
`profits decline`. All four had zero lexical hits and a target rank of 1
with real BGE inference. Every returned citation was exact. The first call
built 20 vectors in 0.359 seconds; a reopened warm call took 0.283 seconds
and built no new vectors. This shows a concrete zero-overlap capability,
with no claim about general recall, production accuracy, or public-corpus
improvement.

## Reproduce

Install dependencies and explicitly provision model/tokenizer files once:

```sh
uv sync --locked --extra semantic --extra tokens --extra benchmark
uv run workspool model-download .local-models/bge-small-en-v1.5
uv run python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base"); tiktoken.get_encoding("o200k_base")'
```

Acquire the Wikipedia manifest using the [public-data reproduction
instructions](PUBLIC_DATA.md#reproduce). To reproduce the historical probe, use
the archived measured source (or its v0.8.1 checkout) in a fresh output directory.
Its initialization-context check was removed from the product in v0.9.2; this is
not a current host-readiness command:

```sh
uv run python benchmarks/semantic_probe.py \
  --manifest .local-benchmarks/public-replay/wikipedia/documents/manifest.json \
  --model-dir .local-models/bge-small-en-v1.5 \
  --output .local-benchmarks/semantic-replay
```

The default selection is 32 articles, with at most 20 build calls of 128 chunks
and 60 seconds each. Partial coverage, timeouts, and retrieval misses remain
visible; validation does not require hybrid retrieval to beat lexical search.
The model assets and source manifest hashes identify the measured data. Four
focused harness tests cover fixed selection, independently read byte
citations, ranking with misses, and the publication allowlist; they do not
mock a claim of semantic quality.
