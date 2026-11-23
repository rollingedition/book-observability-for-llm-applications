---
chapter_id: name-spans
---

# Name spans the way the standard does

By the end of this chapter you can give each span in the assistant's traces the name, the kind and the attributes that OpenTelemetry's GenAI conventions expect for its step. You can say which software made the span and which schema version its names follow, and you can run a check that fails when a span drifts from the registry.

Ambrewick's on-call engineers, who run the assistant whose traces this book builds ([one message, one trace](#one-message-one-trace)), want those traces in the tools they already run. Those tools know one vocabulary: the names the conventions give a model call, a retrieval, a tool run and an agent's turn. [Chapter 1's names](#spans-by-hand) mean nothing there. The validator, the check this chapter builds, compares each span with the registry's rules. Run on message 809, a devices question with a retrieval, its {{out:validate-the-spans:spans checked}} spans draw {{out:validate-the-spans:findings, all}} findings, one per span, and each says the span has no known span type. The SDK raised none of them, because the SDK accepts any name. So this chapter gives each span the standard's name and kind, records who made it, and builds the check that keeps the names right. Chapter 3 then measures a client library's spans against these names.

## The standard at a Development version {#the-standard-at-v0-1-0}

A [backend](#otlp-over-http) answers a question about a span by its name. A question such as how long the model calls took this week only has one answer if every backend agrees which spans are model calls, and the standard is that agreement. A semantic convention is a published name, type and meaning for a span, attribute or [metric](#totals-from-metrics-not-spans), so that any backend reads the same name the same way. The standard is OpenTelemetry's GenAI semantic conventions.

The GenAI names first lived in OpenTelemetry's core semantic conventions repository. That repository deprecated its names under the `gen_ai` prefix in its release 1.42.0, and they moved to a repository of their own ([changelog](https://raw.githubusercontent.com/open-telemetry/semantic-conventions/v1.42.0/CHANGELOG.md)). This book follows that repository's first standalone release, GenAI conventions, version {{out:validate-the-spans:GenAI conventions version}} ([changelog](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/v0.1.0/CHANGELOG.md)).

The conventions come in two forms: pages for people to read, and the registry, a YAML model of the span types, their kinds and their attributes that a program can read. The book's validator reads the registry at the pinned version, so the names it checks are the ones the registry lists.

Every page of the GenAI conventions, version {{out:validate-the-spans:GenAI conventions version}}, is Development: its names may change in any release. The specification defines Development for its own components, and its status page says of such a component: "The component MAY be removed without prior notice. The component SHOULD NOT be used in production." ([status page](https://raw.githubusercontent.com/open-telemetry/opentelemetry-specification/v1.52.0/specification/document-status.md)). The GenAI pages carry the same status, and their changelog shows the names moving. The first standalone release renamed the [inference span](#span-types-for-each-step) type from `gen_ai.inference.client` to `gen_ai.client.inference`. The book's names follow that release, so a later release can rename them.

**Working rule:** Pin the conventions' version beside every name you publish. A dashboard built on a Development name needs a check that runs when the name changes.

## Span types for each step {#span-types-for-each-step}

The registry sorts spans into span types, and each type has a name pattern, a [span kind](#span-kind-follows-the-process) and the attributes it requires. [Chapter 1's steps](#spans-by-hand) fall into four of them. An inference span is the span of one client call to a generative model that answers the prompt or asks for a tool call. A retrieval span is the span of one run of the application's retrieval step, which returns the documents relevant to a query. A tool-execution span is the span of one run of a tool, named for the tool. An agent-invocation span is the span of one invocation of an agent. The book's agent runs in the assistant's process, so its span is the in-process type, which also covers workflows. A remote agent's span is a CLIENT span of its own type. The route and answer steps are model calls, the retrieve step is a search, the tool runs are tool runs, and each message's root is one agent run.

| Span type | Name pattern | Kind | Operation name |
| --- | --- | --- | --- |
| Inference | `{operation} {model}` | CLIENT, or INTERNAL in process | `chat` |
| Retrieval | `{operation}` | INTERNAL | `retrieval` |
| Tool execution | `execute_tool {tool}` | INTERNAL | `execute_tool` |
| Agent invocation | `invoke_agent {agent}` | INTERNAL in process; CLIENT for a remote agent | `invoke_agent` |

[Example: inference-span-names](../examples/inference-span-names.py)

The model span changes most. Its name is the operation, a space, and the model's name, so a reader scanning a trace sees which model answered each call. Message 11159, a lists request with one tool call, has its first model call named `{{out:inference-span-names:inference span name}}`. The registry gives one list of well-known values for `gen_ai.operation.name` on an inference span, and a custom value MAY be used beyond it ([registry attributes](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/v0.1.0/docs/registry/attributes/gen-ai.md)). The validator maps each value to a span type through a map the book makes, and that map gives the inference type `{{out:inference-span-names:operation names, gen_ai.client.inference}}`. The assistant writes `chat` for each of its calls. The example asserts the model span's name against the operation and the model the run used, and it builds the tool and agent names from the attributes their spans carry, so a drift in either one fails the run.

The retrieval span is named by its operation alone, `{{out:inference-span-names:retrieval span name}}`. The registry's pattern leaves no room for the index it searched, so the knowledge base's name stays out of the span name. A tool run is named for its tool, `{{out:inference-span-names:tool span name}}`, the run of the list tool that removed an item in message 11159. The root is named `{{out:inference-span-names:agent span name}}`, and it replaces chapter 1's `message` span. We keep `invoke_agent` rather than `invoke_workflow`, because the model chooses the route and whether to call a tool. The model drives the control flow through tool calls, which is the case the registry's agent type describes.

One attribute the registry leaves open is the provider. In the book, the inference span's `gen_ai.provider.name` is `{{out:inference-span-names:inference provider name}}`, and that value is our judgement. The registry's well-known values for the attribute include no value for llama.cpp, and a provider the list lacks may take a custom value ([registry](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/v0.1.0/model/gen-ai/registry.yaml)). The attribute's general note says it "SHOULD be set based on the instrumentation's best knowledge and may differ from the actual upstream provider" ([client inference](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/v0.1.0/docs/gen-ai/client-inference.md)). We record `openai` because Ambrewick's client speaks OpenAI's API to its endpoint, and the official package records `openai` for that call, as chapter 3 shows. A reader whose model is served another way should choose by the same rule and say so.

Of the spans the assistant makes, the fallback span is the only one the registry does not name. The book keeps it, the validator flags it, and the [validation section](#validating-against-the-registry) says why that flag is the right answer.

**Working rule:** Name each span from its registry pattern, not from the function that made it, and take the operation name from the registry's well-known values. Where the registry gives no value for something you run, use the value the client and its official instrumentation would write, and say that it's a judgement.

## The span kind follows the process {#span-kind-follows-the-process}

A span's kind says where its work ran: CLIENT for a call out of the process, SERVER for one received, INTERNAL for work inside (two more, PRODUCER and CONSUMER, mark work handed to a queue). The model call is the hard case. The registry says "It's RECOMMENDED to use `CLIENT` kind when the GenAI system being instrumented usually runs in a different process than its client or when the GenAI call happens over instrumented protocol such as HTTP" ([client inference](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/v0.1.0/docs/gen-ai/client-inference.md)). A model behind an endpoint is reached by a call that leaves the assistant's process, so its span is CLIENT. The registry also allows INTERNAL for a model running in the same process, which is why the kind is a judgement at all. The book applies the rule with a test a span can show: whether the call names a server. A call that names a server is CLIENT, and a call that names none can be INTERNAL.

[Example: span-kinds-by-process](../examples/span-kinds-by-process.py)

Running message 809's model calls both ways shows the rule at work. Message 809 is a devices question with a route and an answer call. Behind the endpoint the span's kind is `{{out:span-kinds-by-process:model call kind, behind an endpoint}}`, and the same calls run in process give `{{out:span-kinds-by-process:model call kind, in process}}`. The endpoint's span names a server, and the in-process span names none. The validator reports `{{out:span-kinds-by-process:findings on the in-process span}}` findings on the in-process span, which the example asserts. Marked INTERNAL, the endpoint's span still names a server, and the validator reports `{{out:span-kinds-by-process:findings on the endpoint span marked INTERNAL}}` finding on it. The finding's text is asserted to name the kind, `kind is INTERNAL`, and only its count is printed. That finding is the failure the rule prevents.

The [dev-day](#replay-and-the-virtual-clock) traces in this book are all CLIENT, because the recorded model calls were served over an endpoint. The in-process case is the same model called from the assistant's own code, and the book runs it only in this example. A reader who moves a model into the process should expect the kind to change with it, and should expect the validator to say so if the span still names a server.

The retrieval span and the agent span are both INTERNAL, which the example asserts for each, because their work runs inside the assistant. The printed kinds are `{{out:span-kinds-by-process:retrieval span kind}}` for the retrieval span and `{{out:span-kinds-by-process:agent span kind}}` for the agent span. The registry gives both kinds ([registry](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/v0.1.0/model/gen-ai/spans.yaml)), and the validator checks them.

**Working rule:** Give a model span CLIENT when the call goes to a server, even one on the same machine. Give it INTERNAL only when the model runs in your process and the span names no server, and let the validator check each kind you set.

## Which attributes a span must carry {#requirement-levels}

A name tells a backend what a span is. The requirement level tells it which attributes that span must carry. A requirement level says when an attribute must appear: required, conditionally required, recommended or opt-in ([requirement levels](https://raw.githubusercontent.com/open-telemetry/semantic-conventions/v1.45.0/docs/general/attribute-requirement-level.md)). A required attribute appears on every span of its type. A conditionally required one appears when its condition holds, and the registry writes the condition beside it. A recommended attribute says what a good span carries, and an opt-in one appears only when something switches it on.

The registry also says which attributes each span type must carry. The book's module, `lib.genai`, holds every requirement level from the registry, so the requirement levels and kinds that the [validator](#validating-against-the-registry) checks are the registry's, not a list someone typed. The validator checks the kind and the required attributes, and nothing else, so a span can pass it and still lack what the registry asks for in a conditional case. The validator's counts, as the inference-span-names example prints them, show how many each span type requires: `{{out:inference-span-names:required attributes, gen_ai.client.inference}}` on an inference span, `{{out:inference-span-names:required attributes, gen_ai.execute_tool}}` on a tool-execution span, `{{out:inference-span-names:required attributes, gen_ai.retrieval}}` on the retrieval span and `{{out:inference-span-names:required attributes, gen_ai.invoke_agent.internal}}` on the agent span. Its required attributes are its operation name and its provider name, so the [provider judgement](#span-types-for-each-step) is not optional. A span without either one fails the check.

Opt-in attributes are the ones to watch for privacy. The message contents of a model call are opt-in, so the book's spans carry none of them unless a switch asks for them, and chapter 3 says what that switch does.

Conditional requirements are the ones the validator does not read. `gen_ai.prompt.version` is conditionally required on the inference span when the span carries `gen_ai.prompt.name` and a version is available. `gen_ai.prompt.name` is conditionally required when a named prompt template is used ([registry](https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/v0.1.0/model/gen-ai/spans.yaml)). The assistant's prompts are named and versioned, so its inference spans carry both: `{{out:inference-span-names:inference prompt name}}` and `{{out:inference-span-names:inference prompt version}}` on the route call in message 11159. Chapter 10 queries that pair to tell releases apart. The validator reads only required attributes, so it would not report a span that lacks either one, and a gap in a conditional attribute is a gap it will not report.

**Working rule:** Read a span's requirement levels before you read its name. A missing required attribute is a defect in the span. A missing conditionally required attribute is a defect only when its condition holds, so read the condition in the registry before you file it.

## Who made the span {#who-made-the-span}

A span says what it measured, and two other places say who measured it: the [resource](#spans-by-hand) and the instrumentation scope. The instrumentation scope names the unit of code that made a span, usually an [instrumentation library](#a-call-through-the-client), and here the assistant's own module. A schema URL names a published telemetry schema, a release of the semantic conventions, that the telemetry follows. The prose never types a schema URL; each one here is printed by its example.

The resource carries three attributes the book sets itself. `service.name` is `{{out:resource-and-schema:service.name}}`. `service.version` is `{{out:resource-and-schema:service.version}}`, and it marks the release, which chapter 10 reads to split traces by release. `service.instance.id` is `{{out:resource-and-schema:service.instance.id}}`. The [replay](#replay-and-the-virtual-clock) sets the instance ID itself, so a rerun names the same instance and its traces can be compared run to run. A process that restarts keeps its instance ID only if one was set, and a query that follows one instance through a restart needs the ID to stay put.

[Example: resource-and-schema](../examples/resource-and-schema.py)

The schema URL moved in this chapter, and the example prints both values. [Chapter 1's resource](#spans-by-hand) said `{{out:resource-and-schema:chapter 1's resource schema URL}}`. That was the newest schema the pinned semantic-conventions package carries, `{{version:opentelemetry-semantic-conventions}}`, and its highest schema is `{{out:resource-and-schema:highest schema in the SDK's constants}}`. Chapter 1's names followed no convention, so that stamp described nothing the book's spans said. The renamed spans stamp `{{out:resource-and-schema:renamed spans' resource schema URL}}`, which is the core semantic-conventions release that GenAI conventions v0.1.0 depend on. The GenAI conventions publish no schema URL of their own at v0.1.0. Core deprecated its `gen_ai.*` names in 1.42.0, when they moved to their own repository. So the stamp says which core release the resource's `service.*` names follow, and the GenAI version travels beside the check, `{{out:validate-the-spans:GenAI conventions version}}`. The book's generated module takes that URL from the registry's manifest, and the example asserts that the SDK's constants do not contain it. The constants stop at their own release. Of the `service.*` attributes, the release that URL names deprecates only `service.peer.name` and `service.peer.namespace` ([changelog](https://raw.githubusercontent.com/open-telemetry/semantic-conventions/v1.45.0/CHANGELOG.md)), so the three the resource sets read the same under either URL.

The instrumentation scope of the renamed spans is `{{out:resource-and-schema:instrumentation scope name}}`, and its schema URL is the same as the resource's, which the example asserts. The official client library writes a scope of its own.

**Working rule:** Set `service.name` and `service.version` on the resource yourself, and give `service.instance.id` a value unique to the instance, as the production section explains. Then give the tracer the schema URL of the core release your conventions depend on, from the registry's manifest and not from the SDK's constants, which can lag behind the conventions. Pin the GenAI conventions' version beside the validator, because at v0.1.0 they have no schema URL to stamp.

## Validating against the registry {#validating-against-the-registry}

A name is a claim about the registry, and the validator makes that claim testable. It finds each span's type from `gen_ai.operation.name`, or, for an MCP span, from its method. MCP is the Model Context Protocol, the protocol a tool server speaks. The validator then checks the span's kind and required attributes against the registry's rules. A span whose type it cannot find gets a finding that says so, and each finding names its span, so the output reads as a list of what to fix.

The validator cannot name [chapter 1's spans](#one-message-one-trace), because they carry no `gen_ai.operation.name`, the attribute it reads to find a span's type. Message 809's {{out:validate-the-spans:spans checked}} spans are `message`, `route`, `retrieve` and `answer`, and the check prints one finding for each: `no known span type for this row`. The finding gives the reason. Once the root is renamed, `message` becomes `{{out:inference-span-names:agent span name}}`, and that span is checked against its own type.

[Example: validate-the-spans](../examples/validate-the-spans.py)

Renamed, the spans pass. Across all {{out:validate-the-renamed-spans:messages}} dev-day messages the example checks {{out:validate-the-renamed-spans:spans checked}} spans, and it finds {{out:validate-the-renamed-spans:findings on registry spans}} problems on any span the registry defines. A single assertion holds that count at zero, so a change that breaks a name fails the run instead of printing a new number.

[Example: validate-the-renamed-spans](../examples/validate-the-renamed-spans.py)

The validator does flag other spans. The {{out:validate-the-renamed-spans:fallback replies}} [fallback replies](#where-the-time-went-in-twenty-messages) each have an `ambrewick.fallback` span, and the check flags all {{out:validate-the-renamed-spans:findings on the book's fallback span}} of them, each with `no known span type`. That is correct, and it is not a failure of the conventions. The registry defines the types it names, and a span outside them has no type. The book's own spans and attributes sit in the `ambrewick.` namespace, which is the book's, and the naming rules ask that names sit in a namespace ([naming rules](https://raw.githubusercontent.com/open-telemetry/semantic-conventions/v1.45.0/docs/general/naming.md)). A finding that says a span is outside the registry says the span is the book's own. Each flag is asserted to be that finding and no other, so a registry span that gained a flag would fail the run.

**Working rule:** Read a finding on a registry span as a defect. Read a finding on a span in your own namespace as the registry's answer that the span is yours.

## Name spans in production {#name-spans-in-production}

The validator here runs on recorded spans, inside examples. In production the same check has to run on the spans a service actually exports, and three things make it fail quietly. A fourth decides whether the instance ID means anything. We recommend using a Development name in production when it's pinned and validated, because the validator's first run after an upgrade shows what moved.

The first is the name's content. The specification asks that a span name be "the most general string" for its class of spans, and it marks `get_account/42` as too specific ([span API](https://raw.githubusercontent.com/open-telemetry/opentelemetry-specification/v1.52.0/specification/trace/api.md)). The model span's name carries the model, from a short fixed list, which is general enough. A name built from a user's ID would be a new name for each user, and the registry's pattern has no room for one. A reviewer who sees an identifier in a span name should treat it as a defect, whatever the validator says.

The second is the SDK. It does not stop a wrong name. [Chapter 1's rough names](#spans-by-hand) went through unchanged, and a misspelled registry name would too. Only the validator reads the registry, so a pipeline that exports spans without running it has no check at all. We recommend running the validator in the pipeline that exports spans, not only in tests.

The third is upgrades. A Development name can change in any release, and the validator's first run after an upgrade lists what moved. Read that list before you change the pinned version, and change the names in the same pull request as the version. Spans from another package that writes older names may also reach the same backend. A backend holding those and this book's spans sees two vocabularies for one step.

The fourth is the instance ID. The `service.namespace`, `service.name` and `service.instance.id` triplet MUST be globally unique ([service registry](https://raw.githubusercontent.com/open-telemetry/semantic-conventions/v1.45.0/model/service/registry.yaml)). The SDK's default is a new random UUID in each process. In production, keep that random UUID, or derive a UUIDv5 from an ID unique to the instance, and never share one value across replicas. The replay's fixed instance ID is right for a replay and wrong to copy.

When you add a span, write its registry type first, then its name, then its kind. We recommend this order because the type fixes the name pattern and the kind.

**Working rule:** Run the validator in the pipeline that exports your spans, and fail the build on a finding. Pin the conventions' version beside the check, and read the changelog before you raise it.

## What this chapter decided {#name-spans-decisions}

The registry will move, and the names will move with it. What this chapter fixes is which version the book's spans follow, and a check that reports when they stop following it. Each choice below is one the rest of the book relies on, and an example asserts each one it can.

- The book's names follow GenAI conventions {{out:validate-the-spans:GenAI conventions version}}, and the version is pinned beside the validator, because the registry is Development.
- The retrieval span is named by its operation alone, `retrieval`, because the registry's pattern has no place for anything else.
- The provider value is `openai`. That is our judgement, quoted beside the registry's guidance, and the validator cannot check it, because the registry has no value for llama.cpp to check it against.
- The assistant's prompts are named and versioned, so an inference span carries `gen_ai.prompt.name` with `gen_ai.prompt.version`, which the registry makes conditionally required.
- A model span's kind follows the process: CLIENT behind a server, and INTERNAL only in process, when the span names no server.
- The book's resource and tracer carry the schema URL of the core release the registry depends on, not the SDK's newest constant.
- The validator reports a finding on a registry span as a defect, and the book's own fallback spans get findings of their own, which are not defects.
