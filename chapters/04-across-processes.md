---
chapter_id: across-processes
---

# Follow a request across processes

By the end of this chapter you can make a span that another team's server writes join the trace of the message that caused it, check that it has joined, and find the spans that have not. The problem starts with one request. Message 16297 on baseline day 1, a recorded day of 200 messages, asks about a contact, so the router sends it to the `messages` route, which calls `find_contact`. That tool runs on the contacts team's MCP server, a separate process that the assistant starts and talks to over stdio, and the server writes its own spans. Until those spans carry the message's trace ID and name the call that asked for them, the trace stops at the tool step, and nothing says which message the lookup served.

The lookup is replayed on the recording machine, a 4-vCPU Intel Xeon at 2.10 GHz, with the virtual clock from [Replay and the virtual clock](#replay-and-the-virtual-clock). So this chapter makes the server's spans join the message's trace, and builds the counts that find the ones that don't.

## The contacts server is another process {#the-contacts-server}

The assistant does not run `find_contact` itself. The contacts team owns the server, and the mcp package at {{version:mcp}} starts it as a subprocess and sends it requests over standard input and output, the transport the specification calls stdio. Because the server is its own process, its spans never enter the assistant's memory: it writes them to a JSON-lines file of its own, which the assistant reads once the process has exited. The MCP SDK makes a span for each request it sends, and [Trace calls with OpenTelemetry](https://rollingedition.com/model-context-protocol-in-practice/s/trace-with-opentelemetry) covers those spans in depth. The join is what ties the server's spans to the message. A request that loses its header leaves the server's work in the file with no link to the question that caused it.

The other tools in the case do not have this problem. The calendar, alarm and list tools run inside the assistant's own process, so their spans are made in the same memory, and they join the trace by the ordinary parent link that [One message, one trace](#one-message-one-trace) showed.

This chapter calls the contacts server's life a run, not a session. The [2026-07-28 revision of the Model Context Protocol specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/index) says that an open connection, such as a stdio process, is not a conversation or a session, and a server must not treat it as one. The replay starts one process for each run, inside a `contacts startup` span, and every message the run answers reuses it, so a run has one process and one span file, read after the run ends. Nothing carries over from one run to the next, because each run starts its own process. That is the replay's choice, as the MCP specification's statelessness section recommends. In production, a client should not tie the process's life to one task, thread or conversation. Start one process per run and reuse it across messages, as `propagate-into-the-tool` does with its two calls. A server that outlives a run cannot be read at exit, so it needs a flush or a receiver, which [The book's receiver](#the-book-receiver) provides.

The recording shows the failure this section is about. Baseline day 1 was recorded with a client that had no tracer provider, so the client sent no context. The example reads that recording.

[Example: server-span-without-context](../examples/server-span-without-context.py)

The file holds {{out:server-span-without-context:tools/call find_contact spans in the contacts server}} `tools/call find_contact` spans, and {{out:server-span-without-context:of them with a parent}} of them have a parent. The lookup's own span still sits under its call in the server's file. But a call with no parent is a detached root (defined in [Context is lost at a thread boundary](#lost-context)), so nothing ties it to the message.

**Working rule:** Give each server process its own tracer provider and exporter, and read its span file only after the process has exited. The server uses `SimpleSpanProcessor`, which hands each span to the exporter when the span ends, so a file read after exit holds every span the server made. A `BatchSpanProcessor` keeps spans in a queue until a flush or a shutdown, so with one, the file is complete only after one of those.

## The MCP SDK writes traceparent into _meta {#traceparent-in-meta}

For a server span to join the trace, the request has to carry the trace's identity, and MCP gives each request a `_meta` object beside its arguments for that. The field that holds the identity is defined by [W3C Trace Context](https://www.w3.org/TR/2021/REC-trace-context-1-20211123/), a Recommendation of 23 November 2021. Its `traceparent` header has four fields joined by hyphens: a version, a trace ID, a parent span ID and flags. The trace ID is 32 hexadecimal digits and the parent ID 16; all-zero values of either are invalid. The flags carry the sampled decision, which chapter 11 uses in [parent-based sampling](#parent-based-sampling).

Context propagation carries the context, the span's identity and any [baggage](#what-goes-in-baggage), to the next process; `traceparent` is the W3C field for the span's part: the trace ID, the parent's span ID and the trace flags, of which the sampled flag is the only one defined. A span's context has more in it, the trace state among other things, but those three are the part a server needs to attach its span under the right parent.

The first thing to try is to write the header yourself, and see what the server does with it. Below, one call carries a header that names a trace this assistant never made, and a second call asks for a tool the server does not have.

[Example: propagate-into-the-tool](../examples/propagate-into-the-tool.py)

With an OpenTelemetry tracer provider configured, the header in `_meta` is replaced before the request leaves the client. The server's span still joins the assistant's trace, and the header the code wrote does not survive, as the second printed line states. The third line shows how this server answers an unknown tool: with an [error](#how-a-span-records-failure) result, `isError` set, which the client hands back as a reply. The specification asks for a different reply, covered in [A timeout is raised, not returned](#errors-across-the-boundary). The yes lines rest on asserts, so the example stops if the server's span joins the header's trace instead.

The specification's 2026-07-28 revision reserves three `_meta` keys for trace context, `traceparent`, `tracestate` and `baggage`, and requires the W3C formats for their values. The proposal behind that reservation, SEP-414, came before the revision. The [GenAI conventions' MCP page](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/v0.1.0/docs/gen-ai/mcp.md) says that an instrumentation should inject through the configured propagators, and write the three keys without a prefix. That page links the earlier 2025-11-25 revision, so the two pages name different revisions, and we follow the later one.

**Working rule:** With an OpenTelemetry tracer provider configured, let the MCP SDK write `traceparent`, and never put `traceparent`, `tracestate` or `baggage` into `_meta` by hand. The SDK replaces a hand-written `traceparent`, but a hand-written `tracestate` or `baggage` survives beside the SDK's, so the server reads a value the SDK did not make. Write the header yourself only in code that runs without the SDK, and there the server reads it as written.

## The server span joins the trace {#joining-the-trace}

With the header written by the MCP SDK, as [The MCP SDK writes traceparent into _meta](#traceparent-in-meta) showed, the server's span lands under the right parent. Replaying message 16297 with the live server shows the parent links from the assistant's tool span down to the server's own lookup.

[Example: server-span-joins](../examples/server-span-joins.py)

The chain has four spans and three links. The assistant's `execute_tool find_contact` span is the parent of `MCP send tools/call find_contact`, which the SDK makes as a CLIENT span for the request it sends. That client span is the parent of `tools/call find_contact` on the server, a SERVER span, and the server's `contacts.lookup` is the child of that. All four share one trace ID. The assistant holds {{out:server-span-joins:spans in the message's trace, assistant}} spans of the message's trace, and the server writes {{out:server-span-joins:spans in the message's trace, contacts server}} more, in the same trace.

The client span is the one the assistant does not write. Its name, `MCP send tools/call find_contact`, does not follow the pattern the GenAI conventions' MCP page gives for spans, `{mcp.method.name} {target}`. We keep the MCP SDK's name, as chapter 3 keeps the names its package emits, because a span's name should match the code that made it.

The figure draws the same trace as a tree. Dots are the assistant's spans and squares are the server's, and a dashed line marks each parent link that crosses from one process to the other. The layout puts each span by its depth, not its time, because the server's clock is its own, and no time in this chapter comes from a server span: the figure and the output carry structure and parent links only.

[Example: figure-cross-process-tree](../examples/figure-cross-process-tree.py)

![One trace drawn as an indented tree: the assistant's spans, as dots, lead down to `execute_tool find_contact`, whose two SDK client spans each have one contacts-server span, as a square, joined by a dashed link; the server's `contacts.lookup` sits under its `tools/call` span](../examples/figures/cross-process-tree.svg "The spans of message 16297 as one trace, by depth; dashed links cross between the two processes")

{{fig:cross-process-tree}} has two dashed links, and the printed count agrees: {{out:figure-cross-process-tree:parent links across the boundary}}. One is the server's `tools/call find_contact`, whose parent is the client span for the lookup. The other is the server's `tools/list`, whose parent is the client span for the request the SDK sends for the server's tool list. The MCP SDK lists a server's tools after the first successful reply from a tool it has not listed yet, so that it can check the reply against the tool's output schema (`src/mcp/client/session.py`, `call_tool` and `validate_tool_result`, at mcp {{version:mcp}}). This message's lookup succeeded, which is why it has that second link.

**Working rule:** After any call that crosses a process, check that each server span names a client span from the caller's trace as its parent, and count the links that cross. A link count of zero on a call you expected to cross means the header did not reach the server, so look at the request before you look at the server.

## Context is lost at a thread boundary {#lost-context}

The join works because the SDK's call runs in the same context as the tool span that made it. Python keeps the current span in a `ContextVar`, and each thread and each asyncio task has its own value of it. Three common ways of moving work lose that value: `threading.Thread(target=...)`, `executor.submit` and `loop.run_in_executor` each start the function with an empty context, so a span made there finds no parent. `asyncio.to_thread` and `asyncio.run_coroutine_threadsafe` copy the context, and that is why the book's `LiveContacts`, which sends each call to the client's thread with `run_coroutine_threadsafe`, keeps `find_contact`'s span in the message's trace.

A span that starts a trace of its own this way is a detached root: a span whose context was lost, so it starts a new trace and sits outside the message's trace. An orphan is a different failure. An orphan is a span whose parent ID names a span the store does not hold. It keeps the message's trace ID and stays in the message's trace. The two look different in the store. An orphan shows as a gap, a parent with no span behind it. A detached root looks like a healthy one-span trace, so nothing marks what it lost. [Spans by hand](#spans-by-hand) shows the other way a parent goes missing, when a process crashes partway through a message.

The example makes one span on each of four workers, all started inside one tool span, and shows which of them find its context.

[Example: orphan-span-demo](../examples/orphan-span-demo.py)

The plain thread's span is a detached root, and the first printed line says it did not join the tool span. The wrapped thread's span joins, and so does the task's, because asyncio gives a task a copy of the context that was current when the task was made. The executor worker loses the context like the plain thread, so its span is a detached root too. The counts make the [cost](#cost-per-route) plain: {{out:orphan-span-demo:detached roots without the helper}} detached roots without the helper, and {{out:orphan-span-demo:detached roots with the helper}} with it. The last line states that a detached root's trace differs from the tool span's, which is the test for one: a trace ID other than the one its work belongs to.

The fix is `carry()`, from the book's shared module `lib/context.py`, which wraps a function so it runs in a copy of the caller's context. `new_task()` starts an asyncio task with an explicit copy of the context. asyncio copies it anyway, and the helper makes the copy visible in code. Two standard alternatives exist. `opentelemetry-instrumentation-threading` patches `Thread` and `ThreadPoolExecutor` so they carry the context, and `asyncio.to_thread` copies it for the work it starts. We recommend `carry()` in code the team owns, because the copy is visible at the call site and nothing is patched. The instrumentation is for libraries whose own thread pools the team cannot change.

**Working rule:** Wrap every function that a thread, a thread pool or `run_in_executor` runs in `carry()`, and start asyncio tasks with `new_task()`, so each one starts from a copy of the caller's context. A span that finds no parent raises no error and changes no status, so the context has to be carried at every hand-off.

## What goes in baggage {#what-goes-in-baggage}

A trace ID and a parent ID say where a span belongs, and a server often needs one more value to label its own spans: which release and which [prompt version](#release-markers-in-the-spans) made the call. Baggage is the mechanism for that. The standard for it is the [W3C Baggage specification](https://www.w3.org/TR/2024/CR-baggage-20240530/), a Candidate Recommendation Snapshot of 30 May 2024. It is not yet a Recommendation, and we cite it at that status.

Baggage is a set of key-value pairs that travels with the context into every later process when every process along the way propagates it. The specification warns that baggage may carry sensitive information, advises that no proprietary or confidential information goes in it, and says that its keys and values can be passed on to other systems. Those warnings decide what we put there. Only the release and the prompt version go in, under the keys `ambrewick.release` and `ambrewick.prompt.version`. A release has the form `MAJOR.MINOR.PATCH` in digits, and a prompt version is `p` followed by digits. Running the example below shows the allowed list, the keys a call carries, and the refusals.

[Example: baggage-keys](../examples/baggage-keys.py)

Only the two allowed keys leave the process: the propagator writes them into one `baggage` entry, and the SDK sends that entry in `_meta` beside `traceparent`. The helper refuses a key outside the allowed list, and a value outside its key's form, so a user identifier is refused even under an allowed key. The last two lines report those refusals.

`baggage_context` does not see two cases. It starts from the current context, so baggage that arrived from upstream passes through it unchecked, and a direct call to `opentelemetry.baggage.set_baggage` skips it altogether. We recommend that code calls `baggage_context` and never `set_baggage` directly.

A user identifier does not go into baggage, and we do not put one there in any form. The keyed pseudonym we use for users is in [Keyed user pseudonyms](#keyed-user-pseudonyms), where chapter 12 explains why it is safe to put in a span.

**Working rule:** Put a value in baggage only when a process downstream needs it to label its own spans, and only under a key on the allowed list. A value that identifies a person stays out, however short or harmless it looks in one message.

## A timeout is raised, not returned {#errors-across-the-boundary}

The lookup can fail in more than one way, and the failures arrive differently at the caller. A tool that the server does not have comes back as an error result from this server. A conforming server built by another team fails differently: the [specification's tools page](https://modelcontextprotocol.io/specification/2026-07-28/server/tools) makes an unknown tool a protocol error, a JSON-RPC error with code -32602, which the client raises as `MCPError`. A read timeout does not come back from any server at all. The SDK raises it as an `MCPError`, with the code -32001.

The specification assigns no code to errors that are purely local to an implementation, such as a request timeout raised inside an SDK. So -32001 is not an MCP code. The client made it itself, and no server sent it. The specification puts -32001 in its legacy range, -32000 to -32019, where new implementations should not use codes and receivers must not assume a meaning, and it asks that a local error never be mistaken for a peer's. mcp raises the timeout as the same `MCPError` class it uses for a server's errors, so a query over `ambrewick.mcp.error_code` must not count -32001 as the server's error. We call -32001 the SDK's code for a read timeout, and [chapter 8](#retries-under-a-timeout) reads these timeouts across the incident day under the same name.

This section uses an injected incident, `inc-contacts-timeout`, which makes the contacts server answer too slowly for a window of the incident day. The injection is labelled as one in the spans it touches, and the example's first printed line names it.

[Example: contacts-timeout-live](../examples/contacts-timeout-live.py)

Message 16297 on the incident day runs with the live server inside the injected window. The first attempt times out, and the printed lines show its status, its [error type](#how-a-span-records-failure) and its code, which is {{out:contacts-timeout-live:attempt 1, error code}}. The attempt takes {{out:contacts-timeout-live:attempt 1, time}} on the virtual clock, which is the timeout the client is configured with. The second attempt starts {{out:contacts-timeout-live:attempt 2 starts after attempt 1}} after the first, also times out, and the tool step gives up after {{out:contacts-timeout-live:tool step, time}} in all.

Each attempt is its own `execute_tool` span, and we count each one as its own operation. A failed attempt is therefore ERROR, with `error.type` set to the class the caller received, `MCPError`, not the inner `TimeoutError` that the SDK caught underneath it. The code goes on `ambrewick.mcp.error_code`, not on `rpc.status_code`: the conventions define that attribute as the error code from the JSON-RPC response, and a local timeout has no response. The retry does not turn the first attempt into a success. The conventions' [recording-errors page](https://raw.githubusercontent.com/open-telemetry/semantic-conventions/v1.45.0/docs/general/recording-errors.md) says that retried errors stay off the span, but that rule covers an operation that succeeds on retry, and here each attempt is one operation that failed. The alternative reading would hide the first attempt's timeout, and chapter 8 needs that timeout to count the failures a message met.

**Working rule:** Record each attempt of a call as its own span, with its own status and error type, and never let one error span stand for a call whose earlier attempts also failed. Read the error type from the exception the caller receives, and read the code from the error the client raises.

## What goes wrong in production {#across-processes-in-production}

The failures in this chapter raise no error. A hand-written header that the SDK replaces passes silently, and a server span with no parent is exported like any other span. Three queries find them, and each needs a place where the spans of both processes sit together, which chapter 5 provides. One counts the cross-process links: the parent links that cross from one process to the other. One counts detached roots: root spans other than the agent span `invoke_agent ambrewick-assistant`, in either process, and a server span that arrived with no `traceparent` is one too. The last counts orphans: spans whose parent ID names no stored span. Without the queries, nothing in the process fails, so the counts are the only signal.

Two more things go wrong in production. Libraries that run work on their own thread pools lose the context unless they were written to copy it, and a team's `carry()` cannot reach a pool it does not own, so a span that looks like it belongs to a message can start a trace of its own. Baggage goes wherever the request goes, to every process the call reaches, so a value that was harmless in one process can become a leak in the next. The helper checks only what the book sends. Baggage that arrives from upstream passes through unchecked, which is the limit [What goes in baggage](#what-goes-in-baggage) describes.

The trace's two halves now sit in two places, the assistant's spans in its memory and the server's in its file, and [The book's receiver](#the-book-receiver) is where chapter 5 puts them together.

**Working rule:** Run the cross-process link count and the detached-root and orphan counts after any change to tool or worker code, and on every release. Treat a change in any of them as a broken join until someone has explained it.

## The decisions this chapter made {#across-processes-decisions}

Each decision below is a judgement we could be wrong about, so each gives its reason and the example that checks it.

- We let the MCP SDK write `traceparent` whenever a tracer provider is configured, and we keep hand-written trace keys out of `_meta`. The header the server reads is the one the SDK writes, so a second copy in the code would never reach the server. `propagate-into-the-tool` checks it, where the hand-written header is replaced.
- We treat each server process as one run and read its span file once the process has exited. A run starts and ends as one unit, and the file holds every span only after the process has finished writing. `server-span-joins` checks the join, and `server-span-without-context` shows the recording where it is missing.
- We carry the context into every thread, pool and asyncio task that makes spans, with `carry()` and `new_task()`. A detached root raises nothing, so the context has to be carried at every hand-off. `orphan-span-demo` checks it, where the plain thread's span and the executor's are the detached roots.
- We put only the release and the prompt version in baggage, and we refuse every other key and every value outside its form. Baggage travels to every process a call reaches, and the specification warns about what it may carry. `baggage-keys` checks it, where a user identifier is refused under an unknown key and under an allowed one.
- We record each attempt of a tool call as its own span, and we name -32001 as the SDK's code, not an MCP code. Chapter 8 counts the failures a message met, and the first attempt's timeout is one of them, so one span for the whole call would hide it. `contacts-timeout-live` shows both attempts as ERROR spans.
