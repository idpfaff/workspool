# Bundled tokenizer data

Workspool ships the standard OpenAI `cl100k_base` and `o200k_base` byte-pair
encoding ranks so named token budgets work offline, including a fresh install
with an empty or disabled tiktoken cache. The optional `tokens` extra supplies
the tiktoken implementation; no model or vocabulary download occurs at runtime.

The rank bytes are unchanged, compressed with gzip (level 9, timestamp 0).
`budget.py` validates their uncompressed size and SHA-256 before constructing
`tiktoken.Encoding` directly. Patterns and special-token definitions are pinned
to [OpenAI tiktoken 0.14.0](https://github.com/openai/tiktoken/blob/0.14.0/tiktoken_ext/openai_public.py).
Content containing special-token spellings is counted as ordinary text.

| Encoding | Official rank source | Uncompressed bytes | SHA-256 |
|---|---|---:|---|
| cl100k_base | [OpenAI ranks](https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken) | 1,681,126 | `223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7` |
| o200k_base | [OpenAI ranks](https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken) | 3,613,922 | `446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d` |

These hashes are the upstream expected hashes. Assets were copied from a local
cache and verified against them; the cache location is not used by Workspool.
To update, verify the new upstream definitions and hashes, replace the assets,
and rerun exact token-ID, offline subprocess, and package-content tests.

Third-party tokenizer material retains the upstream MIT license and copyright
notice in [LICENSE.tiktoken](LICENSE.tiktoken), from the
[official project](https://github.com/openai/tiktoken/blob/0.14.0/LICENSE).
Workspool's Commons Clause does not replace this third-party license.
