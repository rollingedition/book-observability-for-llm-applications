---
description: Use when an LLM application's traces, token counts and cost, latency, failures, alerts, release comparisons, sampling or private data have to be worked out from OpenTelemetry GenAI spans. Covers naming spans to the GenAI conventions, tracing a request across an MCP server into one OTLP backend, deriving cached input tokens and pricing a call, finding the slow step and the failed step, setting a burn-rate alert, gating a release on paired traces, choosing a trace sampling policy, and keeping personal data out of telemetry.
---

## Name each span by the GenAI conventions

Use when a backend or another team has to read an LLM application's spans by the standard's names, types and kinds. The conventions are at version {{out:validate-the-spans:GenAI conventions version}}, a Development release whose names may change in any release.

1. Give each model call an inference span named `{operation} {model}`, with the operation `chat`, as [Span types for each step](#span-types-for-each-step) gives. Its kind is CLIENT when the model runs in another process and INTERNAL when it runs in the application's own process.
2. Give each retrieval step a retrieval span, each tool run an `execute_tool {tool}` span and each agent run an `invoke_agent` span, using the table in the same section. [The example](examples/inference-span-names.py) prints the names.
3. Check each span's required attributes against its type, as [Which attributes a span must carry](#requirement-levels) says. A required attribute appears on every span of its type; a conditionally required one appears when its condition holds.
4. Run the validator over the whole run and fail the run on any finding for a span the registry defines, as [Validating against the registry](#validating-against-the-registry) does ([the example](examples/validate-the-renamed-spans.py)).

Check: zero findings on registry spans. A span with no known type gets a finding that says so, which means a span still carries a name from before the rename. The validator checks the kind and the required attributes only, so a conditional case can pass it and still lack an attribute ([Which attributes a span must carry](#requirement-levels)).

## Let the instrumentation package make the model spans

Use when the application calls a model through a client library that ships its own instrumentation, and the spans should come from that package rather than from hand-made code.

1. Install the pinned package, {{version:opentelemetry-instrumentation-genai-openai}}, with `uv pip install -r examples/requirements.txt`, and install the instrumentation once in the start-up code, as [A call through the client](#a-call-through-the-client) does.
2. Make one call and list the span's attributes, as [What the package emits](#what-the-package-emits) does. Set them beside the hand-made spans with [Hand-made spans against the instrumented ones](#hand-made-against-instrumented) ([the example](examples/diff-hand-and-instrumented.py)), and rename any that differ.
3. Before relying on content capture, check which variable the package reads. Set the spec's variable, count the content attributes on the span, and compare with the package's own variable, as [The capture switch](#the-capture-switch) does ([the example](examples/capture-switch-names.py)). A variable the package does not read leaves the span with no content and no error.

Pitfall: each package has its own default for capture. [The capture switch](#the-capture-switch) gives the defaults of the packages the book compares, so read the one you load.

## Trace a request across an MCP server into one backend

Use when a tool server in another process must join the trace of the request that called it, and both processes' spans must land in one store.

1. Make the calling tool span the current span when the request is sent. The MCP SDK then writes `traceparent` into the request's `_meta`, as [The MCP SDK writes traceparent into _meta](#traceparent-in-meta) describes. Check the join: the server's span has the caller's client span as its parent, and all of them share one trace ID, as [The server span joins the trace](#joining-the-trace) shows ([the example](examples/server-span-joins.py)).
2. Keep work that moves to another thread inside the caller's context. `threading.Thread`, `executor.submit` and `loop.run_in_executor` start with an empty context, while `asyncio.to_thread` and `asyncio.run_coroutine_threadsafe` copy it, as [Context is lost at a thread boundary](#lost-context) says. Wrap such functions to run in a copy of the caller's context (the book's `carry()`), or use `opentelemetry-instrumentation-threading` for pools you don't own.
3. Count detached roots apart: root spans other than the agent span, and any server span that arrived with no `traceparent`, as [Context is lost at a thread boundary](#lost-context) defines them. Count orphans apart again: spans whose parent ID names a span the store does not hold, as the same section defines them. The [example](examples/orphan-span-demo.py) prints the detached roots; no example counts orphans.
4. Put only the release and the prompt version in baggage, under the keys the section names, and refuse other values, as [What goes in baggage](#what-goes-in-baggage) does ([the example](examples/baggage-keys.py)). Baggage can carry sensitive information, so nothing else goes in it.
5. Send each process's spans over OTLP/HTTP to one receiver. Each exporter posts to `/v1/traces` and gets HTTP 200 on success, as [The exporter sends each span over OTLP](#otlp-over-http) describes. Check that both processes' rows are in the store, as [The book's receiver holds both processes' spans](#the-book-receiver) does ([the example](examples/two-processes-one-receiver.py)).

Pitfall: the book's receiver keeps its rows in memory for one run. A team needs a store of its own, as [Changing the backend](#changing-backends) explains.

## Derive cached input tokens and price a call

Use when a bill, or a cost per message, has to be attributed to a route, a user or a release, and the runtime reports no cached-token count.

1. Read the input and output counts for each call. Input tokens include the cached part, so adding cached tokens to input counts them twice, as [Three counts for each call](#three-token-counts) says.
2. Derive the cached count where the runtime does not report it. Cached input is the prompt tokens minus the tokens the runtime evaluated; the evaluated count is a difference between two calls, because the counters accumulate, as [Cached input, derived from the counters](#cached-prompt-tokens) shows ([the example](examples/cached-tokens-from-replay.py)). A reuse counter that counts graph reuse, not tokens, is not the cached count.
3. Price each call from a dated price table of rates per million tokens. Cached input is billed at its rate and the rest of the input and the output at theirs, from the row that applies on the call's date, as [The price table](#the-price-table) describes ([the example](examples/price-one-request.py)).
4. Before pricing, check how each provider reports input: Anthropic's `input_tokens` excludes cached tokens, and the conventions ask instrumentation to add them in, so add them before pricing ([The price table](#the-price-table)).
5. Compute cost at query time from the table, not from a stored cost, and attribute it to route and user as [Cost by route and step](#cost-per-route) does ([the example](examples/cost-by-route-and-user.py)). Total tokens from the meter's counters rather than by summing each span's usage, which double counts, as [Summing spans double counts](#totals-from-metrics-not-spans) explains ([the example](examples/totals-metric-against-spans.py)).

## Read latency tails with an interval

Use when the median message is fast enough and the slowest messages are not, and the question is which step makes them slow.

1. Walk the critical path back from the root's end: take the child that ends last, then the child that ends last before it starts, until the walk reaches the root's start, as [The critical path sets the end time](#the-critical-path) describes ([the example](examples/critical-path-one-message.py)).
2. Split a streamed model call at its first chunk. [Time to first chunk](#time-to-first-chunk) gives the wait from sending the call to the first streamed piece, and [Prefill against generation](#prefill-against-generation) separates the model reading the prompt from the model writing the reply ([the example](examples/prefill-generation-split.py)).
3. Take nearest-rank percentiles and state the count, n, beside each. Put an interval on each: the middle 95% of 2,000 bootstrap resamples of messages, not of spans, as [Tail percentiles with intervals](#tail-percentiles-with-intervals) does ([the example](examples/percentiles-by-route.py)).
4. Read a percentile from histogram buckets only to within its bucket, as [Histogram buckets and what they hide](#histogram-buckets-and-what-they-hide) says ([the example](examples/bucket-edges-hide-tail.py)).

Pitfall: twenty messages cannot support a tail percentile, as [Tail percentiles with intervals](#tail-percentiles-with-intervals) explains. Read the n with every percentile.

## Find which step failed

Use when users got no useful answer and the question is which step failed, how often, and why.

1. Count messages that had an error. A message has an error when any of its spans has status ERROR. Count messages or attempts, never ERROR spans, because one fault can leave several, as [MCP errors](#mcp-errors) and [How a span records failure](#how-a-span-records-failure) set out ([the example](examples/error-status-by-step.py)).
2. Split failures by `error.type`: the class name of the exception the caller sees, or the registry value where one exists, `tool_error` for an MCP result with `isError` set, which raises nothing ([MCP errors](#mcp-errors)). A span with no `error.type` is still an error, so never filter on the attribute to find failures ([How a span records failure](#how-a-span-records-failure)) ([the example](examples/error-status-by-step.py)).
3. Find replies cut at the token cap: a finish reason of `length` in `gen_ai.response.finish_reasons`. [Replies cut at the cap](#replies-cut-at-the-cap) explains that such a reply raises nothing, so the status rule misses it ([the example](examples/truncated-replies.py)).
4. Find wrong routes by comparing each route's choice with the labelled expected route. The route span holds that label in an event. A fallback reply is right only for a message that no tool or article resolves, as [Wrong routes against the labels](#wrong-routes-against-labels) says ([the example](examples/wrong-routes-by-label.py)).
5. Write one query for each failure mode over the receiver's rows, and keep the baseline and the incident day apart, as [Querying each failure mode](#querying-each-failure-mode) does ([the example](examples/failure-modes-query.py)).

Pitfall: keep both counts and label each: errors by span status ([How a span records failure](#how-a-span-records-failure)) and failed messages, those whose user got no reply ([Service-level objectives](#service-level-objectives)). A refused request whose retry got a reply, or a lookup that timed out on both attempts and still got a reply, is an error but not a failed message.

## Set a burn-rate alert

Use when someone must be paged about a failure rate, not about each failure.

1. Set the objective: the share of messages that must get a reply over the period. The error budget is 1 minus that share, as [Service-level objectives](#service-level-objectives) defines them ([the example](examples/slo-and-budget.py)).
2. Compute each window's burn rate as its failed share of messages divided by the error budget. Read each window as the difference between two cumulative reads of the same histogram, as [Burn rate](#burn-rate) does ([the example](examples/burn-rate-by-window.py)).
3. Page only when the one-hour window and the five-minute window both reach 14.4, and the hour holds at least 10 messages. The short window clears the page once the spend stops, as [An alert that stays quiet](#an-alert-that-stays-quiet) sets it ([the example](examples/alert-on-incident-day.py)).
4. Run the same alert over a quiet day and check that it pages nowhere, as [Test the same alert on a quiet day](#quiet-day-on-baseline-day-two) does ([the example](examples/alert-quiet-on-baseline-day-two.py)).
5. Keep each metric's labels to a few values each, such as error type, operation and model; never a user, since a label per user adds a series per user, as [Cardinality](#cardinality) explains ([the example](examples/cardinality-of-attributes.py)).

Pitfall: the 14.4 comes from the *Site Reliability Workbook*'s traffic ([Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/)). Treat it as a starting point, as [An alert that stays quiet](#an-alert-that-stays-quiet) says. A fault the model answers around, like the timeout window, never reaches the error objective, so watch the tool's ERROR attempts for it ([One page on the incident day](#an-alert-that-stays-quiet)).

## Gate a release on paired traces

Use when a release claims to change nothing that matters, and the question is which numbers moved, on the same messages.

1. Mark each release: `service.version` on the resource and `gen_ai.prompt.version` on each inference span, so a query splits the traces by release, as [Release markers in the spans](#release-markers-in-the-spans) says ([the example](examples/release-markers-query.py)).
2. Run the baseline and the candidate on the same messages. For each message, take the candidate's value minus the baseline's, for latency, tokens, cost, errors and route, as [Paired comparison on messages](#paired-comparison-on-messages) does ([the example](examples/paired-latency-and-tokens.py)).
3. Put an interval on each difference, and gate with the regression rule in [The CI gate](#the-ci-gate): a release is flagged when the end of its interval nearest the limit lies past the limit. A flag therefore means worse than the limit beyond the 95% interval ([the example](examples/gate-on-prompt-release.py)).
4. Run the same gate on a second baseline day. It must not flag ([the example](examples/gate-on-baseline-day-two.py)).

## Choose a trace sampling policy

Use when keeping every trace costs too much, and the kept set must still hold the failures and the slow tail.

1. Start with head sampling at a fixed ratio on the trace ID. Every span of a trace gets the same answer on every run, but the sampler cannot aim at failures, as [Head sampling decides before the trace runs](#head-sampling) explains ([the example](examples/head-sample-ratio.py)).
2. Make each span follow its parent's sampled flag, so a server in another process keeps the spans of a sampled trace, as [Children follow the root's decision](#parent-based-sampling) says ([the example](examples/parent-based-follows-root.py)).
3. Add tail rules, which decide once the whole trace is in. The chapter's rule keeps a trace whose message has an error, one past the latency objective, and one whose model calls cost more than a baseline percentile, as [Tail sampling reads the whole trace](#tail-sampling) sets out ([the example](examples/tail-rule-keeps-errors.py)).
4. Estimate totals only for a split that the counters cannot make. Scale a head sample's total by the inverse of its ratio. Print its 95% interval, the estimate plus or minus 1.96 standard errors from the sampled traces alone. This is the sample's own interval, not the bootstrap of [Tail percentiles with intervals](#tail-percentiles-with-intervals). Check the estimate against the counter total for the same day where one exists. At small kept counts the interval holds the full count less often than its label says. Do not scale tail-kept traces by the ratio, since they are not a fixed share of the day, as [Estimate the day's totals from a sample](#estimating-totals-from-a-sample) explains ([the example](examples/estimate-totals-error.py)).
5. Set the retention period with the team that runs the backend. The chapter sets none, and [Retention](#retention) says why.

## Keep personal data out of telemetry

Use when spans could carry user names, email addresses, user IDs, or the people and places a request's text mentions.

1. Leave content capture off unless a stated purpose needs prompts or replies. Check the default of each package the service loads, as [Capture off comes first](#capture-off-by-default) says ([the example](examples/capture-modes.py)).
2. If capture is on, the content lands in span attributes or in log records, depending on the mode, so check where each mode writes, as [What leaks when capture is on](#what-leaks-when-capture-is-on) shows ([the example](examples/leak-with-capture-on.py)).
3. Redact before the span is stored. Redact in every process with a span processor that exports a redacted copy as each span ends, because the SDK freezes a span's attributes at that point. Redact again where every service's spans pass, such as the Collector, because a process's redactor never sees another process's spans, as [Redaction is defence in depth](#redaction-as-defence-in-depth) describes ([the example](examples/redact-and-hash.py)).
4. Replace each user ID with a keyed pseudonym: an HMAC-SHA256 of the ID under a key, kept to twelve hexadecimal digits. Keep the key out of the code and out of the telemetry. The book's key is a demonstration, as [Keyed user pseudonyms](#keyed-user-pseudonyms) says.
5. Drop `exception.stacktrace` and keep the exception event's class and message, rather than replacing the stack trace, since its file paths name the folders of the process that ran the code, with capture off as well. Do it where every service's spans pass. Ask the team that owns each server to set `record_exception=False` and record the class and message by hand ([Stack traces leak without capture](#stack-traces-leak-without-capture)). The [example](examples/leak-with-capture-on.py) shows the leak, and [the scan](examples/scan-after-redaction.py) shows both processes' stack traces removed by redaction at the receiver.
6. Where content must be kept, store it by reference: a `*_ref` attribute on the span, and the content in a store with its own access rules, as [Keep content by reference](#keep-content-by-reference) describes ([the example](examples/content-by-reference.py)).
7. Scan what is left for the values redaction should have removed, and count them. Seed the test traffic with known identifiers; after redaction their count must be zero. Count, and don't assert, the free-text names the redactor can't recognise, as [Count what redaction leaves](#scanning-and-counting) explains ([the example](examples/scan-after-redaction.py)).
