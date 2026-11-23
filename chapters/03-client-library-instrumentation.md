---
chapter_id: client-library-instrumentation
---

# Let the client library instrument the calls

By the end of this chapter you can read what the official OpenAI instrumentation writes for one chat call, say which of its names differ from the GenAI conventions version {{out:diff-hand-and-instrumented:registry version}} that this book follows, and name the variable that decides whether prompts and replies are recorded, with its default. Chapter 1 wrote the model spans by hand. Chapter 2, "Name spans the way the standard does", gave them the standard's names. This chapter asks whether a library can write those spans instead, and where its spans come out different.

Ambrewick's assistant calls its model through the `openai` client library, and that library has packages that wrap the client and write a span for each call. The model calls are the only steps with recorded times in chapter 1, so a package that writes them correctly would spare the book a large part of its hand-made spans. A package that writes them differently changes every query in the chapters after this one, because those chapters read the names. The question is concrete: for one real call, what does the package emit, and where does it differ from what the book would write by hand?

## A call through the client {#a-call-through-the-client}

There are two ways to get a span for a model call. Manual instrumentation is spans your code makes; automatic instrumentation collects telemetry with no change to the application's source code. Chapter 1 used the first kind. This section uses an instrumentation library. An instrumentation library is a package that makes the telemetry for another library's calls: one call in the start-up code installs it, and the zero-code agent, `opentelemetry-instrument`, loads the same package with no change to the calls.

[Example: instrumented-chat-call](../examples/instrumented-chat-call.py)

The run sends one route request through the `openai` client: the route call for the dev day's first message, which `load_messages(...)[0]` replays. The client does not reach a model. Its HTTP exchange is a recording, served by the mock transport of `httpx` from `lib/http_replay.py`. The transport answers every request, so no socket opens even though the URL names the model server. The recorded server sends usage only on a non-streamed reply, so the recording keeps that reply, and a span takes its [token counts](#three-token-counts) from usage. The run also patches the `time_ns` in the SDK's trace module, which the package's spans take their times from, so the call's span gets the virtual times the replay gives. [Replay and the virtual clock](#replay-and-the-virtual-clock) explains the rest. The package's duration [metrics](#totals-from-metrics-not-spans) are not patched, and [what the package emits](#what-the-package-emits) says why.

The trace holds {{out:instrumented-chat-call:spans in the trace}} spans: the message's root, `invoke_agent ambrewick-assistant`, and the call. The package marks the call {{out:instrumented-chat-call:instrumented span, kind}}, as the book marks its own model calls, because the model runs behind a server. The call's span is named `{{out:instrumented-chat-call:instrumented span, name}}`, the operation followed by the model, which is the registry's pattern for an inference span.

The call's usage reads {{out:instrumented-chat-call:input count on the span}} [input tokens](#three-token-counts) and {{out:instrumented-chat-call:output count on the span}} [output tokens](#three-token-counts). These are the usage counts the llama-cpp-python server reported in its reply, which the package copied onto the span. They are not the book's recorded counts, which re-tokenize the prompt. The attribute's name, `gen_ai.usage.input_tokens`, says what the value counts, not where it came from.

The book's code makes only the root; the package makes the call's span inside it. The script asserts that the call's parent is the root, and that its two usage counts equal the reply's, so a change in either fails the run.

**Working rule:** Wrap the client once, at start-up, and make your own spans only for work the library cannot see, such as the message's root, its tool calls and its fallback reply. When no package wraps a client, or the one that does writes names the table can't map, make the call's span by hand, as chapter 1 did. Then check the call's parent in a test, as the script does, because the parent is what places the call in its message's trace.

## What the package emits {#what-the-package-emits}

The package writes one span per call, named for the operation and the model, and the output lists the attribute names on that span in sorted order. The ones this book reads are `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.finish_reasons`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `server.address` and `server.port`. The provider value is {{out:instrumented-chat-call:gen_ai.provider.name on the span}}, the value chapter 2 gives the book's own spans for an `openai` client.

One difference from the book's own spans shows in that list: content. No attribute holds a prompt or a reply, because content capture is off. [The capture switch](#the-capture-switch) says what that switch is and what its default is. The package takes `server.port` from the client's base URL, and leaves it out when that port is implicit or is 443 (`opentelemetry/instrumentation/genai/openai/utils.py` at 1.3b0).

The call makes two metrics, and the output gives their names, units and point counts. `gen_ai.client.operation.duration`, in seconds, has {{out:instrumented-chat-call:metric gen_ai.client.operation.duration, unit s, data points}} data point. `gen_ai.client.token.usage`, in tokens, has {{out:instrumented-chat-call:metric gen_ai.client.token.usage, unit token, data points}} data points, one for input and one for output. The points are split by `gen_ai.token.type`, which is the older conventions' name for that split. It is still a deprecated constant in `opentelemetry-semantic-conventions` {{version:opentelemetry-semantic-conventions}}, and v0.1.0 replaced it with the two token histograms. In this chapter, "metric" and "[histogram](#totals-from-metrics-not-spans)" name the package's instruments only. Chapter 6 defines both.

A streamed call adds two more instruments, `gen_ai.client.operation.time_to_first_chunk` and `gen_ai.client.operation.time_per_output_chunk`, and one span attribute, `gen_ai.response.time_to_first_chunk`. This chapter's example does not stream, so its output prints none of them. The name table holds all three stream names, which the generator observed from the streamed reply it builds, as [hand-made spans against the instrumented ones](#hand-made-against-instrumented) explains.

The chapter prints each instrument's name, unit and point count, and no value, for one reason. The package takes its span times from the clock the script patches, but its duration and chunk instruments read a monotonic timer that the patch does not reach. Their values are therefore the recording machine's real timings, and they change on every run. The token values are fixed, because they are the reply's usage, and they print as counts so that every instrument prints the same way.

**Working rule:** Count a package's spans and its instruments in a test, and read each name, unit and point count from its output. Do not assert a value from an instrument whose timer your tests do not control, because the count holds on every run and the value does not.

## Hand-made spans against the instrumented ones {#hand-made-against-instrumented}

Chapter 2 checks the book's own spans against the registry, and this section measures the package the same way. The names are observed from the package's output, and the mapping to v0.1.0 is the book's judgement, written once in the generator, `examples/data/genai/build_names_map.py`, and checked against the registry. The output came from the recorded call, from a streamed reply that the generator builds for the names a stream adds, and from a call with content events on, which gives the name of the event. One entry comes from the package's source rather than from a run: `gen_ai.usage.cache_read.input_tokens`. The package writes it only when a reply carries cached prompt tokens, and the recorded replies carry none.

[Example: diff-hand-and-instrumented](../examples/diff-hand-and-instrumented.py)

The table holds {{out:diff-hand-and-instrumented:names the package emits, in the table}} names that the package emits. Of those, {{out:diff-hand-and-instrumented:names the same in v0.1.0}} are the same in v0.1.0, and {{out:diff-hand-and-instrumented:names that differ from v0.1.0}} differ. Each of the five that differ is printed with what it became in v0.1.0.

Four of the five differences are in metrics. The registry's metrics file says `gen_ai.client.operation.duration` SHOULD NOT be reported for inference, and it names the inference duration `gen_ai.client.inference.duration`. The token metric is the one that splits: where the package writes one histogram with a `gen_ai.token.type` point attribute, the registry has two histograms, one for input and one for output. The fifth, `gen_ai.token.type`, is the name the older conventions gave that split, and v0.1.0 replaced it with the two token histograms, so a query written from the registry cannot select the package's input points with it.

Because chapter 2 checked the book's own spans against the registry, every gap in this table belongs to the package. The table is what chapters 6 and 7 use when they read a package's output. The first uses are [`totals-from-metrics-not-spans`](#totals-from-metrics-not-spans) in chapter 6 and [`time-to-first-chunk`](#time-to-first-chunk) in chapter 7.

**Working rule:** Keep one table of the names a package emits, generated from its output and checked against the registry, and read the package's data through it. A query for a name the table lacks should fail in the test, as the book's examples fail, and not return nothing in production. Regenerate the table when the package's version changes, not when a chart goes quiet.

## Three packages called openai {#the-three-openai-packages}

Three packages called `openai` instrument the client, and they differ on names and on content. Their names, as PyPI spells them, are `opentelemetry-instrumentation-genai-openai`, `opentelemetry-instrumentation-openai-v2` and `opentelemetry-instrumentation-openai`. The first two are OpenTelemetry's. The third is Traceloop's. Only the first is installed here, at the version the pins hold. The other two are neither installed nor pinned, so this section reads their published wheels, at the versions and dates the table gives, and does not run them. Their versions are written as text for that reason: the pins file does not hold them, and no example prints them.

[Example: three-openai-packages](../examples/three-openai-packages.py)

The output gives each package's project, its naming, its content switch and that switch's default, and it says for each package whether those lines come from the installed source or from a wheel reading. The script checks that `opentelemetry.util.genai.utils`, the module that reads the switch, names the first package's switch.

| Package, version and date | Project | Names | Content switch, default |
| --- | --- | --- | --- |
| `opentelemetry-instrumentation-genai-openai`, {{version:opentelemetry-instrumentation-genai-openai}}, 23 October 2026 | OpenTelemetry | GenAI conventions, experimental set | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`, `NO_CONTENT` |
| `opentelemetry-instrumentation-openai-v2`, 2.4b0, 1 May 2026 | OpenTelemetry | GenAI conventions v1.30.0, unless opted in | `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`, `false` |
| `opentelemetry-instrumentation-openai`, 0.63.1, 16 November 2026 | Traceloop | its own names, from `opentelemetry-semantic-conventions-ai` 0.5.1 | `TRACELOOP_TRACE_CONTENT`, `true` |

The first package's page calls its version a pre-release, and its description says, "The latest experimental features may introduce breaking changes in future releases." The page also says that it uses the experimental GenAI conventions unconditionally, with no opt-in and no version named. The page names no conventions version, but the scope stamps {{out:instrumented-chat-call:instrumented span, scope schema URL}}, beside the book's 1.45.0. The table names no version of the conventions for the first package, because the package states none, and [the earlier section](#hand-made-against-instrumented) measures it against v0.1.0, the version the book follows.

The second package keeps the older naming by default, and its switch has two meanings. On its legacy path the variable takes `true`, and the default is `false`. On its experimental path the variable takes `span_only`, `event_only` or `span_and_event`. Which meaning applies depends on the experimental opt-in, so a team reads the package's version and its opt-in together before it sets the variable. Both OpenTelemetry packages read the same variable, with different values, so a team that moves from the second and keeps `true` gets `NO_CONTENT` from the first, with only a warning in the log.

Traceloop's package names its span `openai.chat`, not the operation and the model, and it writes prompts and completions into the span by default. Its switch is `TRACELOOP_TRACE_CONTENT`, and it stays on unless someone sets it to `false`. Any value other than `true`, `1`, `yes` or `on` turns it off, and a context value, `override_enable_content_tracing`, turns it back on. That is the reverse of the OpenTelemetry package, and it is the difference that matters most in this chapter. A team that adds Traceloop's package to a service that sends private text to a model has changed what its telemetry holds, and it has not set any variable to do so.

For this book we use the first package. It is the one the pins hold, and its names are the registry's, bar the five differences in [the earlier section](#hand-made-against-instrumented). We would choose the second only for a [backend](#otlp-over-http) that reads its older names and nothing else, and the third only for the names its own project defines. Either choice needs its content switch read at the version pinned, with content capture off unless someone turned it on for a stated purpose.

**Working rule:** Read each instrumentation package's content switch and its default from that package's own source or documentation at the version you pin, and not from the spec alone. The defaults differ between packages, so a service that adds a second package changes what its telemetry can hold.

## The capture switch {#the-capture-switch}

Content capture is the instrumentation's switch for recording prompts and replies themselves, and it is off, `NO_CONTENT`, unless set. The first package's default is `NO_CONTENT` and the second's is `false`; Traceloop's 0.63.1 wheel turns it on ([Three packages called openai](#the-three-openai-packages)).

The GenAI conventions v0.1.0 spans page says the switch MUST be named `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT`, and the first package departs from that MUST. It reads a different variable, `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`, and reads the spec's name nowhere. The next run shows what that does. It sets the spec's name to `SPAN_ONLY` and makes one call. Then it does the same with the package's own name, as a control, and counts the content attributes each call's span holds.

[Example: capture-switch-names](../examples/capture-switch-names.py)

The point is the difference between two counts: the spec's name gives {{out:capture-switch-names:content attributes on the span, spec variable set}} content attributes, where the package's own name gives {{out:capture-switch-names:content attributes on the span, package variable set}}, and nothing reports the difference. A team that sets the spec's name gets the output it would get with no setting at all, and that is the failure to guard against. It is quiet, and it looks like success until someone asks for a prompt and finds none.

The package parses four values: `NO_CONTENT`, `SPAN_ONLY`, `EVENT_ONLY` and `SPAN_AND_EVENT`. The package's source at 1.3b0 (`opentelemetry/util/genai/utils.py`) upper-cases the value before it matches a mode, so the match ignores letter case. For anything else it logs a warning and returns `NO_CONTENT`. The output shows both fallbacks. With the package's variable unset, the mode is {{out:capture-switch-names:mode, package variable unset}}. With a value that is not a mode, it is {{out:capture-switch-names:mode, value that is not a mode}}.

Chapter 12, in [What leaks when capture is on](#what-leaks-when-capture-is-on), says what each of the four modes writes and leaks. Here one call records content as a control, and that example prints only how many content attributes its span holds. Every other example in this chapter runs with capture off, and the book keeps it off until chapter 12 turns it on for a stated purpose.

A second switch, `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK`, can upload content to files and write references to them. The package writes those references as `gen_ai.input.messages_ref` and `gen_ai.output.messages_ref`, the registry's names; only the instrumentation's docstring spells them with dots. No run in this chapter emits either one, because the hook also needs `OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH` set, and no run here sets it. Chapter 12 meets the hook.

**Working rule:** Set a package's content variable by the name its own source reads, at the version you pin, and check the setting in a test that asserts the span holds content exactly when the variable names a mode that records it. Leave content capture off in any service that has not named a purpose for it, and read the default of every package the service loads.

## Two other naming schemes {#two-other-naming-schemes}

Two more naming schemes describe the same facts as the GenAI conventions: OpenInference, which Arize publishes, with its own `openinference-instrumentation-openai` package as a fourth package for the client, and OpenLLMetry, the project behind Traceloop's package. We describe both so that a reader who meets their names knows what they stand for. The book emits neither.

OpenInference's module, at its pinned version `{{version:openinference-semantic-conventions}}`, names the model, the provider, the token counts, the span kind and the messages in its own constants. The script reads those constants and sets each one beside the GenAI name for the same fact.

[Example: openinference-mapping](../examples/openinference-mapping.py)

The example prints each OpenInference name beside its GenAI name, and the run checks every pair against the name table, so a constant that moves at the next pin fails the run. The token counts are the pairs a reader meets first: `llm.token_count.prompt` pairs with `gen_ai.usage.input_tokens`, and `llm.token_count.completion` with `gen_ai.usage.output_tokens`.

The script asserts that each GenAI name is one the book's [name table](#hand-made-against-instrumented) holds, so a mapping that names a missing name fails the run. OpenInference's span kind has no GenAI attribute of its own. The output records that a model call is `{{out:openinference-mapping:span kind for a model call}}` in OpenInference, and the operation name carries the same meaning in the GenAI names, as `chat`. Its OpenAI provider value is `{{out:openinference-mapping:llm.system for an OpenAI client}}`, the same string the first OpenTelemetry package writes for an `openai` client.

OpenLLMetry's names come from `opentelemetry-semantic-conventions-ai`, version 0.5.1, released 26 March 2026, which Traceloop's 0.63.1 package uses. The caveat about these readings is in [Three packages called openai](#the-three-openai-packages). Four of OpenLLMetry's names have a registry name that does the same job: `gen_ai.client.operation.duration` for `gen_ai.client.inference.duration`, `gen_ai.client.token.usage` for the input and output histograms, `gen_ai.usage.cache_creation.input_tokens` for `gen_ai.usage.cache_write.input_tokens`, and `gen_ai.usage.reasoning_tokens` for `gen_ai.usage.reasoning.output_tokens`.

We recommend the OpenTelemetry names throughout this book. The backends that chapter 5, [What others do](#what-others-do), describes say they ingest those names, and each claim is given there with its version and date. Choose a scheme for another reason only when a backend you run reads that scheme and nothing else. Do not mix schemes in one trace. A query for `gen_ai.usage.input_tokens` finds no span that writes `llm.token_count.prompt`, and its total comes out short, with no [error](#how-a-span-records-failure) to say so.

**Working rule:** Map each scheme's names onto the registry's once, in a table in code, and fail the run for any name the table does not hold, as the mapping example above does. Choose one scheme for a service's spans before the first span is sent, and keep the names of the others out of its traces.

## What goes wrong in production {#client-library-instrumentation-in-production}

Four things go wrong with the package in production.

- **The pre-release.** The first package is at a pre-release version, and its page warns that its experimental features may change without notice. The pin holds one version. A team that upgrades takes whatever names the next release writes, so the name table must be regenerated, and the run must fail on a name it does not hold.
- **The switch in the wrong place.** The spec's name does nothing with this package, and no error says so. The opposite mistake is worse. A team that adds Traceloop's package and sets nothing has turned prompt capture on, because that package's default is on, in its 0.63.1 wheel, and a context override can turn it back on even when the variable is `false`.
- **Dashboards on the registry's names.** A chart that reads `gen_ai.client.inference.duration` from this package finds no points, since the package writes `gen_ai.client.operation.duration`. [The earlier section](#hand-made-against-instrumented) lists each name that differs.
- **Two packages on one client.** A service that loads a second instrumentation package for the same client may write each call's span twice, under two sets of names. No example here loads two, so this is not shown; a test that counts spans per call would show it.

## The decisions this chapter made {#client-library-instrumentation-decisions}

A package's names lag the standard, and each choice below says how the book lives with that:

- The model calls are wrapped by the OpenTelemetry package pinned at {{version:opentelemetry-instrumentation-genai-openai}}, and the book's own spans wrap only the work the package cannot see.
- The five names that the package writes differently from the registry are mapped once, in a generated table, and the rest of the book reads the package's output through that table.
- Content capture is off in every example in this chapter except one control, which records content for one call and prints only its count. Chapter 12 turns it on, on purpose, and says what it leaks.
- Metrics are printed by name, unit and point count, and never by value, because their duration instruments use real time.
- The book uses the OpenTelemetry names because they are the ones the backends in [chapter 5](#what-others-do) say they ingest, so [changing the backend](#changing-backends) is a change of settings, not of spans.
- The three package names are given exactly, and only the first is run. The other two are wheel readings, as [the section on the three packages](#the-three-openai-packages) explains.

**Working rule:** Pin the instrumentation package at its exact version, generate its name table from its own output, and read its content switch from its source at that version before you trust the content a span holds. A package that changes its names or its defaults changes what your traces hold, and its version and its scopes' schema URL are the signals you get that it has.
