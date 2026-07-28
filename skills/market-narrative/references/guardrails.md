# Market-Narrative Guardrails

The narrative is grounded in two layers so a language model cannot introduce a news
claim, outlet, or event that was not in the supplied headlines. Live implementation:
`app/agents/scanner/narrative_guard.py`.

## Layer 1 — prompt + schema (soft)

- The system prompt instructs: **do not name any news outlet, publication, event, or
  claim not directly present in the supplied headlines.** If there is no relevant
  headline for a point, omit that world-events angle rather than filling it from general
  knowledge.
- The JSON schema forces every bullet to carry a `cited_domains` array. Bullets drawing
  on a headline must list that headline's exact domain string(s); figure-only bullets
  (sector allocation, breadth, congress, cycle) leave `cited_domains` empty.

## Layer 2 — mechanical guard (hard)

`validate_against_context(draft, news_context)` checks the draft against the domains that
were actually fed, and returns a `ValidationResult`:

1. **Fed domain set** — the deduped, `www.`-stripped, lowercased set of domains from
   `news_context.items`. This is the only set of domains a bullet may cite.
2. **Headline check** — if the **headline** names a well-known outlet absent from the fed
   set, the **whole draft is rejected** (`valid=False`) → caller falls back to the
   deterministic narrative. A bad headline is too prominent to fix by dropping bullets.
3. **Per-bullet check** — a bullet is **kept** only if *both*:
   - every domain in its `cited_domains` is in the fed set, **and**
   - its text does not name a well-known outlet absent from the fed set.
   Otherwise the bullet is **dropped**.
4. **Cited domains** — the deduped roots from the kept bullets become
   `MarketNarrative.sources`.

### Known-outlet safety net

`_KNOWN_OUTLET_DOMAINS` maps ~26 well-known outlet names (reuters, bloomberg, cnbc, wall
street journal / wsj, financial times, marketwatch, barron's, yahoo finance, associated
press, the economist, forbes, business insider, axios, seeking alpha, benzinga, zacks,
motley fool, npr, the guardian, new york times, washington post, …) to the domain that
would have to be in the fed set to justify naming them. This catches an outlet name
embedded in free prose even when the bullet carries no matching citation — a safety net
alongside the schema-enforced `cited_domains` tagging. Matching is word-boundary and
case-insensitive.

The guard is pure and side-effect-free (no API calls) — unit-tested in
`tests/test_narrative_guard.py`.

## Tone / safety rules (from the prompt)

- **Hedged, never certain** — read cycle position and risk posture as "likely" or
  "consistent with", never as certainty. The narrative closes with a short, plain-English
  summary of the market's likely short-term direction and risk posture, framed the same way.
- **Not advice** — the rendered `MarketNarrative.not_advice` note is always present on the
  output regardless of which builder ran, keeping the blurb informational rather than a
  buy/sell recommendation.

## Deterministic fallback — when the LLM path is abandoned

`build_llm_narrative` returns `None` (and the caller renders
`build_deterministic_narrative` instead) in any of these cases:

- `ANTHROPIC_API_KEY` is unset (LLM disabled — news is never even fetched).
- News context is `degraded` or has no items (both feeds down/empty).
- The model call returned `None` (import/API error, safety refusal, `max_tokens`,
  or unparseable JSON).
- The guard rejected the headline (`valid=False`), or dropped every bullet (no
  `kept_bullets`).

The deterministic narrative is rule-based, network-free, and always available. It emits
the same `MarketNarrative` shape with `sources=[]`.
