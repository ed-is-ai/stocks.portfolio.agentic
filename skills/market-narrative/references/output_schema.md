# Market-Narrative Output Schema

Three shapes are involved, in order of the pipeline:

1. **Raw model output** — constrained by a JSON schema at call time.
2. **Parsed draft** (`LlmNarrativeDraft`) — the raw output validated into pydantic,
   before the citation guard runs.
3. **Rendered narrative** (`MarketNarrative`) — the provider-agnostic object the digest
   email and web banner actually render, produced after the guard drops any ungrounded
   bullets. The deterministic fallback builder produces this same shape directly.

## 1. Raw model output — JSON schema

The model is forced to return exactly this shape (`_NARRATIVE_SCHEMA` in
`app/integrations/anthropic_client.py`). `additionalProperties` is `false` throughout;
`headline` and `bullets` are required, and each bullet requires both `text` and
`cited_domains`.

```json
{
  "type": "object",
  "properties": {
    "headline": { "type": "string" },
    "bullets": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "text": { "type": "string" },
          "cited_domains": { "type": "array", "items": { "type": "string" } }
        },
        "required": ["text", "cited_domains"],
        "additionalProperties": false
      }
    }
  },
  "required": ["headline", "bullets"],
  "additionalProperties": false
}
```

`cited_domains` lists the exact source domain string(s) of the headline(s) a bullet
draws on, and is **empty** for bullets based only on the supplied deterministic figures
(sector allocation, breadth, congress, cycle). See [`guardrails.md`](guardrails.md) for
how these citations are enforced.

## 2. Parsed draft — `LlmNarrativeDraft`

`app/schemas/llm_narrative.py`. The raw output validated into pydantic before guarding:

```python
class LlmNarrativeBullet(BaseModel):
    text: str
    cited_domains: list[str] = []   # empty ⇒ figure-only bullet, nothing to hallucinate

class LlmNarrativeDraft(BaseModel):
    headline: str
    bullets: list[LlmNarrativeBullet] = []
```

## 3. Rendered narrative — `MarketNarrative`

`app/schemas/market_narrative.py`. Deliberately provider-agnostic: both the LLM path
(after guarding) and the deterministic fallback produce this identical shape, so the
email and web banner render whatever they are given without caring how it was built.

```python
class MarketNarrativeSource(BaseModel):
    label: str            # e.g. the source domain
    url: str | None = None

class MarketNarrative(BaseModel):
    headline: str
    bullets: list[str] = []                 # flat text, guard-approved only
    sources: list[MarketNarrativeSource] = []  # from kept bullets' cited domains; [] for deterministic
    not_advice: str = "Informational only — not financial advice."
    generated_at: str = ""                  # ISO timestamp, set by the builder
```

Only guard-approved bullets and their confirmed cited domains flow from the draft into
`MarketNarrative.bullets` / `.sources`. The `not_advice` note is always present.
