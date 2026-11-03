# ADR-0004: Local deepseek-r1:8b via Ollama, structured outputs, deterministic graph

- Status: Accepted · 2026-10-08

## Decision
- All LLM work uses `langchain-ollama` `ChatOllama(model="deepseek-r1:8b", reasoning=True, temperature=0)`
  against the host's Ollama; embeddings use `nomic-embed-text`.
- The agent workflow is a LangGraph `StateGraph` with deterministic routing. The model fills typed
  schemas (`with_structured_output(..., method="json_schema")`) rather than choosing tools freely, because
  an 8B reasoning model is unreliable at tool selection. Reasoning text is kept out of outputs and stored
  in the trace.
- Human-in-the-loop via `interrupt()` at gates T1/T2/T3; state in `PostgresSaver` (schema `orchestrator`).
- The model only sees pseudonymised text (privacy-service), satisfying the "data stays local/EU" promise.

## Consequences
+ No data leaves the machine; predictable flow; easy to test with fake chat models.
− Slow inference; mitigated by bulkhead(2) + long timeout and async runs. Model swappable via Admin.

## Amendment · 2026-10-08 (milestone 4, measured live)
- **Temperature 0.6, not 0.** At temperature 0 deepseek-r1:8b repeated the same text inside a JSON field until the
  token limit (a real support email, twice, both past the 180 s timeout). DeepSeek recommends 0.5–0.7.
- **Bounded schemas.** Every string in an LLM output schema has `max_length`, every list `max_length` (items);
  Ollama compiles these into the sampling grammar, so the model cannot ramble inside a field. Validators also trim
  and normalise ("None" → null, keep the id from "SITE-A014 (machine …").
- **Token cap** `num_predict=2048`: at ~14–23 tokens/s on this PC a bad answer still ends inside the timeout.
- Result on the same email: 23–58 s per intake instead of timeouts. Answers now vary slightly between runs
  (e.g. sev2 vs sev4); the approver sees "AI-generated · check before approving" and can change severity.

